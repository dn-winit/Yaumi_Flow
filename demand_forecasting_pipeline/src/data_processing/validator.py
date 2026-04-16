import pandas as pd


def validate_input(df, cfg):
    date_col = cfg["data"]["date_col"]
    target_col = cfg["data"]["target_col"]
    group_keys = cfg["data"]["forecast_level"]
    required = group_keys + [date_col, target_col]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError("Missing required columns: {}".format(missing))
    if not pd.api.types.is_numeric_dtype(df[target_col]):
        raise ValueError("Target column '{}' is not numeric".format(target_col))
    parsed = pd.to_datetime(df[date_col], errors="coerce")
    bad_dates = parsed.isna().sum()
    if bad_dates == len(df):
        raise ValueError("Date column '{}' could not be parsed".format(date_col))
    causal = cfg["data"].get("causal_cols", [])
    missing_causal = [c["col"] for c in causal if c["col"] not in df.columns]
    if missing_causal:
        raise ValueError("Missing causal columns: {}".format(missing_causal))
    return True
