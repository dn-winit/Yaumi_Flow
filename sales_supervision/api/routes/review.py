"""
Review endpoints -- load saved sessions, list dates, list sessions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from sales_supervision.api.dependencies import get_db_saver, get_session_manager, get_store
from sales_supervision.api.schemas import (
    DatesResponse,
    LoadSessionRequest,
    ReviewResponse,
    SessionListResponse,
)
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.storage.store import SessionStore

router = APIRouter(prefix="/review", tags=["review"])


@router.post("/load", response_model=ReviewResponse)
def load_session(
    req: LoadSessionRequest,
    store: SessionStore = Depends(get_store),
    mgr: SessionManager = Depends(get_session_manager),
    db_saver: DbSaver = Depends(get_db_saver),
):
    # Try DB first, then file fallback
    data = None
    if db_saver.available:
        data = db_saver.load_session(req.route_code, req.date)
    if data is None:
        data = store.load(req.route_code, req.date)
    if data is None:
        return ReviewResponse(success=True, exists=False)

    session = mgr.rebuild_session(data)
    return ReviewResponse(success=True, exists=True, session=session.to_dict())


@router.get("/exists")
def check_exists(
    route_code: str = Query(...),
    date: str = Query(...),
    store: SessionStore = Depends(get_store),
):
    return {"exists": store.exists(route_code, date)}


@router.get("/dates", response_model=DatesResponse)
def list_dates(
    route_code: Optional[str] = Query(None),
    store: SessionStore = Depends(get_store),
):
    return DatesResponse(success=True, dates=store.list_dates(route_code))


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    date: Optional[str] = Query(None),
    store: SessionStore = Depends(get_store),
):
    return SessionListResponse(success=True, sessions=store.list_sessions(date))


@router.delete("/delete")
def delete_session(
    route_code: str = Query(...),
    date: str = Query(...),
    store: SessionStore = Depends(get_store),
):
    deleted = store.delete(route_code, date)
    return {"success": deleted}
