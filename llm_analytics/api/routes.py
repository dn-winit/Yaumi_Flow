"""
API routes for LLM analytics.

``success`` on the response is derived from the analyzer's
``_degraded`` marker: a fallback dict (rate limit, retries exhausted,
parse failure) downgrades ``success`` to ``False`` so the supervision
saver skips the DB UPDATE and a later tick can retry against a clean
slate. Without this, fallback strings ("Analysis not available: ...")
would land in the user-facing column and the column's NOT NULL state
would block the retry from ever running.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from llm_analytics.api.dependencies import get_analyzer
from llm_analytics.api.schemas import (
    AnalysisResponse,
    CacheClearResponse,
    CacheStatsResponse,
    CostSummaryResponse,
    CustomerAnalysisRequest,
    HealthResponse,
    PreviewResponse,
    PreVisitRequest,
    RouteAnalysisRequest,
)
from llm_analytics.config.settings import get_settings
from llm_analytics.core.analyzer import DEGRADED_KEY, Analyzer
from llm_analytics.core.schema_render import render_schema_for_prompt
from llm_analytics.models.schemas import CustomerAnalysis, PreVisitBriefing, RouteAnalysis
from llm_analytics.services.cost_tracker import get_cost_tracker

router = APIRouter()


def _estimate_tokens(text: str) -> int:
    """Rough char->token heuristic (~4 chars/token). Order of magnitude
    accurate; used only for the preview endpoint where the actual count
    comes from the provider on the live call."""
    return max(1, len(text) // 4)


def _success_of(result: dict) -> bool:
    return not bool(result.get(DEGRADED_KEY))


# ------------------------------------------------------------------
# Customer analysis
# ------------------------------------------------------------------

@router.post("/analyze/customer", response_model=AnalysisResponse)
def analyze_customer(
    req: CustomerAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    result = analyzer.analyze_customer(
        customer_code=req.customer_code,
        route_code=req.route_code,
        date=req.date,
        current_items=req.current_items,
        performance_score=req.performance_score,
        coverage=req.coverage,
        accuracy=req.accuracy,
    )

    return AnalysisResponse(success=_success_of(result), analysis_type="customer", data=result)


# ------------------------------------------------------------------
# Route analysis
# ------------------------------------------------------------------

@router.post("/analyze/route", response_model=AnalysisResponse)
def analyze_route(
    req: RouteAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    codes = set(req.actual_customer_codes) if req.actual_customer_codes else None

    result = analyzer.analyze_route(
        route_code=req.route_code,
        date=req.date,
        visited_customers=req.visited_customers,
        total_customers=req.total_customers,
        total_actual=req.total_actual,
        total_recommended=req.total_recommended,
        actual_customer_codes=codes,
    )

    return AnalysisResponse(success=_success_of(result), analysis_type="route", data=result)


# ------------------------------------------------------------------
# Pre-visit briefing
# ------------------------------------------------------------------

@router.post("/analyze/pre-visit", response_model=AnalysisResponse)
def pre_visit_briefing(
    req: PreVisitRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    result = analyzer.pre_visit_briefing(
        customer_code=req.customer_code,
        customer_name=req.customer_name,
        route_code=req.route_code,
        date=req.date,
        items=req.items,
    )
    return AnalysisResponse(success=_success_of(result), analysis_type="pre_visit", data=result)


# ------------------------------------------------------------------
# Health + cache
# ------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
def health_check(analyzer: Analyzer = Depends(get_analyzer)):
    """Liveness probe -- mirrors the analyzer's provider / model / cache state."""
    return HealthResponse(**analyzer.health())


@router.get("/cache/stats", response_model=CacheStatsResponse)
def cache_stats(analyzer: Analyzer = Depends(get_analyzer)):
    return CacheStatsResponse(**analyzer.cache_stats())


@router.post("/cache/clear", response_model=CacheClearResponse)
def clear_cache(analyzer: Analyzer = Depends(get_analyzer)):
    cleared = analyzer.clear_cache()
    return CacheClearResponse(success=True, cleared=cleared)


# ------------------------------------------------------------------
# Preview (dry-run prompts) -- verify data correctness without firing the LLM
# ------------------------------------------------------------------

