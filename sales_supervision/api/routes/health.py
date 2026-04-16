"""
Health check endpoint.
"""

from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import get_db_saver, get_store
from sales_supervision.api.schemas import HealthResponse, SupervisionSummaryResponse
from sales_supervision.config.settings import get_settings
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.storage.store import SessionStore

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(store: SessionStore = Depends(get_store)):
    settings = get_settings()
    sessions = store.list_sessions()
    return HealthResponse(
        status="healthy",
        storage_dir=settings.storage_dir,
        saved_sessions=len(sessions),
    )


@router.get("/summary", response_model=SupervisionSummaryResponse)
def summary(
    store: SessionStore = Depends(get_store),
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Aggregated KPI summary for dashboard."""
    settings = get_settings()
    sessions = store.list_sessions()
    today_str = _date.today().isoformat()
    sessions_today = sum(1 for s in sessions if s.get("date") == today_str)
    return SupervisionSummaryResponse(
        saved_sessions=len(sessions),
        sessions_today=sessions_today,
        storage_dir=settings.storage_dir,
        has_db=db_saver.available,
    )
