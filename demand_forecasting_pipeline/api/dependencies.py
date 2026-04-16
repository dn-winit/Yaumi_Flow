"""
FastAPI dependency injection -- singleton instances shared across requests.
"""

from __future__ import annotations

from functools import lru_cache

from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.accuracy_service import AccuracyService
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.db_pusher import DbPusher
from demand_forecasting_pipeline.services.pipeline_service import PipelineService
from demand_forecasting_pipeline.services.retrain_scheduler import AutoRetrainConfig


@lru_cache(maxsize=1)
def get_artifact_service() -> ArtifactService:
    return ArtifactService(get_settings())


@lru_cache(maxsize=1)
def get_pipeline_service() -> PipelineService:
    return PipelineService(get_settings())


@lru_cache(maxsize=1)
def get_db_pusher() -> DbPusher:
    return DbPusher(get_settings())


@lru_cache(maxsize=1)
def get_accuracy_service() -> AccuracyService:
    return AccuracyService(get_settings())


@lru_cache(maxsize=1)
def get_retrain_config() -> AutoRetrainConfig:
    return AutoRetrainConfig(settings=get_settings())
