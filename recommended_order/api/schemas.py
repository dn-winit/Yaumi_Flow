"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Trigger recommendation generation for a date."""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Target date YYYY-MM-DD")
    route_codes: list[str] | None = Field(default=None, description="Specific routes (None = all)")
    force: bool = Field(default=False, description="Regenerate even if recs already exist")


class RetrieveRequest(BaseModel):
    """Retrieve stored recommendations."""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    route_code: str | None = None
    customer_code: str | None = None
    item_code: str | None = None
    tier: str | None = None
    min_priority: float | None = Field(default=None, ge=0, le=100)
    limit: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


# ------------------------------------------------------------------
# Responses
# ------------------------------------------------------------------


class RecommendationItem(BaseModel):
    TrxDate: str
    RouteCode: str
    CustomerCode: str
    CustomerName: str | None = ""
    ItemCode: str
    ItemName: str | None = ""
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
    TrendFactor: float | None = None
    # Sprint-1 explainability
    Signals: list[dict[str, Any]] | None = None
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
    details: list[dict[str, Any]] = []


class EmptyRouteCustomer(BaseModel):
    customer_code: str
    customer_name: str = ""
    typical_items: list[dict[str, str]] = []  # [{code, name}]


class EmptyRouteDiagnosis(BaseModel):
    """Why a route returned 0 recommendations -- structured for the UI."""
    reason: str = Field(
        description="no_plan|no_journey|no_van|all_new_customers|van_mismatch|mixed|engine_no_match",
    )
    headline: str = Field(description="Short, positive title for the empty state")
    detail: str = Field(description="One-sentence explanation of what to check")
    customers: list[EmptyRouteCustomer] = []


class RetrieveResponse(BaseModel):
    success: bool
    date: str
    total: int
    data: list[RecommendationItem]
    source: str = Field(default="store", description="store | generated")
    generated_routes: int = 0
    diagnosis: EmptyRouteDiagnosis | None = None


class HealthResponse(BaseModel):
    status: str
    last_refresh: str | None = None
    route_codes: list[str]
    # Sprint-1 freshness visibility
    journey_max_date: str | None = None
    customer_max_date: str | None = None
    demand_max_date: str | None = None
    # Sprint-3 observability (all default-safe so existing callers don't break)
    per_route_last_generation: dict[str, str] = Field(default_factory=dict)
    calibration_cache_size: int = 0
    lookalike_cache_size: int = 0
    avg_generation_seconds_last_n: float = 0.0
    feedback_routes_active: int = 0


class FilterOptionsResponse(BaseModel):
    routes: list[str]
    dates: list[str] = []
    journey_counts: dict[str, int] = {}  # {route: customer_count} for the requested date
    # Populated only when a route has planned customers but no stored recs.
    route_diagnoses: dict[str, EmptyRouteDiagnosis] = {}


class RecommendationSummaryResponse(BaseModel):
    routes_configured: int
    last_generated_date: str | None = None
    total_recs_latest_date: int = 0
    routes_with_recs_latest: int = 0
    customers_latest: int = 0


# Analytics envelopes: nested payloads stay Dict[str, Any] so additive
# fields don't require a coordinated FE/BE deploy.


class AdoptionResponse(BaseModel):
    available: bool = True
    message: str | None = None
    summary: dict[str, Any] | None = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
    top_over_recommended: list[dict[str, Any]] = Field(default_factory=list)
    top_missed: list[dict[str, Any]] = Field(default_factory=list)


class UpcomingPlanResponse(BaseModel):
    available: bool = True
    message: str | None = None
    today: str | None = None
    days: int = 0
    summary: dict[str, Any] | None = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
