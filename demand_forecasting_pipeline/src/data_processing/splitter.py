import pandas as pd

from ..utils.time_utils import period_offset


def time_based_split(df, date_col, test_horizon, val_horizon=0, granularity="D"):
    df = df.sort_values(date_col)
    max_date = df[date_col].max()
    test_start = max_date - period_offset(granularity, test_horizon - 1)
    val_start = test_start - period_offset(granularity, val_horizon) if val_horizon > 0 else test_start

    train = df[df[date_col] < val_start].copy()
    val = df[(df[date_col] >= val_start) & (df[date_col] < test_start)].copy() if val_horizon > 0 else df.iloc[0:0].copy()
    test = df[df[date_col] >= test_start].copy()
    return train, val, test
