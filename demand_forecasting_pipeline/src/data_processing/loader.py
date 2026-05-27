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
    allow_empty: bool = False,
) -> pd.DataFrame:
    """Read a CSV, parse ``date_col``, drop rows with unparseable dates.

    Raises ``ValueError`` if the resulting frame is empty unless
    ``allow_empty=True``. An empty source is almost always a data_import
    failure (DB connectivity issue, route filter too narrow, date range
    outside the data window). Failing here surfaces the issue at the
    earliest, most actionable point rather than letting the pipeline
    crash deep in feature engineering with a cryptic "no data" error.
    """
    df = pd.read_csv(path, low_memory=low_memory, dtype=dtypes or None)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).reset_index(drop=True)
    if df.empty and not allow_empty:
        raise ValueError(
            f"Source {str(path)!r} contained no rows with a parseable "
            f"{date_col!r}. This is almost always an upstream data_import "
            f"failure - check the import status endpoint, the route/item "
            f"filter, and the data date range. Pass allow_empty=True only "
            f"when an empty source is a legitimate state (e.g. unit tests)."
        )
    return df
