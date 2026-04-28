"""
API routes for LLM analytics.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends

from llm_analytics.api.dependencies import get_analyzer
from llm_analytics.api.schemas import (
    AnalysisResponse,
    CacheClearResponse,
    CacheStatsResponse,
    CustomerAnalysisRequest,
    HealthResponse,
    PreVisitRequest,
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
    return AnalysisResponse(success=True, analysis_type="pre_visit", data=result)


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
