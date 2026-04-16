import numpy as np
import pandas as pd


def compute_adi_cv2(series):
    s = pd.Series(series).astype(float).values
    n = len(s)
    if n == 0:
        return np.nan, np.nan
    nz = s[s > 0]
    if len(nz) == 0:
        return np.inf, np.nan
    adi = n / len(nz)
    mean_nz = nz.mean()
    if mean_nz == 0:
        return adi, np.nan
    cv2 = (nz.std(ddof=0) / mean_nz) ** 2
    return adi, cv2


def classify_pair(adi, cv2, adi_thr, cv2_thr):
    if np.isnan(adi) or np.isnan(cv2):
        return "intermittent"
    if adi < adi_thr and cv2 < cv2_thr:
        return "smooth"
    if adi >= adi_thr and cv2 < cv2_thr:
        return "intermittent"
    if adi < adi_thr and cv2 >= cv2_thr:
        return "erratic"
    return "lumpy"


def classify_dataset(df, group_keys, target_col, adi_thr, cv2_thr):
    rows = []
    for keys, g in df.groupby(group_keys):
        adi, cv2 = compute_adi_cv2(g[target_col].values)
        cls = classify_pair(adi, cv2, adi_thr, cv2_thr)
        keys = keys if isinstance(keys, tuple) else (keys,)
        rows.append(list(keys) + [adi, cv2, cls])
    cols = group_keys + ["adi", "cv2", "class"]
    out = pd.DataFrame(rows, columns=cols).set_index(group_keys)
    return out
