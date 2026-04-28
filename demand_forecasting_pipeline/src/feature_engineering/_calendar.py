"""
Internal calendar helpers shared by the feature-engineering modules.

Everything here is pure, vectorized, and config-agnostic: the callers tell
these functions *what* events to measure proximity to — they don't know or
care which events those are.

Kept under a single underscore to signal "package-internal".
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Granularity gates
# ---------------------------------------------------------------------------


def _gran_letter(freq: str) -> str:
    """Return the first letter of a pandas freq string (``D``/``W``/``M``/...)."""
    return (freq or "D").strip().upper()[0]


def is_daily(freq: str) -> bool:
    return _gran_letter(freq) == "D"


def is_daily_or_weekly(freq: str) -> bool:
    return _gran_letter(freq) in ("D", "W")


def is_monthly_or_finer(freq: str) -> bool:
    return _gran_letter(freq) in ("D", "W", "M")


# ---------------------------------------------------------------------------
# Weekend resolver — honours date-dependent weekend regimes.
# ---------------------------------------------------------------------------


def resolve_weekend_mask(dates: pd.Series, spec) -> pd.Series:
    """Vectorized ``is_weekend`` computation supporting two config forms.

    Legacy (flat):   ``[4, 5]``  — these weekday ints are weekend at all times.
    Regimes:         ``[{from: "2022-01-01", days: [5, 6]},
                         {until: "2022-01-01", days: [4, 5]}]``

    Each regime entry can optionally set ``from`` and/or ``until`` (ISO date).
    Rules are applied in order; later rules overwrite earlier ones where their
    date bounds hit.
    """
    dates = pd.to_datetime(dates)
    dow = dates.dt.dayofweek.to_numpy()

    if not spec:
        return pd.Series(False, index=dates.index)

    # Legacy flat form: list of ints.
    if all(isinstance(x, int) for x in spec):
        weekend_set = set(int(d) for d in spec)
        return pd.Series(np.isin(dow, list(weekend_set)), index=dates.index)

    result = np.zeros(len(dates), dtype=bool)
    date_values = dates.to_numpy()
    for rule in spec:
        days = [int(d) for d in rule.get("days", [])]
        if not days:
            continue
        mask = np.ones(len(dates), dtype=bool)
        if "from" in rule and rule["from"] is not None:
            mask &= date_values >= np.datetime64(pd.Timestamp(rule["from"]))
        if "until" in rule and rule["until"] is not None:
            mask &= date_values < np.datetime64(pd.Timestamp(rule["until"]))
        result[mask] = np.isin(dow[mask], days)
    return pd.Series(result, index=dates.index)


# ---------------------------------------------------------------------------
# Event proximity — the one source of truth for days_to/days_since/windows.
# ---------------------------------------------------------------------------


def proximity_features(
    dates: pd.Series,
    event_dates: Iterable[pd.Timestamp],
    *,
    prefix: str,
    pre_window: int = 0,
    post_window: int = 0,
) -> pd.DataFrame:
    """For each row date, return distance-to-nearest-event features.

    Columns returned (only those that are meaningful — if ``event_dates`` is
    empty, an empty DataFrame with the row index is returned):

      - ``days_to_next_<prefix>``
      - ``days_since_last_<prefix>``
      - ``is_pre_<prefix>_<N>d``   (one col per unique N in ``pre_window``)
      - ``is_post_<prefix>_<N>d``  (one col per unique N in ``post_window``)

    The implementation is O(N log E) via ``np.searchsorted``. No Python loops
    over rows.
    """
    idx = dates.index
    if not len(dates):
        return pd.DataFrame(index=idx)

    events = pd.to_datetime(list(event_dates)).sort_values().unique()
    if len(events) == 0:
        return pd.DataFrame(index=idx)

    d64 = pd.to_datetime(dates).to_numpy().astype("datetime64[ns]")
    e64 = np.asarray(events, dtype="datetime64[ns]")

    # Next event: first event with date >= row date
    nxt = np.searchsorted(e64, d64, side="left")
    has_next = nxt < len(e64)
    days_to_next = np.full(len(d64), np.nan, dtype=float)
    days_to_next[has_next] = (
        (e64[nxt[has_next]] - d64[has_next]).astype("timedelta64[D]").astype(float)
    )

    # Last event: last event with date <= row date
    prv = np.searchsorted(e64, d64, side="right") - 1
    has_prev = prv >= 0
    days_since_last = np.full(len(d64), np.nan, dtype=float)
    days_since_last[has_prev] = (
        (d64[has_prev] - e64[prv[has_prev]]).astype("timedelta64[D]").astype(float)
    )

    out = pd.DataFrame(
        {
            f"days_to_next_{prefix}": days_to_next,
            f"days_since_last_{prefix}": days_since_last,
        },
        index=idx,
    )

    if pre_window and pre_window > 0:
        out[f"is_pre_{prefix}_{int(pre_window)}d"] = (
            (days_to_next >= 0) & (days_to_next <= pre_window)
        ).astype(int)
    if post_window and post_window > 0:
        out[f"is_post_{prefix}_{int(post_window)}d"] = (
            (days_since_last >= 0) & (days_since_last <= post_window)
        ).astype(int)

    return out


# ---------------------------------------------------------------------------
# Payday timeline generator (shared by salary_cycle).
# ---------------------------------------------------------------------------


def build_payday_timeline(
    pay_rules: list[dict],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DatetimeIndex:
    """Expand pay rules (``day_of_month``) into explicit dates in [start, end].

    Days after month-end are clamped to month-end so ``day_of_month: 31`` in
    February becomes the 28th / 29th.
    """
    if not pay_rules:
        return pd.DatetimeIndex([])

    out: list[pd.Timestamp] = []
    for month_start in pd.date_range(start.normalize().replace(day=1), end, freq="MS"):
        n_days = month_start.days_in_month
        for rule in pay_rules:
            dom = int(rule.get("day_of_month", 0))
            if dom < 1:
                continue
            day = min(dom, n_days)
            pay = month_start + pd.Timedelta(days=day - 1)
            if start <= pay <= end:
                out.append(pay)
    return pd.DatetimeIndex(sorted(set(out)))
