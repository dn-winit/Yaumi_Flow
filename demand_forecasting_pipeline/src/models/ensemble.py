"""
Weighted-average ensembles across per-model predictions.

``weighted_average_ensemble`` combines aligned prediction frames into a
single frame. ``weights_from_metric`` turns per-model metric scores into
normalised weights using either inverse (lower-is-better error metrics) or
value-based (higher-is-better score metrics) weighting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def weighted_average_ensemble(
    predictions: dict[str, pd.DataFrame],
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Combine per-model prediction frames via weighted average.

    All frames must be aligned on the non-``prediction`` columns (group keys
    and date). If ``weights`` is omitted, components are averaged uniformly.
    """
    if not predictions:
        return pd.DataFrame()

    keys = list(predictions.keys())
    if weights is None:
        weights = {k: 1.0 / len(keys) for k in keys}

    total_w = sum(weights.values()) or 1.0
    reference = predictions[keys[0]]
    combined = np.zeros(len(reference), dtype=float)
    for k in keys:
        w = weights.get(k, 0.0) / total_w
        combined += w * predictions[k]["prediction"].to_numpy(dtype=float)

    out = reference.drop(columns=["prediction"]).copy().reset_index(drop=True)
    out["prediction"] = combined
    return out


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
