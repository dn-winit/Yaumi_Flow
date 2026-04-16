import numpy as np
import pandas as pd

from .classifier import compute_adi_cv2, classify_pair


def compute_pair_explainability(df, group_keys, target_col, adi_thr, cv2_thr):
    rows = []
    for keys, g in df.groupby(group_keys):
        s = g[target_col].astype(float)
        nz = s[s > 0]
        adi, cv2 = compute_adi_cv2(s.values)
        cls = classify_pair(adi, cv2, adi_thr, cv2_thr)
        gap = _avg_gap(s.values)
        record = {
            "n_periods": int(len(s)),
            "n_nonzero_periods": int((s > 0).sum()),
            "nonzero_ratio": float((s > 0).mean()) if len(s) else 0.0,
            "mean_qty": float(s.mean()) if len(s) else 0.0,
            "median_qty": float(s.median()) if len(s) else 0.0,
            "min_qty": float(s.min()) if len(s) else 0.0,
            "max_qty": float(s.max()) if len(s) else 0.0,
            "std_qty": float(s.std(ddof=0)) if len(s) else 0.0,
            "sum_qty": float(s.sum()),
            "nonzero_mean": float(nz.mean()) if len(nz) else 0.0,
            "nonzero_std": float(nz.std(ddof=0)) if len(nz) else 0.0,
            "adi": float(adi) if np.isfinite(adi) else None,
            "cv2": float(cv2) if cv2 is not None and not (isinstance(cv2, float) and np.isnan(cv2)) else None,
            "class": cls,
            "avg_gap_between_demand": gap,
        }
        keys = keys if isinstance(keys, tuple) else (keys,)
        for k, v in zip(group_keys, keys):
            record[k] = v
        rows.append(record)
    cols = group_keys + [c for c in rows[0].keys() if c not in group_keys] if rows else group_keys
    return pd.DataFrame(rows, columns=cols)


def _avg_gap(arr):
    idx = np.where(arr > 0)[0]
    if len(idx) < 2:
        return None
    gaps = np.diff(idx)
    return float(gaps.mean())
