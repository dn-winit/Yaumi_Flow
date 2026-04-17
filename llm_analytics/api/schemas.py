"""
API request/response schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------

class CustomerAnalysisRequest(BaseModel):
    customer_code: str
    route_code: str
    date: str
    customer_data: List[Dict[str, Any]] = Field(description="Historical purchase rows")
    current_items: List[Dict[str, Any]] = Field(description="Current visit items with actualQuantity/recommendedQuantity")
    performance_score: float = 0.0
    coverage: float = 0.0
    accuracy: float = 0.0


class RouteAnalysisRequest(BaseModel):
    route_code: str
    date: str
    visited_customers: List[Dict[str, Any]] = Field(description="Visited customer summaries")
    total_customers: int = 0
    total_actual: int = 0
    total_recommended: int = 0
    pre_context: str = ""
    actual_customer_codes: Optional[List[str]] = None


class PreVisitRequest(BaseModel):
    customer_code: str
    customer_name: str = ""
    route_code: str
    date: str
    items: List[Dict[str, Any]] = Field(description="Recommendation items with qty, tier, source, whyItem")


class PlanningAnalysisRequest(BaseModel):
    route_code: str
    date: str
    van_load_items: List[Dict[str, Any]] = Field(description="Van items with vanQty/recQty")
    customer_recommendations: List[Dict[str, Any]] = Field(description="Per-customer item recommendations")
    van_load_skus: int = 0
    van_load_qty: int = 0
    total_customers: int = 0
    total_rec_qty: int = 0


# ------------------------------------------------------------------
# Responses
# ------------------------------------------------------------------

class AnalysisResponse(BaseModel):
    success: bool
    analysis_type: str
    data: Dict[str, Any]
    cached: bool = False


class HealthResponse(BaseModel):
    available: bool
    provider: str
    model: str
    cache: Dict[str, Any]
    prompts: List[str]


class CacheStatsResponse(BaseModel):
    hits: int
    misses: int
    hit_rate: float
    cached_entries: int


class LlmSummaryResponse(BaseModel):
    provider: str
    model: str
    available: bool
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float
    prompts_loaded: List[str]
