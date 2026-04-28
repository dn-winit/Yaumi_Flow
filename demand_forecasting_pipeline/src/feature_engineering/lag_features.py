"""Lag features — the target value at period t-k per pair."""

from __future__ import annotations

import pandas as pd


def add_lag_features(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    lags: list[int],
) -> pd.DataFrame:
    """Add ``lag_<k>`` columns for each ``k`` in ``lags``. Lags are applied
    within each pair's history (``groupby(group_keys).shift(k)``) so the
    feature at time t never references values from a different pair."""
    df = df.copy()
    grp = df.groupby(group_keys)[target_col]
    for lag in lags:
        df[f"lag_{int(lag)}"] = grp.shift(int(lag))
    return df
