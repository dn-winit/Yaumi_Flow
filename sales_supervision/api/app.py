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
    review_router,
    scoring_router,
    session_router,
)
from sales_supervision.config.settings import Settings, get_settings


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
        _logger.info("Sales Supervision Service starting -- storage=%s", settings.storage_dir)
        yield
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

    prefix = f"{settings.api_prefix}/supervision"
    app.include_router(health_router, prefix=prefix)
    app.include_router(session_router, prefix=prefix)
    app.include_router(review_router, prefix=prefix)
    app.include_router(scoring_router, prefix=prefix)

    return app
