import pandas as pd


def load_raw(path, date_col):
    df = pd.read_csv(path, low_memory=False)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    return df
