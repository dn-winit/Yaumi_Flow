"""
Holiday and custom-event features.

What gets emitted (all opt-in via config, nothing hardcoded):

  - ``hol_<slug>`` — one binary per public holiday returned by the
    ``holidays`` library.
  - ``hol_group_<name>`` — aggregate flag for every group declared in
    ``holidays.groups`` (user-configured; empty by default).
  - ``n_holidays_in_period`` — count per period (useful at >= weekly grain).
  - Proximity features (``days_to_next_holiday`` /
    ``days_since_last_holiday`` / ``is_pre_holiday_<N>d`` /
    ``is_post_holiday_<N>d``) when ``holidays.proximity.enabled`` is true.
  - ``evt_<name>`` — binary for flat custom events (``start``/``end``).
  - ``evt_<name>_<phase>`` + ``evt_<name>_any`` — per-phase binaries for
    phased custom events.

Nothing in this module decides what counts as a "religious" or "national"
holiday — the groups map lives in config.
"""

from __future__ import annotations

import re

import pandas as pd

from ._calendar import proximity_features

try:
    import holidays as _holidays_lib
    _HAS_HOLIDAYS = True
except ImportError:  # pragma: no cover — library is a declared dep
    _HAS_HOLIDAYS = False


_PARENS_RE = re.compile(r"\s*\([^)]*\)")
_HOLIDAY_SUFFIX_RE = re.compile(r"\s*holiday\s*$", re.IGNORECASE)
_APOSTROPHE_RE = re.compile(r"['’]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    s = _PARENS_RE.sub("", name or "")
    s = _HOLIDAY_SUFFIX_RE.sub("", s)
    s = _APOSTROPHE_RE.sub("", s)
    s = _SLUG_RE.sub("_", s.lower()).strip("_")
    return s


def _split_names(raw_name: str) -> list[str]:
    parts = [p.strip() for p in (raw_name or "").split(";") if p.strip()]
    return [_slug(p) for p in parts]


def get_holiday_calendar(
    country_code: str | None,
    years,
    subdivision: str | None = None,
) -> pd.DataFrame:
    """Return ``DataFrame[date, name]`` for all holidays in the year set."""
    if not _HAS_HOLIDAYS or not country_code:
        return pd.DataFrame(columns=["date", "name"])
    cal = _holidays_lib.country_holidays(
        country_code, subdiv=subdivision, years=list(years)
    )
    rows: list[dict] = []
    for d, name in cal.items():
        for n in _split_names(name):
            if n:
                rows.append({"date": pd.Timestamp(d).normalize(), "name": n})
    if not rows:
        return pd.DataFrame(columns=["date", "name"])
    return (
        pd.DataFrame(rows)
        .drop_duplicates()
        .sort_values("date")
        .reset_index(drop=True)
    )


def add_holiday_features(
    df: pd.DataFrame,
    date_col: str,
    granularity: str,
    cfg: dict,
    *,
    full_date_range=None,
) -> pd.DataFrame:
    """Emit holiday + custom-event + proximity features driven entirely by
    ``cfg``. Never mutates behaviour based on unconfigured knobs."""
    df = df.copy()
    if df.empty or not cfg:
        return df

    gran = (granularity or "D").strip().upper()[0]
    period_dates = (
        df[date_col].dt.normalize()
        if gran == "D"
        else df[date_col].dt.to_period(granularity).dt.to_timestamp()
    )

    # --- Years to fetch from the library ---
    if full_date_range is not None:
        years = sorted({pd.Timestamp(d).year for d in full_date_range})
    else:
        years = sorted(df[date_col].dt.year.unique().tolist())

    cal = get_holiday_calendar(cfg.get("country_code"), years, cfg.get("subdivision"))

    # --- Per-holiday binaries + group aggregates + count ---
    per_holiday_cols: dict[str, str] = {}
    if not cal.empty:
        cal_match = (
            cal["date"]
            if gran == "D"
            else cal["date"].dt.to_period(granularity).dt.to_timestamp()
        )
        cal = cal.assign(_match=cal_match)

        for name in sorted(cal["name"].unique()):
            col = f"hol_{name}"
            dates_with = set(cal.loc[cal["name"] == name, "_match"].tolist())
            df[col] = period_dates.isin(dates_with).astype(int)
            per_holiday_cols[name] = col

        # Groups (config-driven; empty by default)
        groups = cfg.get("groups") or {}
        for group_name, member_slugs in groups.items():
            group_col = f"hol_group_{_slug(group_name)}"
            member_cols = [
                per_holiday_cols[slug]
                for slug in (member_slugs or [])
                if slug in per_holiday_cols
            ]
            if not member_cols:
                df[group_col] = 0
            else:
                df[group_col] = df[member_cols].max(axis=1).astype(int)

        # Count per period
        count_by_date = cal.groupby("_match").size()
        df["n_holidays_in_period"] = (
            period_dates.map(count_by_date).fillna(0).astype(int)
        )
    else:
        df["n_holidays_in_period"] = 0

    # --- Proximity (daily grain only — concept doesn't map cleanly above) ---
    prox = cfg.get("proximity") or {}
    if prox.get("enabled", False) and gran == "D" and not cal.empty:
        prox_df = proximity_features(
            df[date_col],
            cal["date"].tolist(),
            prefix="holiday",
            pre_window=int(prox.get("pre_window_days", 0)),
            post_window=int(prox.get("post_window_days", 0)),
        )
        df = pd.concat([df, prox_df], axis=1)

    # --- Custom events (flat or phased) ---
    for ev in cfg.get("custom_events") or []:
        name = _slug(ev.get("name", "event"))
        phases = ev.get("phases") or []
        if phases:
            per_phase_masks = []
            for phase in phases:
                phase_slug = _slug(phase.get("name", ""))
                if not phase_slug:
                    continue
                col = f"evt_{name}_{phase_slug}"
                mask = _event_mask(period_dates, phase, granularity)
                df[col] = mask.astype(int)
                per_phase_masks.append(mask)
            if per_phase_masks:
                any_mask = per_phase_masks[0].copy()
                for m in per_phase_masks[1:]:
                    any_mask |= m
                df[f"evt_{name}_any"] = any_mask.astype(int)
        else:
            df[f"evt_{name}"] = _event_mask(period_dates, ev, granularity).astype(int)

    return df


def _event_mask(period_dates: pd.Series, ev: dict, granularity: str) -> pd.Series:
    """Build a boolean mask for a ``{start, end}`` event at the given grain."""
    start = pd.Timestamp(ev["start"]).normalize()
    end = pd.Timestamp(ev["end"]).normalize()
    gran = (granularity or "D").strip().upper()[0]
    if gran != "D":
        start = start.to_period(granularity).to_timestamp()
        end = end.to_period(granularity).to_timestamp()
    return (period_dates >= start) & (period_dates <= end)
