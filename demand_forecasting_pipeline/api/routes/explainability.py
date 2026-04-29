from __future__ import annotations

from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import ClassSummaryResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

router = APIRouter(prefix="/explainability", tags=["explainability"])


@router.get("/classes/summary", response_model=ClassSummaryResponse)
def get_class_summary(svc: ArtifactService = Depends(get_artifact_service)):
    """Demand-class breakdown (smooth / intermittent / erratic / lumpy)."""
    summary = svc.get_class_summary()
    raw_total = summary.get("total_pairs")
    return ClassSummaryResponse(
        success=True,
        total_pairs=int(raw_total) if isinstance(raw_total, int) and raw_total > 0 else None,
        classes=summary.get("classes", {}),
    )
