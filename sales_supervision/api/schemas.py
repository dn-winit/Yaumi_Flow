from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Redistribution wire models: extra=forbid so stray fields surface as 422.


class RedistributionEntry(BaseModel):
    """One recipient row inside a per-item group (``from`` is implicit)."""
    model_config = ConfigDict(extra="forbid")

    to: str
    toName: str = ""
    quantity: int                                  # always positive magnitude
    direction: Literal["add", "reduce"] = "add"


class RedistributionGroup(BaseModel):
    """Per-item group of recipient entries; keptOnTruck is the surplus not absorbed downstream (ADD only)."""
    model_config = ConfigDict(extra="forbid")

    itemCode: str
    itemName: str = ""
    entries: list[RedistributionEntry] = Field(default_factory=list)
    keptOnTruck: int = 0


class RedistributionView(BaseModel):
    """Redistribution payload; empty groups signals "nothing to redistribute"."""
    model_config = ConfigDict(extra="forbid")

    groups: list[RedistributionGroup] = Field(default_factory=list)


class InitSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_code: str
    date: str
    # Optional; server fetches from recommended_order when absent. Pass through to bypass the cache.
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


class ProcessVisitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    customer_code: str
    # Taken from the session; kept optional for client-side diagnostics echo.
    route_code: str | None = None
    date: str | None = None


class UnplannedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_code: str
    qty: int


class UnplannedCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_code: str
    customer_name: str = ""
    items: list[UnplannedItem] = Field(default_factory=list)
    total_qty: int = 0
    unique_skus: int = 0
    live_visited: bool = True
    redistributions: RedistributionView = Field(default_factory=RedistributionView)


class UnplannedVisitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    error: str | None = None
    route_code: str = ""                            # required string, empty on error
    date: str = ""                                  # required string, empty on error
    planned_count: int = 0
    live_count: int = 0
    unplanned_count: int = 0
    planned_visited_codes: list[str] = Field(default_factory=list)
    customers: list[UnplannedCustomer] = Field(default_factory=list)


class SavedVisitScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    coverage: float
    accuracy: float


class SavedVisit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: SavedVisitScore
    actualSales: dict[str, int] = Field(default_factory=dict)
    totalActual: int = 0
    totalRecommended: int = 0
    preVisitBriefing: str | None = None
    customerAnalysis: str | None = None
    redistributions: RedistributionView = Field(default_factory=RedistributionView)
    # Off-plan items hydrated from yf_supervision_items (rec=0, actual>0); same shape as /visit's alsoBought.
    alsoBought: list[AlsoBoughtRow] = Field(default_factory=list)


class SessionVisitTotals(BaseModel):
    """Visit aggregates from Session.summary(); includes drop-in count for the live tile."""
    model_config = ConfigDict(extra="forbid")

    visited_count: int = 0
    total_actual: int = 0
    total_recommended: int = 0
    avg_score: float | None = None
    unplanned_visited_count: int = 0


class SessionRecommendationTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items_count: int = 0
    total_units: int = 0
    customers_count: int = 0


class SessionCustomerGrouped(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_code: str
    customer_name: str = ""
    # Raw rec rows; opaque to backend, surfaced for the UI's per-row render.
    items: list[dict[str, Any]] = Field(default_factory=list)


class SessionCustomerTile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_code: str
    customer_name: str = ""
    unique_skus: int = 0
    total_units: int = 0
    visited: bool = False


class SessionSummary(BaseModel):
    """Shape Session.summary() emits."""
    model_config = ConfigDict(extra="forbid")

    sessionId: str
    routeCode: str
    date: str
    status: str
    plannedCustomers: int = 0
    plannedVisitedCustomers: int = 0
    unplannedVisitedCustomers: int = 0
    totalCustomers: int = 0
    visitedCustomers: int = 0
    remainingCustomers: int = 0
    totalRecommended: int = 0
    totalActual: int = 0
    visitedRecommended: int = 0
    visitedActual: int = 0
    visitedAchievement: float = 0.0
    overallAchievement: float = 0.0
    customers_grouped: list[SessionCustomerGrouped] = Field(default_factory=list)
    customer_tiles: list[SessionCustomerTile] = Field(default_factory=list)
    recommendation_totals: SessionRecommendationTotals = Field(default_factory=SessionRecommendationTotals)
    visit_totals: SessionVisitTotals = Field(default_factory=SessionVisitTotals)


class VisitScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    coverage: float
    accuracy: float


class AlsoBoughtRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_code: str
    qty: int


class VisitResultPayload(BaseModel):
    """/session/visit response: post-visit score, actuals, redistribution view, and session totals."""
    model_config = ConfigDict(extra="forbid")

    score: VisitScore
    actualSales: dict[str, int] = Field(default_factory=dict)
    actualQty: int = 0
    recommendedQty: int = 0
    alsoBought: list[AlsoBoughtRow] = Field(default_factory=list)
    redistributions: RedistributionView = Field(default_factory=RedistributionView)
    sessionTotals: SessionVisitTotals = Field(default_factory=SessionVisitTotals)


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    session: SessionSummary


class VisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    visit: VisitResultPayload


class SavedVisitsResponse(BaseModel):
    """Snapshot for hydrating already-saved visits on UI mount."""
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    session_id: str | None = None
    visits: dict[str, SavedVisit] = Field(default_factory=dict)
    # routeAnalysis kept as Optional[str] for wire-shape parity with the
    # webapp's TypeScript model; always None now -- LLM analyses are
    # generated on-demand by the webapp directly against llm_analytics.
    routeAnalysis: str | None = None
    visit_totals: SessionVisitTotals = Field(default_factory=SessionVisitTotals)
    # Always-empty dict kept for wire-shape parity; LLM briefings are
    # generated on-demand by the webapp via llm_analytics, not pre-cached.
    briefings: dict[str, str] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    db_configured: bool
    last_reconcile_epoch: float | None = None
    reconcile_lag_seconds: float | None = None
    reconcile_stale: bool = False
