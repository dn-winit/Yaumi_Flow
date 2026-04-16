"""
Aggregated KPI summary endpoint for dashboard consumption.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("", response_model=ForecastSummaryResponse)
def forecast_summary(svc: ArtifactService = Depends(get_artifact_service)):
    """Small aggregated payload for KPI dashboards."""
    metrics_df = svc.get_model_metrics()
    accuracy_pct = 0.0
    if not metrics_df.empty and "mape" in metrics_df.columns:
        mape_series = metrics_df["mape"].dropna()
        if not mape_series.empty:
            accuracy_pct = round(float(100.0 - mape_series.mean()), 2)

    class_summary = svc.get_class_summary()
    total_pairs = int(class_summary.get("total_pairs", 0))
    classes = {str(k): int(v) for k, v in class_summary.get("classes", {}).items()}

    test_df, test_total = svc.get_test_predictions(limit=1, offset=0)
    future_df, future_total = svc.get_future_forecast(limit=1, offset=0)

    last_forecast_date = None
    if future_total > 0:
        full_future_df, _ = svc.get_future_forecast(limit=10000, offset=0)
        if not full_future_df.empty and "TrxDate" in full_future_df.columns:
            last_forecast_date = str(full_future_df["TrxDate"].max())

    training_summary_exists = bool(svc.get_training_summary())

    return ForecastSummaryResponse(
        accuracy_pct=accuracy_pct,
        total_pairs=total_pairs,
        classes=classes,
        test_predictions_count=int(test_total),
        future_forecast_count=int(future_total),
        last_forecast_date=last_forecast_date,
        training_summary_exists=training_summary_exists,
    )
