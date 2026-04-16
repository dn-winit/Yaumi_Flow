import numpy as np
import pandas as pd


def _nonzero_ratio(x):
    if len(x) == 0:
        return 0.0
    return float((x > 0).sum()) / float(len(x))


_STAT_FUNCS = {
    "mean": "mean",
    "std": "std",
    "min": "min",
    "max": "max",
    "median": "median",
    "sum": "sum",
}


def add_rolling_features(df, group_keys, target_col, windows, stats):
    df = df.copy()
    grp = df.groupby(group_keys)[target_col]
    shifted = grp.shift(1)
    for w in windows:
        roll = shifted.groupby([df[k] for k in group_keys]).rolling(window=w, min_periods=1)
        for stat in stats:
            col = "roll_{}_{}".format(stat, w)
            if stat in _STAT_FUNCS:
                vals = getattr(roll, _STAT_FUNCS[stat])().reset_index(level=list(range(len(group_keys))), drop=True)
            elif stat == "nonzero_ratio":
                vals = roll.apply(_nonzero_ratio, raw=True).reset_index(level=list(range(len(group_keys))), drop=True)
            else:
                continue
            df[col] = vals.values
    return df


def add_intermittent_features(df, group_keys, target_col, cfg):
    df = df.copy()
    if cfg.get("nonzero_mean", False):
        df["nonzero_mean_to_date"] = (
            df.groupby(group_keys)[target_col]
            .apply(lambda s: s.shift(1).expanding().apply(lambda x: x[x > 0].mean() if (x > 0).any() else 0.0, raw=True))
            .reset_index(level=list(range(len(group_keys))), drop=True)
            .values
        )
    if cfg.get("inter_demand_interval", False):
        df["inter_demand_interval"] = (
            df.groupby(group_keys)[target_col]
            .apply(lambda s: s.shift(1).expanding().apply(_adi_expanding, raw=True))
            .reset_index(level=list(range(len(group_keys))), drop=True)
            .values
        )
    if cfg.get("last_nonzero_gap", False):
        df["last_nonzero_gap"] = (
            df.groupby(group_keys)[target_col]
            .apply(lambda s: _gap_since_last_nonzero(s.shift(1)))
            .reset_index(level=list(range(len(group_keys))), drop=True)
            .values
        )
    return df


def _adi_expanding(x):
    if len(x) == 0:
        return np.nan
    nz = (x > 0).sum()
    if nz == 0:
        return float(len(x))
    return float(len(x)) / float(nz)


def _gap_since_last_nonzero(s):
    out = []
    gap = 0
    for v in s.values:
        if pd.isna(v):
            out.append(np.nan)
            continue
        if v > 0:
            gap = 0
        else:
            gap += 1
        out.append(gap)
    return pd.Series(out, index=s.index)
