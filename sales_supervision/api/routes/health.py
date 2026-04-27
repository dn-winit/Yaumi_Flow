from __future__ import annotations

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import get_store
from sales_supervision.api.schemas import HealthResponse
from sales_supervision.config.settings import get_settings
from sales_supervision.services.storage.store import SessionStore

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(store: SessionStore = Depends(get_store)):
    """Liveness probe -- counts saved sessions on disk."""
    settings = get_settings()
    return HealthResponse(
        status="healthy",
        storage_dir=settings.storage_dir,
        saved_sessions=len(store.list_sessions()),
    )
