"""FastAPI application factory."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.api_middleware import install_all as _install_observability_middleware
from common.observability import configure_logging, get_logger
from recommended_order.api.dependencies import get_data_manager
from recommended_order.api.routes import router
from recommended_order.config.settings import Settings, get_settings

SERVICE_NAME = "recommended_order"


def _logs_dir() -> Path:
    env = os.getenv("RO_LOGS_DIR")
    if env:
        return Path(env)
    root_env = os.getenv("YF_DATA_ROOT", "").strip()
    root = Path(root_env).resolve() if root_env else Path(__file__).resolve().parent.parent.parent / "data"
    return root / "recommendations" / "logs"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service_name=SERVICE_NAME,
        level=settings.log_level,
        logs_dir=_logs_dir(),
    )
    _logger = get_logger("recommended_order.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- Startup ---
        _logger.info("initializing_data_manager")
        dm = get_data_manager()
        result = dm.initialize()
        if result["success"]:
            _logger.info("data_loaded", data=result["data"])
        else:
            _logger.error("data_load_errors", errors=result["errors"])

        if settings.scheduler.enabled:
            # Leader election: under ``workers>1`` only one worker per host
            # fires the daily generation cron; followers boot the API but
            # skip the scheduler (no concurrent SERIALIZABLE writes to
            # ``yf_recommended_orders``).
            from common.leader_election import try_acquire_leader_lock
            if try_acquire_leader_lock(settings.scheduler.lock_path):
                from recommended_order.scheduler.jobs import start_scheduler
                start_scheduler(settings)
                _logger.info("Scheduler started")
            else:
                _logger.info("recommended_order scheduler skipped: another worker holds the leader lease")

        # Planning warm-up: pay the 10-12s cold reconcile in a daemon
        # thread so the first /analytics/upcoming hit after restart is
        # sub-300ms. Best-effort -- failures fall back to lazy cold path.
        import threading

        def _warm_planning() -> None:
            try:
                from recommended_order.api.dependencies import get_planning_service
                t0 = time.perf_counter()
                planning = get_planning_service()
                planning.get_upcoming(days=7, route_code=None)
                _logger.info(
                    "planning_warmup_complete",
                    duration_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                )
            except Exception as exc:  # pragma: no cover -- defensive
                _logger.warning(
                    "planning_warmup_skipped",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        threading.Thread(
            target=_warm_planning,
            name="ro-planning-warmup",
            daemon=True,
        ).start()

        yield  # app is running

        # --- Shutdown ---
        from recommended_order.scheduler.jobs import get_scheduler
        sched = get_scheduler()
        if sched and sched.running:
            sched.shutdown(wait=False)
            _logger.info("Scheduler stopped")

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    prefix = f"{settings.api_prefix}/recommended-order"
    _install_observability_middleware(
        app,
        service_name=SERVICE_NAME,
        metrics_path=f"{prefix}/metrics/prometheus",
        logger=_logger,
    )

    app.include_router(router, prefix=prefix)

    return app
