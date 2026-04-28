"""
Per-pair split balance audit.

The global time-based split doesn't guarantee every pair has data in both
train and test. Some pairs only sold in the training window (recently went
silent); others appeared only in the test window (brand new). This module
produces the per-pair counts that let downstream stages — routing, the
training loop, the data-quality report — handle those pairs deliberately
instead of dropping them silently.
"""

from __future__ import annotations

import pandas as pd

_COUNT_COLS_PER_SPLIT = ("rows", "nonzero")


def audit_split(
    classes_df: pd.DataFrame,
    train: pd.DataFrame,
    val: pd.DataFrame,
    test: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    *,
    eps: float = 1e-9,
) -> pd.DataFrame:
    """One row per classified pair; columns for each split's row / nonzero count.

    Returned columns (always present, zero-filled if a pair is absent from a
    given split):
      ``n_train_rows``, ``n_train_nonzero``
      ``n_val_rows``,   ``n_val_nonzero``
      ``n_test_rows``,  ``n_test_nonzero``
    """
    pairs = classes_df.reset_index()[group_keys].drop_duplicates()

    out = pairs.copy()
    for label, df in (("train", train), ("val", val), ("test", test)):
        counts = _per_pair_counts(df, group_keys, target_col, label, eps)
        out = out.merge(counts, on=group_keys, how="left")

    for col in out.columns:
        if col.startswith("n_"):
            out[col] = out[col].fillna(0).astype(int)
    return out


def _per_pair_counts(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    label: str,
    eps: float,
) -> pd.DataFrame:
    cols = group_keys + [f"n_{label}_{suffix}" for suffix in _COUNT_COLS_PER_SPLIT]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    tmp = df.assign(_nz=(df[target_col] > eps).astype(int))
    return (
        tmp.groupby(group_keys, as_index=False)
        .agg(
            **{
                f"n_{label}_rows": (target_col, "count"),
                f"n_{label}_nonzero": ("_nz", "sum"),
            }
        )
    )


def summarise(
    audit: pd.DataFrame,
    *,
    min_train_rows: int = 30,
    min_test_rows: int = 3,
    min_train_nonzero: int = 2,
) -> dict:
    """Pair-count summary for the data-quality report. Thresholds come from
    the caller so they match the same values the routing rules use."""
    total = len(audit)
    if total == 0:
        return {"total_pairs": 0}

    no_test = int((audit["n_test_rows"] == 0).sum())
    no_train = int((audit["n_train_rows"] == 0).sum())
    thin_train = int((audit["n_train_rows"] < min_train_rows).sum())
    thin_test = int((audit["n_test_rows"] < min_test_rows).sum())
    sparse_train = int((audit["n_train_nonzero"] < min_train_nonzero).sum())

    return {
        "total_pairs": total,
        "pairs_no_train_rows": no_train,
        "pairs_no_test_rows": no_test,
        "pairs_below_min_train_rows": thin_train,
        "pairs_below_min_test_rows": thin_test,
        "pairs_below_min_train_nonzero": sparse_train,
        "thresholds": {
            "min_train_rows": int(min_train_rows),
            "min_test_rows": int(min_test_rows),
            "min_train_nonzero": int(min_train_nonzero),
        },
    }
