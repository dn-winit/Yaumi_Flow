"""
Metrics endpoints -- model performance data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import MetricsResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/models", response_model=MetricsResponse)
def get_model_metrics(
    demand_class: Optional[str] = Query(None, description="Filter by demand class"),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df = svc.get_model_metrics(demand_class)
    return MetricsResponse(
        success=True, total=len(df),
        data=df.to_dict("records") if not df.empty else [],
    )
