"""
Salary-cycle features.

Pay dates are fully config-driven - the module never assumes a cycle. A
user with no pay rules configured gets no features. Everything below is
vectorized via ``searchsorted`` on a pre-computed timeline of pay dates.

Config shape::

    salary_cycle:
      enabled: true
      pay_rules:
        - {day_of_month: 1,  window_days: 5}
        - {day_of_month: 27, window_days: 3}
      features:
        days_since_last_payday: true
        days_to_next_payday: true
        in_payday_window: true   # honours each rule's window_days

Emitted columns (only those whose ``features`` flag is True):

  - ``days_since_last_payday``
  - ``days_to_next_payday``
  - ``in_payday_window``  (any rule's window - union)

Daily grain only. At coarser grains the concept collapses because every
period contains at least one payday.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._calendar import build_payday_timeline, is_daily


def add_salary_cycle_features(
    df: pd.DataFrame,
    date_col: str,
    granularity: str,
    cfg: dict,
) -> pd.DataFrame:
    """Emit salary-cycle features driven entirely by ``cfg``."""
    df = df.copy()
    if df.empty or not cfg or not cfg.get("enabled", False) or not is_daily(granularity):
        return df

    rules = cfg.get("pay_rules") or []
    features = cfg.get("features") or {}
    if not rules or not any(features.values()):
        return df

    dates = pd.to_datetime(df[date_col])
    # Extend the timeline a month either side so edge rows have a
    # previous / next payday even at the panel boundary.
    span_start = dates.min() - pd.DateOffset(months=1)
    span_end = dates.max() + pd.DateOffset(months=1)
    timeline = build_payday_timeline(rules, span_start, span_end)
    if len(timeline) == 0:
        return df

    t64 = timeline.to_numpy().astype("datetime64[D]")
    d64 = dates.to_numpy().astype("datetime64[D]")

    if features.get("days_since_last_payday"):
        prev = np.searchsorted(t64, d64, side="right") - 1
        has_prev = prev >= 0
        out = np.full(len(d64), np.nan, dtype=float)
        out[has_prev] = (d64[has_prev] - t64[prev[has_prev]]).astype("timedelta64[D]").astype(float)
        df["days_since_last_payday"] = out

    if features.get("days_to_next_payday"):
        nxt = np.searchsorted(t64, d64, side="left")
        has_next = nxt < len(t64)
        out = np.full(len(d64), np.nan, dtype=float)
        out[has_next] = (t64[nxt[has_next]] - d64[has_next]).astype("timedelta64[D]").astype(float)
        df["days_to_next_payday"] = out

    if features.get("in_payday_window"):
        # Each rule's own window - union across rules.
        in_win = np.zeros(len(d64), dtype=bool)
        for rule in rules:
            win = int(rule.get("window_days", 0))
            if win <= 0:
                continue
            # Build per-rule timeline, compute days_since, flag rows in [0, win].
            rule_timeline = build_payday_timeline([rule], span_start, span_end).to_numpy().astype("datetime64[D]")
            if len(rule_timeline) == 0:
                continue
            prev = np.searchsorted(rule_timeline, d64, side="right") - 1
            has_prev = prev >= 0
            delta = np.full(len(d64), np.inf, dtype=float)
            delta[has_prev] = (d64[has_prev] - rule_timeline[prev[has_prev]]).astype("timedelta64[D]").astype(float)
            in_win |= (delta >= 0) & (delta <= win)
        df["in_payday_window"] = in_win.astype(int)

    return df
