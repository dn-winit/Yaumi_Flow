import re
import pandas as pd

try:
    import holidays as _holidays_lib
    _HAS_HOLIDAYS = True
except Exception:
    _HAS_HOLIDAYS = False


_PARENS_RE = re.compile(r"\s*\([^)]*\)")
_HOLIDAY_SUFFIX_RE = re.compile(r"\s*holiday\s*$", re.IGNORECASE)
_APOSTROPHE_RE = re.compile(r"['\u2019]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(name):
    s = _PARENS_RE.sub("", name or "")
    s = _HOLIDAY_SUFFIX_RE.sub("", s)
    s = _APOSTROPHE_RE.sub("", s)
    s = _SLUG_RE.sub("_", s.lower()).strip("_")
    return s


def _split_names(raw_name):
    parts = [p.strip() for p in (raw_name or "").split(";") if p.strip()]
    return [_normalize_name(p) for p in parts]


def get_holiday_calendar(country_code, years, subdivision=None):
    if not _HAS_HOLIDAYS or not country_code:
        return pd.DataFrame(columns=["date", "name"])
    cal = _holidays_lib.country_holidays(country_code, subdiv=subdivision, years=list(years))
    rows = []
    for d, name in cal.items():
        for n in _split_names(name):
            if n:
                rows.append({"date": pd.Timestamp(d).normalize(), "name": n})
    if not rows:
        return pd.DataFrame(columns=["date", "name"])
    return pd.DataFrame(rows).drop_duplicates().sort_values("date").reset_index(drop=True)


def add_holiday_features(df, date_col, country_code, subdivision, custom_events,
                        granularity, full_date_range=None):
    df = df.copy()
    if df.empty:
        return df

    if granularity == "D":
        df_dates = df[date_col].dt.normalize()
    else:
        df_dates = df[date_col].dt.to_period(granularity).dt.to_timestamp()

    if full_date_range is not None:
        years = sorted({d.year for d in full_date_range})
    else:
        years = sorted(df[date_col].dt.year.unique().tolist())

    cal = get_holiday_calendar(country_code, years, subdivision)
    if not cal.empty:
        if granularity == "D":
            cal_dates = cal["date"]
        else:
            cal_dates = cal["date"].dt.to_period(granularity).dt.to_timestamp()
            cal = cal.copy()
        cal["_match_date"] = cal_dates

        for name in sorted(cal["name"].unique()):
            dates_with = set(cal.loc[cal["name"] == name, "_match_date"].tolist())
            df["hol_" + name] = df_dates.isin(dates_with).astype(int)

        date_counts = cal.groupby("_match_date").size()
        df["n_holidays_in_period"] = df_dates.map(date_counts).fillna(0).astype(int)
    else:
        df["n_holidays_in_period"] = 0

    for ev in custom_events or []:
        ev_name = _normalize_name(ev.get("name", "event"))
        start = pd.Timestamp(ev["start"]).normalize()
        end = pd.Timestamp(ev["end"]).normalize()
        if granularity != "D":
            start = start.to_period(granularity).to_timestamp()
            end = end.to_period(granularity).to_timestamp()
        df["evt_" + ev_name] = ((df_dates >= start) & (df_dates <= end)).astype(int)

    return df
