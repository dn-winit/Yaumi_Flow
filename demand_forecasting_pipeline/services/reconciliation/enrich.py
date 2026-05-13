"""Canonical helper to enrich any forecast frame with the reconciled
van-load column.

This is the **single implementation** of:
    forecast row + bias + opening-stock  ->  recommended load

Used by every consumer that needs a reconciled value:
    * data_import.services.eda_service          (forecast-rows, business-kpis)
    * demand_forecasting_pipeline.api.routes.predictions
    * demand_forecasting_pipeline.services.accuracy_service
    * recommended_order.data.manager

Process-wide singletons:
    * BiasService -- its own mtime cache over demand_forecast.csv
    * ReconciliationEngine -- stateless
    * Closing-stock index -- mtime-keyed cache so the CSV is parsed once
      per process per file revision, regardless of how many endpoints
      call this helper.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import L4_DISABLED

logger = logging.getLogger(__name__)

# Process-wide flag so the "L4 disabled" warning fires once per process.
# The Prometheus counter still increments on every call so monitoring sees
# the rate -- only the log line is rate-limited.
_L4_WARNED = False
_L4_WARN_LOCK = threading.Lock()

def _warn_l4_disabled_once(reason: str) -> None:
    """Emit a single WARNING per process when reconciliation degrades to
    V5_b because L4 inputs (q_low / q_high) are missing from the forecast
    frame. Bumps the Prometheus counter every time."""
    global _L4_WARNED
    L4_DISABLED.inc()
    with _L4_WARN_LOCK:
        if _L4_WARNED:
            return
        _L4_WARNED = True
    logger.warning(
        "reconciliation_l4_disabled",
        extra={"reason": reason, "fallback": "V5_b"},
    )

# ----------------------------------------------------------------------
# Lazy-loaded engine + bias -- one instance per process.
# ----------------------------------------------------------------------

_ENGINE_LOCK = threading.Lock()
_ENGINE_READY: bool = False
_BIAS: Any = None
_ENGINE: Any = None

def _load_engine(settings: Settings) -> tuple[Any, Any]:
    """Return ``(bias_service, engine)`` -- both ``None`` if unavailable."""
    global _ENGINE_READY, _BIAS, _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE_READY:
            return _BIAS, _ENGINE
        _ENGINE_READY = True
        try:
            # Local imports avoid a circular reference back to the
            # ``__init__`` re-exports that pull this module in.
            from demand_forecasting_pipeline.services.reconciliation.bias_service import (
                BiasService,
            )
            from demand_forecasting_pipeline.services.reconciliation.engine import (
                ReconciliationEngine,
            )
            _BIAS = BiasService(settings)
            _ENGINE = ReconciliationEngine(settings=settings)
        except Exception as exc:
            logger.warning("enrich_with_load: engine unavailable (%s)", exc)
    return _BIAS, _ENGINE

# ----------------------------------------------------------------------
# Closing-stock helpers
#
# ``enrich_with_load`` no longer reads closing_stock.csv -- the
# reconciliation runs against a SIMULATED leftover (counterfactual,
# day-1 zero, walks forward). The closing-stock file is still parsed
# by the back-test path (``routes/reconciliation.py:past_performance``)
# for the rep's real truck state on the chart, and that path uses the
# ``forward_fill_closing`` helper below.
# ----------------------------------------------------------------------

def forward_fill_closing(df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Per-(route, item) forward-fill of a closing-stock frame.

    Input frame has columns RouteCode, ItemCode, TrxDate, ClosingQty,
    one row per (route, item, observed date). Output has a row for
    every calendar day between each pair's first and last observation,
    with the most recent prior ClosingQty propagated forward up to
    ``lookback_days``. Used by both the production enrich path and the
    past-performance back-test to deliver consistent leftover lookups.
    """
    if df.empty:
        return df

    def _per_pair(group: pd.DataFrame) -> pd.DataFrame:
        group = group.set_index("TrxDate").sort_index()
        full_range = pd.date_range(group.index.min(), group.index.max(), freq="D")
        group = group.reindex(full_range)
        group["ClosingQty"] = group["ClosingQty"].ffill(limit=int(lookback_days))
        out = group.dropna(subset=["ClosingQty"]).reset_index().rename(
            columns={"index": "TrxDate"},
        )
        return out

    filled = (
        df.groupby(["RouteCode", "ItemCode"], group_keys=True, sort=False)
          .apply(_per_pair, include_groups=False)
          .reset_index()
    )
    # ``apply(include_groups=False)`` drops the group columns; restore
    # them from the resulting MultiIndex level columns.
    keep = ["RouteCode", "ItemCode", "TrxDate", "ClosingQty"]
    return filled[keep]

# ----------------------------------------------------------------------
# Public helper
# ----------------------------------------------------------------------

# Diagnostic columns the engine produces alongside the load number.
# Exposed when ``with_diagnostics=True`` so the explainability modal
# and audit views can show the breakdown without a second engine pass.
#
# ``load_lower_bound`` / ``load_upper_bound`` are the model's quantile
# band (lower_bound / upper_bound) put through the same bias-correction
# + leftover-subtraction the V5_b kernel applies to the point estimate,
# then ceil'd to the next integer so they read as whole units. They
# are the band of "fresh-issuance the rep should load" rather than the
# raw "demand could be in this range" -- consistent with
# ``recommended_load`` being the headline van-load number.
#
# ``recent_avg_per_selling_day`` is the per-(route, item) mean over the
# trailing ``forecast_below_recent_window_days`` working days, populated
# only for pairs that have recent activity. ``expected_demand`` is the
# class-aware pattern-envelope clip of ``forecast_corrected``; it is the
# value the engine consumes (instead of ``forecast_corrected``) so a
# stable item the model under-shoots gets pulled toward its recent
# pattern rather than passing a too-low fresh-load to the truck.
# ``pattern_floor_applied`` / ``pattern_ceiling_applied`` flag which
# side of the envelope (if any) bound the row.
_DIAGNOSTIC_COLS = (
    "forecast_corrected", "bias_pct", "opening_stock",
    "load_lower_bound", "load_upper_bound",
    "recent_avg_per_selling_day", "recent_std_per_selling_day",
    "expected_demand",
)

