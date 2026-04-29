from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import get_pipeline_service
from demand_forecasting_pipeline.api.schemas import (
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineStatusResponse,
)
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


@router.get("/status", response_model=Dict[str, PipelineStatusResponse])
def get_all_status(svc: PipelineService = Depends(get_pipeline_service)):
    """Bulk status -- one round-trip for every known pipeline name."""
    return svc.get_all_status()
