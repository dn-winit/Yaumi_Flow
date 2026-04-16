"""
Explainability endpoints -- pair classes, demand patterns, ADI/CV2 analysis.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import (
    ClassSummaryResponse,
    ExplainabilityResponse,
)
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

router = APIRouter(prefix="/explainability", tags=["explainability"])


@router.get("/classes/summary", response_model=ClassSummaryResponse)
def get_class_summary(svc: ArtifactService = Depends(get_artifact_service)):
    summary = svc.get_class_summary()
    return ClassSummaryResponse(
        success=True,
        total_pairs=summary.get("total_pairs", 0),
        classes=summary.get("classes", {}),
    )


@router.get("/classes", response_model=ExplainabilityResponse)
def get_pair_classes(
    demand_class: Optional[str] = Query(None),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df = svc.get_pair_classes(demand_class)
    return ExplainabilityResponse(
        success=True, total=len(df),
        data=df.to_dict("records") if not df.empty else [],
    )


@router.get("/pairs", response_model=ExplainabilityResponse)
def get_pair_explainability(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    demand_class: Optional[str] = Query(None),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df = svc.get_pair_explainability(route_code, item_code, demand_class)
    return ExplainabilityResponse(
        success=True, total=len(df),
        data=df.to_dict("records") if not df.empty else [],
    )
