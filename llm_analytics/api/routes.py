"""
API routes for LLM analytics.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends

from llm_analytics.api.dependencies import get_analyzer
from llm_analytics.api.schemas import (
    AnalysisResponse,
    CacheStatsResponse,
    CustomerAnalysisRequest,
    HealthResponse,
    LlmSummaryResponse,
    PlanningAnalysisRequest,
    RouteAnalysisRequest,
)
from llm_analytics.core.analyzer import Analyzer

router = APIRouter()


# ------------------------------------------------------------------
# Customer analysis
# ------------------------------------------------------------------

@router.post("/analyze/customer", response_model=AnalysisResponse)
def analyze_customer(
    req: CustomerAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    customer_df = pd.DataFrame(req.customer_data) if req.customer_data else pd.DataFrame()

    result = analyzer.analyze_customer(
        customer_code=req.customer_code,
        route_code=req.route_code,
        date=req.date,
        customer_data=customer_df,
        current_items=req.current_items,
        performance_score=req.performance_score,
        coverage=req.coverage,
        accuracy=req.accuracy,
    )

    return AnalysisResponse(success=True, analysis_type="customer", data=result)


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
        pre_context=req.pre_context,
        actual_customer_codes=codes,
    )

    return AnalysisResponse(success=True, analysis_type="route", data=result)


# ------------------------------------------------------------------
# Planning insights
# ------------------------------------------------------------------

@router.post("/analyze/planning", response_model=AnalysisResponse)
def analyze_planning(
    req: PlanningAnalysisRequest,
    analyzer: Analyzer = Depends(get_analyzer),
):
    result = analyzer.analyze_planning(
        route_code=req.route_code,
        date=req.date,
        van_load_items=req.van_load_items,
        customer_recommendations=req.customer_recommendations,
        van_load_skus=req.van_load_skus,
        van_load_qty=req.van_load_qty,
        total_customers=req.total_customers,
        total_rec_qty=req.total_rec_qty,
    )

    return AnalysisResponse(success=True, analysis_type="planning", data=result)


# ------------------------------------------------------------------
# Health + cache
# ------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
def health_check(analyzer: Analyzer = Depends(get_analyzer)):
    return HealthResponse(**analyzer.health())


@router.get("/summary", response_model=LlmSummaryResponse)
def summary(analyzer: Analyzer = Depends(get_analyzer)):
    """Aggregated KPI summary reshaped from analyzer.health()."""
    h = analyzer.health()
    cache = h.get("cache", {}) or {}
    return LlmSummaryResponse(
        provider=h.get("provider", ""),
        model=h.get("model", ""),
        available=bool(h.get("available", False)),
        cache_hits=int(cache.get("hits", 0)),
        cache_misses=int(cache.get("misses", 0)),
        cache_hit_rate=float(cache.get("hit_rate", 0.0)),
        prompts_loaded=list(h.get("prompts", [])),
    )


@router.get("/cache/stats", response_model=CacheStatsResponse)
def cache_stats(analyzer: Analyzer = Depends(get_analyzer)):
    return CacheStatsResponse(**analyzer.cache_stats())


@router.post("/cache/clear")
def clear_cache(analyzer: Analyzer = Depends(get_analyzer)):
    cleared = analyzer.clear_cache()
    return {"success": True, "cleared": cleared}
