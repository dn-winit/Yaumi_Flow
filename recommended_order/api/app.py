"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
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
            from recommended_order.scheduler.jobs import start_scheduler
            start_scheduler(settings)
            _logger.info("Scheduler started")

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
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=f"{settings.api_prefix}/recommended-order")

    return app
