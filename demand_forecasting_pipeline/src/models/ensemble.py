"""
Weighted-average ensembles across per-model predictions.

``weighted_average_ensemble`` combines aligned prediction frames into a
single frame. ``weights_from_metric`` turns per-model metric scores into
normalised weights using either inverse (lower-is-better error metrics) or
value-based (higher-is-better score metrics) weighting.
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import pandas as pd


def _infer_key_cols(frame: pd.DataFrame) -> list[str]:
    """The non-``prediction`` columns are the keys we blend on. Order is
    preserved as it appears in ``frame.columns`` so the output keeps the
    caller's column ordering."""
    return [c for c in frame.columns if c != "prediction"]


def weighted_average_ensemble(
    predictions: dict[str, pd.DataFrame],
    weights: Optional[dict[str, float]] = None,
    *,
    key_cols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Combine per-model prediction frames via weighted average on shared
    (group_keys + date) keys.

    Why key-aligned, not positional
    -------------------------------
    Component frames can arrive in different row orders depending on
    which model produced them (LightGBM emits rows grouped by tree, ETS
    by pair, etc.). Blending by row position silently produces garbage
    when frames disagree -- one frame's ``(R1, I7, 2025-01-01)`` lining
    up against another's ``(R3, I2, 2025-01-01)``. We merge on the
    explicit key columns so a reordered or partially-overlapping
    component produces a correct result or a loud error, never silent
    garbage.

    Coverage contract
    -----------------
    Every component frame MUST cover the same key set. We assert this
    after the outer-merge:
      * no NaN predictions after the blend (would mean a frame skipped
        a key the others covered) -- raise.
      * no extra keys appearing in only one frame (the row count after
        merge must equal the reference frame's row count) -- raise.

    Either condition fires a clear error with the offending key sample.
    Empty input dict -> empty DataFrame (no work to do).
    """
    if not predictions:
        return pd.DataFrame()

    names = list(predictions.keys())
    reference = predictions[names[0]]
    if reference.empty:
        return reference.copy()

    keys = list(key_cols) if key_cols is not None else _infer_key_cols(reference)
    if not keys:
        raise ValueError(
            "weighted_average_ensemble: cannot infer blend keys -- "
            "every component frame has only a 'prediction' column"
        )

    # Validate: every component must have the same key columns. Mismatched
    # schemas are a config bug, not a runtime condition we can recover
    # from -- raise immediately with the offending model name.
    for name, frame in predictions.items():
        missing = [k for k in keys + ["prediction"] if k not in frame.columns]
        if missing:
            raise ValueError(
                f"weighted_average_ensemble: component {name!r} missing "
                f"columns {missing}; expected {keys + ['prediction']}"
            )

    if weights is None:
        weights = {k: 1.0 / len(names) for k in names}
    total_w = sum(weights.values()) or 1.0

    # Outer-merge every component on the key columns. ``how="outer"`` so
    # any coverage mismatch surfaces as NaN we then detect and raise.
    # We rename ``prediction`` per-component so columns don't collide.
    merged: Optional[pd.DataFrame] = None
    for name in names:
        col = f"_pred_{name}"
        f = predictions[name][keys + ["prediction"]].rename(columns={"prediction": col})
        # Deduplicate: a component frame must not have duplicate keys.
        # If it does we'd silently re-blend the same row multiple times.
        dup_count = int(f.duplicated(subset=keys).sum())
        if dup_count:
            sample = f.loc[f.duplicated(subset=keys, keep=False), keys].head(3).to_dict("records")
            raise ValueError(
                f"weighted_average_ensemble: component {name!r} has "
                f"{dup_count} duplicate key rows; sample={sample}"
            )
        merged = f if merged is None else merged.merge(f, on=keys, how="outer")

    assert merged is not None  # `predictions` non-empty so loop ran

    # Coverage check: every component must cover every key.
    pred_cols = [f"_pred_{n}" for n in names]
    nan_rows = merged[pred_cols].isna().any(axis=1)
    if nan_rows.any():
        offending = merged.loc[nan_rows, keys].head(3).to_dict("records")
        n_missing = int(nan_rows.sum())
        raise ValueError(
            f"weighted_average_ensemble: {n_missing} key rows are missing "
            f"a prediction in at least one component; sample={offending}"
        )

    # Weighted blend, then drop the per-component columns.
    blended = np.zeros(len(merged), dtype=float)
    for name in names:
        w = weights.get(name, 0.0) / total_w
        blended += w * merged[f"_pred_{name}"].to_numpy(dtype=float)

    out = merged[keys].copy()
    out["prediction"] = blended
    return out.reset_index(drop=True)


def weights_from_metric(
    metric_dict: dict[str, float | None],
    kind: str = "lower_is_better",
    eps: float = 1e-9,
) -> dict[str, float] | None:
    """Turn ``{model: metric}`` into normalised weights.

    ``lower_is_better`` uses inverse weighting (``1 / (v + eps)``) - the
    default for error metrics like RMSE. Anything else treats larger values
    as better (clipped at 0 to keep weights non-negative).

    Returns ``None`` when every input is missing / non-finite.
    """
    valid = {k: v for k, v in metric_dict.items() if v is not None and np.isfinite(v)}
    if not valid:
        return None

    if kind == "lower_is_better":
        inv = {k: 1.0 / (v + eps) for k, v in valid.items()}
    else:
        inv = {k: max(v, 0.0) + eps for k, v in valid.items()}

    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()}
