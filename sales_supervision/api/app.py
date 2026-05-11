"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sales_supervision.api.routes import (
    health_router,
    session_router,
)
from sales_supervision.config.settings import Settings, get_settings


def _probe_data_import(settings: Settings, log: logging.Logger) -> None:
    """One-shot probe of the data_import service. Non-fatal -- the app
    still boots if the URL is unreachable; we just want a loud signal
    instead of silent zero-actuals later."""
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

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("sales_supervision.startup")

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

        # Probe data_import on startup. Live actuals (and therefore
        # rep-vs-recommendation scoring) silently degrade to zeros when
        # this URL is unreachable -- a loud WARN here flags ops before
        # supervisors see ghost-perfect tiles.
        _probe_data_import(settings, _logger)

        # Auto-visit reconciler -- mirrors live YaumiLive invoices into
        # yf_supervision_* every ``auto_visit_poll_seconds``. Browser-
        # independent: works with or without the supervisor's UI open.
        scheduler = None
        if settings.auto_visit_enabled:
            from sales_supervision.api.dependencies import get_auto_visit_service
            from sales_supervision.services.auto_visit_scheduler import AutoVisitScheduler
            scheduler = AutoVisitScheduler(
                get_auto_visit_service(), settings=settings,
            )
            scheduler.start()

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
    app.include_router(health_router, prefix=prefix)
    app.include_router(session_router, prefix=prefix)

    return app
