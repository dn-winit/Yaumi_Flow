"""
Per-pair outlier treatment + negative clipping.

Split into three functions so training (fit + apply) and inference (load +
apply) share one code path:

  :func:`fit_outlier_bounds`   - compute per-pair IQR bounds from a source
                                  (usually the train window). Training only.
  :func:`apply_outlier_bounds` - clip a panel using pre-fit bounds. Used at
                                  both training (immediately after fit) and
                                  inference (with bounds loaded from disk).
  :func:`per_pair_outlier_treatment` - thin wrapper that fits + applies.
                                  Preserved as the training call site's one-
                                  liner.

Robustness:
  - ``skip_if_intermittent`` keeps intermittent/lumpy series untouched.
  - ``min_nonzero_count`` guards against pathological IQR collapse.
  - ``skip_if_flagged`` preserves rows carrying any of the listed flag
    columns (lifecycle / anomaly) so the cleaner never overrides signals
    earlier stages produced.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_BOUND_LO = "bound_lo"
_BOUND_HI = "bound_hi"


def _iqr_bounds(series: pd.Series, mult: float) -> tuple[float, float]:
    """IQR clipping bounds for ``series``.

    Returns ``(NaN, NaN)`` when the series is degenerate (Q3 <= 0,
    i.e. at least 75% of values are zero). Without this guard, an
    intermittent / lumpy pair that wasn't filtered out upstream (any
    of three known misfire paths: missing classification file, key
    mismatch in ``_lookup_class``, or the bare-except swallowing a
    KeyError) would produce ``(0, 0)`` bounds and the apply step
    would clip every real positive sale to zero -- silent total
    data destruction. The NaN signal is filtered out by the caller
    so the pair is skipped instead.
    """
    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    if not (q3 > 0):
        return float("nan"), float("nan")
    iqr = q3 - q1
    return q1 - mult * iqr, q3 + mult * iqr


def fit_outlier_bounds(
    source_df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    cfg: dict,
) -> pd.DataFrame:
    """Compute per-pair IQR bounds. Returns a DataFrame keyed by ``group_keys``
    with ``bound_lo`` and ``bound_hi`` columns. Pairs with insufficient
    non-zero observations are omitted - the apply step leaves them untouched.
    """
    if not cfg.get("enabled", False) or source_df is None or source_df.empty:
        return pd.DataFrame(columns=group_keys + [_BOUND_LO, _BOUND_HI])

    method = cfg.get("method", "iqr")
    if method != "iqr":
        raise ValueError(
            f"Unsupported outlier method: {method!r}. Only 'iqr' is implemented."
        )
    mult = float(cfg.get("iqr_multiplier", 3.0))
    min_nonzero = int(cfg.get("min_nonzero_count", 8))

    rows: list[dict] = []
    for keys, g in source_df.groupby(group_keys):
        s = g[target_col].astype(float)
        if int((s > 0).sum()) < min_nonzero:
            continue
        lo, hi = _iqr_bounds(s, mult)
        # Skip degenerate pairs (NaN bounds from _iqr_bounds when Q3<=0).
        # Belt-and-braces against the three classification misfire paths;
        # an emitted (0,0) row would have clipped every real sale to zero.
        if not (np.isfinite(lo) and np.isfinite(hi)):
            continue
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        row = {k: v for k, v in zip(group_keys, key_tuple)}
        row[_BOUND_LO] = float(max(lo, 0.0))
        row[_BOUND_HI] = float(hi)
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=group_keys + [_BOUND_LO, _BOUND_HI])
    return pd.DataFrame(rows)


def apply_outlier_bounds(
    df: pd.DataFrame,
    bounds: pd.DataFrame | None,
    group_keys: list[str],
    target_col: str,
    cfg: dict,
    *,
    classes: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Clip per-pair target values using pre-fit bounds.

    ``bounds`` is a DataFrame with ``group_keys + [bound_lo, bound_hi]`` (as
    returned by :func:`fit_outlier_bounds`). If None or empty, the input is
    returned unchanged.
    """
    if not cfg.get("enabled", False) or bounds is None or bounds.empty:
        return df

    skip_intermittent = bool(cfg.get("skip_if_intermittent", True))
    skip_flag_cols = [c for c in (cfg.get("skip_if_flagged") or []) if c in df.columns]

    # Vectorised path: merge ``bounds`` onto ``df`` and clip with one np.clip.
    # The previous per-pair groupby + Python loop + per-pair g.copy() was the
    # single dominant cost of the inference pre-feature stage at ~6000 pairs
    # (each loop iteration allocated a fresh frame; concat at the end did
    # another O(N) copy). Merging on group_keys preserves row order via a
    # stable left join, so the output ordering matches the input.
    merged = df.merge(bounds[group_keys + [_BOUND_LO, _BOUND_HI]],
                      on=group_keys, how="left")

    # Pairs without bounds (absent from the fit, or absent because they're
    # intermittent/lumpy and bounds are skipped at fit-time) pass through.
    have_bounds = merged[_BOUND_LO].notna() & merged[_BOUND_HI].notna()

    # Class-aware skip: build the per-row class via the same MultiIndex lookup
    # without iterating. ``classes`` is indexed by group_keys with a ``class``
    # column; reindex onto the merged frame's key columns -> NaN for unknown.
    if skip_intermittent and classes is not None and "class" in classes.columns:
        cls_indexed = classes["class"]
        cls_per_row = pd.Series(
            cls_indexed.reindex(
                pd.MultiIndex.from_frame(merged[group_keys])
                if len(group_keys) > 1
                else merged[group_keys[0]]
            ).to_numpy(),
            index=merged.index,
        )
        protect_class = cls_per_row.isin({"intermittent", "lumpy"})
    else:
        protect_class = pd.Series(False, index=merged.index)

    # Per-row protection flag from caller-listed columns (lifecycle / anomaly).
    if skip_flag_cols:
        protect_flag = merged[skip_flag_cols].any(axis=1)
    else:
        protect_flag = pd.Series(False, index=merged.index)

    # Clip only rows that (a) have bounds AND (b) are not protected.
    clip_mask = have_bounds & ~protect_class & ~protect_flag
    original = merged[target_col].astype(float).to_numpy()
    lo_arr = merged[_BOUND_LO].to_numpy()
    hi_arr = merged[_BOUND_HI].to_numpy()
    # np.clip with row-wise bounds; rows where lo/hi are NaN are filtered
    # out via clip_mask so the NaN-propagation in np.clip is irrelevant.
    clipped_all = np.clip(original, lo_arr, hi_arr)
    out_target = np.where(clip_mask.to_numpy(), clipped_all, original)

    result = merged.drop(columns=[_BOUND_LO, _BOUND_HI])
    result[target_col] = out_target
    return result.reset_index(drop=True)


def per_pair_outlier_treatment(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    cfg: dict,
    classes: pd.DataFrame | None = None,
    source_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Training-time convenience: fit on ``source_df`` (or ``df``) and apply to ``df``.

    Inference uses :func:`apply_outlier_bounds` directly with bounds loaded
    from the training artifact.
    """
    if not cfg.get("enabled", False):
        return df
    bounds_src = source_df if source_df is not None else df
    bounds = fit_outlier_bounds(bounds_src, group_keys, target_col, cfg)
    return apply_outlier_bounds(df, bounds, group_keys, target_col, cfg, classes=classes)


def clip_negative_to_zero(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    df = df.copy()
    df[target_col] = df[target_col].fillna(0.0)
    df.loc[df[target_col] < 0, target_col] = 0.0
    return df
