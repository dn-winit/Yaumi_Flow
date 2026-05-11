"""
Target encoding with Bayesian smoothing toward the global mean.

Pair count x pair mean is pulled toward the global mean by a fixed
``smoothing`` prior so small-count pairs don't overfit their own history.
Source dataframe MUST be the train window - never pass test/val-inclusive
data, or the encoding leaks future targets into features.

Persistence
-----------
The encoding map computed at training time is the contract inference
must honour. Recomputing it from "current data" at inference makes the
feature drift every time new transactions land, breaking train/serve
consistency. The loader/saver below freezes the (per_pair_mean,
global_mean) pair into a single JSON artifact so inference reads
exactly what training wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..utils.io_utils import save_json


def compute_target_encoding(
    train_df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    smoothing: int,
) -> tuple[pd.DataFrame, float]:
    """Return ``(encoding_df, global_mean)``.

    ``encoding_df`` has columns ``group_keys + [te_pair_mean]``. Pairs not
    present in ``train_df`` are simply absent from the encoding - callers
    fill them with ``global_mean`` via :func:`apply_target_encoding`.
    """
    if train_df.empty:
        empty = pd.DataFrame(columns=list(group_keys) + ["te_pair_mean"])
        return empty, 0.0

    global_mean = float(train_df[target_col].mean())
    stats = (
        train_df.groupby(group_keys)[target_col]
        .agg(pair_mean="mean", pair_count="count")
        .reset_index()
    )
    stats["te_pair_mean"] = (
        (stats["pair_count"] * stats["pair_mean"] + smoothing * global_mean)
        / (stats["pair_count"] + smoothing)
    )
    return stats[list(group_keys) + ["te_pair_mean"]], global_mean


def apply_target_encoding(
    df: pd.DataFrame,
    group_keys: list[str],
    encoding_df: pd.DataFrame,
    global_mean: float,
) -> pd.DataFrame:
    """Left-merge encoding onto ``df``; unknown pairs fall back to ``global_mean``."""
    merged = df.merge(encoding_df, on=group_keys, how="left")
    merged["te_pair_mean"] = merged["te_pair_mean"].fillna(global_mean)
    return merged


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_target_encoding(
    path: str | Path,
    *,
    encoding_df: pd.DataFrame,
    global_mean: float,
    group_keys: list[str],
    smoothing: int,
) -> None:
    """Write the encoding map to a JSON artifact.

    Schema (versioned so loader can detect a stale shape):

        {
          "schema_version": "1.0",
          "global_mean": float,
          "smoothing": int,
          "group_keys": [str, ...],
          "encoding": [
            {<group_keys[0]>: <value>, ..., "te_pair_mean": float},
            ...
          ]
        }

    Group-key values are stringified so leading-zero codes (e.g. RouteCode
    "00123") survive the JSON round-trip without being silently coerced.
    """
    p = Path(path)
    if encoding_df is None or encoding_df.empty:
        encoding_records: list[dict[str, Any]] = []
    else:
        cols = list(group_keys) + ["te_pair_mean"]
        sub = encoding_df[cols].copy()
        for k in group_keys:
            sub[k] = sub[k].astype(str)
        sub["te_pair_mean"] = sub["te_pair_mean"].astype(float)
        encoding_records = sub.to_dict(orient="records")
    payload = {
        "schema_version": "1.0",
        "global_mean": float(global_mean),
        "smoothing": int(smoothing),
        "group_keys": list(group_keys),
        "encoding": encoding_records,
    }
    # Atomic tmp+rename so a killed training run never leaves inference
    # to load a torn JSON.
    save_json(payload, str(p))


def load_target_encoding(
    path: str | Path,
    *,
    expected_group_keys: list[str],
) -> tuple[pd.DataFrame, float]:
    """Read the JSON artifact and return ``(encoding_df, global_mean)``.

    Raises ``FileNotFoundError`` if the file is missing -- caller decides
    whether to degrade silently or refuse the run. Validates the
    persisted ``group_keys`` against ``expected_group_keys`` so a config
    rename doesn't silently apply the wrong encoding.
    """
    p = Path(path)
    with open(p, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    saved_keys = list(payload.get("group_keys") or [])
    if saved_keys != list(expected_group_keys):
        raise ValueError(
            f"target_encoding artifact group_keys={saved_keys!r} do not match "
            f"runtime group_keys={list(expected_group_keys)!r}; retrain to refresh"
        )
    encoding_records = payload.get("encoding") or []
    if not encoding_records:
        empty = pd.DataFrame(columns=list(expected_group_keys) + ["te_pair_mean"])
        return empty, float(payload.get("global_mean", 0.0))
    df = pd.DataFrame(encoding_records)
    for k in expected_group_keys:
        df[k] = df[k].astype(str)
    df["te_pair_mean"] = pd.to_numeric(df["te_pair_mean"], errors="coerce")
    df = df.dropna(subset=["te_pair_mean"])
    return df[list(expected_group_keys) + ["te_pair_mean"]], float(payload["global_mean"])
