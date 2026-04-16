"""
Model endpoints -- training summary, model files, pair-model lookup.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import (
    ModelFilesResponse,
    PairModelLookupResponse,
    TrainingSummaryResponse,
)
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/summary", response_model=TrainingSummaryResponse)
def get_training_summary(svc: ArtifactService = Depends(get_artifact_service)):
    data = svc.get_training_summary()
    return TrainingSummaryResponse(success=True, data=data)


@router.get("/files", response_model=ModelFilesResponse)
def get_model_files(svc: ArtifactService = Depends(get_artifact_service)):
    files = svc.list_model_files()
    return ModelFilesResponse(success=True, total=len(files), files=files)


@router.get("/pair-lookup", response_model=PairModelLookupResponse)
def get_pair_model_lookup(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df = svc.get_pair_model_lookup(route_code, item_code)
    return PairModelLookupResponse(
        success=True, total=len(df),
        data=df.to_dict("records") if not df.empty else [],
    )
