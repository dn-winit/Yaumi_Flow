"""
Pydantic request/response schemas for the API.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Trigger recommendation generation for a date."""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Target date YYYY-MM-DD")
    route_codes: Optional[List[str]] = Field(default=None, description="Specific routes (None = all)")
    force: bool = Field(default=False, description="Regenerate even if recs already exist")


class RetrieveRequest(BaseModel):
    """Retrieve stored recommendations."""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    route_code: Optional[str] = None
    customer_code: Optional[str] = None
    item_code: Optional[str] = None
    tier: Optional[str] = None
    min_priority: Optional[float] = Field(default=None, ge=0, le=100)
    limit: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


class ExistsRequest(BaseModel):
    """Check if recommendations exist."""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    route_codes: Optional[List[str]] = None


# ------------------------------------------------------------------
# Responses
# ------------------------------------------------------------------


class RecommendationItem(BaseModel):
    TrxDate: str
    RouteCode: str
    CustomerCode: str
    CustomerName: Optional[str] = ""
    ItemCode: str
    ItemName: Optional[str] = ""
    RecommendedQuantity: int
    Tier: str
    VanLoad: int
    PriorityScore: float
    AvgQuantityPerVisit: int
    DaysSinceLastPurchase: int
    PurchaseCycleDays: float
    FrequencyPercent: float
    ChurnProbability: float
    PatternQuality: float
    PurchaseCount: int
    TrendFactor: Optional[float] = None
    # Sprint-1 explainability
    Signals: Optional[List[Dict[str, Any]]] = None
    WhyItem: str = ""
    WhyQuantity: str = ""
    Confidence: float = 0.0
    Source: str = ""


class GenerateResponse(BaseModel):
    success: bool
    message: str
    date: str
    routes_processed: int = 0
    total_records: int = 0
    duration_seconds: float = 0.0
    details: List[Dict[str, Any]] = []


class RetrieveResponse(BaseModel):
    success: bool
    date: str
    total: int
    data: List[RecommendationItem]
    source: str = Field(default="store", description="store | generated")
    generated_routes: int = 0


class ExistsResponse(BaseModel):
    date: str
    exists: Dict[str, bool]


class GenerationInfoResponse(BaseModel):
    exists: bool
    date: str
    total_records: int = 0
    routes_count: int = 0
    customers_count: int = 0
    items_count: int = 0
    generated_at: Optional[str] = None
    generated_by: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    last_refresh: Optional[str] = None
    route_codes: List[str]
    # Sprint-1 freshness visibility
    journey_max_date: Optional[str] = None
    customer_max_date: Optional[str] = None
    demand_max_date: Optional[str] = None
    # Sprint-3 observability (all default-safe so existing callers don't break)
    per_route_last_generation: Dict[str, str] = Field(default_factory=dict)
    calibration_cache_size: int = 0
    lookalike_cache_size: int = 0
    avg_generation_seconds_last_n: float = 0.0
    feedback_routes_active: int = 0


class FilterOptionsResponse(BaseModel):
    routes: List[str]
    dates: List[str] = []


class RecommendationSummaryResponse(BaseModel):
    routes_configured: int
    last_generated_date: Optional[str] = None
    total_recs_latest_date: int = 0
    routes_with_recs_latest: int = 0
    customers_latest: int = 0
