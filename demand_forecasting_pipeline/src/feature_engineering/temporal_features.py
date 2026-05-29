"""
Calendar-derived features.

Each component is gated by granularity: weekday-family features only emit at
daily grain, week-of-year at daily-or-weekly, etc. This stops meaningless
constants from being trained on when the panel is aggregated monthly.

All components are opt-in via ``cfg.temporal.components``. Unknown names are
skipped silently (caller decides the feature surface).

Train/serve parity for ``periods_since_start``
----------------------------------------------
The trend feature is date-anchored at each pair's FIRST observation in
the training data. If we recompute that anchor from the inference frame
(which may not extend back to the pair's true origin), every pair
silently resets its trend to 0 at the start of the inference window --
a textbook train/serve skew bug. The fix is to persist the per-pair
origin map at training time (``save_pair_origins``) and load it at
inference (``load_pair_origins``). Cold-start fallback to inline
groupby min is kept as a safety net for legacy callers that don't
pass the artifact, but the production path is artifact-driven.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..utils.io_utils import save_json
from ._calendar import (
    is_daily,
    is_daily_or_weekly,
    is_monthly_or_finer,
    resolve_weekend_mask,
)

# Component name -> (granularity predicate, emitter).
# Emitters receive ``(df, date_series, cfg)`` and mutate df in-place.
#
# This registry is the single source of truth for temporal feature names.
# To add a feature: add one entry here.


def _emit_day_of_week(df, d, _):
    df["day_of_week"] = d.dt.dayofweek

def _emit_day_of_week_sin(df, d, _):
    df["day_of_week_sin"] = np.sin(2 * np.pi * d.dt.dayofweek / 7.0)

def _emit_day_of_week_cos(df, d, _):
    df["day_of_week_cos"] = np.cos(2 * np.pi * d.dt.dayofweek / 7.0)

def _emit_day_of_month(df, d, _):
    df["day_of_month"] = d.dt.day

def _emit_days_in_month(df, d, _):
    df["days_in_month"] = d.dt.days_in_month

def _emit_days_to_month_end(df, d, _):
    df["days_to_month_end"] = (d.dt.days_in_month - d.dt.day).astype(int)

def _emit_week_of_year(df, d, _):
    df["week_of_year"] = d.dt.isocalendar().week.astype(int)

def _emit_is_weekend(df, d, cfg):
    df["is_weekend"] = resolve_weekend_mask(d, cfg.get("weekend_days", [])).astype(int)

def _emit_month(df, d, _):
    df["month"] = d.dt.month

def _emit_month_sin(df, d, _):
    df["month_sin"] = np.sin(2 * np.pi * d.dt.month / 12.0)

def _emit_month_cos(df, d, _):
    df["month_cos"] = np.cos(2 * np.pi * d.dt.month / 12.0)

def _emit_quarter(df, d, _):
    df["quarter"] = d.dt.quarter

def _emit_quarter_sin(df, d, _):
    df["quarter_sin"] = np.sin(2 * np.pi * d.dt.quarter / 4.0)

def _emit_quarter_cos(df, d, _):
    df["quarter_cos"] = np.cos(2 * np.pi * d.dt.quarter / 4.0)

def _emit_year(df, d, _):
    df["year"] = d.dt.year

# ``*_since_start`` needs group_keys - handled in the orchestrator below.

_COMPONENTS: dict[str, tuple] = {
    "day_of_week":       (is_daily,             _emit_day_of_week),
    "day_of_week_sin":   (is_daily,             _emit_day_of_week_sin),
    "day_of_week_cos":   (is_daily,             _emit_day_of_week_cos),
    "day_of_month":      (is_daily,             _emit_day_of_month),
    "days_in_month":     (is_monthly_or_finer,  _emit_days_in_month),
    "days_to_month_end": (is_daily,             _emit_days_to_month_end),
    "week_of_year":      (is_daily_or_weekly,   _emit_week_of_year),
    "is_weekend":        (is_daily,             _emit_is_weekend),
    "month":             (is_monthly_or_finer,  _emit_month),
    "month_sin":         (is_monthly_or_finer,  _emit_month_sin),
    "month_cos":         (is_monthly_or_finer,  _emit_month_cos),
    "quarter":           (is_monthly_or_finer,  _emit_quarter),
    "quarter_sin":       (is_monthly_or_finer,  _emit_quarter_sin),
    "quarter_cos":       (is_monthly_or_finer,  _emit_quarter_cos),
    "year":              (lambda _f: True,      _emit_year),
}


def fit_pair_origins(
    df: pd.DataFrame, group_keys: list[str], date_col: str,
) -> pd.DataFrame:
    """Compute the earliest date per pair. Returned as a DataFrame with
    columns ``group_keys + ['pair_min_date']`` (date strings). Training
    persists this so the trend feature stays date-anchored at the same
    origin even if the inference frame doesn't reach back that far."""
    if df.empty:
        return pd.DataFrame(columns=list(group_keys) + ["pair_min_date"])
    d = pd.to_datetime(df[date_col], errors="coerce")
    sub = df.assign(_d=d).dropna(subset=["_d"])
    origins = (
        sub.groupby(group_keys, sort=False)["_d"].min().reset_index()
        .rename(columns={"_d": "pair_min_date"})
    )
    origins["pair_min_date"] = origins["pair_min_date"].dt.strftime("%Y-%m-%d")
    return origins


