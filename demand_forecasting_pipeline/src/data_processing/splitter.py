"""
Time-based train / val / test split.

Invariants (verified in code, not just in docs):

  - Strict time ordering. ``train.date < val.date < test.date`` at every
    boundary. No overlap.
  - Dynamic horizons. ``test_horizon`` / ``val_horizon`` come from config and
    drive both the cut points and the leakage guarantees downstream.
  - No cross-pair contamination: the cut is a global date, same for every
    pair, so classification / feature stats that limit themselves to the
    train window by date do so consistently for every pair.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..utils.time_utils import period_offset

logger = logging.getLogger(__name__)


def time_based_split(
    df: pd.DataFrame,
    date_col: str,
    test_horizon: int,
    val_horizon: int = 0,
    granularity: str = "D",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split ``df`` into (train, val, test) by global time cut.

    ``test_horizon`` and ``val_horizon`` are expressed in *periods* at the
    supplied ``granularity`` (so ``test_horizon=30`` means 30 days at
    ``granularity="D"``, 30 months at ``"M"``, etc.).

    Resulting windows:
      - ``test``  = ``[max_date - test_horizon + 1, max_date]``    (inclusive)
      - ``val``   = ``[test_start - val_horizon,   test_start)``   (half-open)
      - ``train`` = ``[min_date,                   val_start)``    (half-open)

    Empty ``val`` is returned when ``val_horizon == 0``.
    """
    if test_horizon <= 0:
        raise ValueError(f"test_horizon must be positive, got {test_horizon}")
    if val_horizon < 0:
        raise ValueError(f"val_horizon must be non-negative, got {val_horizon}")

    df = df.sort_values(date_col)
    if df.empty:
        empty = df.iloc[0:0].copy()
        return empty, empty.copy(), empty.copy()

    max_date = df[date_col].max()
    test_start = max_date - period_offset(granularity, test_horizon - 1)
    val_start = (
        test_start - period_offset(granularity, val_horizon)
        if val_horizon > 0 else test_start
    )

    train = df[df[date_col] < val_start].copy()
    val = (
        df[(df[date_col] >= val_start) & (df[date_col] < test_start)].copy()
        if val_horizon > 0 else df.iloc[0:0].copy()
    )
    test = df[df[date_col] >= test_start].copy()

    if train.empty:
        logger.warning(
            "time_based_split produced an empty train set "
            "(test_horizon=%d + val_horizon=%d covers the entire data range). "
            "Check that horizons are smaller than the panel length.",
            test_horizon, val_horizon,
        )
    return train, val, test
