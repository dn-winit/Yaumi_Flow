"""
Target encoding with Bayesian smoothing toward the global mean.

Pair count x pair mean is pulled toward the global mean by a fixed
``smoothing`` prior so small-count pairs don't overfit their own history.
Source dataframe MUST be the train window - never pass test/val-inclusive
data, or the encoding leaks future targets into features.

K-fold OOF for training rows
----------------------------
Bayesian smoothing alone damps but does NOT eliminate train-row
self-leak: every training row's target contributes to its own pair's
encoding with weight ``1/(n + smoothing)`` per row. For a small pair
(n=1, smoothing=10) that's ~9% of the row's own target leaking into
its feature. Across the long tail of small pairs this is a real
systematic train/test gap.

The fix is K-fold out-of-fold (OOF) encoding for the TRAINING rows:
each fold's encoding is computed from the OTHER K-1 folds so a
training row never sees its own target. The inference rows, by
construction, weren't in the training set at all, so a single
full-train encoding is correct for them. ``fit_oof_encoding`` returns
both the per-row OOF map (for training) and the full-train map (for
val/test/inference).

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

import numpy as np
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


def fit_oof_encoding(
    train_df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    smoothing: int,
    *,
    n_folds: int = 5,
    seed: int = 1729,
) -> tuple[pd.Series, pd.DataFrame, float]:
    """Compute leak-free training encodings + the full-train inference map.

    Returns
    -------
    oof : pd.Series
        Per-row OOF encoding aligned to ``train_df.index``. For each row
        the encoding is fit on the OTHER K-1 folds so the row's own
        target never enters its own feature. Pairs absent from the
        other folds (a small-count pair that lives in one fold only)
        get the global_mean fallback -- the same fallback inference
        uses for cold-start pairs, so train and serve degrade
        identically.
    full_encoding : pd.DataFrame
        The non-OOF encoding fit on the entire ``train_df``. This is
        the artifact saved to disk and applied to val/test/inference
        rows. None of those rows were in the training fold set, so they
        cannot leak.
    global_mean : float
        Train-window global mean of the target.

    Why a single function (and not "compute OOF, then re-compute full")
    -------------------------------------------------------------------
    A two-call API would invite a caller to forget the full re-fit and
    accidentally ship the OOF encoding to inference (which would then
    drift fold-by-fold). One function = one contract, no ambiguity.
    """
    if train_df.empty:
        empty_series = pd.Series([], dtype=float)
        empty_df = pd.DataFrame(columns=list(group_keys) + ["te_pair_mean"])
        return empty_series, empty_df, 0.0

    n_folds = max(2, int(n_folds))
    global_mean = float(train_df[target_col].mean())
    smoothing = int(smoothing)

    # Assign each ROW to one of K folds. Random assignment (seeded) is
    # the standard OOF construction for tabular encoding -- it's
    # equivalent to KFold(shuffle=True) but avoids the sklearn import
    # cost on this hot path. Per-row (not per-pair) so even within a
    # single pair, half the rows train on the other half.
    rng = np.random.default_rng(seed)
    fold_ids = rng.integers(0, n_folds, size=len(train_df))

    oof = np.full(len(train_df), global_mean, dtype=float)

    # Vectorised per-fold pass: build the fold's encoding on the held-out
    # rows via a left-merge on ``group_keys``, then write back the encoded
    # column into ``oof``. The earlier implementation iterated every
    # ``score_idx`` row in Python, paying a 1M-row dict lookup loop per
    # fold; on a 5-fold x 1M-row train this dominated the encoding stage.
    key_frame = train_df[list(group_keys)].reset_index(drop=True)
    key_frame["_row"] = np.arange(len(train_df))

    for f in range(n_folds):
        mask_train = fold_ids != f         # rows used to fit this fold
        mask_score = ~mask_train           # rows to encode this fold
        if not mask_train.any() or not mask_score.any():
            continue
        sub = train_df.iloc[mask_train]
        stats = (
            sub.groupby(group_keys)[target_col]
               .agg(pair_mean="mean", pair_count="count")
               .reset_index()
        )
        stats["te"] = (
            (stats["pair_count"] * stats["pair_mean"] + smoothing * global_mean)
            / (stats["pair_count"] + smoothing)
        )
        scored = (
            key_frame.loc[mask_score, list(group_keys) + ["_row"]]
            .merge(stats[list(group_keys) + ["te"]], on=group_keys, how="left")
        )
        # Pairs absent from the other folds (single-fold pairs) keep the
        # global_mean fallback we initialised ``oof`` with.
        valid = scored["te"].notna()
        if valid.any():
            oof[scored.loc[valid, "_row"].to_numpy()] = scored.loc[valid, "te"].to_numpy()

    oof_series = pd.Series(oof, index=train_df.index)

    # Full-train encoding for inference (and for any caller that needs
    # the standard non-OOF map). Identical math to ``compute_target_
    # encoding`` so the disk artifact format is unchanged.
    full_encoding, _ = compute_target_encoding(
        train_df, group_keys, target_col, smoothing,
    )
    return oof_series, full_encoding, global_mean


def apply_target_encoding(
    df: pd.DataFrame,
    group_keys: list[str],
    encoding_df: pd.DataFrame,
    global_mean: float,
) -> pd.DataFrame:
    """Left-merge encoding onto ``df``; unknown pairs fall back to ``global_mean``.

    Idempotent on a pre-existing ``te_pair_mean`` column: callers like
    the recursive multi-step test evaluator hand us a frame that has
    ALREADY been through ``build_features`` once (so it carries
    ``te_pair_mean`` from the prior pass). A naive merge would
    produce ``te_pair_mean_x`` / ``te_pair_mean_y`` and the
    downstream ``merged["te_pair_mean"]`` access would KeyError.
    Dropping the pre-existing column first makes ``encoding_df`` the
    single source of truth: same authority contract as the
    ``classes_df`` re-merge in ``build_features`` for the ``class``
    column. The persisted training-time encoding is always the
    canonical value the model was fit on; never trust an in-frame
    leftover.
    """
    df_for_merge = df.drop(columns=["te_pair_mean"]) if "te_pair_mean" in df.columns else df
    merged = df_for_merge.merge(encoding_df, on=group_keys, how="left")
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
    # ``astype("string")`` (pandas StringDtype), not ``astype(str)``
    # (object/Python str), so the artifact's group_keys match the
    # panel's dtype that was pinned to ``string`` via resolve_dtypes.
    # An object/string mismatch silently works in pandas today but is
    # a slow-coerce path and can break under stricter merge behaviour.
    for k in expected_group_keys:
        df[k] = df[k].astype("string")
    df["te_pair_mean"] = pd.to_numeric(df["te_pair_mean"], errors="coerce")
    df = df.dropna(subset=["te_pair_mean"])
    return df[list(expected_group_keys) + ["te_pair_mean"]], float(payload["global_mean"])
