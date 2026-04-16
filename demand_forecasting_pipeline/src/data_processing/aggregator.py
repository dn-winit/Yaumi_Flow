import pandas as pd


from ..utils.time_utils import period_alias
from ..utils.io_utils import ensure_tuple


_AGG_MAP = {"sum": "sum", "mean": "mean", "max": "max", "min": "min", "median": "median", "nunique": "nunique"}


def aggregate_to_period(df, group_keys, date_col, target_col, meta_cols, freq, causal_cols=None):
    df = df.copy()
    df["_period"] = df[date_col].dt.to_period(freq).dt.to_timestamp()
    agg_dict = {target_col: "sum"}
    for cc in causal_cols or []:
        col = cc["col"]
        agg = cc.get("agg", "mean")
        if col in df.columns:
            agg_dict[col] = _AGG_MAP.get(agg, agg)
    grouped = df.groupby(group_keys + ["_period"], as_index=False).agg(agg_dict)
    grouped = grouped.rename(columns={"_period": date_col})
    if meta_cols:
        meta_present = [c for c in meta_cols if c in df.columns]
        if meta_present:
            meta = df.groupby(group_keys, as_index=False)[meta_present].first()
            grouped = grouped.merge(meta, on=group_keys, how="left")
    return grouped


def fill_missing_periods(df, group_keys, date_col, target_col, freq, fill_value=0.0,
                         add_activity_flag=False, keep_nan_cols=None):
    out = []
    alias = period_alias(freq)
    keep_nan = set(keep_nan_cols or [])
    for keys, g in df.groupby(group_keys):
        g = g.sort_values(date_col)
        idx = pd.date_range(g[date_col].min(), g[date_col].max(), freq=alias)
        g = g.set_index(date_col).reindex(idx)
        g.index.name = date_col
        if add_activity_flag:
            g["activity_flag"] = g[target_col].notna().astype(int)
        for k, v in zip(group_keys, ensure_tuple(keys)):
            g[k] = v
        g[target_col] = g[target_col].fillna(fill_value)
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True)


def build_panel(raw, group_keys, date_col, target_col, meta_cols, freq,
                fill_missing=True, fill_value=0.0, causal_cols=None, activity_flag=False):
    agg = aggregate_to_period(raw, group_keys, date_col, target_col, meta_cols, freq, causal_cols)
    if not fill_missing:
        if activity_flag:
            agg["activity_flag"] = 1
        return agg
    keep_cols = group_keys + [date_col, target_col]
    causal_names = [cc["col"] for cc in (causal_cols or []) if cc["col"] in agg.columns]
    keep_cols += causal_names
    filled = fill_missing_periods(
        agg[keep_cols], group_keys, date_col, target_col, freq,
        fill_value=fill_value, add_activity_flag=activity_flag,
        keep_nan_cols=causal_names,
    )
    if meta_cols:
        meta_present = [c for c in meta_cols if c in agg.columns]
        if meta_present:
            meta = agg.groupby(group_keys, as_index=False)[meta_present].first()
            filled = filled.merge(meta, on=group_keys, how="left")
    return filled
