"""
Small IO helpers used across the pipeline.

Every writer creates parent directories as needed; every reader raises
with the original exception (no silent swallowing — callers decide).
"""

from __future__ import annotations

import json
import os
from typing import Any

import joblib
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


def ensure_tuple(keys) -> tuple:
    """Normalize group-key scalars and tuples into a tuple."""
    return keys if isinstance(keys, tuple) else (keys,)


def pair_mask(df: pd.DataFrame, group_keys: list[str], pair_keys: tuple) -> pd.Series:
    """Boolean mask selecting the rows of ``df`` that match every group key."""
    mask = df[group_keys[0]] == pair_keys[0]
    for i in range(1, len(group_keys)):
        mask &= df[group_keys[i]] == pair_keys[i]
    return mask