@router.post("/analyze/customer/preview", response_model=PreviewResponse)
def preview_customer(
    req: CustomerAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    """Render the customer-analysis prompt without calling the LLM.

    Operators can inspect what data is being sent and verify nothing
    looks wrong before a paid call. Same builder the live path uses, so
    the preview cannot diverge.
    """
    s = get_settings()
    system, user = analyzer.build_customer_prompt(
        customer_code=req.customer_code, route_code=req.route_code, date=req.date,
        current_items=req.current_items,
        performance_score=req.performance_score,
        coverage=req.coverage, accuracy=req.accuracy,
    )
    return PreviewResponse(
        artifact="customer_analysis",
        system_prompt=system, user_prompt=user,
        model=s.model, provider=s.provider,
        max_tokens=s.max_tokens_customer_analysis,
        estimated_input_tokens=_estimate_tokens(system) + _estimate_tokens(user),
        schema=CustomerAnalysis.model_json_schema(),
    )


@router.post("/analyze/route/preview", response_model=PreviewResponse)
def preview_route(
    req: RouteAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    s = get_settings()
    system, user = analyzer.build_route_prompt(
        route_code=req.route_code, date=req.date,
        visited_customers=req.visited_customers,
        total_customers=req.total_customers,
        total_actual=req.total_actual,
        total_recommended=req.total_recommended,
    )
    return PreviewResponse(
        artifact="route_analysis",
        system_prompt=system, user_prompt=user,
        model=s.model, provider=s.provider,
        max_tokens=s.max_tokens_route_analysis,
        estimated_input_tokens=_estimate_tokens(system) + _estimate_tokens(user),
        schema=RouteAnalysis.model_json_schema(),
    )


@router.post("/analyze/pre-visit/preview", response_model=PreviewResponse)
def preview_pre_visit(
    req: PreVisitRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    s = get_settings()
    system, user = analyzer.build_pre_visit_prompt(
        customer_code=req.customer_code, customer_name=req.customer_name,
        route_code=req.route_code, date=req.date, items=req.items,
    )
    return PreviewResponse(
        artifact="pre_visit_briefing",
        system_prompt=system, user_prompt=user,
        model=s.model, provider=s.provider,
        max_tokens=s.max_tokens_briefing,
        estimated_input_tokens=_estimate_tokens(system) + _estimate_tokens(user),
        schema=PreVisitBriefing.model_json_schema(),
    )


# ------------------------------------------------------------------
# Cost telemetry -- what did we spend, where, and was it cached?
# ------------------------------------------------------------------

@router.get("/cost/today", response_model=CostSummaryResponse)
def cost_today(
    route_code: Optional[str] = Query(default=None, description="Filter to one route"),
):
    """Aggregate LLM spend since start-of-day UTC.

    Cost is computed from in-buffer token counts × configured per-token
    rates -- always factual (no estimates). USD is the contract currency
    with the provider; the configured display rate adds a labelled
    second column so reports match local accounting (default AED).
    """
    s = get_settings()
    summary = get_cost_tracker().summary(
        price_input_usd_per_million=s.price_input_usd_per_million,
        price_output_usd_per_million=s.price_output_usd_per_million,
        display_currency=s.cost_display_currency,
        display_rate=s.cost_display_rate,
        route_code=route_code,
    )
    return CostSummaryResponse(**summary)


@router.get("/cost/since", response_model=CostSummaryResponse)
def cost_since(
    since_iso: str = Query(description="ISO timestamp lower bound (UTC), e.g. 2026-05-27T00:00:00Z"),
    route_code: Optional[str] = Query(default=None),
):
    """Aggregate spend since an arbitrary ISO timestamp -- supports
    custom windows (last hour, last shift, etc.) without restarting."""
    s = get_settings()
    try:
        cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"invalid since_iso: {exc}")
    summary = get_cost_tracker().summary(
        price_input_usd_per_million=s.price_input_usd_per_million,
        price_output_usd_per_million=s.price_output_usd_per_million,
        display_currency=s.cost_display_currency,
        display_rate=s.cost_display_rate,
        since=cutoff,
        route_code=route_code,
    )
    return CostSummaryResponse(**summary)
