"""
Per-pair Conformalized Quantile Regression (CQR) calibration.

The class-level quantile model emits a wide ``[q_low, q_high]`` band that
covers the worst pair in its class — too loose for stable pairs. CQR shrinks
or widens the band per pair using validation-set residuals, while preserving
the configured marginal coverage.

For each calibration row the *conformity score* is::

    E_i = max(q_low_i - y_i,  y_i - q_high_i)

Positive ``E_i`` means the actual fell outside the predicted band (band too
tight). Negative means the actual was inside (band could be tighter without
losing coverage). The per-pair offset is the empirical ``(1-α)`` quantile of
``{E_i}`` — at inference we shift ``q_low`` down and ``q_high`` up by that
offset.

Fallback hierarchy (config-driven via ``min_samples_per_pair``):

  pair → class → global → zero (no calibration)

Pairs with fewer than ``min_samples_per_pair`` calibration rows borrow the
class-wide offset; classes with no rows fall through to the global offset;
empty calibration sets fall through to zero (the bands stay as-is).

This module is pure compute — no I/O. Train- and inference-side wiring is
in ``pipelines/{train,inference}_pipeline.py``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Returned offset frame columns — keep the surface area explicit so the
# inference-side merge knows exactly which columns to expect.
OFFSET_FRAME_COLUMNS = ("conformal_offset", "n_calibration_samples", "fallback_source")


def _empirical_one_sided_quantile(scores: np.ndarray, target_coverage: float) -> float:
    """Finite-sample-corrected ``(1-α)`` empirical quantile.

    Uses the standard split-conformal correction: rank the scores, pick the
    ``ceil((n+1)·target_coverage)`` th value (1-indexed). When
    ``ceil(...) > n`` (very small ``n`` and high coverage) the offset
    saturates at the maximum observed score — the most conservative
    finite-sample answer.
    """
    n = scores.size
    if n == 0:
        return 0.0
    sorted_scores = np.sort(scores)
    rank = int(np.ceil((n + 1) * target_coverage))
    rank = max(1, min(rank, n))
    return float(sorted_scores[rank - 1])


def compute_pair_offsets(
    val_calibration: pd.DataFrame,
    *,
    group_keys: list[str],
    target_col: str,
    target_coverage: float,
    min_samples_per_pair: int,
    q_low_col: str = "q_10",
    q_high_col: str = "q_90",
    class_col: str | None = "class",
) -> pd.DataFrame:
    """Compute one offset per pair from validation-window calibration data.

    ``val_calibration`` must contain ``group_keys + [target_col, q_low_col,
    q_high_col]`` plus optionally ``class_col``. Each row is one pair-period
    where actuals are known and the class quantile model has produced a
    band — so the residual ``E_i`` is well defined.

    Returns a frame with one row per pair seen in the calibration set, with
    columns::

        *group_keys, conformal_offset, n_calibration_samples, fallback_source

    where ``fallback_source`` is one of ``{"pair", "class", "global"}``.
    Pairs absent from this frame should be treated as ``offset=0`` by the
    caller (typical at inference for cold-start pairs).
    """
    if val_calibration.empty:
        return pd.DataFrame(columns=group_keys + list(OFFSET_FRAME_COLUMNS))

    needed = set(group_keys) | {target_col, q_low_col, q_high_col}
    missing = needed - set(val_calibration.columns)
    if missing:
        logger.warning("conformal: calibration set missing columns %s — returning empty offsets", missing)
        return pd.DataFrame(columns=group_keys + list(OFFSET_FRAME_COLUMNS))

    df = val_calibration[list(needed | ({class_col} if class_col and class_col in val_calibration.columns else set()))].copy()
    y = pd.to_numeric(df[target_col], errors="coerce")
    ql = pd.to_numeric(df[q_low_col], errors="coerce")
    qh = pd.to_numeric(df[q_high_col], errors="coerce")
    df["_score"] = np.maximum(ql - y, y - qh)
    df = df.dropna(subset=["_score"])
    if df.empty:
        return pd.DataFrame(columns=group_keys + list(OFFSET_FRAME_COLUMNS))

    # --- per-pair offset ---------------------------------------------------
    pair_groups = df.groupby(group_keys, dropna=False, sort=False)
    pair_offset = pair_groups["_score"].apply(
        lambda s: _empirical_one_sided_quantile(s.to_numpy(dtype=float), target_coverage)
    ).rename("offset_pair")
    pair_n = pair_groups["_score"].size().rename("n_calibration_samples")
    pair_df = pd.concat([pair_offset, pair_n], axis=1).reset_index()

    # Carry class label per pair (first non-null) so the class fallback merge
    # below has something to join on. Pairs without a class go to global.
    if class_col and class_col in df.columns:
        pair_class = pair_groups[class_col].agg(lambda s: s.dropna().iloc[0] if not s.dropna().empty else np.nan)
        pair_df = pair_df.merge(pair_class.rename(class_col).reset_index(), on=group_keys, how="left")

    # --- class-level fallback offset --------------------------------------
    class_offset_map: dict = {}
    if class_col and class_col in df.columns:
        for cls, sub in df.groupby(class_col, dropna=True, sort=False):
            class_offset_map[cls] = _empirical_one_sided_quantile(
                sub["_score"].to_numpy(dtype=float), target_coverage,
            )

    # --- global fallback offset -------------------------------------------
    global_offset = _empirical_one_sided_quantile(df["_score"].to_numpy(dtype=float), target_coverage)

    # --- pick offset per pair using fallback hierarchy --------------------
    def _resolve(row: pd.Series) -> tuple[float, str]:
        if row["n_calibration_samples"] >= min_samples_per_pair:
            return float(row["offset_pair"]), "pair"
        cls = row.get(class_col) if class_col else None
        if cls is not None and not pd.isna(cls) and cls in class_offset_map:
            return float(class_offset_map[cls]), "class"
        return float(global_offset), "global"

    resolved = pair_df.apply(_resolve, axis=1, result_type="expand")
    resolved.columns = ["conformal_offset", "fallback_source"]
    out = pd.concat([pair_df[group_keys + ["n_calibration_samples"]], resolved], axis=1)

    # Order columns deterministically so the saved CSV matches what
    # downstream readers expect.
    return out[group_keys + list(OFFSET_FRAME_COLUMNS)]


def apply_pair_offsets(
    forecast: pd.DataFrame,
    offsets: pd.DataFrame,
    *,
    group_keys: list[str],
    q_low_col: str = "q_10",
    q_high_col: str = "q_90",
    floor: float = 0.0,
) -> pd.DataFrame:
    """Shift every pair's band by its calibrated offset.

    ``forecast`` carries the raw ``q_low_col`` / ``q_high_col`` from the
    class quantile model. ``offsets`` is the frame produced by
    :func:`compute_pair_offsets`. Pairs missing from ``offsets`` get
    ``offset = 0`` — the band is unchanged, which matches the conservative
    behaviour we want for cold-start pairs at inference time.

    The lower bound is floored at ``floor`` (default 0) since demand can't
    go negative; the upper bound is left unbounded — letting it stay
    honest, which is the whole point of widening when calibration says so.
    """
    if forecast.empty or q_low_col not in forecast.columns or q_high_col not in forecast.columns:
        return forecast

    if offsets is None or offsets.empty:
        return forecast

    join = offsets[group_keys + ["conformal_offset"]]
    out = forecast.merge(join, on=group_keys, how="left")
    out["conformal_offset"] = pd.to_numeric(out["conformal_offset"], errors="coerce").fillna(0.0)

    out[q_low_col] = (pd.to_numeric(out[q_low_col], errors="coerce") - out["conformal_offset"]).clip(lower=floor)
    out[q_high_col] = pd.to_numeric(out[q_high_col], errors="coerce") + out["conformal_offset"]

    # Defensive: keep low ≤ high. A pathological negative offset that
    # exceeds the band width could otherwise invert the interval.
    swap_mask = out[q_low_col] > out[q_high_col]
    if swap_mask.any():
        out.loc[swap_mask, [q_low_col, q_high_col]] = out.loc[swap_mask, [q_high_col, q_low_col]].values

    return out.drop(columns=["conformal_offset"])
