"""Post-push production-sanity guard catching magnitude regressions invisible to WAPE.

After db_push, snapshots per-route forecast totals + warm-start counts and
compares to prior run; material drops surface as structured warnings.
Snapshot at data/forecast/production_sanity.json; retrain history exposes last_sanity_regressions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings

logger = logging.getLogger(__name__)


# 50% floor: catches the activity_flag-class regressions (30-70% drops) without
# firing on normal day-to-day variance (<15% per route).
DEFAULT_ROUTE_MAGNITUDE_THRESHOLD = 0.50
DEFAULT_WARM_START_THRESHOLD = 0.50
HORIZON_WINDOW_DAYS = 7  # short window keeps comparison stable against horizon edge effects


@dataclass(frozen=True)
class SanitySnapshot:
    """One snapshot of production forecast magnitudes for regression detection."""
    snapshot_at: str
    horizon_start: str
    horizon_end: str
    per_route_totals: dict[str, float]
    warm_start_pairs: int
    total_predicted_rows: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Regression:
    """One specific magnitude drop detected by the comparison."""
    kind: str         # "route_total" | "warm_start"
    key: str          # route_code, or "warm_start" for the global count
    baseline: float
    current: float
    drop_pct: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _snapshot_path(settings: Settings) -> Path:
    return Path(settings.artifacts_dir) / "production_sanity.json"


def take_snapshot(settings: Settings) -> Optional[SanitySnapshot]:
    """Capture per-route totals over next HORIZON_WINDOW_DAYS; None on cold start."""
    forecast_csv = Path(settings.predictions_path(settings.future_forecast_file))
    if not forecast_csv.exists():
        return None
    df = pd.read_csv(forecast_csv, usecols=["TrxDate", "RouteCode", "prediction", "model_used"], low_memory=False)
    if df.empty:
        return None
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
    df["RouteCode"] = df["RouteCode"].astype(str)
    df["prediction"] = pd.to_numeric(df["prediction"], errors="coerce").fillna(0.0)
    today = pd.Timestamp.utcnow().normalize().tz_localize(None)
    horizon_end = today + pd.Timedelta(days=HORIZON_WINDOW_DAYS)
    scope = df[(df["TrxDate"] >= today) & (df["TrxDate"] < horizon_end)]
    if scope.empty:
        return None
    per_route = (
        scope.groupby("RouteCode")["prediction"].sum().round(1).to_dict()
    )
    warm_start_pairs = int(
        scope.loc[scope["model_used"] == "route_sibling_median", ["RouteCode"]]
        .drop_duplicates().shape[0]
    )
    return SanitySnapshot(
        snapshot_at=datetime.now(timezone.utc).isoformat(),
        horizon_start=today.date().isoformat(),
        horizon_end=horizon_end.date().isoformat(),
        per_route_totals={str(k): float(v) for k, v in per_route.items()},
        warm_start_pairs=warm_start_pairs,
        total_predicted_rows=int(scope.shape[0]),
    )


def compare(
    current: SanitySnapshot,
    baseline: SanitySnapshot,
    *,
    route_threshold: float = DEFAULT_ROUTE_MAGNITUDE_THRESHOLD,
    warm_start_threshold: float = DEFAULT_WARM_START_THRESHOLD,
) -> list[Regression]:
    """Regressions where route total / warm-start count dropped > threshold (fraction of baseline)."""
    out: list[Regression] = []
    for route, baseline_total in baseline.per_route_totals.items():
        if baseline_total <= 0:
            continue
        current_total = current.per_route_totals.get(route, 0.0)
        drop = (baseline_total - current_total) / baseline_total
        if drop > route_threshold:
            out.append(Regression(
                kind="route_total", key=route,
                baseline=baseline_total, current=current_total,
                drop_pct=round(100.0 * drop, 1),
            ))
    if baseline.warm_start_pairs > 0:
        ws_drop = (baseline.warm_start_pairs - current.warm_start_pairs) / baseline.warm_start_pairs
        if ws_drop > warm_start_threshold:
            out.append(Regression(
                kind="warm_start", key="warm_start",
                baseline=float(baseline.warm_start_pairs),
                current=float(current.warm_start_pairs),
                drop_pct=round(100.0 * ws_drop, 1),
            ))
    return out


def record_and_check(settings: Settings) -> dict[str, Any]:
    """Snapshot + compare vs prior + persist + return status. Never raises.

    Wire from pipeline_service._push_to_db on success.
    """
    path = _snapshot_path(settings)
    current = take_snapshot(settings)
    if current is None:
        logger.info("production_sanity: skipped (no forecast artifact yet)")
        return {"status": "no_snapshot", "regressions": []}

    regressions: list[Regression] = []
    if path.exists():
        try:
            prior = json.loads(path.read_text())
            baseline = SanitySnapshot(**prior)
            regressions = compare(current, baseline)
        except Exception as exc:
            logger.warning("production_sanity: baseline read failed (%s); resetting", exc)

    # Persist for next run; atomic-via-rename to survive mid-write crash.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current.to_json(), indent=2))
    tmp.replace(path)

    if regressions:
        logger.critical(
            "production_sanity_regression detected count=%d details=%s",
            len(regressions),
            [r.to_json() for r in regressions],
        )
        return {
            "status": "regression",
            "regressions": [r.to_json() for r in regressions],
            "snapshot": current.to_json(),
        }
    logger.info(
        "production_sanity ok: %d routes, %d warm_start pairs, %d rows in %s..%s",
        len(current.per_route_totals), current.warm_start_pairs,
        current.total_predicted_rows, current.horizon_start, current.horizon_end,
    )
    return {"status": "ok", "regressions": [], "snapshot": current.to_json()}
