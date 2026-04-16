"""
Prediction endpoints -- test predictions and future forecasts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import PredictionResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService, DEFAULT_PAGE_LIMIT

router = APIRouter(prefix="/predictions", tags=["predictions"])


@router.get("/test", response_model=PredictionResponse)
def get_test_predictions(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df, total = svc.get_test_predictions(route_code, item_code, limit, offset)
    return PredictionResponse(
        success=True, source="test_predictions", total=total,
        data=df.to_dict("records") if not df.empty else [],
    )


@router.get("/forecast/route-summary")
def get_future_route_summary(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to full horizon"),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """Per-route aggregates from the future forecast (tiny payload for grid views)."""
    return {"success": True, "date": date, "routes": svc.get_future_route_summary(date)}


@router.get("/forecast", response_model=PredictionResponse)
def get_future_forecast(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df, total = svc.get_future_forecast(route_code, item_code, limit, offset)
    return PredictionResponse(
        success=True, source="future_forecast", total=total,
        data=df.to_dict("records") if not df.empty else [],
    )
