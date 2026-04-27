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


class HealthResponse(BaseModel):
    status: str
    storage_dir: str
    saved_sessions: int
