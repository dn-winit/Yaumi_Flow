"""
Per-pair treatment helpers: trend / seasonal / boundary signals + cold-start
warm-start.

Each function is pure compute — no I/O. Callers (train_pipeline,
inference_pipeline) decide when to invoke and what to do with the output.

Goals:
  * Treat each pair as it deserves (declining pairs route to damped-trend
    ETS, growing to ETS additive, strongly-seasonal to seasonal ETS,
    pairs near the ADI/CV2 boundary get a wider candidate pool).
  * Cold-start pairs (in inference feed but never trained) get a warm-start
    forecast from same-route trained siblings instead of a silent drop.

Everything below is config-driven via the ``routing`` block in config.yaml;
no thresholds or window lengths are hardcoded in callers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Routing signals: trend, seasonality, near-boundary
# ---------------------------------------------------------------------------


def compute_pattern_signals(
    train_window: pd.DataFrame,
    *,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    granularity: str,
    recent_window_periods: int,
    growing_threshold: float,
    declining_threshold: float,
    seasonal_lag: int | None = None,
    adi_threshold: float,
    cv2_threshold: float,
    adi_boundary_tolerance: float,
    cv2_boundary_tolerance: float,
    classes_df: pd.DataFrame,
) -> pd.DataFrame:
    """Return one row per pair with new routing signals merged onto classes.

    Signals emitted (all from ``train_window`` only -- no leakage):

      * ``trend_slope``       -- least-squares slope of target on the last
                                 ``recent_window_periods`` rows per pair.
      * ``trend_direction``   -- ``growing`` / ``stable`` / ``declining``,
                                 thresholded on slope*window/mean (relative
                                 trend per period).
      * ``seasonal_strength`` -- autocorrelation at ``seasonal_lag`` (7 for
                                 daily, 4 for quarterly, ...). Clamped to
                                 [0, 1]. Pairs without enough history get 0.
      * ``near_class_boundary`` -- True when |adi - threshold| <=
                                 ``adi_boundary_tolerance`` OR
                                 |cv2 - threshold| <= ``cv2_boundary_tolerance``.
                                 Pairs on the boundary get a wider model
                                 pool because the class assignment is
                                 ambiguous on the data.
    """
    # Default seasonal lag picks the dominant cycle for the granularity.
    if seasonal_lag is None:
        seasonal_lag = {"D": 7, "W": 52, "M": 12, "Q": 4, "Y": 1}.get(
            (granularity or "D").strip().upper()[0], 7,
        )

    p = train_window[group_keys + [date_col, target_col]].copy()
    p[date_col] = pd.to_datetime(p[date_col], errors="coerce")
    p = p.dropna(subset=[date_col]).sort_values(group_keys + [date_col])

    # ---- Trend signals over the recent window per pair ------------------
    tail = p.groupby(group_keys, sort=False).tail(recent_window_periods)
    trend_rows: list[dict] = []
    for keys, g in tail.groupby(group_keys, sort=False):
        y = g[target_col].to_numpy(dtype=float)
        if y.size < 2:
            slope = 0.0
            mean_val = float(y.mean()) if y.size else 0.0
        else:
            x = np.arange(y.size, dtype=float)
            # numpy polyfit returns [slope, intercept].
            slope = float(np.polyfit(x, y, 1)[0])
            mean_val = float(y.mean())
        # Relative trend: how much the level moves over the window vs the
        # local mean. Dimensionless. Robust to scale differences across pairs.
        denom = mean_val if mean_val > 1e-9 else 1e-9
        rel = slope * recent_window_periods / denom
        if rel > growing_threshold:
            direction = "growing"
        elif rel < declining_threshold:
            direction = "declining"
        else:
            direction = "stable"
        # Build the row from group_keys -> values
        row: dict = {}
        if not isinstance(keys, tuple):
            keys = (keys,)
        for k, v in zip(group_keys, keys, strict=False):
            row[k] = v
        row["trend_slope"] = slope
        row["trend_direction"] = direction
        trend_rows.append(row)
    trend_df = pd.DataFrame(trend_rows, columns=group_keys + ["trend_slope", "trend_direction"])

    # ---- Seasonal strength: autocorr at seasonal lag --------------------
    seasonal_rows: list[dict] = []
    min_history = seasonal_lag * 2 + 1
    for keys, g in p.groupby(group_keys, sort=False):
        y = g[target_col].to_numpy(dtype=float)
        if y.size < min_history or y.std() < 1e-9:
            strength = 0.0
        else:
            # Pearson autocorrelation between y[lag:] and y[:-lag].
            try:
                rho = float(np.corrcoef(y[seasonal_lag:], y[:-seasonal_lag])[0, 1])
                strength = max(0.0, rho if not np.isnan(rho) else 0.0)
            except Exception:
                strength = 0.0
        row = {}
        if not isinstance(keys, tuple):
            keys = (keys,)
        for k, v in zip(group_keys, keys, strict=False):
            row[k] = v
        row["seasonal_strength"] = strength
        seasonal_rows.append(row)
    seasonal_df = pd.DataFrame(
        seasonal_rows, columns=group_keys + ["seasonal_strength"],
    )

    # ---- Near-class-boundary flag ---------------------------------------
    boundary_df = classes_df.reset_index()[group_keys + ["adi", "cv2"]].copy()
    near_adi = (boundary_df["adi"] - adi_threshold).abs() <= adi_boundary_tolerance
    near_cv2 = (boundary_df["cv2"] - cv2_threshold).abs() <= cv2_boundary_tolerance
    boundary_df["near_class_boundary"] = (
        (near_adi.fillna(False)) | (near_cv2.fillna(False))
    ).astype(bool)
    boundary_df = boundary_df[group_keys + ["near_class_boundary"]]

    # ---- Merge all three onto a single signal frame ----------------------
    out = trend_df.merge(seasonal_df, on=group_keys, how="outer")
    out = out.merge(boundary_df, on=group_keys, how="outer")
    # Defaults for pairs missing from any of the three (defensive).
    out["trend_slope"] = out["trend_slope"].fillna(0.0)
    out["trend_direction"] = out["trend_direction"].fillna("stable")
    out["seasonal_strength"] = out["seasonal_strength"].fillna(0.0)
    out["near_class_boundary"] = out["near_class_boundary"].fillna(False).astype(bool)
    return out


# ---------------------------------------------------------------------------
# Cold-start warm-start: borrow forecast from same-route siblings
# ---------------------------------------------------------------------------


def warm_start_unseen_pairs(
    forecast: pd.DataFrame,
    *,
    feed_pairs: pd.DataFrame,
    classes_df: pd.DataFrame,
    group_keys: list[str],
    date_col: str,
    min_siblings: int = 2,
) -> tuple[pd.DataFrame, list[tuple], list[tuple]]:
    """Augment ``forecast`` with warm-start rows for cold-start pairs.

    For each pair in ``feed_pairs`` that is NOT in ``classes_df`` (cold-start),
    look up trained pairs on the SAME route (first group_key). If at least
    ``min_siblings`` trained pairs exist on that route, emit a warm-start
    forecast equal to the per-date MEDIAN of those siblings' predictions.

    Median (not mean) is used so a single outlier sibling can't drag the
    warm-start. Probabilistic columns (``p_demand``, ``qty_if_demand``) are
    set conservatively: p_demand=1.0 (no probabilistic model exists for the
    new pair), qty_if_demand=prediction.

    Returns ``(augmented_forecast, warm_started_pair_keys, still_cold_pair_keys)``
    so the caller can record provenance in pair_coverage.csv. A pair whose
    route has fewer than ``min_siblings`` trained pairs stays cold (the
    pipeline's existing drop+log path handles those).
    """
    if forecast.empty or feed_pairs.empty or len(group_keys) < 1:
        return forecast, [], []

    trained_pair_keys = set(
        classes_df.reset_index()[group_keys].itertuples(index=False, name=None)
    )
    feed_pair_keys = set(feed_pairs[group_keys].itertuples(index=False, name=None))
    cold_pairs = sorted(feed_pair_keys - trained_pair_keys)
    if not cold_pairs:
        return forecast, [], []

    route_col = group_keys[0]
    trained_table = classes_df.reset_index()[group_keys]

    new_rows: list[pd.DataFrame] = []
    warm_started: list[tuple] = []
    still_cold: list[tuple] = []

    # Pre-group forecast by route once for O(routes) sibling lookup.
    if route_col not in forecast.columns:
        return forecast, [], cold_pairs
    forecast_by_route = forecast.groupby(route_col, sort=False)

    for cold_pair in cold_pairs:
        cold_route = cold_pair[0]
        sibling_keys = trained_table[trained_table[route_col] == cold_route]
        if len(sibling_keys) < min_siblings:
            still_cold.append(cold_pair)
            continue
        if cold_route not in forecast_by_route.groups:
            still_cold.append(cold_pair)
            continue
        route_forecast = forecast_by_route.get_group(cold_route)
        sibling_forecast = route_forecast.merge(
            sibling_keys, on=group_keys, how="inner",
        )
        if sibling_forecast.empty:
            still_cold.append(cold_pair)
            continue
        # Per-date median across siblings -- robust to outlier siblings.
        median = (
            sibling_forecast.groupby(date_col, sort=False)["prediction"]
            .median().rename("prediction").reset_index()
        )
        # Stamp the cold pair's identity into the warm-start rows.
        for k, v in zip(group_keys, cold_pair, strict=False):
            median[k] = v
        # Conservative defaults: no probabilistic / quantile model exists
        # for an unseen pair, so we mark it explicitly.
        median["class"] = "warm_start"
        median["model_used"] = "route_sibling_median"
        median["p_demand"] = 1.0
        median["qty_if_demand"] = median["prediction"]
        new_rows.append(median)
        warm_started.append(cold_pair)

    if not new_rows:
        return forecast, [], still_cold

    warm_df = pd.concat(new_rows, ignore_index=True)
    # Align columns: keep every column the original forecast had; missing
    # ones (q_10, q_90, adi, ...) stay NaN so downstream readers (DbPusher,
    # API) handle them via their existing missing-column tolerance.
    out = pd.concat([forecast, warm_df], ignore_index=True, sort=False)
    return out, warm_started, still_cold
