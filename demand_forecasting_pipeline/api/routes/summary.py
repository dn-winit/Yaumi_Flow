"""
Aggregated KPI summary endpoint for dashboard consumption.
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.src.evaluation.metrics import composite_summary

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("", response_model=ForecastSummaryResponse)
def forecast_summary(svc: ArtifactService = Depends(get_artifact_service)):
    """KPI payload for the Pipeline page. Accuracy is the canonical
    class-aware composite, shared with drift + Past-performance drawer."""
    test_df, test_total = svc.get_test_predictions(limit=50_000, offset=0)

    # New artifacts use TotalQuantity / prediction; fall back for older snapshots.
    actual_col = "TotalQuantity" if "TotalQuantity" in test_df.columns else "actual_qty"
    pred_col = "prediction" if "prediction" in test_df.columns else "predicted"

    accuracy_pct = 0.0
    if not test_df.empty and actual_col in test_df.columns and pred_col in test_df.columns:
        actual = pd.to_numeric(test_df[actual_col], errors="coerce").fillna(0)
        predicted = pd.to_numeric(test_df[pred_col], errors="coerce").fillna(0)
        cls = test_df["class"].astype(str).to_numpy() if "class" in test_df.columns else None
        accuracy_pct = composite_summary(actual.to_numpy(), predicted.to_numpy(), cls)["accuracy_pct"]

    class_summary = svc.get_class_summary()
    total_pairs = int(class_summary.get("total_pairs", 0))
    classes = {str(k): int(v) for k, v in class_summary.get("classes", {}).items()}

    future_df, future_total = svc.get_future_forecast(limit=1, offset=0)

    last_forecast_date = None
    if future_total > 0:
        full_future_df, _ = svc.get_future_forecast(limit=10_000, offset=0)
        if not full_future_df.empty and "TrxDate" in full_future_df.columns:
            last_forecast_date = str(full_future_df["TrxDate"].max())

    # Training overview — extracted from artifacts already in memory, no extra I/O.
    training_overview = _build_training_overview(svc, test_df)

    return ForecastSummaryResponse(
        accuracy_pct=accuracy_pct,
        total_pairs=total_pairs,
        classes=classes,
        test_predictions_count=int(test_total),
        future_forecast_count=int(future_total),
        last_forecast_date=last_forecast_date,
        training_summary_exists=bool(svc.get_training_summary()),
        training_overview=training_overview,
    )


def _build_training_overview(svc: ArtifactService, test_df: pd.DataFrame) -> dict:
    """Assemble a client-friendly overview of the last training run."""
    overview: dict = {}

    # Test date range (from the df we already loaded for WAPE)
    if not test_df.empty and "TrxDate" in test_df.columns:
        dates = test_df["TrxDate"].dropna()
        overview["test_date_start"] = str(dates.min())
        overview["test_date_end"] = str(dates.max())
        overview["test_routes"] = int(test_df["RouteCode"].nunique()) if "RouteCode" in test_df.columns else 0
        overview["test_items"] = int(test_df["ItemCode"].nunique()) if "ItemCode" in test_df.columns else 0

    # Per-class best model + its WAPE
    ts = svc.get_training_summary() or {}
    per_class = ts.get("per_class", {})
    class_winners = []
    total_models = 0
    for cls, info in per_class.items():
        metrics = info.get("metrics", {})
        models = info.get("models_trained", [])
        total_models += len(models)
        if metrics:
            best_name, best_wape = min(metrics.items(), key=lambda x: x[1])
            class_winners.append({
                "demand_class": cls,
                "best_model": best_name,
                "wape": round(best_wape, 1),
                "models_competed": len(models),
            })
    overview["class_winners"] = class_winners
    overview["total_models_trained"] = total_models

    # Feature count from schema
    schema = ts.get("schema", {})
    feature_cols = schema.get("feature_cols", [])
    overview["feature_count"] = len(feature_cols)

    # Trained-at from model file modification times
    model_files = svc.list_model_files()
    if model_files:
        latest_mtime = max(f.get("modified", 0) for f in model_files)
        if latest_mtime > 0:
            from datetime import datetime
            overview["trained_at"] = datetime.fromtimestamp(latest_mtime).isoformat()

    return overview
