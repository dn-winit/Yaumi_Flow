"""
Calendar-derived features.

Each component is gated by granularity: weekday-family features only emit at
daily grain, week-of-year at daily-or-weekly, etc. This stops meaningless
constants from being trained on when the panel is aggregated monthly.

All components are opt-in via ``cfg.temporal.components``. Unknown names are
skipped silently (caller decides the feature surface).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._calendar import (
    is_daily,
    is_daily_or_weekly,
    is_monthly_or_finer,
    resolve_weekend_mask,
)

# Component name → (granularity predicate, emitter).
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

# ``*_since_start`` needs group_keys — handled in the orchestrator below.

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


def add_temporal_features(
    df: pd.DataFrame,
    date_col: str,
    cfg: dict,
    *,
    group_keys: list[str] | None = None,
    granularity: str = "D",
) -> pd.DataFrame:
    """Emit temporal features listed in ``cfg['components']`` that are valid
    at the current granularity. Unknown / out-of-scope components are
    silently skipped."""
    df = df.copy()
    d = df[date_col]
    components = cfg.get("components", []) or []

    for name in components:
        entry = _COMPONENTS.get(name)
        if entry is None:
            continue
        gate, emit = entry
        if not gate(granularity):
            continue
        emit(df, d, cfg)

    # Per-pair trend feature. Expressed in periods, not days, so the name is
    # correct at any granularity. Requires group_keys to scope per pair.
    if "periods_since_start" in components and group_keys:
        df["periods_since_start"] = (
            df.groupby(group_keys).cumcount().astype(int)
        )

    return df
