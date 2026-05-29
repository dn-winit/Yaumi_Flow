"""Forecast-KPI computation; produces ForecastSummaryResponse from artifact reads.

In the service layer so retrain_scheduler can reuse it without route->service inversion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService

# Avoid circular import (api.schemas <- api package init <- routes <- this module).
if TYPE_CHECKING:
    from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse

logger = logging.getLogger(__name__)


def compute_forecast_summary(svc: ArtifactService) -> ForecastSummaryResponse:
    """KPI payload for the Pipeline page; canonical class-aware composite shared with drift."""
    # Lazy import to avoid circular dep.
    from demand_forecasting_pipeline.api.schemas import ForecastSummaryResponse

    settings = get_settings()
    test_df, test_total = svc.get_test_predictions(
        limit=settings.summary_test_predictions_limit, offset=0,
    )

    # Shared scorer (same call drift baseline uses).
    has_training_summary = bool(svc.get_training_summary())
    accuracy_pct, _ = svc.score_test_predictions()

    class_summary = svc.get_class_summary()
    raw_total = class_summary.get("total_pairs")
    total_pairs: int | None = (
        int(raw_total) if isinstance(raw_total, int) and raw_total > 0 else None
    )
    classes = {str(k): int(v) for k, v in class_summary.get("classes", {}).items()}

    # Single cache read for count + max date (avoids prior dual-fetch + 10k cap).
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
            # accuracy_pct mirrors ``composite_summary`` so the per-class
            # subtitle in the webapp can render directly without recomputing
            # ``100 - wape`` client-side. Floor at 0 in case of a degenerate
            # >100% WAPE (shouldn't happen post-clamp, but keeps the contract
            # honest).
            class_winners.append({
                "demand_class": cls,
                "best_model": best_name,
                "wape": round(best_wape, 1),
                "accuracy_pct": round(max(0.0, 100.0 - best_wape), 1),
                "models_competed": len(models),
            })
    overview["class_winners"] = class_winners
    overview["total_models_trained"] = total_models

    # Feature count from schema
    schema = ts.get("schema", {})
    feature_cols = schema.get("feature_cols", [])
    overview["feature_count"] = len(feature_cols)

    # Trained-at: prefer metadata.trained_at (canonical); legacy fallbacks then DB MAX(created_at).
    # DB fallback can be stale (MERGE preserves created_at on UPDATE), so artifact key wins.
    canonical = (
        (ts.get("trained_at") if ts else None)
        or (ts.get("metadata", {}).get("trained_at") if ts else None)
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
    """MAX(created_at) on the demand-forecast table as ISO; None if DB unreachable / empty.

    Reads the table FQN from settings (``demand_table``) rather than a string
    literal -- a schema rename or staging-env retarget must not silently
    break the staleness probe.
    """
    try:
        s = getattr(svc, "_s", None)
        if s is None or not getattr(s.db, "host", ""):
            return None
        table = getattr(s, "demand_table", "") or "[YaumiAIML].[dbo].[yf_demand_forecast]"
        from common.db_pool import get_pool
        pool = get_pool(s.db.connection_string(), query_timeout=10, autocommit=True)
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(created_at) FROM {table} WITH (NOLOCK)")
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return row[0].isoformat()
    except Exception as exc:
        logger.warning("last_demand_forecast_push probe failed: %s", exc)
        return None
