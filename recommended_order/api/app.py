"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from recommended_order.api.dependencies import get_data_manager
from recommended_order.api.routes import router
from recommended_order.config.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("recommended_order.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- Startup ---
        _logger.info("Initializing data manager...")
        dm = get_data_manager()
        result = dm.initialize()
        if result["success"]:
            _logger.info("Data loaded: %s", result["data"])
        else:
            _logger.error("Data load errors: %s", result["errors"])

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
                    "planning_warmup_complete duration_ms=%.1f",
                    (time.perf_counter() - t0) * 1000.0,
                )
            except Exception as exc:  # pragma: no cover -- defensive
                _logger.warning(
                    "planning_warmup_skipped error=%s type=%s",
                    str(exc), type(exc).__name__,
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

    app.include_router(router, prefix=f"{settings.api_prefix}/recommended-order")

    return app
