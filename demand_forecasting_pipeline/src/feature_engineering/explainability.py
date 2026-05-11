"""
Per-pair descriptive statistics - feeds ``pair_explainability.csv`` for the
UI, drift checks, and routing signals.

Explainability never re-derives ADI / CV^2 / class: those are already
computed by :func:`classify_dataset` and are supplied via ``classes_df``.
This keeps the per-pair loop single-purpose.

Column surface (single source of truth for downstream consumers):

    n_periods              total periods in the pair's train history
    n_nonzero_periods      periods with target > 0
    nonzero_ratio          n_nonzero_periods / n_periods
    mean_qty               arithmetic mean over all periods (includes zeros)
    sum_qty                total observed demand
    min_qty                smallest single-period demand (incl. zeros)
    max_qty                largest single-period demand
    range_qty              max_qty - min_qty - the OBSERVED historical range
                           of demand. Directly comparable to the q_90 - q_10
                           interval width: when the band looks "wide", this
                           number explains why (the data itself is wide).
    std_qty                std over all periods (includes zeros)
    nonzero_mean           mean demand on days with demand
    nonzero_median         median demand on days with demand
    nonzero_min            smallest positive demand ever seen
    nonzero_std            std of demand on days with demand
    avg_gap_days           average gap between successive demand days

``min_qty`` is included because, even though it's almost always 0 for
intermittent / lumpy pairs (the filler inserts zeros for missing periods),
that 0 is itself the explanation a planner needs when asking "why is the
lower bound q_10 also 0?". For dense smooth pairs that never sell zero,
``min_qty`` is positive and informative. ``nonzero_min`` /
``nonzero_median`` complement it with shape-of-distribution detail.

``avg_gap_days`` is granularity-correct: computed from positional gaps
between non-zero rows, then converted to days via
``_DAYS_PER_PERIOD[granularity]``. Downstream DB columns named
``avg_gap_days`` now match their units.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


_STATS_COLUMNS = (
    "n_periods",
    "n_nonzero_periods",
    "nonzero_ratio",
    "mean_qty",
    "sum_qty",
    "min_qty",
    "max_qty",
    "range_qty",
    "std_qty",
    "nonzero_mean",
    "nonzero_median",
    "nonzero_min",
    "nonzero_std",
    "avg_gap_days",
    # Recent-window slice (last N periods of training history per pair).
    # Lets a planner ground the band against *recent* reality without
    # losing the lifetime context.
    "recent_window_periods",
    "recent_n_periods",
    "recent_min_qty",
    "recent_max_qty",
    "recent_range_qty",
)

# Gregorian-average days per aggregation period. Months and quarters use
# calendar averages; downstream consumers read "avg_gap_days" and expect a
# day count regardless of how the panel was aggregated.
_DAYS_PER_PERIOD: dict[str, float] = {
    "D": 1.0,
    "W": 7.0,
    "M": 30.4375,
    "Q": 91.3125,
    "Y": 365.25,
}


def compute_pair_explainability(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    classes_df: pd.DataFrame,
    *,
    date_col: str,
    granularity: str,
    recent_window_periods: int,
) -> pd.DataFrame:
    """Descriptive per-pair stats + class assignment.

    ``classes_df`` is the output of :func:`classify_dataset` (indexed by
    ``group_keys``, with ``adi`` / ``cv2`` / ``class`` columns). We do NOT
    recompute those columns here - they come pre-computed in one place.

    ``recent_window_periods`` controls a second slice: each pair's last N
    *periods* of training history (sorted by ``date_col``) feeds the
    ``recent_min_qty`` / ``recent_max_qty`` / ``recent_range_qty`` columns.
    Useful for grounding band-width conversations in recent demand instead
    of multi-year extremes. Periods, not days, so the window adapts to the
    configured granularity.
    """
    days_factor = _DAYS_PER_PERIOD.get((granularity or "D").strip().upper()[0], 1.0)
    recent_n = max(1, int(recent_window_periods))

    rows: list[dict] = []
    for keys, g in df.groupby(group_keys):
        # Sort by date so ``tail`` actually grabs the latest periods,
        # regardless of the input order.
        g_sorted = g.sort_values(date_col)
        s = g_sorted[target_col].astype(float)
        nz = s[s > 0]
        avg_gap_periods = _avg_gap_periods(s.values)

        min_q = float(s.min()) if len(s) else 0.0
        max_q = float(s.max()) if len(s) else 0.0

        # Recent-window slice - the customer's "what we've seen lately" view.
        s_recent = s.tail(recent_n)
        recent_min = float(s_recent.min()) if len(s_recent) else 0.0
        recent_max = float(s_recent.max()) if len(s_recent) else 0.0

        record = {
            "n_periods": int(len(s)),
            "n_nonzero_periods": int((s > 0).sum()),
            "nonzero_ratio": float((s > 0).mean()) if len(s) else 0.0,
            "mean_qty": float(s.mean()) if len(s) else 0.0,
            "sum_qty": float(s.sum()),
            "min_qty": min_q,
            "max_qty": max_q,
            "range_qty": max_q - min_q,
            "std_qty": float(s.std(ddof=0)) if len(s) else 0.0,
            "nonzero_mean": float(nz.mean()) if len(nz) else 0.0,
            "nonzero_median": float(nz.median()) if len(nz) else 0.0,
            "nonzero_min": float(nz.min()) if len(nz) else 0.0,
            "nonzero_std": float(nz.std(ddof=0)) if len(nz) else 0.0,
            "avg_gap_days": (
                round(float(avg_gap_periods * days_factor), 2)
                if avg_gap_periods is not None else None
            ),
            "recent_window_periods": recent_n,
            "recent_n_periods": int(len(s_recent)),
            "recent_min_qty": recent_min,
            "recent_max_qty": recent_max,
            "recent_range_qty": recent_max - recent_min,
        }
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        for k, v in zip(group_keys, key_tuple):
            record[k] = v
        rows.append(record)

    if not rows:
        return pd.DataFrame(columns=list(group_keys) + list(_STATS_COLUMNS) + ["adi", "cv2", "class"])

    stats_df = pd.DataFrame(rows)[list(group_keys) + list(_STATS_COLUMNS)]
    classes_reset = classes_df.reset_index()[list(group_keys) + ["adi", "cv2", "class"]]
    return stats_df.merge(classes_reset, on=group_keys, how="left")


def _avg_gap_periods(arr: np.ndarray) -> float | None:
    """Average number of PERIODS between successive non-zero observations.
    Callers convert to days via :data:`_DAYS_PER_PERIOD`."""
    idx = np.where(arr > 0)[0]
    if idx.size < 2:
        return None
    return float(np.diff(idx).mean())
