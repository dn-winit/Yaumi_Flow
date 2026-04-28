"""
Partial stockout mitigation using only sales history.

Without availability data we can't *detect* stockouts. What we can detect is
that a zero streak is *unusual* for a given pair. For each pair we compute
the distribution of historical zero-run lengths and flag any zero that sits
in a run longer than ``mean + z * std`` of that distribution.

Behaviour by design:

  - Conservative. Pairs with too little history, too few zero runs, or zero
    variance in gap lengths are left unflagged (the column stays False).
  - Respects lifecycle flags. Rows already in a new-launch or likely-EOL
    window are not flagged, so a cold-start pair or a discontinued SKU
    doesn't get spuriously tagged.
  - Pair-local. Every pair is scored against its own distribution, so the
    detector adapts automatically to smooth vs. lumpy demand shapes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def detect_suspicious_zeros(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    date_col: str,
    *,
    min_history_periods: int,
    min_zero_runs: int,
    z_threshold: float,
    skip_flag_cols: list[str] | None = None,
    eps: float = 1e-9,
) -> pd.DataFrame:
    """Return ``df`` with a new ``suspicious_zero`` bool column."""
    if min_history_periods < 1 or min_zero_runs < 1 or z_threshold <= 0:
        raise ValueError(
            "min_history_periods, min_zero_runs must be >= 1; z_threshold must be > 0"
        )

    out = df.sort_values(group_keys + [date_col]).reset_index(drop=True).copy()
    out["suspicious_zero"] = False

    skip_cols = [c for c in (skip_flag_cols or []) if c in out.columns]

    for _, idx in out.groupby(group_keys, sort=False).groups.items():
        pair = out.loc[idx]
        if len(pair) < min_history_periods:
            continue

        values = pair[target_col].to_numpy(dtype=float)
        is_zero = values <= eps

        # Run-length encoding: same run_id for consecutive equal-state rows.
        boundaries = np.r_[True, is_zero[1:] != is_zero[:-1]]
        run_id = pd.Series(np.cumsum(boundaries), index=pair.index)

        # Per-row length of the run the row belongs to.
        row_run_len = run_id.groupby(run_id).transform("size").to_numpy()

        # Historical distribution of zero-run lengths (one entry per distinct run).
        lengths_by_run = run_id.groupby(run_id).size()
        is_zero_run = pd.Series(is_zero, index=pair.index).groupby(run_id).any()
        zero_run_lengths = lengths_by_run[is_zero_run].to_numpy()

        if len(zero_run_lengths) < min_zero_runs:
            continue

        sd = float(zero_run_lengths.std(ddof=0))
        if sd == 0.0:
            continue

        threshold = float(zero_run_lengths.mean()) + z_threshold * sd
        flag = is_zero & (row_run_len > threshold)

        if skip_cols:
            flag &= ~pair[skip_cols].any(axis=1).to_numpy()

        if flag.any():
            out.loc[pair.index[flag], "suspicious_zero"] = True

    return out
