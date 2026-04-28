"""
Rolling-window features + intermittent-specific features.

Leakage is prevented by shifting the target by one period before the rolling
window is applied — the feature at time ``t`` never sees ``target[t]``.

``min_periods`` is driven by ``min_periods_fraction`` (default 0.5). A roll of
window 28 therefore requires at least 14 observations before it emits a
value; earlier rows are left NaN. This stops the "28-period mean computed
from 2 rows" surprise that ``min_periods=1`` produces.

All intermittent-specific features are vectorised via per-group cumulative
sums. No Python expanding-apply loops — this is an O(N) module.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


_STAT_FUNCS = {
    "mean": "mean",
    "std": "std",
    "min": "min",
    "max": "max",
    "median": "median",
    "sum": "sum",
}


def _nonzero_ratio(x: np.ndarray) -> float:
    if len(x) == 0:
        return 0.0
    return float((x > 0).sum()) / float(len(x))


def _resolve_min_periods(window: int, fraction: float | None) -> int:
    if fraction is None:
        return 1
    f = float(fraction)
    if not (0.0 < f <= 1.0):
        raise ValueError(f"min_periods_fraction must be in (0, 1], got {f}")
    return max(1, int(window * f))


def add_rolling_features(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    windows: list[int],
    stats: list[str],
    *,
    min_periods_fraction: float | None = 0.5,
) -> pd.DataFrame:
    """Emit ``roll_<stat>_<window>`` columns for each (stat, window) combo."""
    df = df.copy()
    grp = df.groupby(group_keys)[target_col]
    shifted = grp.shift(1)
    key_series = [df[k] for k in group_keys]

    for w in windows:
        min_periods = _resolve_min_periods(int(w), min_periods_fraction)
        roll = shifted.groupby(key_series).rolling(window=int(w), min_periods=min_periods)
        for stat in stats:
            col = f"roll_{stat}_{w}"
            if stat in _STAT_FUNCS:
                vals = getattr(roll, _STAT_FUNCS[stat])()
            elif stat == "nonzero_ratio":
                vals = roll.apply(_nonzero_ratio, raw=True)
            else:
                continue
            # Index-based assignment: pandas aligns on df.index, so a groupby
            # that reorders rows internally still lands values in the right
            # spots. Positional assignment (.to_numpy()) would silently break
            # if the input frame weren't pre-sorted by group_keys.
            df[col] = vals.droplevel(list(range(len(group_keys))))
    return df


def add_intermittent_features(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    cfg: dict,
) -> pd.DataFrame:
    """Per-pair expanding features for intermittent patterns, vectorised."""
    df = df.copy()
    grp = df.groupby(group_keys)[target_col]
    lag = grp.shift(1)  # leakage-free: feature at t uses values up to t-1

    if any(cfg.get(k, False) for k in ("nonzero_mean", "inter_demand_interval", "last_nonzero_gap")):
        key_series = [df[k] for k in group_keys]
        is_nonzero = (lag > 0).astype("float")
        cum_nonzero = is_nonzero.groupby(key_series).cumsum()
        lag_observed = (~lag.isna()).astype("float")
        cum_observed = lag_observed.groupby(key_series).cumsum()

        if cfg.get("nonzero_mean", False):
            cum_nz_sum = lag.where(lag > 0, 0.0).groupby(key_series).cumsum()
            mean = cum_nz_sum / cum_nonzero.replace(0.0, np.nan)
            df["nonzero_mean_to_date"] = mean.fillna(0.0).to_numpy()

        if cfg.get("inter_demand_interval", False):
            # ADI = (periods observed) / (periods with demand). When no demand
            # has been observed yet we mirror the expanding-ADI convention of
            # returning the observed period count.
            with np.errstate(invalid="ignore", divide="ignore"):
                adi = np.where(
                    cum_nonzero.to_numpy() > 0,
                    cum_observed.to_numpy() / cum_nonzero.to_numpy(),
                    cum_observed.to_numpy(),
                )
            # First row of each group has no lag observation → NaN.
            adi = np.where(lag_observed.to_numpy() == 0, np.nan, adi)
            df["inter_demand_interval"] = adi

        if cfg.get("last_nonzero_gap", False):
            df["last_nonzero_gap"] = _gap_since_last_nonzero_vectorized(
                df, group_keys, lag,
            )

    return df


def _gap_since_last_nonzero_vectorized(
    df: pd.DataFrame, group_keys: list[str], lag: pd.Series
) -> np.ndarray:
    """Per-row gap (in periods) since the last non-zero lag value.

    Single vectorised pass — forward-fill the position of the most recent
    non-zero within each group, subtract from the current position. When no
    prior non-zero exists, the gap equals the number of observed zero rows
    so far within the group.
    """
    pos = np.arange(len(df), dtype=float)
    last_nonzero_pos = pd.Series(
        np.where(lag > 0, pos, np.nan), index=df.index,
    )
    grouper = (
        df[group_keys].apply(tuple, axis=1) if len(group_keys) > 1 else df[group_keys[0]]
    )
    last_nonzero_pos = last_nonzero_pos.groupby(grouper).ffill().to_numpy()

    gap = pos - last_nonzero_pos
    # Rows before the first non-zero: count zeros seen so far within the group.
    group_pos = df.groupby(group_keys).cumcount().to_numpy()
    gap = np.where(np.isnan(gap), group_pos + 1, gap)
    # First row of a group has no lag observation at all.
    gap[lag.isna().to_numpy()] = np.nan
    return gap
