"""
Pipeline endpoints -- trigger training/inference, check status.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from fastapi import Query

from demand_forecasting_pipeline.api.dependencies import get_artifact_service, get_db_pusher, get_pipeline_service
from demand_forecasting_pipeline.api.schemas import (
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineStatusResponse,
)
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.db_pusher import DbPusher
from demand_forecasting_pipeline.services.pipeline_service import PipelineService

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/train", response_model=PipelineRunResponse)
def trigger_training(
    req: PipelineRunRequest = PipelineRunRequest(),
    svc: PipelineService = Depends(get_pipeline_service),
):
    result = svc.run_training(req.config_path)
    return PipelineRunResponse(**result)


@router.post("/inference", response_model=PipelineRunResponse)
def trigger_inference(
    req: PipelineRunRequest = PipelineRunRequest(),
    svc: PipelineService = Depends(get_pipeline_service),
):
    result = svc.run_inference(req.config_path)
    return PipelineRunResponse(**result)


@router.get("/status/{pipeline_name}", response_model=PipelineStatusResponse)
def get_pipeline_status(
    pipeline_name: str,
    svc: PipelineService = Depends(get_pipeline_service),
):
    return PipelineStatusResponse(**svc.get_status(pipeline_name))


@router.get("/status")
def get_all_status(svc: PipelineService = Depends(get_pipeline_service)):
    return svc.get_all_status()


@router.post("/invalidate-cache")
def invalidate_cache(svc: ArtifactService = Depends(get_artifact_service)):
    svc.invalidate_cache()
    return {"success": True, "message": "Cache cleared"}


@router.post("/push-to-db")
def push_to_db(
    datasplit: str = Query(default="forecast", description="forecast | test"),
    pusher: DbPusher = Depends(get_db_pusher),
):
    """Push prediction artifacts to YaumiAIML database."""
    return pusher.push_predictions(datasplit)
