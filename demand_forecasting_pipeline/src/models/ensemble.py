import numpy as np
import pandas as pd


def weighted_average_ensemble(predictions, weights=None):
    keys = list(predictions.keys())
    if weights is None:
        weights = {k: 1.0 / len(keys) for k in keys}
    base = predictions[keys[0]][["prediction"]].copy() * 0.0
    meta = predictions[keys[0]].drop(columns=["prediction"]).copy()
    total_w = sum(weights.values())
    for k in keys:
        w = weights.get(k, 0.0) / total_w
        base["prediction"] = base["prediction"] + w * predictions[k]["prediction"].values
    out = pd.concat([meta.reset_index(drop=True), base.reset_index(drop=True)], axis=1)
    return out


def weights_from_metric(metric_dict, kind="lower_is_better", eps=1e-9):
    vals = {}
    for k, v in metric_dict.items():
        if v is None or not np.isfinite(v):
            continue
        vals[k] = v
    if not vals:
        return None
    if kind == "lower_is_better":
        inv = {k: 1.0 / (v + eps) for k, v in vals.items()}
    else:
        inv = {k: max(v, 0.0) + eps for k, v in vals.items()}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()}
