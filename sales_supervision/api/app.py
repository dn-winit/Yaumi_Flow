"""
FastAPI application factory.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.api_middleware import install_all as _install_observability_middleware
from common.observability import configure_logging, get_logger
from sales_supervision.api.routes import (
    health_router,
    session_router,
)
from sales_supervision.config.settings import Settings, get_settings

SERVICE_NAME = "sales_supervision"


def _logs_dir() -> Path:
    env = os.getenv("SS_LOGS_DIR")
    if env:
        return Path(env)
    root_env = os.getenv("YF_DATA_ROOT", "").strip()
    root = Path(root_env).resolve() if root_env else Path(__file__).resolve().parent.parent.parent / "data"
    return root / "supervision" / "logs"


def _probe_data_import(settings: Settings, log: Any) -> None:
    """Non-fatal startup probe of data_import; warns loudly so silent zero-actuals don't sneak in."""
    url = (settings.data_import_url or "").rstrip("/")
    if not url:
        log.warning("data_import_url unset; live actuals will be empty")
        return
    try:
        import httpx
        resp = httpx.get(f"{url}/api/v1/data/health", timeout=settings.data_import_timeout)
        resp.raise_for_status()
        log.info("data_import probe OK at %s", url)
    except Exception as exc:
        log.warning(
            "data_import probe FAILED at %s (%s) -- live actuals will be "
            "empty until reachable; rep scoring will read zero invoiced qty",
            url, exc,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service_name=SERVICE_NAME,
        level=settings.log_level,
        logs_dir=_logs_dir(),
    )
    _logger = get_logger("sales_supervision.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info(
            "Sales Supervision Service starting -- "
            "registry_max=%d ttl_seconds=%d "
            "route_table=%s customer_table=%s item_table=%s "
            "auto_visit_enabled=%s auto_visit_poll=%ds",
            settings.session_registry_max,
            settings.session_ttl_seconds,
            settings.route_summary_table or "(unset)",
            settings.customer_summary_table or "(unset)",
            settings.item_details_table or "(unset)",
            settings.auto_visit_enabled,
            settings.auto_visit_poll_seconds,
        )

        # Loud-warn on unreachable data_import so supervisors don't see ghost-perfect tiles.
        _probe_data_import(settings, _logger)

        # Auto-visit reconciler: mirrors YaumiLive into yf_supervision_* independent of the browser.
        # Leader election: under ``workers>1`` only one worker runs the reconciler;
        # followers boot the API but skip the scheduler (no concurrent SERIALIZABLE writes).
        scheduler = None
        if settings.auto_visit_enabled:
            from common.leader_election import try_acquire_leader_lock
            if try_acquire_leader_lock(settings.scheduler_lock_path):
                from sales_supervision.api.dependencies import get_auto_visit_service
                from sales_supervision.services.auto_visit_scheduler import AutoVisitScheduler
                scheduler = AutoVisitScheduler(
                    get_auto_visit_service(), settings=settings,
                )
                scheduler.start()
            else:
                _logger.info("auto-visit reconciler skipped: another worker holds the leader lease")

        # Saved-visits warm-up on a daemon thread; first /session/saved hit returns ~1ms instead of 1-3s.
        if settings.saved_visits_warmup_enabled:

            def _warm_saved_visits() -> None:
                try:
                    from sales_supervision.api.dependencies import (
                        get_auto_visit_service,
                        get_db_saver,
                    )
                    db_saver = get_db_saver()
                    if not db_saver.available:
                        return
                    routes = []
                    avs = get_auto_visit_service()
                    if avs is not None:
                        routes = list(avs._configured_routes())
                    if not routes:
                        return
                    today = datetime.now(
                        tz=ZoneInfo(settings.auto_visit_timezone),
                    ).strftime("%Y-%m-%d")
                    t0 = time.perf_counter()
                    warmed = 0
                    for route in routes:
                        try:
                            db_saver.load_session_visits(
                                route, today,
                                include_redistributions=False,
                            )
                            warmed += 1
                        except Exception:
                            continue
                    _logger.info(
                        "saved_visits_warmup_complete routes=%d/%d "
                        "duration_ms=%.1f ttl_seconds=%.1f",
                        warmed, len(routes),
                        (time.perf_counter() - t0) * 1000.0,
                        settings.effective_saved_visits_cache_ttl,
                    )
                except Exception as exc:  # pragma: no cover -- defensive
                    _logger.warning(
                        "saved_visits_warmup_skipped error=%s type=%s",
                        exc, type(exc).__name__,
                    )

            threading.Thread(
                target=_warm_saved_visits,
                name="ss-saved-warmup",
                daemon=True,
            ).start()

        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()
            _logger.info("Shutting down")

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

    prefix = f"{settings.api_prefix}/supervision"
    _install_observability_middleware(
        app,
        service_name=SERVICE_NAME,
        metrics_path=f"{prefix}/metrics/prometheus",
        logger=_logger,
    )

    app.include_router(health_router, prefix=prefix)
    app.include_router(session_router, prefix=prefix)

    return app
