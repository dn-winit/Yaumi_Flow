"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from demand_forecasting_pipeline.api.routes import (
    accuracy_router,
    explainability_router,
    health_router,
    metrics_router,
    models_router,
    pipeline_router,
    predictions_router,
    summary_router,
)
from demand_forecasting_pipeline.config.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("demand_forecasting_pipeline.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info("Demand Forecasting Service starting...")
        _logger.info("Config: %s", settings.pipeline_config)
        _logger.info("Artifacts: %s", settings.artifacts_dir)

        # Pre-warm cache with frequently accessed artifacts
        from demand_forecasting_pipeline.api.dependencies import get_artifact_service
        svc = get_artifact_service()
        artifacts = svc.check_artifacts()
        present = sum(1 for v in artifacts.values() if v)
        _logger.info("Artifacts found: %d/%d", present, len(artifacts))

        yield  # app is running

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

    prefix = f"{settings.api_prefix}/forecast"
    app.include_router(health_router, prefix=prefix)
    app.include_router(predictions_router, prefix=prefix)
    app.include_router(metrics_router, prefix=prefix)
    app.include_router(models_router, prefix=prefix)
    app.include_router(explainability_router, prefix=prefix)
    app.include_router(pipeline_router, prefix=prefix)
    app.include_router(summary_router, prefix=prefix)
    app.include_router(accuracy_router, prefix=prefix)

    return app