def enrich_with_load(
    df: pd.DataFrame,
    *,
    route_col: str = "RouteCode",
    item_col: str = "ItemCode",
    date_col: str = "TrxDate",
    predicted_col: str = "Predicted",
    output_col: str = "recommended_load",
    with_diagnostics: bool = True,
    settings: Optional[Settings] = None,
    # V2 column hints. ``None`` => the helper introspects the frame for
    # the standard names (``q_10`` / ``q_90`` / ``class`` / ``DemandClass``)
    # and silently disables the layer when neither is present. Callers
    # can pin a specific column to override.
    q_low_col: Optional[str] = None,
    q_high_col: Optional[str] = None,
    class_col: Optional[str] = None,
) -> pd.DataFrame:
    """Add the reconciled van load (and, by default, diagnostic columns).

    Adds ``output_col`` plus, when ``with_diagnostics=True``, three more:
    ``forecast_corrected``, ``bias_pct``, ``opening_stock`` -- all four
    are computed by the engine in the same pass, so exposing them is
    free.

    Behaviour contract:
      * Returns the **same frame untouched** if any required column is
        missing -- callers fall back to ``predicted_col`` in that case.
      * Returns a copy with ``output_col`` populated otherwise. Never
        mutates the input.
      * If the engine cannot load (cold install, missing artifacts) the
        ``output_col`` is filled with the clipped raw forecast and the
        diagnostic columns are left as zeros, so the column shape is
        stable for downstream consumers.
    """
    if df is None or df.empty:
        return df
    needed = {route_col, item_col, date_col, predicted_col}
    missing = needed - set(df.columns)
    if missing:
        # Loud signal so a malformed forecast CSV doesn't silently push
        # zero-filled reconciliation columns to the DB. Callers (DbPusher,
        # API routes) catch this in their existing try/except and emit a
        # user-actionable error.
        logger.warning(
            "enrich_with_load: input frame missing required columns %s "
            "(have: %s); returning frame untouched",
            sorted(missing), sorted(df.columns),
        )
        return df

    s = settings or get_settings()
    bias, engine = _load_engine(s)

    out = df.copy()
    if bias is None or engine is None:
        out[output_col] = out[predicted_col].clip(lower=0).astype(float)
        if with_diagnostics:
            for c in _DIAGNOSTIC_COLS:
                out[c] = 0.0
            # Boolean diagnostic flags: cold-start defaults to False so
            # the schema is stable across the with/without-engine paths.
            out["forecast_below_recent"] = False
            out["pattern_floor_applied"] = False
            out["pattern_ceiling_applied"] = False
            # Envelope basis: with no engine / no recent stats we
            # couldn't compute either envelope; the row's class factors
            # would be applied if envelope was reached, so report that
            # as the (unused) basis. Schema-stable across cold paths.
            out["envelope_basis"] = "class_factors"
        return out

    # Counterfactual reconciliation: ``opening_stock`` does NOT come from
    # closing_stock.csv (rep's actual leftover). It comes from a forward
    # simulation per (route, item):
    #   day 1 of the per-(route, item) timeline: opening = 0
    #   day d > 1: opening = max(0, prior_van_load - prior_demand)
    # The result is "what our policy would carry, given our policy ran
    # the truck from t=0."
    #
    # Symmetry with the rep side: ``prior_demand`` is the REAL sale
    # (sum of TotalQuantity from sales_recent.csv on that day) whenever
    # actuals exist; we fall back to the model's ``predicted_col`` only
    # on the forecast horizon (dates beyond ``latest_actual_date``).
    # This mirrors the rep formula ``rep_van_load[d] = closing[d-1] +
    # alloc[d]`` which is grounded in measurements on both sides. Using
    # the model's own forecast for the carry term inflates apparent
    # accuracy and -- when the model under-predicts -- understates the
    # leftover the policy would actually be carrying forward.
    out[date_col] = pd.to_datetime(out[date_col], errors="coerce").dt.normalize()
    # Defensive dedup: the simulation advances leftover sequentially per
    # (route, item, date). Duplicate rows on that key would each see
    # the same opening, then their per-row updates would race -- the
    # last one would win and the others' contribution would be lost.
    # Aggregate predictions on the natural key so the sim sees one row
    # per cell. ``first()`` on the supporting columns (q_lows, classes,
    # bounds) is safe because the engine treats them all equivalently
    # within a single (route, item, date).
    pre_rows = len(out)
    out = (
        out.groupby([route_col, item_col, date_col], as_index=False, sort=False)
           .agg({c: ("sum" if c == predicted_col else "first") for c in out.columns
                 if c not in (route_col, item_col, date_col)})
    )
    if len(out) < pre_rows:
        logger.info(
            "enrich_with_load: deduplicated %d row(s) on (route, item, date)",
            pre_rows - len(out),
        )
    # Sort ascending by (route, item, date) so the forward simulation
    # advances in chronological order. Stash the caller's original row
    # position under a hash-suffixed name so a caller's accidental
    # ``_order_`` column never collides with our preservation slot.
    order_col = "_enrich_order_d4f1_"
    out = out.assign(**{order_col: np.arange(len(out))})
    out = out.sort_values(
        [route_col, item_col, date_col], kind="stable",
    ).reset_index(drop=True)
    rcodes = out[route_col].astype(str)
    icodes = out[item_col].astype(str)
    preds  = out[predicted_col]
    dates  = out[date_col]

    # V2 column resolution -- introspect once, reuse per row. Cheap and
    # bounded by the column count (~15).
    # Recognise both the legacy training-time names (``q_10``/``q_90``)
    # and the canonical DB-mirror names (``lower_bound``/``upper_bound``)
    # so the engine can be called against either source without an
    # explicit ``q_low_col``/``q_high_col`` override.
    q_low_resolved  = q_low_col  or next(
        (c for c in ("q_10", "q10", "Q10", "lower_bound", "LowerBound") if c in out.columns),
        None,
    )
    q_high_resolved = q_high_col or next(
        (c for c in ("q_90", "q90", "Q90", "upper_bound", "UpperBound") if c in out.columns),
        None,
    )
    class_resolved  = class_col  or next((c for c in ("class", "DemandClass") if c in out.columns), None)
    q_lows  = pd.to_numeric(out[q_low_resolved],  errors="coerce") if q_low_resolved  else None
    q_highs = pd.to_numeric(out[q_high_resolved], errors="coerce") if q_high_resolved else None
    classes = out[class_resolved].astype(str)                       if class_resolved  else None

    # L4 (quantile loading) needs both bounds AND a class column. Anything
    # missing degrades to V5_b silently -- emit one structured warning per
    # process plus a Prometheus counter so monitoring sees the rate.
    l4_active = q_lows is not None and q_highs is not None and classes is not None
    if not l4_active:
        missing: list[str] = []
        if q_lows is None:
            missing.append("q_low")
        if q_highs is None:
            missing.append("q_high")
        if classes is None:
            missing.append("class")
        _warn_l4_disabled_once(reason=f"missing columns: {missing}")

    # L1b input: per-pair sample size in the bias window. Mtime-cached
    # so sales_recent.csv is parsed once per file revision per process.
    n_active_idx = _pair_active_days_index(
        Path(s.shared_data_dir) / s.sales_recent_file,
        bias_lookback_days=int(getattr(s, "bias_lookback_days", 30)),
    )

    # Actual-sale lookup for the per-day carry simulation. The
    # simulation step that advances ``sim_leftover`` between dates
    # uses real sales whenever they exist, and falls back to the
    # model's prediction only on the forecast horizon. Same mtime-keyed
    # caching pattern as the other ``sales_recent.csv`` consumers in
    # this module so the CSV is parsed once per revision per process.
    actual_sold_at, latest_actual_date = _actual_sold_index(
        Path(s.shared_data_dir) / s.sales_recent_file,
    )

    # Guard indices: mtime-cached, empty => no-op (cold install / synthetic frames).
    if getattr(s, "concentration_guard_enabled", False):
        concentrated_idx = _concentrated_buyers_index(
            Path(s.shared_data_dir) / s.customer_data_file,
            window_days=int(s.concentration_window_days),
            threshold=float(s.concentration_threshold),
            top_k=int(s.concentration_top_k),
            min_units=float(s.concentration_min_units),
        )
        journey_idx = (
            _journey_index(Path(s.shared_data_dir) / s.journey_plan_file)
            if concentrated_idx else {}
        )
    else:
        concentrated_idx = {}
        journey_idx = {}

    # Build the per-row inputs as numpy arrays in a single pass (no
    # Python loop over the engine call). This is the live-path hot
    # surface; vectorising matches the past-performance handler's
    # ``recommend_batch`` usage and gives ~5-10x speedup on big frames.
    n = len(out)
    if n == 0:
        out[output_col] = pd.Series([], dtype="float64")
        if with_diagnostics:
            for c in _DIAGNOSTIC_COLS:
                out[c] = pd.Series([], dtype="float64")
            out["forecast_below_recent"] = pd.Series([], dtype="bool")
            out["pattern_floor_applied"] = pd.Series([], dtype="bool")
            out["pattern_ceiling_applied"] = pd.Series([], dtype="bool")
            out["envelope_basis"] = pd.Series([], dtype="object")
        return out

    rc_arr = rcodes.to_numpy()
    ic_arr = icodes.to_numpy()
    forecasts_arr = pd.to_numeric(preds, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    bias_arr = np.fromiter(
        (float(bias.lookup(rc_arr[i], ic_arr[i]) or 0.0) for i in range(n)),
        dtype=float, count=n,
    )
    # Adaptive per-pair calibration ratio (industry-grade replacement
    # for the legacy bias formula). Pairs with no history land as NaN
    # so the engine kernel falls back to bias_pct for them.
    calib_table = bias.get_calibration_table()
    if calib_table:
        calibration_arr: Optional[np.ndarray] = np.fromiter(
            (
                calib_table.get((str(rc_arr[i]), str(ic_arr[i])), float("nan"))
                for i in range(n)
            ),
            dtype=float, count=n,
        )
    else:
        calibration_arr = None

    # Class-aware bias trim cap (settings-driven; no hardcoded values).
    # The legacy bias formula and the preferred calibration_ratio path
    # both feed into the same forecast_corrected output; capping ONLY
    # bias_pct would leave the calibration path uncapped and the trim
    # would still amplify model swings on erratic / lumpy items. Cap
    # both surfaces uniformly so the engine sees a bounded correction
    # regardless of which path drives p_corr. Smooth and intermittent
    # rows pass through unchanged -- their bias estimates are stable.
    if classes is not None:
        cls_iter = classes.to_numpy() if hasattr(classes, "to_numpy") else np.asarray(classes)
        for i in range(n):
            cap = s.bias_trim_cap_for_class(
                cls_iter[i] if cls_iter[i] is not None else None
            )
            if cap is None:
                continue
            # Cap |bias_pct| symmetrically around 0 (legacy path).
            if bias_arr[i] > cap:
                bias_arr[i] = cap
            elif bias_arr[i] < -cap:
                bias_arr[i] = -cap
            # Cap |calibration_ratio - 1.0| (preferred path). The ratio
            # multiplies the forecast directly, so a cap on its deviation
            # from neutral (1.0) matches the cap on bias_pct's deviation
            # from 0. Pairs without a calibration ratio (NaN) skip this.
            if calibration_arr is not None and np.isfinite(calibration_arr[i]):
                lo, hi = 1.0 - cap, 1.0 + cap
                if calibration_arr[i] < lo:
                    calibration_arr[i] = lo
                elif calibration_arr[i] > hi:
                    calibration_arr[i] = hi

    n_active_arr = (
        np.fromiter(
            (float(n_active_idx.get((rc_arr[i], ic_arr[i]), 0)) for i in range(n)),
            dtype=float, count=n,
        )
        if n_active_idx else None
    )
    classes_arr = (
        np.array([str(c) if c is not None else "" for c in classes], dtype=object)
        if classes is not None else None
    )
    q_lows_arr  = q_lows.to_numpy(dtype=float)  if q_lows  is not None else None
    q_highs_arr = q_highs.to_numpy(dtype=float) if q_highs is not None else None

    # Recent per-selling-day stats index (mtime-cached). Built before
    # the per-date sim loop so the envelope step inside the loop can
    # clip each row's ``forecast_corrected`` against the z-score band
    # without rebuilding the index per call. Empty dict => no recent
    # activity available, envelope step degrades to a no-op for every
    # row and the engine consumes the unmodified ``forecast_corrected``.
    # The stats index returns (mean, std, active_days) per (route, item)
    # so the z-score envelope and the cold-start fallback (multiplicative
    # class factors when active_days < min_active_days) share one CSV
    # parse + one cache entry per file revision.
    recent_window_days = int(s.forecast_below_recent_window_days)
    recent_stats_idx = _recent_stats_per_selling_day_index(
        Path(s.shared_data_dir) / s.sales_recent_file,
        window_working_days=recent_window_days,
    )
    # Convenience view: legacy mean-only lookup used by the below_recent
    # flag below (same data, no second CSV parse).
    recent_avg_idx = {k: v[0] for k, v in recent_stats_idx.items()}
    min_active_days = int(s.pattern_envelope_min_active_days)

    # Forward simulation per date. Within a single date we batch every
    # (route, item) row through the engine in one call (vectorised); the
    # outer loop advances ``sim_leftover`` between dates so each (route,
    # item) walks its own carry trajectory. Day 1 of each pair starts at
    # opening = 0 by construction (no inheritance from rep's history).
    loads      = np.zeros(n, dtype=float)
    p_corr     = np.zeros(n, dtype=float)
    openings_arr = np.zeros(n, dtype=float)
    # Pattern-envelope diagnostics. Populated row-by-row inside the
    # per-date sim loop so the forward simulation advances against the
    # CLIPPED expected_demand (truck weight the policy actually loads)
    # rather than the raw forecast_corrected.
    recent_avg_arr = np.zeros(n, dtype=float)
    recent_std_arr = np.zeros(n, dtype=float)
    expected_arr   = np.zeros(n, dtype=float)
    floor_applied_arr   = np.zeros(n, dtype=bool)
    ceiling_applied_arr = np.zeros(n, dtype=bool)
    # Per-row tag of which envelope basis fired: "z_score" when we
    # had enough active days (>= min_active_days) and a non-zero std,
    # "class_factors" when we fell back to the multiplicative factors.
    # Cold-start (no recent activity at all) also lands as "class_factors"
    # by construction -- the envelope is a no-op there.
    envelope_basis_arr = np.full(n, "class_factors", dtype=object)
    # True => concentrated item whose whale is absent from today's journey;
    # the engine's load is overridden to zero on those rows.
    guard_mask_arr = np.zeros(n, dtype=bool)
    sim_leftover: dict[tuple[str, str], float] = {}
    dates_arr = dates.to_numpy()
    # Group row indices by date in ascending order. ``out`` is already
    # sorted by (route, item, date), so within each date the rows still
    # cover every (route, item) pair that has data for that date.
    unique_dates = pd.unique(dates_arr)
    valid_dates = pd.Series(unique_dates).dropna().sort_values().to_numpy()
    for d in valid_dates:
        day_mask = dates_arr == d
        day_idx = np.where(day_mask)[0]
        if day_idx.size == 0:
            continue
        rc_day = rc_arr[day_idx]
        ic_day = ic_arr[day_idx]
        # Build openings from the simulated leftover state.
        openings_day = np.fromiter(
            (sim_leftover.get((rc_day[i], ic_day[i]), 0.0) for i in range(day_idx.size)),
            dtype=float, count=day_idx.size,
        )
        loads_day, p_corr_day, _, _ = engine.recommend_batch(
            forecasts=forecasts_arr[day_idx],
            bias_pcts=bias_arr[day_idx],
            openings=openings_day,
            use_carry_floor=False,
            q_lows=q_lows_arr[day_idx]   if q_lows_arr   is not None else None,
            q_highs=q_highs_arr[day_idx] if q_highs_arr  is not None else None,
            classes=classes_arr[day_idx] if classes_arr  is not None else None,
            n_active_days=n_active_arr[day_idx] if n_active_arr is not None else None,
            calibration_ratios=calibration_arr[day_idx] if calibration_arr is not None else None,
        )
        # Pattern-envelope reconciliation -- per-(route, item) z-score
        # band (preferred) with multiplicative class factors as the
        # cold-start fallback. For each row k:
        #     z = pattern_envelope_z_for_class(cls)
        #     if recent_avg > 0 and recent_std > 0 and active >= min_active_days:
        #         floor   = max(0, recent_avg - z * recent_std)
        #         ceiling = recent_avg + z * recent_std
        #         basis   = "z_score"
        #     else:
        #         floor   = recent_avg * floor_factor[cls]
        #         ceiling = recent_avg * ceiling_factor[cls]
        #         basis   = "class_factors"
        #     expected_demand = clip(forecast_corrected, floor, ceiling)
        # Rows without recent activity (``recent_avg == 0``) skip the
        # envelope entirely -- there's no pattern to anchor against; the
        # engine consumes the unmodified forecast_corrected.
        expected_day      = p_corr_day.copy()
        recent_day        = np.zeros(day_idx.size, dtype=float)
        std_day           = np.zeros(day_idx.size, dtype=float)
        floor_app_day     = np.zeros(day_idx.size, dtype=bool)
        ceiling_app_day   = np.zeros(day_idx.size, dtype=bool)
        basis_day         = np.full(day_idx.size, "class_factors", dtype=object)
        if recent_stats_idx:
            for k in range(day_idx.size):
                stats = recent_stats_idx.get((rc_day[k], ic_day[k]))
                if stats is None:
                    continue
                ra, rstd, active = stats
                if ra <= 0.0:
                    continue
                recent_day[k] = ra
                std_day[k]    = rstd
                cls_k = (classes_arr[day_idx[k]] if classes_arr is not None else None)
                if rstd > 0.0 and active >= min_active_days:
                    z = s.pattern_envelope_z_for_class(cls_k)
                    floor   = max(0.0, ra - z * rstd)
                    ceiling = ra + z * rstd
                    basis_day[k] = "z_score"
                else:
                    floor   = ra * s.pattern_floor_factor_for_class(cls_k)
                    ceiling = ra * s.pattern_ceiling_factor_for_class(cls_k)
                    basis_day[k] = "class_factors"
                clipped = float(np.clip(p_corr_day[k], floor, ceiling))
                expected_day[k] = clipped
                # Identity: floor wins only when the clip lifted the
                # value; ceiling wins only when the clip lowered it.
                # Equality with both bounds (rare degenerate case)
                # collapses to neither flag firing.
                if clipped > p_corr_day[k] + 1e-9:
                    floor_app_day[k] = True
                elif clipped < p_corr_day[k] - 1e-9:
                    ceiling_app_day[k] = True
        # Engine input rewire: the fresh-load is now driven by
        # ``expected_demand``, not ``forecast_corrected``. Identity
        # ``recommended_load = max(0, expected_demand - opening_stock)``
        # is preserved -- only the value being subtracted from
        # changes. The downstream identity
        # ``recommended_van_load = opening_stock + recommended_load``
        # holds unchanged.
        loads_day = np.maximum(0.0, expected_day - openings_day)
        # Whale-driven row + whale not on today's journey => override load to 0.
        if concentrated_idx and journey_idx:
            d_iso = pd.Timestamp(d).normalize().date().isoformat()
            day_journey = journey_idx.get(d_iso)
            if day_journey is not None:
                day_mask = np.zeros(day_idx.size, dtype=bool)
                for k in range(day_idx.size):
                    whales = concentrated_idx.get((rc_day[k], ic_day[k]))
                    if whales is None:
                        continue
                    route_journey = day_journey.get(rc_day[k])
                    if route_journey is None or whales.isdisjoint(route_journey):
                        day_mask[k] = True
                if day_mask.any():
                    loads_day = np.where(day_mask, 0.0, loads_day)
                    p_corr_day = np.where(day_mask, 0.0, p_corr_day)
                    expected_day = np.where(day_mask, 0.0, expected_day)
                    guard_mask_arr[day_idx] = day_mask
        loads[day_idx] = loads_day
        p_corr[day_idx] = p_corr_day
        openings_arr[day_idx] = openings_day
        recent_avg_arr[day_idx]      = recent_day
        recent_std_arr[day_idx]      = std_day
        expected_arr[day_idx]        = expected_day
        floor_applied_arr[day_idx]   = floor_app_day
        ceiling_applied_arr[day_idx] = ceiling_app_day
        envelope_basis_arr[day_idx]  = basis_day
        # Advance leftover with REAL sales when available, falling back
        # to the model's forecast only for future-horizon dates where
        # actuals don't exist yet. This keeps the policy-side carry
        # ledger as actuals-grounded as the rep side (rep_van_load[d]
        # = closing[d-1] + alloc[d] -- both measurements). Masked rows
        # force demand=0 so prior leftover is preserved (whale absent
        # => no sale).
        d_ts = pd.Timestamp(d).normalize() if not pd.isna(d) else None
        use_actuals = (
            d_ts is not None
            and latest_actual_date is not None
            and d_ts <= latest_actual_date
        )
        for k in range(day_idx.size):
            van_load_k = openings_day[k] + max(loads_day[k], 0.0)
            if guard_mask_arr[day_idx[k]]:
                demand_k = 0.0
            elif use_actuals:
                demand_k = max(
                    float(actual_sold_at.get((rc_day[k], ic_day[k], d_ts), 0.0)),
                    0.0,
                )
            else:
                demand_k = max(forecasts_arr[day_idx[k]], 0.0)
            sold_k = min(demand_k, van_load_k)
            sim_leftover[(rc_day[k], ic_day[k])] = max(0.0, van_load_k - sold_k)

    # Sanity flag: forecast_corrected falls below class-aware fraction
    # of recent_avg_per_selling_day. Surfaces rows where today's reconciled
    # forecast is materially under the item's recent pattern -- the rep
    # can decide whether to trust the model dampening or override on the
    # van. ``recent_avg_idx`` already built above; the flag keys off
    # ``forecast_corrected`` (pre-envelope), not ``expected_demand`` -- the
    # supervisor's interest signal is "model under-shot recent pattern"
    # regardless of whether the envelope caught it downstream.
    below_recent = np.zeros(n, dtype=bool)
    if recent_avg_idx:
        for i in range(n):
            recent_avg = recent_avg_idx.get((rc_arr[i], ic_arr[i]), 0.0)
            if recent_avg <= 0.0:
                continue
            cls_i = (classes_arr[i] if classes_arr is not None else None)
            factor = s.forecast_below_recent_factor_for_class(cls_i)
            if p_corr[i] < recent_avg * factor:
                below_recent[i] = True

    out[output_col] = pd.Series(loads, index=out.index, dtype="float64")
    if with_diagnostics:
        out["forecast_corrected"] = pd.Series(p_corr,       index=out.index, dtype="float64")
        out["bias_pct"]           = pd.Series(bias_arr,     index=out.index, dtype="float64")
        out["opening_stock"]      = pd.Series(openings_arr, index=out.index, dtype="float64")
        out["forecast_below_recent"] = pd.Series(below_recent, index=out.index, dtype="bool")
        # Pattern-envelope diagnostics. ``recent_avg_per_selling_day``
        # is the per-(route, item) anchor; ``expected_demand`` is the
        # class-aware clip of ``forecast_corrected`` against the
        # envelope; the two booleans surface which side (if any) the
        # clip bound. The engine consumes ``expected_demand`` as its
        # fresh-load driver -- not ``forecast_corrected`` -- so a
        # supervisor can verify the math chain raw -> bias ->
        # forecast_corrected -> expected_demand -> recommended_load.
        out["recent_avg_per_selling_day"] = pd.Series(
            recent_avg_arr, index=out.index, dtype="float64",
        )
        out["recent_std_per_selling_day"] = pd.Series(
            recent_std_arr, index=out.index, dtype="float64",
        )
        out["expected_demand"] = pd.Series(
            expected_arr, index=out.index, dtype="float64",
        )
        out["pattern_floor_applied"] = pd.Series(
            floor_applied_arr, index=out.index, dtype="bool",
        )
        out["pattern_ceiling_applied"] = pd.Series(
            ceiling_applied_arr, index=out.index, dtype="bool",
        )
        # Envelope basis: "z_score" when the per-(route, item) std drove
        # the band, "class_factors" when active days were below the
        # min-active-days threshold (cold-start fallback). Pre-recent
        # rows (no recent_avg) also land as "class_factors" -- the
        # envelope was a no-op, the row's basis tag stays at the
        # default. Supervisor can quickly distinguish "the engine
        # personalised this item's envelope" from "we used the class
        # default" in the explainability popup.
        out["envelope_basis"] = pd.Series(
            envelope_basis_arr, index=out.index, dtype="object",
        )
        # Per-row flag so consumers can render "skipped: top buyer not on
        # today's journey plan" instead of an unexplained zero.
        out["guard_skipped"]      = pd.Series(guard_mask_arr, index=out.index, dtype="bool")

        # Reconciled VAN LOAD bounds. Identity:
        #   recommended_van_load = recommended_load + opening_stock
        # The bracket here covers the truck's total weight under our
        # policy, not the fresh issuance alone, so the dashboard tile's
        # headline value always sits inside the displayed range.
        #
        # Why the formula MUST mirror the engine kernel (engine.py L4):
        #   target_lo = min(q_low, p_corr)   <- kernel uses RAW q_low
        #   target_hi = max(q_high, p_corr)  <- kernel uses RAW q_high
        # The kernel does NOT multiply q_low/q_high by the calibration
        # ratio -- only the point estimate (p_corr) is calibrated. An
        # earlier version of this bound applied calibration to the
        # quantiles too, which made the lower bound overshoot the
        # actual L4-dampened target on lumpy / intermittent items
        # (260 rows / 8864 in production), producing van_load < lower.
        # Mirroring the kernel exactly is the only formulation that
        # keeps the bracket strict.
        #
        # Rounding: lower uses ``floor`` and upper uses ``ceil`` so the
        # bracket is GUARANTEED to contain the real-valued van_load.
        # Using ``ceil`` on both sides would let the lower bound exceed
        # actual van_load by up to 1 unit due to rounding alone.
        if q_lows_arr is not None and q_highs_arr is not None:
            # p_corr the same way the kernel computes it (engine.py L1):
            #   * with calibration ratio: forecast * calibration_ratio
            #     (NaN ratio -> cold-start fallback ratio)
            #   * without calibration: forecast / (1 + bias_pct)
            if calibration_arr is not None:
                cr_clean = np.where(
                    np.isfinite(calibration_arr) & (calibration_arr >= 0.0),
                    calibration_arr, np.nan,
                )
                cold_start = float(s.calibration_cold_start_ratio)
                use_calib = np.isfinite(cr_clean)
                p_corr_for_bounds = np.where(
                    use_calib,
                    forecasts_arr * cr_clean,
                    forecasts_arr * cold_start,
                )
            else:
                denom = np.maximum(1.0 + bias_arr, 1e-6)
                p_corr_for_bounds = forecasts_arr / denom
            # Target range across the L4 quantile band (same min/max
            # the kernel takes against p_corr, with raw q_low / q_high).
            target_low  = np.minimum(q_lows_arr,  p_corr_for_bounds)
            target_high = np.maximum(q_highs_arr, p_corr_for_bounds)
            # Van-load range = max(opening, target_range) -- same
            # leftover-aware shape the kernel applies to the point
            # estimate.
            van_load_low  = np.maximum(openings_arr, target_low)
            van_load_high = np.maximum(openings_arr, target_high)
            load_low  = np.floor(van_load_low)
            load_high = np.ceil(van_load_high)
        else:
            # Bounds unknown for this row -- bracket the van_load point
            # estimate (recommended_load + opening) so consumers always
            # have a valid range with the same additive semantics.
            point_van_load = np.maximum(0.0, loads) + openings_arr
            load_low  = np.floor(point_van_load)
            load_high = np.ceil(point_van_load)
        # Masked rows: zero fresh issuance, so collapse bracket to bracket(opening).
        if guard_mask_arr.any():
            load_low  = np.where(guard_mask_arr, np.floor(openings_arr), load_low)
            load_high = np.where(guard_mask_arr, np.ceil(openings_arr),  load_high)
        out["load_lower_bound"] = pd.Series(load_low,  index=out.index, dtype="float64")
        out["load_upper_bound"] = pd.Series(load_high, index=out.index, dtype="float64")

        # Visibility flag so API responses can show whether the row's
        # ``recommended_load`` reflects the full L4 + V5_b stack or just
        # the V5_b kernel (degraded mode).
        out["l4_active"] = bool(l4_active)

    # Restore caller's original row order, then drop the helper column.
    out = out.sort_values(order_col, kind="stable").reset_index(drop=True)
    out = out.drop(columns=[order_col])
    return out

# ----------------------------------------------------------------------
# Actual-sale lookup index (mtime-keyed). The simulation that advances
# ``sim_leftover`` between dates uses real sales (sum of TotalQuantity
# from sales_recent.csv on the (route, item, date) key) so the policy
# side of the recon ledger is grounded in measurements -- same as the
# rep side. ``latest_actual_date`` is the route-agnostic max date in
# the CSV; rows beyond that fall back to ``predicted_col`` because the
# forecast horizon has no actuals yet.
# ----------------------------------------------------------------------

_ACTUAL_LOCK = threading.Lock()
_ACTUAL_CACHE: "dict[tuple[str, int, int], tuple[dict[tuple[str, str, pd.Timestamp], float], Optional[pd.Timestamp]]]" = {}


def _actual_sold_index(
    sales_path: Path,
) -> tuple[dict[tuple[str, str, pd.Timestamp], float], Optional[pd.Timestamp]]:
    """Per-(route, item, date) actuals lookup plus the max trx date.

    Mtime-keyed on ``sales_recent.csv`` so the CSV is parsed once per
    revision per process. Missing keys return ``0.0`` via dict.get on
    the caller side -- consistent with "no row that day means zero
    units sold" semantics.
    """
    if not sales_path.exists():
        return {}, None
    stat = sales_path.stat()
    key = (str(sales_path), stat.st_mtime_ns, stat.st_size)
    with _ACTUAL_LOCK:
        cached = _ACTUAL_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        df = pd.read_csv(
            sales_path, low_memory=False,
            usecols=lambda c: c in {
                "RouteCode", "ItemCode", "TrxDate", "TotalQuantity",
            },
        )
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
        df = df.dropna(subset=["TrxDate"])
        if df.empty:
            empty: dict[tuple[str, str, pd.Timestamp], float] = {}
            with _ACTUAL_LOCK:
                _ACTUAL_CACHE[key] = (empty, None)
            return empty, None
        df["RouteCode"] = df.RouteCode.astype(str)
        df["ItemCode"]  = df.ItemCode.astype(str)
        df["TotalQuantity"] = pd.to_numeric(df["TotalQuantity"], errors="coerce").fillna(0.0)
        # Aggregate to one row per (route, item, date). The sim consumes
        # a Dict lookup, so collapsing now avoids per-call groupby.
        agg = (
            df.groupby(["RouteCode", "ItemCode", "TrxDate"], sort=False)["TotalQuantity"]
              .sum().astype(float)
        )
        idx: dict[tuple[str, str, pd.Timestamp], float] = {
            (str(r), str(i), pd.Timestamp(d)): float(v)
            for (r, i, d), v in agg.items()
        }
        latest = pd.Timestamp(df["TrxDate"].max()).normalize()
    except Exception as exc:
        logger.warning(
            "enrich_with_load: actual-sales index build failed (%s)", exc,
        )
        return {}, None
    with _ACTUAL_LOCK:
        _ACTUAL_CACHE[key] = (idx, latest)
    return idx, latest


# ----------------------------------------------------------------------
# Recent per-selling-day stats lookup (mtime-keyed). Powers BOTH the
# ``forecast_below_recent`` sanity flag and the per-(route, item)
# z-score pattern envelope. For each (route, item):
#   mean       = mean(daily_sold | selling days, window)
#   std        = sample std(daily_sold | selling days, window)  (ddof=1)
#   active_days = count of selling days in window
# Parsed once per CSV revision per window choice per process.
# ----------------------------------------------------------------------

_RECENT_STATS_LOCK = threading.Lock()
# (path, mtime, size, window) -> {(route, item): (mean, std, active_days)}
_RECENT_STATS_CACHE: "dict[tuple[str, int, int, int], dict[tuple[str, str], tuple[float, float, int]]]" = {}


def _recent_stats_per_selling_day_index(
    sales_path: Path, *, window_working_days: int,
) -> dict[tuple[str, str], tuple[float, float, int]]:
    """Per-(route, item) (mean, std, active_days) across selling days
    in the trailing window.

    Mtime + window keyed so the cost is paid once per CSV revision per
    window choice per process. "Selling day" = a day with sum > 0 for
    the pair. Std is the sample std (ddof=1) computed over daily totals
    on those selling days; pairs with only one selling day get std=0.0
    (single observation -> no spread). ``active_days`` lets callers
    decide whether the std is trustworthy enough to use vs. fall back
    to the multiplicative class factors. Returns ``{}`` when the CSV
    is missing / empty / unparseable.
    """
    if not sales_path.exists():
        return {}
    stat = sales_path.stat()
    key = (
        str(sales_path), stat.st_mtime_ns, stat.st_size,
        int(window_working_days),
    )
    with _RECENT_STATS_LOCK:
        cached = _RECENT_STATS_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        df = pd.read_csv(
            sales_path, low_memory=False,
            usecols=lambda c: c in {
                "RouteCode", "ItemCode", "TrxDate", "TotalQuantity",
            },
        )
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
        df = df.dropna(subset=["TrxDate"])
        if df.empty:
            with _RECENT_STATS_LOCK:
                _RECENT_STATS_CACHE[key] = {}
            return {}
        cutoff = df["TrxDate"].max() - pd.Timedelta(days=int(window_working_days))
        df = df[df["TrxDate"] >= cutoff]
        if df.empty:
            with _RECENT_STATS_LOCK:
                _RECENT_STATS_CACHE[key] = {}
            return {}
        df["RouteCode"] = df.RouteCode.astype(str)
        df["ItemCode"]  = df.ItemCode.astype(str)
        df["TotalQuantity"] = pd.to_numeric(df["TotalQuantity"], errors="coerce").fillna(0.0)
        # Per (route, item, date) daily sum -- one row per selling day.
        daily = (
            df.groupby(["RouteCode", "ItemCode", "TrxDate"], sort=False)["TotalQuantity"]
              .sum().astype(float)
        )
        daily = daily[daily > 0.0]
        if daily.empty:
            with _RECENT_STATS_LOCK:
                _RECENT_STATS_CACHE[key] = {}
            return {}
        # Single groupby pass aggregating mean + std + count keeps the
        # cost the same as the old mean-only path -- no second scan.
        # ``std()`` uses ddof=1 by default; pairs with one selling day
        # land as NaN, which we coerce to 0.0 (single observation has
        # no spread; caller's min-active-days guard suppresses these).
        agg = daily.groupby(level=[0, 1], sort=False).agg(["mean", "std", "count"])
        idx: dict[tuple[str, str], tuple[float, float, int]] = {}
        for (r, i), row in agg.iterrows():
            mean = float(row["mean"])
            std_raw = row["std"]
            std = float(std_raw) if pd.notna(std_raw) else 0.0
            active = int(row["count"])
            idx[(str(r), str(i))] = (mean, std, active)
    except Exception as exc:
        logger.warning(
            "enrich_with_load: recent-stats index build failed (%s)", exc,
        )
        return {}
    with _RECENT_STATS_LOCK:
        _RECENT_STATS_CACHE[key] = idx
    return idx


# ----------------------------------------------------------------------
# Per-pair active-day index (mtime-keyed; parses sales_recent.csv once
# per file revision per process)
# ----------------------------------------------------------------------

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CACHE: "dict[tuple[str, int, int, int], dict[tuple[str, str], int]]" = {}

def _pair_active_days_index(
    sales_path: Path, *, bias_lookback_days: int,
) -> dict[tuple[str, str], int]:
    """Distinct sale-days per (route, item) in the trailing bias window.

    Mtime + lookback keyed so the cost is paid once per CSV revision
    per lookback choice per process, regardless of call volume.
    """
    if not sales_path.exists():
        return {}
    stat = sales_path.stat()
    key = (str(sales_path), stat.st_mtime_ns, stat.st_size, int(bias_lookback_days))
    with _ACTIVE_LOCK:
        cached = _ACTIVE_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        df = pd.read_csv(sales_path, low_memory=False,
                         usecols=lambda c: c in {"RouteCode", "ItemCode", "TrxDate"})
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
        df = df.dropna(subset=["TrxDate"])
        if df.empty:
            return {}
        df["RouteCode"] = df.RouteCode.astype(str)
        df["ItemCode"]  = df.ItemCode.astype(str)
        cutoff = df["TrxDate"].max() - pd.Timedelta(days=int(bias_lookback_days))
        df = df[df["TrxDate"] >= cutoff]
        if df.empty:
            return {}
        idx = df.groupby(["RouteCode", "ItemCode"])["TrxDate"].nunique().astype(int).to_dict()
    except Exception as exc:
        logger.warning("enrich_with_load: pair active-day index build failed (%s)", exc)
        return {}
    with _ACTIVE_LOCK:
        _ACTIVE_CACHE[key] = idx
    return idx


# ----------------------------------------------------------------------
# Journey-aware concentration guard indices (mtime-keyed).
# ----------------------------------------------------------------------

_CONC_LOCK = threading.Lock()
_CONC_CACHE: "dict[tuple, dict[tuple[str, str], frozenset[str]]]" = {}
_JOURNEY_LOCK = threading.Lock()
_JOURNEY_CACHE: "dict[tuple, dict[str, dict[str, frozenset[str]]]]" = {}


def _concentrated_buyers_index(
    customer_path: Path,
    *,
    window_days: int,
    threshold: float,
    top_k: int,
    min_units: float,
) -> dict[tuple[str, str], frozenset[str]]:
    """Map (route, item) -> dominant buyer set for pairs whose top-k buyers
    own >= ``threshold`` of trailing-``window_days`` units. Pairs absent
    from the map are never masked."""
    if not customer_path.exists():
        return {}
    stat = customer_path.stat()
    key = (
        str(customer_path), stat.st_mtime_ns, stat.st_size,
        int(window_days), float(threshold), int(top_k), float(min_units),
    )
    with _CONC_LOCK:
        cached = _CONC_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        df = pd.read_csv(
            customer_path, low_memory=False,
            usecols=lambda c: c in {
                "RouteCode", "ItemCode", "CustomerCode", "TrxDate", "TotalQuantity",
            },
        )
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
        df = df.dropna(subset=["TrxDate"])
        # Drop returns; they erode whale dominance and falsely promote secondary buyers.
        df = df[pd.to_numeric(df["TotalQuantity"], errors="coerce").fillna(0.0) > 0]
        if df.empty:
            return {}
        cutoff = df["TrxDate"].max() - pd.Timedelta(days=int(window_days))
        df = df[df["TrxDate"] >= cutoff]
        if df.empty:
            return {}
        df["RouteCode"]    = df.RouteCode.astype(str)
        df["ItemCode"]     = df.ItemCode.astype(str)
        df["CustomerCode"] = df.CustomerCode.astype(str)
        agg = (
            df.groupby(["RouteCode", "ItemCode", "CustomerCode"])["TotalQuantity"]
              .sum().astype(float)
        )
        out: dict[tuple[str, str], frozenset[str]] = {}
        for (route, item), grp in agg.groupby(level=[0, 1], sort=False):
            total = float(grp.sum())
            if total < min_units:
                continue
            top = grp.nlargest(top_k)
            if float(top.sum()) / total >= threshold:
                buyers = frozenset(
                    str(c) for c in top.index.get_level_values("CustomerCode")
                )
                if buyers:
                    out[(str(route), str(item))] = buyers
    except Exception as exc:
        logger.warning(
            "enrich_with_load: concentrated-buyers index build failed (%s)", exc,
        )
        return {}
    with _CONC_LOCK:
        _CONC_CACHE[key] = out
    return out


def _journey_index(
    journey_path: Path,
) -> dict[str, dict[str, frozenset[str]]]:
    """Map ``date_iso -> {route_code: frozenset(customer_code)}``. Nested
    by date so the per-row guard lookup is two O(1) dict gets."""
    if not journey_path.exists():
        return {}
    stat = journey_path.stat()
    key = (str(journey_path), stat.st_mtime_ns, stat.st_size)
    with _JOURNEY_LOCK:
        cached = _JOURNEY_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        df = pd.read_csv(
            journey_path, low_memory=False,
            usecols=lambda c: c in {"RouteCode", "CustomerCode", "JourneyDate", "TrxDate"},
        )
        date_col = "JourneyDate" if "JourneyDate" in df.columns else "TrxDate"
        if date_col not in df.columns or "RouteCode" not in df.columns or "CustomerCode" not in df.columns:
            return {}
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.normalize()
        df = df.dropna(subset=[date_col])
        if df.empty:
            return {}
        df["RouteCode"]    = df.RouteCode.astype(str)
        df["CustomerCode"] = df.CustomerCode.astype(str)
        out: dict[str, dict[str, frozenset[str]]] = {}
        for (d, route), grp in df.groupby([date_col, "RouteCode"], sort=False):
            d_iso = pd.Timestamp(d).date().isoformat()
            out.setdefault(d_iso, {})[str(route)] = frozenset(grp["CustomerCode"].astype(str))
    except Exception as exc:
        logger.warning("enrich_with_load: journey index build failed (%s)", exc)
        return {}
    with _JOURNEY_LOCK:
        _JOURNEY_CACHE[key] = out
    return out
