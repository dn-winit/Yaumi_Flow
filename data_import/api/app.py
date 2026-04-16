"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from data_import.api.routes import router
from data_import.config.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("data_import.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info("Data Import Service starting -- data_dir=%s", settings.data_dir)

        # Pre-warm EDA aggregations so the first dashboard request is instant
        # (otherwise the user pays for the 50MB CSV parse on initial load).
        try:
            from data_import.api.dependencies import get_eda_service
            import threading
            def _warm() -> None:
                try:
                    svc = get_eda_service()
                    # Dashboard-critical aggregates -- pay the cold cost here so
                    # the first browser hit is served from cache. Any exception
                    # in an individual aggregate is logged and skipped so a
                    # single slow query can't block the rest.
                    for label, fn in (
                        ("sales_overview", svc.get_sales_overview),
                        ("item_catalog", svc.get_item_catalog),
                        ("business_kpis", svc.get_business_kpis),
                        ("customer_overview_90", lambda: svc.get_customer_overview(90)),
                    ):
                        try:
                            fn()
                        except Exception as exc:
                            _logger.warning("EDA warm-up skipped %s: %s", label, exc)
                    _logger.info("EDA cache warmed")
                except Exception as exc:
                    _logger.warning("EDA warm-up skipped: %s", exc)
            # Off the request path so startup isn't blocked.
            threading.Thread(target=_warm, daemon=True, name="eda-warmup").start()
        except Exception as exc:
            _logger.warning("EDA warm-up scheduling failed: %s", exc)

        sched = None
        if settings.scheduler_enabled:
            from data_import.scheduler import start_scheduler
            sched = start_scheduler(settings)
        yield
        if sched is not None:
            sched.shutdown(wait=False)
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
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=f"{settings.api_prefix}/data")

    return app
