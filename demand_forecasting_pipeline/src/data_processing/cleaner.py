
import pandas as pd


def _iqr_bounds(s, mult):
    q1 = s.quantile(0.25)
    q3 = s.quantile(0.75)
    iqr = q3 - q1
    return q1 - mult * iqr, q3 + mult * iqr


def per_pair_outlier_treatment(df, group_keys, target_col, cfg, classes=None, source_df=None):
    """Clip per pair using IQR bounds. Bounds come from source_df (train window) if
    provided, otherwise from df itself. This keeps test data out of bound estimation."""
    if not cfg.get("enabled", False):
        return df
    method = cfg.get("method", "iqr")
    mult = float(cfg.get("iqr_multiplier", 3.0))
    skip_intermittent = bool(cfg.get("skip_if_intermittent", True))

    bounds_src = source_df if source_df is not None else df
    bounds = {}
    for keys, g in bounds_src.groupby(group_keys):
        s = g[target_col].astype(float)
        if method == "iqr":
            lo, hi = _iqr_bounds(s, mult)
            bounds[keys] = (max(lo, 0.0), hi)

    out_parts = []
    for keys, g in df.groupby(group_keys):
        g = g.copy()
        cls = None
        if classes is not None:
            try:
                cls = classes.loc[keys, "class"] if isinstance(keys, tuple) else classes.loc[(keys,), "class"]
            except Exception:
                cls = None
        if skip_intermittent and cls in ("intermittent", "lumpy"):
            out_parts.append(g)
            continue
        if keys in bounds:
            lo, hi = bounds[keys]
            g[target_col] = g[target_col].astype(float).clip(lower=lo, upper=hi)
        out_parts.append(g)
    return pd.concat(out_parts, ignore_index=True)


def clip_negative_to_zero(df, target_col):
    df = df.copy()
    df[target_col] = df[target_col].fillna(0.0)
    df.loc[df[target_col] < 0, target_col] = 0.0
    return df
