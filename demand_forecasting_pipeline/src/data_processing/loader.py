"""
Raw data loader.

Accepts explicit dtype hints so ID columns such as route/item codes stay as
strings (preserves leading zeros, makes joins predictable). If no hints are
given, pandas infers - acceptable for dev but not recommended for prod.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_raw(
    path: str | Path,
    date_col: str,
    *,
    dtypes: dict[str, str] | None = None,
    low_memory: bool = False,
) -> pd.DataFrame:
    """Read a CSV, parse ``date_col``, drop rows with unparseable dates."""
    df = pd.read_csv(path, low_memory=low_memory, dtype=dtypes or None)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    return df.dropna(subset=[date_col]).reset_index(drop=True)
