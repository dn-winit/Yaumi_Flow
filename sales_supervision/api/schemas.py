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


class SaveSessionResponse(BaseModel):
    """Reply from POST /session/save-active.

    ``success`` reflects whether the file write (the source of truth)
    landed. ``db_ok`` is a separate signal because the DB push is best-
    effort -- a False here with success=True means the session is safely
    on disk but the warehouse table was not updated. ``warning`` carries
    a human-readable detail when db_ok is False.
    """
    success: bool
    db_ok: bool = True
    warning: Optional[str] = None
    error: Optional[str] = None
    file: Optional[Dict[str, Any]] = None
    db: Optional[Dict[str, Any]] = None


class UnplannedItem(BaseModel):
    item_code: str
    qty: int


class UnplannedCustomer(BaseModel):
    customer_code: str
    customer_name: str = ""
    items: List[UnplannedItem] = Field(default_factory=list)
    total_qty: int = 0


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


class HealthResponse(BaseModel):
    status: str
    storage_dir: str
    saved_sessions: int
