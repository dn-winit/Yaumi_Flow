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
# `coverage_widening_added` is the cumulative additive shift applied by
# ``calibrate_to_target_coverage`` (0.0 when no class-wide widening was
# needed); the final ``conformal_offset`` already includes it.
OFFSET_FRAME_COLUMNS = (
    "conformal_offset",
    "n_calibration_samples",
    "fallback_source",
    "coverage_widening_added",
)


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
    if class_col and class_col in pair_df.columns:
        out[class_col] = pair_df[class_col].values
    out["coverage_widening_added"] = 0.0

    # Order columns deterministically so the saved CSV matches what
    # downstream readers expect. ``class_col`` is appended at the end so
    # ``apply_pair_offsets`` can ignore it without breaking the schema.
    cols = group_keys + list(OFFSET_FRAME_COLUMNS)
    if class_col and class_col in out.columns and class_col not in cols:
        cols = cols + [class_col]
    return out[cols]


def _coverage_per_class(
    df: pd.DataFrame,
    *,
    target_col: str,
    q_low_col: str,
    q_high_col: str,
    class_col: str,
) -> dict[str, dict[str, float]]:
    """Observed band coverage per demand class on a labelled frame.

    Returns ``{class: {"observed": float, "n": int}}`` for every class
    present in ``df``. ``observed`` is the share of rows where actual
    landed inside ``[q_low, q_high]``. Rows with NaN in any of the four
    relevant columns are skipped (never crash a missing-column case).
    """
    needed = {target_col, q_low_col, q_high_col, class_col}
    if df.empty or not needed.issubset(df.columns):
        return {}
    work = df[list(needed)].copy()
    for c in (target_col, q_low_col, q_high_col):
        work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work.dropna(subset=list(needed))
    if work.empty:
        return {}
    inside = (work[target_col] >= work[q_low_col]) & (work[target_col] <= work[q_high_col])
    out: dict[str, dict[str, float]] = {}
    for cls, sub in work.groupby(class_col, dropna=False, sort=False):
        cls_key = str(cls)
        n = int(len(sub))
        if n == 0:
            continue
        out[cls_key] = {
            "observed": float(inside.loc[sub.index].mean()),
            "n": n,
        }
    return out


def calibrate_to_target_coverage(
    pair_offsets: pd.DataFrame,
    holdout: pd.DataFrame,
    *,
    group_keys: list[str],
    target_col: str,
    target_coverage: float,
    max_iterations: int,
    convergence_pp: float,
    max_shift_per_iter: float,
    q_low_col: str = "q_10",
    q_high_col: str = "q_90",
    class_col: str = "class",
) -> tuple[pd.DataFrame, list[dict]]:
    """Iteratively widen per-pair offsets so each class hits its coverage
    target on a held-out frame.

    Each iteration:
      1. Apply current offsets to the holdout via ``apply_pair_offsets``.
      2. Measure observed coverage per class.
      3. For each class where observed < target - convergence_pp,
         compute the additional additive widening needed: the
         ``(1 - target_coverage / observed)``-quantile of the absolute
         conformity scores on still-uncovered holdout rows of that
         class. The shift is clipped to ``max_shift_per_iter`` so a
         pathological iteration can never blow bands up unboundedly.
      4. ADD the per-class shift to every pair-offset of that class
         (not multiply -- multiplicative scaling inverts when offsets
         are negative, which is the legitimate over-cover case).
      5. Stop when every class is within ``convergence_pp`` of target
         or ``max_iterations`` is exhausted.

    Pure compute -- no I/O, no global state. Returns the adjusted
    offsets frame plus a per-iteration audit log so callers can persist
    the trail (e.g. into ``training_summary.json``).
    """
    if pair_offsets.empty or holdout.empty or class_col not in pair_offsets.columns:
        return pair_offsets, []

    convergence_ratio = convergence_pp / 100.0
    work = pair_offsets.copy()
    audit: list[dict] = []

    for iteration in range(1, max_iterations + 1):
        adjusted_holdout = apply_pair_offsets(
            holdout, work, group_keys=group_keys,
            q_low_col=q_low_col, q_high_col=q_high_col,
        )
        coverage = _coverage_per_class(
            adjusted_holdout,
            target_col=target_col,
            q_low_col=q_low_col,
            q_high_col=q_high_col,
            class_col=class_col,
        )
        shifts: dict[str, float] = {}
        for cls, stats in coverage.items():
            obs = stats["observed"]
            if obs >= target_coverage - convergence_ratio:
                continue
            cls_rows = adjusted_holdout[adjusted_holdout[class_col].astype(str) == cls]
            y = pd.to_numeric(cls_rows[target_col], errors="coerce")
            ql = pd.to_numeric(cls_rows[q_low_col], errors="coerce")
            qh = pd.to_numeric(cls_rows[q_high_col], errors="coerce")
            score = np.maximum(ql - y, y - qh)
            outside = score[(score > 0).values]
            if outside.empty:
                continue
            # We need to capture ``need`` fraction of the rows currently
            # outside the band. The shift that captures that fraction
            # is the ``need``-th quantile of the outside shortfalls.
            need = (target_coverage - obs) / max(1.0 - obs, 1e-9)
            shift_q = float(np.clip(need, 0.0, 1.0))
            raw_shift = float(outside.quantile(shift_q))
            shifts[cls] = float(min(raw_shift, max_shift_per_iter))

        audit.append({
            "iteration": iteration,
            "coverage": {k: round(v["observed"], 4) for k, v in coverage.items()},
            "n_per_class": {k: v["n"] for k, v in coverage.items()},
            "applied_shifts": {k: round(v, 4) for k, v in shifts.items()},
        })

        if not shifts:
            break

        shift_series = work[class_col].astype(str).map(shifts).fillna(0.0)
        work["conformal_offset"] = work["conformal_offset"] + shift_series
        # Track cumulative widening per pair so the audit CSV explains why
        # an offset differs from the raw conformal output.
        work["coverage_widening_added"] = work["coverage_widening_added"] + shift_series

    return work, audit


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
