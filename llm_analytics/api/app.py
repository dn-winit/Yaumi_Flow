"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from llm_analytics.api.routes import router
from llm_analytics.config.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("llm_analytics.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info("LLM Analytics starting -- provider=%s, model=%s", settings.provider, settings.model)
        from llm_analytics.api.dependencies import get_analyzer
        analyzer = get_analyzer()
        health = analyzer.health()
        _logger.info("LLM available: %s, prompts: %s", health["available"], health["prompts"])
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

    app.include_router(router, prefix=f"{settings.api_prefix}/analytics")

    return app
