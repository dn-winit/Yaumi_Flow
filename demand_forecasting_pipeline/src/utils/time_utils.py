"""
Granularity-aware time helpers.

``period_alias`` maps the short granularity codes used across config
(``D``/``W``/``M``/``Q``/``Y``) to the pandas offset aliases used by
``pd.date_range`` / ``pd.Period``. ``period_offset`` produces a
``DateOffset`` for arithmetic. Unknown granularities raise loud —
silently falling back would hide config typos.
"""

from __future__ import annotations

import pandas as pd

_ALIAS_MAP: dict[str, str] = {
    "D": "D",
    "W": "W-MON",
    "M": "MS",
    "Q": "QS",
    "Y": "YS",
}


def _normalise(granularity: str) -> str:
    if not granularity:
        raise ValueError("granularity is required (got empty)")
    return granularity.strip().upper()[0]


def period_alias(granularity: str) -> str:
    """Pandas frequency alias for a granularity code."""
    key = _normalise(granularity)
    if key not in _ALIAS_MAP:
        raise ValueError(
            f"Unknown granularity {granularity!r}; allowed: {sorted(_ALIAS_MAP)}"
        )
    return _ALIAS_MAP[key]


def period_offset(granularity: str, n: int) -> pd.DateOffset:
    """``n`` periods as a ``pd.DateOffset`` at the given granularity."""
    key = _normalise(granularity)
    n = int(n)
    if key == "D":
        return pd.DateOffset(days=n)
    if key == "W":
        return pd.DateOffset(weeks=n)
    if key == "M":
        return pd.DateOffset(months=n)
    if key == "Q":
        return pd.DateOffset(months=3 * n)
    if key == "Y":
        return pd.DateOffset(years=n)
    raise ValueError(
        f"Unknown granularity {granularity!r}; allowed: {sorted(_ALIAS_MAP)}"
    )


def build_date_range(start, end, granularity: str) -> pd.DatetimeIndex:
    """Inclusive date range at the given granularity."""
    return pd.date_range(start, end, freq=period_alias(granularity))
