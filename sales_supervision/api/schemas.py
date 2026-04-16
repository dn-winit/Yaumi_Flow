"""
API request/response schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Session
# ------------------------------------------------------------------

class InitSessionRequest(BaseModel):
    route_code: str
    date: str
    recommendations: List[Dict[str, Any]] = Field(description="Recommendation records")


class ProcessVisitRequest(BaseModel):
    session_id: str
    customer_code: str
    # route_code + date are taken from the session (kept as optional here for
    # diagnostics / logging in case the client wants to echo them back).
    route_code: Optional[str] = None
    date: Optional[str] = None


class UpdateActualsRequest(BaseModel):
    route_code: str
    date: str
    session_id: str
    customer_code: str
    actuals: Dict[str, int]


class SaveSessionRequest(BaseModel):
    session_data: Dict[str, Any] = Field(description="Full session dict from Session.to_dict()")


class SessionResponse(BaseModel):
    success: bool
    session: Dict[str, Any]


class VisitResponse(BaseModel):
    success: bool
    visit: Dict[str, Any]


class ScoreResponse(BaseModel):
    success: bool
    score: float
    coverage: float
    accuracy: float


# ------------------------------------------------------------------
# Review
# ------------------------------------------------------------------

class LoadSessionRequest(BaseModel):
    route_code: str
    date: str


class ReviewResponse(BaseModel):
    success: bool
    exists: bool
    session: Optional[Dict[str, Any]] = None


class DatesResponse(BaseModel):
    success: bool
    dates: List[str]


class SessionListResponse(BaseModel):
    success: bool
    sessions: List[Dict[str, Any]]


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

class ScoreCustomerRequest(BaseModel):
    items: List[Dict[str, Any]] = Field(description="Items with actualQuantity and recommendedQuantity")


class ScoreRouteRequest(BaseModel):
    customers: List[Dict[str, Any]] = Field(description="Customers with items and visited status")


class RouteScoreResponse(BaseModel):
    success: bool
    route_score: float
    customer_coverage: float
    qty_fulfillment: float
    customer_scores: Dict[str, float]


class MethodologyResponse(BaseModel):
    item_accuracy: Dict[str, Any]
    customer_score: Dict[str, Any]
    route_score: Dict[str, Any]


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    storage_dir: str
    saved_sessions: int


class SupervisionSummaryResponse(BaseModel):
    saved_sessions: int
    sessions_today: int
    storage_dir: str
    has_db: bool
