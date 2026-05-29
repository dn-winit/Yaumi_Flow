"""
FastAPI application factory.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.api_middleware import install_all as _install_observability_middleware
from common.observability import configure_logging, get_logger
from llm_analytics.api.routes import router
from llm_analytics.config.settings import Settings, get_settings

SERVICE_NAME = "llm_analytics"


def _logs_dir() -> Path:
    env = os.getenv("LLM_LOGS_DIR")
    if env:
        return Path(env)
    root_env = os.getenv("YF_DATA_ROOT", "").strip()
    root = Path(root_env).resolve() if root_env else Path(__file__).resolve().parent.parent.parent / "data"
    return root / "analytics" / "logs"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service_name=SERVICE_NAME,
        level=settings.log_level,
        logs_dir=_logs_dir(),
    )
    _logger = get_logger("llm_analytics.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info("LLM Analytics starting", provider=settings.provider, model=settings.model)
        from llm_analytics.api.dependencies import get_analyzer
        analyzer = get_analyzer()
        health = analyzer.health()
        _logger.info("LLM available", available=health["available"], prompts=health["prompts"])
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
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    prefix = f"{settings.api_prefix}/analytics"
    _install_observability_middleware(
        app,
        service_name=SERVICE_NAME,
        metrics_path=f"{prefix}/metrics/prometheus",
        logger=_logger,
    )

    app.include_router(router, prefix=prefix)

    return app
