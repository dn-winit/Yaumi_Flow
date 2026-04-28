"""
Data-quality report — one artifact per training run.

The report captures:

  - ``input``            raw-feed stats: row count, %zeros, %negatives, target
                         total, date range, unique pairs, sha256 of the file.
  - ``validation``       findings from :class:`ValidationReport`.
  - ``post_processing``  panel stats after aggregation and inactivity filter.
  - ``flags``            per-flag counts (rows + distinct pairs affected).

It is the single place a run's data-quality signal is consolidated, so the
API / dashboard / drift check can read it without re-deriving anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .validator import ValidationReport

_FLAG_COLUMNS = ("suspicious_zero", "is_new_launch", "is_likely_eol")


@dataclass
class DataQualityReport:
    generated_at: str
    input: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    post_processing: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _file_sha256(path: str | Path) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _target_stats(df: pd.DataFrame, target_col: str) -> dict[str, Any]:
    n = len(df)
    if n == 0 or target_col not in df.columns:
        return {"rows": int(n), "zero_pct": 0.0, "negative_pct": 0.0, "total": 0.0}
    tgt = pd.to_numeric(df[target_col], errors="coerce")
    return {
        "rows": int(n),
        "zero_pct": round(float((tgt == 0).mean() * 100), 2),
        "negative_pct": round(float((tgt < 0).mean() * 100), 2),
        "total": round(float(tgt.sum()), 2),
    }


def _date_range(df: pd.DataFrame, date_col: str) -> tuple[str, str] | None:
    if date_col not in df.columns or len(df) == 0:
        return None
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if dates.empty:
        return None
    return (dates.min().date().isoformat(), dates.max().date().isoformat())


def _n_pairs(df: pd.DataFrame, group_keys: list[str]) -> int:
    if not all(c in df.columns for c in group_keys) or len(df) == 0:
        return 0
    return int(df.groupby(group_keys).ngroups)


def build_report(
    *,
    raw: pd.DataFrame,
    panel: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    date_col: str,
    validation: ValidationReport,
    raw_path: str | Path,
    pairs_dropped_inactive: int,
) -> DataQualityReport:
    flags: dict[str, Any] = {}
    for col in _FLAG_COLUMNS:
        if col in panel.columns:
            on = panel[col].astype(bool)
            flags[f"{col}_rows"] = int(on.sum())
            flags[f"{col}_pairs"] = (
                int(panel.loc[on].groupby(group_keys).ngroups) if on.any() else 0
            )

    return DataQualityReport(
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        input={
            **_target_stats(raw, target_col),
            "unique_pairs": _n_pairs(raw, group_keys),
            "date_range": _date_range(raw, date_col),
            "sha256": _file_sha256(raw_path),
            "path": str(raw_path),
        },
        validation=asdict(validation),
        post_processing={
            **_target_stats(panel, target_col),
            "unique_pairs": _n_pairs(panel, group_keys),
            "pairs_dropped_inactive": int(pairs_dropped_inactive),
        },
        flags=flags,
    )
