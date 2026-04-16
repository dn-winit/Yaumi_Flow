import pandas as pd


_ALIAS_MAP = {"M": "MS", "Q": "QS", "Y": "YS", "W": "W-MON", "D": "D"}


def period_alias(granularity):
    return _ALIAS_MAP.get(granularity, granularity)


def period_offset(granularity, n):
    n = int(n)
    if granularity == "M":
        return pd.DateOffset(months=n)
    if granularity == "Q":
        return pd.DateOffset(months=3 * n)
    if granularity == "Y":
        return pd.DateOffset(years=n)
    if granularity == "W":
        return pd.DateOffset(weeks=n)
    if granularity == "D":
        return pd.DateOffset(days=n)
    return pd.DateOffset(months=n)


def build_date_range(start, end, granularity):
    return pd.date_range(start, end, freq=period_alias(granularity))
