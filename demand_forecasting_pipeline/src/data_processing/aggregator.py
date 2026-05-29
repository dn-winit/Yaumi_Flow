"""
Panel builder - aggregates raw transactions to period grain and fills gaps.

Causal columns can be aggregated with any pandas-compatible reduction
(``sum``, ``mean``, ``max``, ...) or ``vwap`` (volume-weighted average using
the target column as the weight). VWAP is the right default for price-like
columns because the arithmetic mean of per-row prices is biased toward
low-volume price points.

Meta columns use ``last`` so the panel reflects current taxonomy (an item
that was rebranded keeps its current name, not the oldest on record).
"""

from __future__ import annotations

import numpy as np
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
        # Replace zero target with ``np.nan`` (not ``pd.NA``) so the
        # divisor stays float64. ``pd.NA`` upcasts the column to
        # ``object`` dtype, and the downstream ``.astype(float)`` cast
        # then fails with ``TypeError: float() argument must be a
        # string or a real number, not 'NAType'`` -- a hard crash the
        # synthetic smoke tests didn't exercise because they don't use
        # vwap. ``np.nan`` propagates through division to NaN, which is
        # exactly the "undefined VWAP for zero-volume group" semantic
        # the float cast can handle.
        den = grouped[target_col].replace(0, np.nan)
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
                         add_activity_flag=False, vwap_causal_cols=None):
    """Reindex each pair onto a regular period grid; fill the target with
    ``fill_value`` for the inserted rows.

    VWAP causal columns get forward-then-backward filled per pair so a
    zero-sale day carries the last-known price instead of NaN. The
    "last observed price still in effect on a no-sale day" reading is
    the only one that doesn't distort downstream features: leaving NaN
    forced the feature builder's last-resort ``fillna(0)`` to write
    ``price = 0`` into the model (the "free product" signal), which
    pulled every regression with a price interaction toward zero. The
    aggregator already produces VWAP NaN on zero-volume rows; this
    fills in the propagated value cleanly. Pairs that have ZERO price
    observations stay NaN here and rely on the builder's tail-end
    fillna -- but that case is now isolated to pairs with no price
    history at all, not every zero-sale day of every pair.

    Simple-reduction causal columns (``mean``, ``sum``, ``max`` ...)
    are NOT touched -- those aggregations already produce meaningful
    values on zero-volume rows (mean of zero rows is NaN by reduction,
    but the caller's choice of agg determines the right fill).

    Meta columns left over from the source frame stay NaN by
    construction (only target + VWAP causals are filled); callers that
    need meta re-merge it from a per-pair lookup in ``build_panel``.
    """
    # Empty input -> return an empty, schema-stable frame instead of
    # falling into ``pd.concat([])`` (which raises ValueError). Callers
    # that legitimately produce a no-data window (a brand-new route, an
    # all-zero filter, a class with zero pairs) get back a frame they
    # can keep merging into without a special-case branch.
    if df.empty:
        cols = list(group_keys) + [date_col, target_col]
        if add_activity_flag:
            cols.append("activity_flag")
        return pd.DataFrame(columns=cols)

    vwap_cols = [c for c in (vwap_causal_cols or []) if c in df.columns]

    out = []
    alias = period_alias(freq)
    for keys, g in df.groupby(group_keys):
        g = g.sort_values(date_col)
        idx = pd.date_range(g[date_col].min(), g[date_col].max(), freq=alias)
        g = g.set_index(date_col).reindex(idx)
        g.index.name = date_col
        if add_activity_flag:
            g["activity_flag"] = g[target_col].notna().astype(int)
        for k, v in zip(group_keys, ensure_tuple(keys), strict=False):
            g[k] = v
        g[target_col] = g[target_col].fillna(fill_value)
        # ffill carries the last-known price forward; bfill covers the
        # rare case where the very first reindexed rows precede the
        # first observation (only possible if upstream code passed a
        # pre-extended range). For pairs that simply have no VWAP
        # observation ever, both fills no-op and the value stays NaN
        # -- the builder's tail-end nan_fill catches that residual.
        for col in vwap_cols:
            g[col] = g[col].ffill().bfill()
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True)


def build_panel(raw, group_keys, date_col, target_col, meta_cols, freq,
                fill_missing=True, fill_value=0.0, causal_cols=None, activity_flag=False):
    agg = aggregate_to_period(raw, group_keys, date_col, target_col, meta_cols, freq, causal_cols)
    if not fill_missing:
        if activity_flag:
            agg["activity_flag"] = 1
        return agg

    # Pick out VWAP causal columns so the reindex step can forward-fill
    # them per pair (so zero-sale dates carry the last-known price
    # instead of NaN). Pulled from ``causal_cols`` config -- never
    # hardcoded -- so adding a new ``agg: vwap`` column to config.yaml
    # automatically gets the right fill treatment without code changes.
    vwap_causal = [
        cc["col"] for cc in (causal_cols or [])
        if cc.get("agg") == _VWAP and cc["col"] in agg.columns
    ]

    keep_cols = group_keys + [date_col, target_col]
    causal_names = [cc["col"] for cc in (causal_cols or []) if cc["col"] in agg.columns]
    keep_cols += causal_names
    filled = fill_missing_periods(
        agg[keep_cols], group_keys, date_col, target_col, freq,
        fill_value=fill_value, add_activity_flag=activity_flag,
        vwap_causal_cols=vwap_causal,
    )
    if meta_cols:
        meta_present = [c for c in meta_cols if c in agg.columns]
        if meta_present:
            meta = agg.groupby(group_keys, as_index=False)[meta_present].first()
            filled = filled.merge(meta, on=group_keys, how="left")
    return filled
