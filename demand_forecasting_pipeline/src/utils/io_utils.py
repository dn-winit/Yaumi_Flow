"""
Small IO helpers used across the pipeline.

Every writer creates parent directories as needed; every reader raises
with the original exception (no silent swallowing - callers decide).

Atomic writes
-------------
``save_json``, ``save_dataframe``, ``save_pickle`` write to a sibling
``<name>.tmp.<pid>`` then ``os.replace`` to the target path. A killed
process leaves a stray ``.tmp.*`` (cleaned up on the next attempt) but
NEVER a half-written target file -- so downstream readers can't observe
a torn artifact.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd


def _atomic_write(path: str, write_fn) -> None:
    """Run ``write_fn(tmp_path)`` then atomically replace ``path``.

    ``os.replace`` is atomic on POSIX and Windows when source and target
    are on the same filesystem. Using a sibling tempfile guarantees that
    invariant. On any error the partial tempfile is cleaned up so
    re-runs don't accumulate ``.tmp.*`` debris in artifact dirs.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=parent, prefix=os.path.basename(path) + ".", suffix=".tmp",
    )
    os.close(fd)
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_json(obj: Any, path: str) -> None:
    def _write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
    _atomic_write(path, _write)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pickle(obj: Any, path: str) -> None:
    _atomic_write(path, lambda tmp: joblib.dump(obj, tmp))


def load_pickle(path: str) -> Any:
    return joblib.load(path)


def save_dataframe(df: pd.DataFrame, path: str) -> None:
    _atomic_write(path, lambda tmp: df.to_csv(tmp, index=False))


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
    """Boolean mask selecting the rows of ``df`` that match every group key.

    ``group_keys`` empty -> every row matches (defensive: a future caller
    that forgets to populate the list shouldn't crash on ``group_keys[0]``).
    """
    if not group_keys:
        return pd.Series(True, index=df.index)
    mask = df[group_keys[0]] == pair_keys[0]
    for i in range(1, len(group_keys)):
        mask &= df[group_keys[i]] == pair_keys[i]
    return mask
