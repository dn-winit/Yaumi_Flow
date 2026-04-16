import numpy as np


def add_temporal_features(df, date_col, components, group_keys=None, granularity="D", weekend_days=None):
    df = df.copy()
    d = df[date_col]

    if "day_of_week" in components:
        df["day_of_week"] = d.dt.dayofweek
    if "day_of_week_sin" in components:
        df["day_of_week_sin"] = np.sin(2 * np.pi * d.dt.dayofweek / 7.0)
    if "day_of_week_cos" in components:
        df["day_of_week_cos"] = np.cos(2 * np.pi * d.dt.dayofweek / 7.0)
    if "day_of_month" in components:
        df["day_of_month"] = d.dt.day
    if "week_of_year" in components:
        df["week_of_year"] = d.dt.isocalendar().week.astype(int)
    if "is_weekend" in components and weekend_days:
        df["is_weekend"] = d.dt.dayofweek.isin(weekend_days).astype(int)
    if "month" in components:
        df["month"] = d.dt.month
    if "month_sin" in components:
        df["month_sin"] = np.sin(2 * np.pi * d.dt.month / 12.0)
    if "month_cos" in components:
        df["month_cos"] = np.cos(2 * np.pi * d.dt.month / 12.0)
    if "quarter" in components:
        df["quarter"] = d.dt.quarter
    if "quarter_sin" in components:
        df["quarter_sin"] = np.sin(2 * np.pi * d.dt.quarter / 4.0)
    if "quarter_cos" in components:
        df["quarter_cos"] = np.cos(2 * np.pi * d.dt.quarter / 4.0)
    if "year" in components:
        df["year"] = d.dt.year
    if "days_since_start" in components and group_keys:
        df["days_since_start"] = df.groupby(group_keys)[date_col].transform(
            lambda s: (s - s.min()).dt.days.astype(int)
        )
    return df
