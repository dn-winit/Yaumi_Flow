"""File-based storage backend; CSV/JSON on disk.

Predictions are DB-canonical: test_predictions / future_forecast are read from
data/imports/demand_forecast.csv (data_import mirror of yf_demand_forecast),
split by DataSplit and projected from PascalCase to snake_case. Other artifacts
live under data/forecast/. Writes use atomic tmp+rename (src.utils.io_utils).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.storage.base import StorageBackend
from demand_forecasting_pipeline.src.utils.io_utils import (
    load_json,
    save_dataframe,
    save_json,
)

logger = logging.getLogger(__name__)


# Wire mapping from demand_forecast.csv (PascalCase) to snake_case; unmapped keys pass through.
_PREDICTIONS_RENAME: Dict[str, str] = {
    "Predicted":          "prediction",
    "DemandProbability":  "p_demand",
    "QtyIfDemand":        "qty_if_demand",
    "ActualQty":          "actual_qty",
    "LowerBound":         "lower_bound",
    "UpperBound":         "upper_bound",
    "DataSplit":          "data_split",
    "DemandClass":        "demand_class",
    "ModelUsed":          "model_used",
    "Adi":                "adi",
    "Cv2":                "cv2",
    "NonzeroRatio":       "nonzero_ratio",
    "MeanQty":            "mean_qty",
    "AvgGapDays":         "avg_gap_days",
    # Reconciliation columns + envelope diagnostics moved to yf_sales_transactions;
    # see SALES_TRANSACTIONS_RENAME for that mirror's lighter rename map.
    "PatternFloorApplied":    "pattern_floor_applied",
    "PatternCeilingApplied":  "pattern_ceiling_applied",
}

# sales-transactions CSV mirror schema; canonical map lives in ``common/``
# so consumers (recommended_order, carry_lookup) don't have to reach back
# into this service module. Re-exported here for backward compat.
from common.wire_schemas import SALES_TRANSACTIONS_RENAME  # noqa: F401

# Keys served from the DB-mirror CSV instead of local artifact files.
_DB_BACKED_PREDICTION_KEYS = {"test_predictions", "future_forecast"}


# Inverse-mapping property check: SQL AS aliases must be the strict inverse of
# SALES_TRANSACTIONS_RENAME, else added/dropped columns silently turn to NaN.
# One regex sweep over the SELECT clause at import time, no live DB needed.

_SQL_AS_ALIAS_RX = re.compile(r"(\w+)\s+AS\s+(\w+)", re.IGNORECASE)


def _aliases_in_select(sql: str) -> Dict[str, str]:
    """{snake_db_col: PascalAlias} from a SELECT; strips CAST(... AS T) wrappers."""
    aliases: Dict[str, str] = {}
    # Strip CAST(... AS T) wrappers; leaves outer "... AS Alias".
    cast_rx = re.compile(r"CAST\s*\(([^()]+?)\s+AS\s+\w+(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?\s*\)",
                         re.IGNORECASE)
    cleaned = cast_rx.sub(lambda m: m.group(1), sql)
    for src, dst in _SQL_AS_ALIAS_RX.findall(cleaned):
        aliases[src] = dst
    return aliases


def _assert_inverse_of_sales_transactions_aliases() -> None:
    """Cross-check rename map vs SQL producer; drift -> RuntimeError (lazy import for tests)."""
    try:
        from data_import.core.queries import QueryBuilder
        from data_import.config.settings import Settings as DISettings
    except Exception as exc:  # pragma: no cover - env w/o data_import
        logger.debug("Skipped sales_transactions alias check (no data_import): %s", exc)
        return
    try:
        sql, _ = QueryBuilder(DISettings()).sales_transactions(routes=["__probe__"])
    except Exception as exc:  # pragma: no cover - settings env not loaded
        logger.debug("Skipped sales_transactions alias check (settings unloaded): %s", exc)
        return
    select_only = sql.split("FROM", 1)[0]
    sql_aliases = _aliases_in_select(select_only)            # snake -> Pascal
    rename_inv = {v: k for k, v in SALES_TRANSACTIONS_RENAME.items()}  # snake -> Pascal

    missing_in_rename = set(sql_aliases) - set(rename_inv)
    missing_in_sql = set(rename_inv) - set(sql_aliases)
    mismatched = {
        k: (sql_aliases[k], rename_inv[k])
        for k in set(sql_aliases) & set(rename_inv)
        if sql_aliases[k] != rename_inv[k]
    }
    problems = []
    if missing_in_rename:
        problems.append(f"in SQL but not in SALES_TRANSACTIONS_RENAME: {sorted(missing_in_rename)}")
    if missing_in_sql:
        problems.append(f"in SALES_TRANSACTIONS_RENAME but not in SQL: {sorted(missing_in_sql)}")
    if mismatched:
        problems.append(f"PascalCase mismatch (sql vs rename): {mismatched}")
    if problems:
        raise RuntimeError(
            "SALES_TRANSACTIONS_RENAME drifted from "
            "data_import.core.queries.sales_transactions SQL aliases: "
            + "; ".join(problems)
        )


_assert_inverse_of_sales_transactions_aliases()


class FileStorage(StorageBackend):
    """Reads/writes artifacts as CSV/JSON; predictions go through the DB mirror."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        # Artifact key -> file paths
        models_dir = Path(self._s.models_dir)
        self._paths: Dict[str, Path] = {
            # Write-side back-compat for predictions; reads use the DB mirror.
            "test_predictions": self._s.predictions_path(self._s.test_predictions_file),
            "future_forecast": self._s.predictions_path(self._s.future_forecast_file),
            "model_metrics": self._s.metrics_path(self._s.model_metrics_file),
            "training_summary": self._s.artifact_path(self._s.training_summary_file),
            "pair_model_lookup": self._s.artifact_path(self._s.pair_model_lookup_file),
            "pair_classes": self._s.explainability_path(self._s.pair_classes_file),
            "pair_explainability": self._s.explainability_path(self._s.pair_explainability_file),
            "data_quality": self._s.artifact_path(self._s.data_quality_file),
            # outlier_bounds.csv is a fit artifact (next to model pickles); the rest in artifacts_dir.
            "outlier_bounds": models_dir / self._s.outlier_bounds_file,
            "conformal_offsets": self._s.artifact_path(self._s.conformal_offsets_file),
            "pair_coverage": self._s.artifact_path(self._s.pair_coverage_file),
            "target_encoding": self._s.artifact_path(self._s.target_encoding_file),
        }
        # Keys stored as JSON (not CSV)
        self._json_keys = {"training_summary", "data_quality", "target_encoding"}
        # DB-mirror source for predictions (data_import maintains it).
        self._predictions_mirror: Path = self._s.shared_data_path(self._s.demand_forecast_file)

    @property
    def name(self) -> str:
        return "file"

    # ------------------------------------------------------------------
    # DataFrame
    # ------------------------------------------------------------------

    def read_dataframe(self, key: str) -> pd.DataFrame:
        if key in _DB_BACKED_PREDICTION_KEYS:
            return self._read_predictions_from_mirror(key)
        path = self._paths.get(key)
        if not path or not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path, low_memory=False)
        except Exception as exc:
            logger.error("Failed to read %s from %s: %s", key, path, exc)
            return pd.DataFrame()

    def _read_predictions_from_mirror(self, key: str) -> pd.DataFrame:
        """Read demand_forecast.csv (DB mirror); ``key`` selects DataSplit Test/Forecast.

        PascalCase wire columns projected to snake_case; missing file -> empty frame.
        """
        path = self._predictions_mirror
        if not path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            logger.error("Failed to read predictions mirror %s: %s", path, exc)
            return pd.DataFrame()
        if df.empty:
            return df
        if "DataSplit" in df.columns:
            wanted = "Test" if key == "test_predictions" else "Forecast"
            df = df[df["DataSplit"].astype(str).str.strip().str.lower() == wanted.lower()]
            if df.empty:
                return df
        # PascalCase -> snake_case projection; unmapped columns (RouteCode, ItemCode, TrxDate, ItemName) survive.
        present = {src: dst for src, dst in _PREDICTIONS_RENAME.items() if src in df.columns}
        if present:
            df = df.rename(columns=present)
        return df.reset_index(drop=True)

    def write_dataframe(self, key: str, df: pd.DataFrame) -> int:
        path = self._paths.get(key)
        if not path:
            logger.error("Unknown artifact key: %s", key)
            return 0
        save_dataframe(df, str(path))
        logger.info("Wrote %d rows to %s", len(df), path)
        return len(df)

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    def read_json(self, key: str) -> Dict[str, Any]:
        path = self._paths.get(key)
        if not path or not path.exists():
            return {}
        try:
            return load_json(str(path))
        except Exception as exc:
            logger.error("Failed to read JSON %s from %s: %s", key, path, exc)
            return {}

    def write_json(self, key: str, data: Dict[str, Any]) -> bool:
        path = self._paths.get(key)
        if not path:
            return False
        save_json(data, str(path))
        logger.info("Wrote JSON to %s", path)
        return True

    # ------------------------------------------------------------------
    # Existence
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        # Predictions: existence == mirror existence, not local artifact.
        if key in _DB_BACKED_PREDICTION_KEYS:
            return self._predictions_mirror.exists()
        path = self._paths.get(key)
        return path is not None and path.exists()

    # ------------------------------------------------------------------
    # Source path (drives upstream mtime-keyed caches)
    # ------------------------------------------------------------------

    def source_path(self, key: str) -> Optional[Path]:
        """File the read path will consult; DB-mirror CSV for predictions, registered path else."""
        if key in _DB_BACKED_PREDICTION_KEYS:
            return self._predictions_mirror
        return self._paths.get(key)
