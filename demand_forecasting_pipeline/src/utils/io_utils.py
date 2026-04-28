"""
Small IO helpers used across the pipeline.

Every writer creates parent directories as needed; every reader raises
with the original exception (no silent swallowing — callers decide).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd


def save_json(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pickle(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    joblib.dump(obj, path)


def load_pickle(path: str) -> Any:
    return joblib.load(path)


def save_dataframe(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)


def ceil_int_columns(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """Mutate ``df`` in place: ceiling-round the named columns to non-negative
    integers. Columns that don't exist are silently skipped so callers can pass
    the canonical quantity-column list without per-frame guards.

    Quantities (predicted, q_10, q_90, qty_if_demand, actual_qty, ...) ship as
    physical units everywhere downstream -- vans load whole pieces, the DB
    columns are int, and the UI renders ints. Ceiling (not nearest) so a
    fractional forecast errs on the side of having stock available.
    """
    for col in columns:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        df[col] = np.ceil(s.fillna(0).clip(lower=0)).astype("int64")
    return df


def ensure_tuple(keys) -> tuple:
    """Normalize group-key scalars and tuples into a tuple."""
    return keys if isinstance(keys, tuple) else (keys,)


def pair_mask(df: pd.DataFrame, group_keys: list[str], pair_keys: tuple) -> pd.Series:
    """Boolean mask selecting the rows of ``df`` that match every group key."""
    mask = df[group_keys[0]] == pair_keys[0]
    for i in range(1, len(group_keys)):
        mask &= df[group_keys[i]] == pair_keys[i]
    return mask
