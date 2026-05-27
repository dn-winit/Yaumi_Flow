"""Aggregated KPI summary endpoint; HTTP shim around services/forecast_kpi.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.forecast_kpi import compute_forecast_summary

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("", response_model=ForecastSummaryResponse)
def forecast_summary(svc: ArtifactService = Depends(get_artifact_service)):
    """KPI payload for the Pipeline page (see services.forecast_kpi)."""
    return compute_forecast_summary(svc)
