


def add_lag_features(df, group_keys, target_col, lags):
    df = df.copy()
    grp = df.groupby(group_keys)[target_col]
    for lag in lags:
        df["lag_{}".format(lag)] = grp.shift(lag)
    return df
