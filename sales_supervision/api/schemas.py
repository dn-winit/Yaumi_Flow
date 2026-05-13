from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------
# Redistribution wire models.
#
# Strict contract -- ``extra="forbid"`` so a stray field on either the
# producer (shape_redistribution_view) or the consumer (frontend panel)
# is surfaced as a 422, not silently dropped. The model_dump output is
# the single source of truth the UI binds to.
# ----------------------------------------------------------------------


class RedistributionEntry(BaseModel):
    """One recipient row inside a per-item group. ``from`` is implicit
    (the surrounding customer's card). The UI colors rows by
    ``direction`` and renders ``toName`` + ``to`` + ``quantity``."""
    model_config = ConfigDict(extra="forbid")

    to: str
    toName: str = ""
    quantity: int                                  # always positive magnitude
    direction: Literal["add", "reduce"] = "add"


class RedistributionGroup(BaseModel):
    """Per-item group of recipient entries. The buffer ledger still
    drives allocation server-side, but the running buffer state is no
    longer surfaced on the wire -- the UI renders only the entries.

    ``keptOnTruck`` carries the source customer's surplus units that did
    NOT reach any downstream recipient -- either because no eligible
    recipient existed or because spare van-load buffer absorbed the
    overflow. Always 0 for ``reduce``-direction groups (excess-demand /
    drop-in side) since "kept" only makes sense on the surplus path."""
    model_config = ConfigDict(extra="forbid")

    itemCode: str
    itemName: str = ""
    entries: List[RedistributionEntry] = Field(default_factory=list)
    keptOnTruck: int = 0


class RedistributionView(BaseModel):
    """Wire-shaped redistribution payload emitted for every visit. The
    panel mounts unconditionally; an empty ``groups`` list signals the
    "nothing to redistribute" case without a separate marker field."""
    model_config = ConfigDict(extra="forbid")

    groups: List[RedistributionGroup] = Field(default_factory=list)


class InitSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_code: str
    date: str
    recommendations: List[Dict[str, Any]] = Field(description="Recommendation records")


class ProcessVisitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    customer_code: str
    # route_code + date are taken from the session; kept optional so the
    # client can echo them back for diagnostics if useful.
    route_code: Optional[str] = None
    date: Optional[str] = None


class UnplannedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_code: str
    qty: int


class UnplannedCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_code: str
    customer_name: str = ""
    items: List[UnplannedItem] = Field(default_factory=list)
    total_qty: int = 0
    unique_skus: int = 0
    live_visited: bool = True
    redistributions: RedistributionView = Field(default_factory=RedistributionView)


class UnplannedVisitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    error: Optional[str] = None
    route_code: str = ""                            # required string, empty on error
    date: str = ""                                  # required string, empty on error
    planned_count: int = 0
    live_count: int = 0
    unplanned_count: int = 0
    planned_visited_codes: List[str] = Field(default_factory=list)
    customers: List[UnplannedCustomer] = Field(default_factory=list)


class SavedVisitScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    coverage: float
    accuracy: float


class SavedVisit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: SavedVisitScore
    actualSales: Dict[str, int] = Field(default_factory=dict)
    totalActual: int = 0
    totalRecommended: int = 0
    preVisitBriefing: Optional[str] = None
    customerAnalysis: Optional[str] = None
    redistributions: RedistributionView = Field(default_factory=RedistributionView)


class SessionVisitTotals(BaseModel):
    """Wire-shaped visit aggregates from Session.summary(). Includes the
    drop-in count so the live tile renders planned vs unplanned distinctly."""
    model_config = ConfigDict(extra="forbid")

    visited_count: int = 0
    total_actual: int = 0
    total_recommended: int = 0
    avg_score: Optional[float] = None
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
    items: List[Dict[str, Any]] = Field(default_factory=list)


class SessionCustomerTile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_code: str
    customer_name: str = ""
    unique_skus: int = 0
    total_units: int = 0
    visited: bool = False


class SessionSummary(BaseModel):
    """Shape Session.summary() emits. Replaces the prior opaque
    SessionResponse.session blob."""
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
    customers_grouped: List[SessionCustomerGrouped] = Field(default_factory=list)
    customer_tiles: List[SessionCustomerTile] = Field(default_factory=list)
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
    """Shape /session/visit emits. Wraps the post-visit score, the
    per-item actual sales, the planned-only quantity totals, the
    off-plan ``alsoBought`` rows, the structured redistribution view,
    and the cumulative session aggregates -- everything the UI needs
    to render a visit result in one round-trip.
    """
    model_config = ConfigDict(extra="forbid")

    score: VisitScore
    actualSales: Dict[str, int] = Field(default_factory=dict)
    actualQty: int = 0
    recommendedQty: int = 0
    alsoBought: List[AlsoBoughtRow] = Field(default_factory=list)
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
    """Snapshot of already-saved visits for a (route, date), used to
    hydrate the live UI on mount so already-visited customers render
    actuals + score (and any prior LLM reviews) without re-running
    the briefing or analysis."""
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    session_id: Optional[str] = None
    visits: Dict[str, SavedVisit] = Field(default_factory=dict)
    routeAnalysis: Optional[str] = None
    visit_totals: SessionVisitTotals = Field(default_factory=SessionVisitTotals)


class SaveBriefingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    customer_code: str
    # Opaque JSON-string payload from the analytics service. Stored as-is.
    content: str


class SaveCustomerAnalysisRequest(SaveBriefingRequest):
    """Same shape as SaveBriefingRequest -- different target column."""


class SaveRouteAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    content: str


class LlmSaveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    error: Optional[str] = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    db_configured: bool
    last_reconcile_epoch: Optional[float] = None
    reconcile_lag_seconds: Optional[float] = None
    reconcile_stale: bool = False