def save_pair_origins(
    path: str | Path, *, origins_df: pd.DataFrame, group_keys: list[str],
) -> None:
    """Persist ``origins_df`` to JSON. Atomic write via the shared
    ``save_json`` helper so a killed training run never leaves
    inference to read a torn artifact."""
    p = Path(path)
    if origins_df is None or origins_df.empty:
        records: list[dict] = []
    else:
        cols = list(group_keys) + ["pair_min_date"]
        sub = origins_df[cols].copy()
        for k in group_keys:
            sub[k] = sub[k].astype(str)
        sub["pair_min_date"] = sub["pair_min_date"].astype(str)
        records = sub.to_dict(orient="records")
    payload = {
        "schema_version": "1.0",
        "group_keys": list(group_keys),
        "origins": records,
    }
    save_json(payload, str(p))


def load_pair_origins(
    path: str | Path, *, expected_group_keys: list[str],
) -> pd.DataFrame:
    """Read the JSON artifact. Validates the persisted ``group_keys``
    against the runtime list so a config rename surfaces loudly rather
    than silently applying the wrong origin map."""
    p = Path(path)
    with open(p, encoding="utf-8") as fh:
        payload = json.load(fh)
    saved = list(payload.get("group_keys") or [])
    if saved != list(expected_group_keys):
        raise ValueError(
            f"pair_origins artifact group_keys={saved!r} do not match "
            f"runtime {list(expected_group_keys)!r}; retrain to refresh"
        )
    records = payload.get("origins") or []
    if not records:
        return pd.DataFrame(columns=list(expected_group_keys) + ["pair_min_date"])
    df = pd.DataFrame(records)
    # ``astype("string")`` (pandas StringDtype), not ``astype(str)``
    # (object/Python str), so the reloaded group_keys match the panel's
    # dtype pinned to ``string`` via resolve_dtypes. Without this
    # coercion the join works today via silent pandas coercion but
    # would break under stricter merge rules.
    for k in expected_group_keys:
        df[k] = df[k].astype("string")
    df["pair_min_date"] = pd.to_datetime(df["pair_min_date"], errors="coerce")
    df = df.dropna(subset=["pair_min_date"])
    return df[list(expected_group_keys) + ["pair_min_date"]]


