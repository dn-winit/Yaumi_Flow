from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import get_auto_visit_service, get_db_saver
from sales_supervision.api.schemas import HealthResponse
from sales_supervision.config.settings import get_settings
from sales_supervision.services.db_saver import DbSaver

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(db_saver: DbSaver = Depends(get_db_saver)) -> HealthResponse:
    """Liveness + reconciler-freshness probe. ``status`` flips to
    ``degraded`` when the reconciler is enabled but its last tick is
    older than ``2 * auto_visit_poll_seconds`` -- a silent reconciler
    means UI tiles freeze on stale counts."""
    s = get_settings()
    last_epoch: float | None = None
    lag: float | None = None
    stale = False

    if s.auto_visit_enabled:
        last_at = get_auto_visit_service().last_reconcile_at
        if last_at is not None:
            last_epoch = last_at.replace(tzinfo=UTC).timestamp()
            lag = (datetime.now(UTC) - last_at).total_seconds()
            stale = lag > 2 * s.auto_visit_poll_seconds
        else:
            # Reconciler enabled but no tick yet -- mark stale so the
            # UI shows the signal even on a fresh boot that hasn't
            # completed its first cycle.
            stale = True

    return HealthResponse(
        status="degraded" if stale else "healthy",
        db_configured=db_saver.available,
        last_reconcile_epoch=last_epoch,
        reconcile_lag_seconds=lag,
        reconcile_stale=stale,
    )
