"""
Input validation for the raw sales feed.

Hard errors (raise ValueError):
  - Required columns missing.
  - Target column not numeric.
  - Date column completely unparseable.

Soft findings (reported, may raise depending on config):
  - Duplicate rows on ``(group_keys, date_col)``.
  - Future-dated rows.
  - Unparseable individual dates.

The function returns a :class:`ValidationReport` so the caller can merge the
numbers into the data-quality artifact without re-deriving them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd


@dataclass
class ValidationReport:
    n_rows: int = 0
    bad_dates: int = 0
    duplicate_rows: int = 0
    future_dates: int = 0
    missing_cols: list[str] = field(default_factory=list)
    missing_causal_cols: list[str] = field(default_factory=list)


def validate_input(df: pd.DataFrame, cfg: dict) -> ValidationReport:
    data = cfg["data"]
    val_cfg = cfg.get("validation", {}) or {}

    date_col = data["date_col"]
    target_col = data["target_col"]
    group_keys = list(data["forecast_level"])
    causal = data.get("causal_cols", []) or []

    report = ValidationReport(n_rows=len(df))

    # --- Structural checks (hard) -----------------------------------------
    required = group_keys + [date_col, target_col]
    report.missing_cols = [c for c in required if c not in df.columns]
    if report.missing_cols:
        raise ValueError(f"Missing required columns: {report.missing_cols}")

    if not pd.api.types.is_numeric_dtype(df[target_col]):
        raise ValueError(f"Target column '{target_col}' is not numeric")

    report.missing_causal_cols = [c["col"] for c in causal if c["col"] not in df.columns]
    if report.missing_causal_cols:
        raise ValueError(f"Missing causal columns: {report.missing_causal_cols}")

    # --- Dates -----------------------------------------------------------
    parsed = pd.to_datetime(df[date_col], errors="coerce")
    report.bad_dates = int(parsed.isna().sum())
    if report.bad_dates == len(df):
        raise ValueError(f"Date column '{date_col}' could not be parsed")

    # Compare in UTC; ``parsed`` came from ``pd.to_datetime`` which yields
    # naive timestamps when the input lacks a tz, so strip the tz from the
    # reference instant to keep the comparison apples-to-apples.
    now = datetime.now(UTC).replace(tzinfo=None)
    report.future_dates = int((parsed > now).sum())

    # --- Duplicates (soft, policy driven) --------------------------------
    dup_mask = df.duplicated(subset=group_keys + [date_col], keep=False)
    report.duplicate_rows = int(dup_mask.sum())
    if report.duplicate_rows and val_cfg.get("fail_on_duplicates", False):
        raise ValueError(
            f"{report.duplicate_rows} duplicate rows on {group_keys + [date_col]}. "
            "Set validation.fail_on_duplicates=false to auto-sum them during aggregation."
        )

    return report
