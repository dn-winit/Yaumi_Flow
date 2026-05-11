"""
Per-pair lifecycle flags derived from sales history alone.

Two flags are produced, both granularity-agnostic (thresholds are in panel
rows, not days):

``is_new_launch``
    True for the first ``new_launch_periods`` rows of a pair starting at its
    first non-zero sale. Captures the ramp-up phase where demand is atypical
    (distributor load-ins, promotional push, onboarding noise).

``is_likely_eol``
    True for rows that fall strictly after the pair's last non-zero sale,
    but only if the silent tail is already longer than
    ``eol_silent_periods``. Prevents fresh single-period gaps from being
    misread as end-of-life.

Both flags are designed to be dependable **signals** that downstream stages
(outlier treatment, loss masking, reporting) can consult. They are not
features - the training pipeline keeps them out of the feature matrix so
there is no risk of temporal leakage.
"""

from __future__ import annotations

import pandas as pd


def assign_lifecycle_flags(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    date_col: str,
    *,
    new_launch_periods: int,
    eol_silent_periods: int,
    eps: float = 1e-9,
) -> pd.DataFrame:
    """Return ``df`` with ``is_new_launch`` and ``is_likely_eol`` bool columns."""
    if new_launch_periods < 0 or eol_silent_periods < 0:
        raise ValueError("new_launch_periods and eol_silent_periods must be >= 0")

    out = df.sort_values(group_keys + [date_col]).reset_index(drop=True).copy()
    out["is_new_launch"] = False
    out["is_likely_eol"] = False

    for _, idx in out.groupby(group_keys, sort=False).groups.items():
        pair = out.loc[idx]
        nonzero = pair[pair[target_col] > eps]
        if nonzero.empty:
            continue

        first_sale = nonzero[date_col].min()
        last_sale = nonzero[date_col].max()

        if new_launch_periods > 0:
            launch = pair[pair[date_col] >= first_sale].head(new_launch_periods)
            out.loc[launch.index, "is_new_launch"] = True

        silent_tail = pair[pair[date_col] > last_sale]
        if len(silent_tail) >= eol_silent_periods and eol_silent_periods > 0:
            out.loc[silent_tail.index, "is_likely_eol"] = True

    return out
