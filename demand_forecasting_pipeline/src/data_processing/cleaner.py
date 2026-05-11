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
    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    iqr = q3 - q1
    return q1 - mult * iqr, q3 + mult * iqr


def _lookup_class(classes: pd.DataFrame | None, keys) -> str | None:
    if classes is None:
        return None
    try:
        idx = keys if isinstance(keys, tuple) else (keys,)
        return classes.loc[idx, "class"]
    except Exception:
        return None


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

    # Build the lookup once for fast per-pair access.
    bounds_lookup: dict = {}
    for _, r in bounds.iterrows():
        key_tuple = tuple(r[k] for k in group_keys)
        key = key_tuple if len(group_keys) > 1 else key_tuple[0]
        bounds_lookup[key] = (float(r[_BOUND_LO]), float(r[_BOUND_HI]))

    out_parts: list[pd.DataFrame] = []
    for keys, g in df.groupby(group_keys):
        g = g.copy()
        cls = _lookup_class(classes, keys)

        if skip_intermittent and cls in ("intermittent", "lumpy"):
            out_parts.append(g)
            continue
        if keys not in bounds_lookup:
            out_parts.append(g)
            continue

        lo, hi = bounds_lookup[keys]
        original = g[target_col].astype(float).to_numpy()
        clipped = np.clip(original, lo, hi)

        if skip_flag_cols:
            protect_mask = g[skip_flag_cols].any(axis=1).to_numpy()
            g[target_col] = np.where(protect_mask, original, clipped)
        else:
            g[target_col] = clipped

        out_parts.append(g)

    return pd.concat(out_parts, ignore_index=True)


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
