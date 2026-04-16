"""
Health check endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_pipeline_service,
)
from demand_forecasting_pipeline.api.schemas import ArtifactStatus, HealthResponse
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.pipeline_service import PipelineService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(
    art_svc: ArtifactService = Depends(get_artifact_service),
    pipe_svc: PipelineService = Depends(get_pipeline_service),
):
    settings = get_settings()
    artifacts = art_svc.check_artifacts()
    all_present = all(artifacts.values())

    statuses = pipe_svc.get_all_status()
    pipe_summary = {k: v.get("status", "unknown") for k, v in statuses.items()}

    return HealthResponse(
        status="healthy" if all_present else "degraded",
        artifacts=ArtifactStatus(**artifacts),
        pipelines=pipe_summary,
        config_path=settings.pipeline_config,
        cache_keys=art_svc._cache.keys,
    )