def add_temporal_features(
    df: pd.DataFrame,
    date_col: str,
    cfg: dict,
    *,
    group_keys: list[str] | None = None,
    granularity: str = "D",
    pair_origins: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Emit temporal features listed in ``cfg['components']`` that are valid
    at the current granularity. Unknown / out-of-scope components are
    silently skipped.

    ``pair_origins`` (optional) is the persisted per-pair origin map
    loaded from the training artifact. When supplied, the
    ``periods_since_start`` feature anchors each pair's trend at the
    persisted date instead of recomputing it from ``df`` -- guarantees
    train/serve parity even when the inference frame doesn't extend
    back to the pair's true origin."""
    df = df.copy()
    d = df[date_col]
    components = cfg.get("components", []) or []

    # Known component names -- the trend feature is registered separately
    # (it needs group_keys), but it is recognised here so a config typo
    # like ``period_since_start`` warns instead of silently producing
    # nothing.
    _TREND_FEATURE = "periods_since_start"
    known_names = set(_COMPONENTS.keys()) | {_TREND_FEATURE}
    for name in components:
        if name not in known_names:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "temporal_features: unknown component %r ignored "
                "(known: %s)", name, sorted(known_names),
            )
            continue
        entry = _COMPONENTS.get(name)
        if entry is None:
            continue
        gate, emit = entry
        if not gate(granularity):
            continue
        emit(df, d, cfg)

    # Per-pair trend feature. DATE-ANCHORED, NOT cumcount-based. The
    # value at any calendar date is identical regardless of input row
    # order, so train and inference produce the same number for the
    # same date even if the panel is sorted differently.
    #
    # ``(date - pair_min_date) / period_unit`` -- for daily data this is
    # "days since the pair's first observation". The divisor mirrors
    # ``period_offset`` granularity semantics so the feature stays
    # meaningful at any granularity.
    #
    # ``pair_origins`` precedence: if the caller supplied a persisted
    # origin map (production inference path), use it so the trend value
    # for date D is identical at train and serve time. Fall back to the
    # inline groupby min ONLY when no map is supplied (training path
    # itself, or a legacy caller). Pairs absent from the persisted map
    # also fall back to inline min for that pair -- cold-start safety so
    # a new pair landed since training doesn't crash the feature build.
    if _TREND_FEATURE in components and group_keys:
        granularity_unit = (granularity or "D").strip().upper()[0]
        period_days_map = {"D": 1, "W": 7, "M": 30, "Q": 90, "Y": 365}
        period_days = period_days_map.get(granularity_unit, 1)
        date_series = pd.to_datetime(df[date_col], errors="coerce")

        if pair_origins is not None and not pair_origins.empty:
            # Persisted-origin path. Merge on group_keys; rows whose
            # pair is absent from the artifact get NaN, which we then
            # fill from an inline per-pair min (cold-start fallback).
            # No dtype coercion here -- the loader path
            # (``load_pair_origins``) already pins group_keys to pandas
            # StringDtype, and the panel's group_keys are also string
            # via ``resolve_dtypes``. Re-coercing with ``astype(str)``
            # would silently downgrade both sides to object dtype and
            # break the StringDtype invariant the rest of the pipeline
            # relies on.
            origins = pair_origins.copy()
            origins["pair_min_date"] = pd.to_datetime(
                origins["pair_min_date"], errors="coerce",
            )
            merged = df.merge(
                origins[group_keys + ["pair_min_date"]],
                on=group_keys, how="left",
            )
            pair_min = merged["pair_min_date"]
            # Cold-start: pair not in the persisted map. Use the inline
            # min computed from the current frame for those pairs only.
            cold = pair_min.isna()
            if cold.any():
                inline_min = (
                    df.assign(_d=date_series)
                      .groupby(group_keys, sort=False)["_d"].transform("min")
                )
                pair_min = pair_min.fillna(inline_min)
        else:
            pair_min = (
                df.assign(_d=date_series)
                  .groupby(group_keys, sort=False)["_d"].transform("min")
            )

        days_since = (date_series - pair_min).dt.days
        df["periods_since_start"] = (
            (days_since // max(1, period_days)).fillna(0).astype(int)
        )

    return df
