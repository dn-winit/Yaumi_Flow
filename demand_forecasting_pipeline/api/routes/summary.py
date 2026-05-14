"""
Aggregated KPI summary endpoint for dashboard consumption.
"""

from __future__ import annotations

import logging

import pandas as pd
from fastapi import APIRouter, Depends

logger = logging.getLogger(__name__)

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.src.evaluation.metrics import composite_summary

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("", response_model=ForecastSummaryResponse)
def forecast_summary(svc: ArtifactService = Depends(get_artifact_service)):
    """KPI payload for the Pipeline page. Accuracy is the canonical
    class-aware composite, shared with drift + Past-performance drawer.

    Schema resolution and tolerance kwargs both come from
    ``ArtifactService`` so this endpoint always scores under the same
    contract as the training-time baseline -- no parallel column-name
    fallbacks, no silent divergence from the YAML."""
    settings = get_settings()
    test_df, test_total = svc.get_test_predictions(
        limit=settings.summary_test_predictions_limit, offset=0,
    )

    actual_col = svc.resolve_actual_column(test_df)
    pred_col = svc.resolve_prediction_column(test_df)

    # Accuracy is sourced from test_predictions.csv -- the same artifact
    # the drift detector reads. training_summary.json's presence gates
    # ``trained_at`` separately (different artifact, different concern).
    # ``None`` only when the test CSV itself is missing, so the UI shows
    # an em-dash consistently across this tile and the drift baseline.
    accuracy_pct: float | None = None
    has_training_summary = bool(svc.get_training_summary())
    if not test_df.empty and actual_col is not None and pred_col is not None:
        actual = pd.to_numeric(test_df[actual_col], errors="coerce").fillna(0)
        predicted = pd.to_numeric(test_df[pred_col], errors="coerce").fillna(0)
        cls = test_df["class"].astype(str).to_numpy() if "class" in test_df.columns else None
        accuracy_pct = composite_summary(
            actual.to_numpy(),
            predicted.to_numpy(),
            cls,
            **svc.composite_accuracy_kwargs,
        )["accuracy_pct"]

    class_summary = svc.get_class_summary()
    raw_total = class_summary.get("total_pairs")
    total_pairs: int | None = int(raw_total) if isinstance(raw_total, int) and raw_total > 0 else None
    classes = {str(k): int(v) for k, v in class_summary.get("classes", {}).items()}

    # Single cache read returns both count + max date -- avoids the pair
    # of get_future_forecast() calls (limit=1 then limit=10_000) the
    # summary endpoint used to issue, and removes the implicit cap that
    # silently dropped rows beyond 10k from the max-date computation.
    future_total, last_forecast_date = svc.get_future_forecast_meta()

    # Training overview - extracted from artifacts already in memory, no extra I/O.
    training_overview = _build_training_overview(svc, test_df)

    return ForecastSummaryResponse(
        accuracy_pct=accuracy_pct,
        total_pairs=total_pairs,
        classes=classes,
        test_predictions_count=int(test_total),
        future_forecast_count=int(future_total),
        last_forecast_date=last_forecast_date,
        training_summary_exists=has_training_summary,
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

    # Trained-at: prefer the canonical timestamp the training pipeline
    # writes into training_summary.json on completion. Falls back to
    # MAX(created_at) on yf_demand_forecast -- that column is stamped
    # by the training step when it pushes predictions to the DB, so it
    # survives a missing training_summary.json (e.g. cleared during a
    # cleanup) and is itself a dynamic, source-of-truth timestamp.
    canonical = (
        (ts.get("trained_at") if ts else None)
        or (ts.get("_metadata", {}).get("trained_at") if ts else None)
    )
    if canonical:
        overview["trained_at"] = str(canonical)
    else:
        db_ts = _last_demand_forecast_push(svc)
        if db_ts is not None:
            overview["trained_at"] = db_ts

    return overview


def _last_demand_forecast_push(svc: ArtifactService) -> str | None:
    """MAX(created_at) on yf_demand_forecast as an ISO string, or None if
    the DB is unreachable / the table has no rows. The timestamp is
    stamped by the training step's DB push so it tracks completion."""
    try:
        s = getattr(svc, "_s", None)
        if s is None or not getattr(s.db, "host", ""):
            return None
        import pyodbc
        with pyodbc.connect(s.db.connection_string(), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(created_at) FROM [YaumiAIML].[dbo].[yf_demand_forecast] WITH (NOLOCK)"
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return row[0].isoformat()
    except Exception as exc:
        logger.warning("last_demand_forecast_push probe failed: %s", exc)
        return None
