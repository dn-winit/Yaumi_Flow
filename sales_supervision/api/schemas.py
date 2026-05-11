from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class InitSessionRequest(BaseModel):
    route_code: str
    date: str
    recommendations: List[Dict[str, Any]] = Field(description="Recommendation records")


class ProcessVisitRequest(BaseModel):
    session_id: str
    customer_code: str
    # route_code + date are taken from the session; kept optional so the
    # client can echo them back for diagnostics if useful.
    route_code: Optional[str] = None
    date: Optional[str] = None


class SessionResponse(BaseModel):
    success: bool
    session: Dict[str, Any]


class VisitResponse(BaseModel):
    success: bool
    visit: Dict[str, Any]


class UnplannedItem(BaseModel):
    item_code: str
    qty: int


class UnplannedCustomer(BaseModel):
    customer_code: str
    customer_name: str = ""
    items: List[UnplannedItem] = Field(default_factory=list)
    total_qty: int = 0
    # Pre-computed tile fields the UI grid renders. Lifted from the
    # webapp adapter so the client renders without aggregating items.
    unique_skus: int = 0
    # Always true for unplanned visitors (we know about them precisely
    # because they invoiced live), but emitted explicitly so the tile
    # never has to assume.
    live_visited: bool = True


class UnplannedVisitsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    route_code: Optional[str] = None
    date: Optional[str] = None
    planned_count: int = 0
    live_count: int = 0
    unplanned_count: int = 0
    planned_visited_codes: List[str] = Field(default_factory=list)
    customers: List[UnplannedCustomer] = Field(default_factory=list)


class SavedVisitScore(BaseModel):
    score: float
    coverage: float
    accuracy: float


class SavedVisit(BaseModel):
    score: SavedVisitScore
    # Item-level actuals keyed by item_code -- same shape the live
    # /visit response returns under ``actualSales``, so the UI
    # consumes either source through one code path.
    actualSales: Dict[str, int] = Field(default_factory=dict)
    totalActual: int = 0
    totalRecommended: int = 0
    # LLM payloads previously saved for this customer. Carried as
    # opaque strings -- the analytics layer round-trips JSON in/out.
    preVisitBriefing: Optional[str] = None
    customerAnalysis: Optional[str] = None


class VisitTotals(BaseModel):
    """Cumulative visit aggregates for the live tile row.

    Same shape ``Session.summary().visit_totals`` emits and the
    ``/visit`` response carries forward, so the saved-visits payload
    drops directly into the same client state slot on mount.
    """
    visited_count: int = 0
    total_actual: int = 0
    total_recommended: int = 0
    avg_score: Optional[float] = None


class SavedVisitsResponse(BaseModel):
    """Snapshot of already-saved visits for a (route, date), used to
    hydrate the live UI on mount so already-visited customers render
    actuals + score (and any prior LLM reviews) without re-running
    the briefing or analysis."""
    available: bool = False
    session_id: Optional[str] = None
    visits: Dict[str, SavedVisit] = Field(default_factory=dict)
    # Route-level LLM review for the (route, date), if any has been saved.
    routeAnalysis: Optional[str] = None
    # Pre-aggregated visit totals so the live UI never re-sums the
    # ``visits`` map. Falls back to all-zeros when no visits land.
    visit_totals: VisitTotals = Field(default_factory=VisitTotals)


class SaveBriefingRequest(BaseModel):
    session_id: str
    customer_code: str
    # Opaque JSON-string payload from the analytics service. Stored as-is.
    content: str


class SaveCustomerAnalysisRequest(SaveBriefingRequest):
    """Same shape as SaveBriefingRequest -- different target column."""


class SaveRouteAnalysisRequest(BaseModel):
    session_id: str
    content: str


class LlmSaveResponse(BaseModel):
    success: bool
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    db_configured: bool
    # Freshness signal: epoch seconds of the last completed reconcile
    # tick + lag in seconds. None when the reconciler is disabled or
    # has not yet run. UI alerts when lag > 2 * auto_visit_poll_seconds.
    last_reconcile_epoch: Optional[float] = None
    reconcile_lag_seconds: Optional[float] = None
    reconcile_stale: bool = False
