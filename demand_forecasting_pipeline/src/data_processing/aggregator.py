"""
Panel builder — aggregates raw transactions to period grain and fills gaps.

Causal columns can be aggregated with any pandas-compatible reduction
(``sum``, ``mean``, ``max``, ...) or ``vwap`` (volume-weighted average using
the target column as the weight). VWAP is the right default for price-like
columns because the arithmetic mean of per-row prices is biased toward
low-volume price points.

Meta columns use ``last`` so the panel reflects current taxonomy (an item
that was rebranded keeps its current name, not the oldest on record).
"""

from __future__ import annotations

import pandas as pd

from ..utils.io_utils import ensure_tuple
from ..utils.time_utils import period_alias

# Pandas reductions we pass through directly.
_SIMPLE_AGGS = {"sum", "mean", "max", "min", "median", "nunique", "first", "last"}
_VWAP = "vwap"


def _split_causal_aggs(causal_cols, available_cols):
    """Separate causal cols into simple-reduction and vwap buckets."""
    simple: dict[str, str] = {}
    vwap: list[str] = []
    for cc in causal_cols or []:
        col = cc["col"]
        if col not in available_cols:
            continue
        agg = cc.get("agg", "mean")
        if agg == _VWAP:
            vwap.append(col)
        elif agg in _SIMPLE_AGGS:
            simple[col] = agg
        else:
            raise ValueError(
                f"Unsupported aggregation '{agg}' for causal column '{col}'. "
                f"Allowed: {sorted(_SIMPLE_AGGS | {_VWAP})}"
            )
    return simple, vwap


def aggregate_to_period(df, group_keys, date_col, target_col, meta_cols, freq, causal_cols=None):
    df = df.copy()
    df["_period"] = df[date_col].dt.to_period(freq).dt.to_timestamp()

    simple_causal, vwap_causal = _split_causal_aggs(causal_cols, df.columns)
    agg_map: dict[str, str] = {target_col: "sum", **simple_causal}

    # VWAP needs sums of (col * target) and target; compute per-row helpers.
    for col in vwap_causal:
        df[f"_{col}_num"] = df[col] * df[target_col]
        agg_map[f"_{col}_num"] = "sum"

    grouped = df.groupby(group_keys + ["_period"], as_index=False).agg(agg_map)

    for col in vwap_causal:
        num = grouped[f"_{col}_num"]
        den = grouped[target_col].replace(0, pd.NA)
        grouped[col] = (num / den).astype(float)
        grouped = grouped.drop(columns=[f"_{col}_num"])

    grouped = grouped.rename(columns={"_period": date_col})

    if meta_cols:
        meta_present = [c for c in meta_cols if c in df.columns]
        if meta_present:
            meta = (
                df.sort_values(date_col)
                .groupby(group_keys, as_index=False)[meta_present]
                .last()
            )
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
