"""
Target encoding with Bayesian smoothing toward the global mean.

Pair count × pair mean is pulled toward the global mean by a fixed
``smoothing`` prior so small-count pairs don't overfit their own history.
Source dataframe MUST be the train window — never pass test/val-inclusive
data, or the encoding leaks future targets into features.
"""

from __future__ import annotations

import pandas as pd


def compute_target_encoding(
    train_df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    smoothing: int,
) -> tuple[pd.DataFrame, float]:
    """Return ``(encoding_df, global_mean)``.

    ``encoding_df`` has columns ``group_keys + [te_pair_mean]``. Pairs not
    present in ``train_df`` are simply absent from the encoding — callers
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
