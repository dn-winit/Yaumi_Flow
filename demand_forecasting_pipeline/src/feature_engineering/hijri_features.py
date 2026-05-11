"""
Hijri-calendar derived features.

Ramadan is month 9 in the Islamic calendar - a 29/30 day window whose
Gregorian dates shift ~11 days per year. Gregorian-only holiday libraries
miss it entirely, so we compute it directly from the Islamic calendar.

Which features get emitted is entirely driven by ``cfg.features``. The
module never decides what is useful; it only knows how to compute each
primitive:

  - ``is_ramadan``            bool
  - ``ramadan_day``           int 1..30 inside Ramadan, else 0
  - ``is_last_10_days_ramadan`` bool
  - ``days_to_ramadan_start`` int   (requires proximity config)
  - ``days_since_ramadan_end`` int  (requires proximity config)

Enable only what the model should see.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ._calendar import is_daily, proximity_features

try:
    from hijridate import Gregorian, Hijri
    _HAS_HIJRI = True
except ImportError:  # pragma: no cover - declared as a dep
    _HAS_HIJRI = False


def _ramadan_ranges_for(panel_start: pd.Timestamp, panel_end: pd.Timestamp) -> list[tuple[date, date]]:
    """Return a sorted list of (start, end) Gregorian dates for every Ramadan
    that overlaps the panel range. Implemented by scanning Hijri years bracketing
    the panel; a small, closed set (<= 10 years in typical training windows)."""
    if not _HAS_HIJRI:
        return []

    start_hy = Gregorian(panel_start.year, panel_start.month, panel_start.day).to_hijri().year
    end_hy = Gregorian(panel_end.year, panel_end.month, panel_end.day).to_hijri().year

    ranges: list[tuple[date, date]] = []
    for hy in range(start_hy - 1, end_hy + 2):
        try:
            ram_start_g = Hijri(hy, 9, 1).to_gregorian()
            shawwal_start_g = Hijri(hy, 10, 1).to_gregorian()
        except (ValueError, OverflowError):
            continue
        ram_start = date(ram_start_g.year, ram_start_g.month, ram_start_g.day)
        ram_end = date(shawwal_start_g.year, shawwal_start_g.month, shawwal_start_g.day) - timedelta(days=1)
        ranges.append((ram_start, ram_end))
    ranges.sort()
    return ranges


def add_hijri_features(
    df: pd.DataFrame,
    date_col: str,
    granularity: str,
    cfg: dict,
) -> pd.DataFrame:
    """Emit Ramadan-related features according to ``cfg``."""
    df = df.copy()
    if df.empty or not cfg or not cfg.get("enabled", False):
        return df
    if not _HAS_HIJRI:
        return df

    features = cfg.get("features") or {}
    if not any(features.values()):
        return df

    dates = pd.to_datetime(df[date_col])
    ranges = _ramadan_ranges_for(dates.min(), dates.max())
    if not ranges:
        return df

    starts = np.array([np.datetime64(r[0]) for r in ranges])
    ends = np.array([np.datetime64(r[1]) for r in ranges])
    d64 = dates.to_numpy().astype("datetime64[D]")

    # Locate each row's candidate Ramadan via searchsorted on starts.
    idx = np.searchsorted(starts, d64, side="right") - 1
    in_range = (idx >= 0) & (d64 >= starts[np.clip(idx, 0, None)]) & (d64 <= ends[np.clip(idx, 0, None)])

    # Day within Ramadan (1-based) - only meaningful at daily grain.
    day_in = np.zeros(len(d64), dtype=int)
    valid = in_range
    day_in[valid] = (d64[valid] - starts[idx[valid]]).astype("timedelta64[D]").astype(int) + 1

    if features.get("is_ramadan"):
        df["is_ramadan"] = in_range.astype(int)
    if features.get("ramadan_day") and is_daily(granularity):
        df["ramadan_day"] = day_in
    if features.get("is_last_10_days_ramadan"):
        # Compute end-relative position; last 10 days = days 21..30 (inclusive).
        df["is_last_10_days_ramadan"] = ((day_in >= 21) & valid).astype(int)

    # Proximity (daily grain only)
    prox = cfg.get("proximity") or {}
    if prox.get("enabled", False) and is_daily(granularity):
        events = list(starts) + list(ends)
        prox_df = proximity_features(
            dates,
            events,
            prefix="ramadan",
            pre_window=int(prox.get("pre_window_days", 0)),
            post_window=int(prox.get("post_window_days", 0)),
        )
        df = pd.concat([df.reset_index(drop=True), prox_df.reset_index(drop=True)], axis=1)

    return df
