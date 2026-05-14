"""
File-based storage backend -- reads/writes CSV and JSON from disk.

Predictions are DB-canonical
----------------------------
``test_predictions`` and ``future_forecast`` are NOT read from local
artifact files. They live in ``yf_demand_forecast`` (DB), are mirrored
into ``data/imports/demand_forecast.csv`` by the data_import service,
and this storage backend reads that mirror -- splitting by ``DataSplit``
and projecting the PascalCase wire schema onto the snake_case shape the
artifact_service expects. One source of truth, no drift between a local
training-time CSV and what the DB serves.

Other artifacts (models, metrics, explainability, training intermediates)
have no DB representation today, so they continue to live as files under
``data/forecast/`` (relocated under ``YF_DATA_ROOT``).

Atomicity
---------
Writes still go through the atomic helpers in ``src.utils.io_utils`` so
a killed process never leaves a half-written artifact. Readers tolerate
missing files (return empty frame / empty dict) but never torn files --
the writers' tmp+rename pattern guarantees the target either contains a
complete prior version or the new one.
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


# Wire mapping from the DB-canonical ``demand_forecast.csv`` (PascalCase,
# imported from yf_demand_forecast) to the snake_case schema the rest of
# the service consumes. Keys missing from the source pass through unchanged.
# The last four entries are the V5_b reconciliation outputs persisted by
# ``db_pusher`` (and refreshed daily by the reconciliation cron).
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
    # The seven reconciliation columns (recommended_load, forecast_corrected,
    # bias_pct, opening_stock, load_lower_bound, load_upper_bound,
    # leftover_to_next_day) and the envelope diagnostics
    # (recent_avg_per_selling_day, expected_demand, pattern_floor_applied,
    # pattern_ceiling_applied, forecast_below_recent) are no longer in the
    # forecast table -- they moved to ``yf_sales_transactions``. The
    # sales-transactions CSV mirror has its own (lighter) rename map; see
    # ``SALES_TRANSACTIONS_RENAME`` below.
    "PatternFloorApplied":    "pattern_floor_applied",
    "PatternCeilingApplied":  "pattern_ceiling_applied",
}

# Wire-schema contract for the sales-transactions CSV mirror
# (``yf_sales_transactions``). PascalCase on the wire, snake_case in
# pandas. This is the single source of truth: anything that reads the
# mirror or merges it into another frame imports this map; nothing
# duplicates it inline. The SQL aliases in
# ``data_import.core.queries.QueryBuilder.sales_transactions`` are the
# producer-side inverse and are validated against this map at module
# import (see ``_assert_inverse_of_sales_transactions_aliases`` below).
SALES_TRANSACTIONS_RENAME: Dict[str, str] = {
    "TrxDate":                "trx_date",
    "RouteCode":              "route_code",
    "ItemCode":               "item_code",
    "OpeningStock":           "opening_stock",
    "FreshLoad":              "fresh_load",
    "TotalVanLoad":           "total_van_load",
    "LeftoverToNextDay":      "leftover_to_next_day",
    "ActualSold":             "actual_sold",
    "BiasPct":                "bias_pct",
    "ForecastCorrected":      "forecast_corrected",
    "ExpectedDemand":         "expected_demand",
    "VanLoadLowerBound":      "van_load_lower_bound",
    "VanLoadUpperBound":      "van_load_upper_bound",
    "RecentDailyAvg":         "recent_daily_avg",
    "PatternFloorApplied":    "pattern_floor_applied",
    "PatternCeilingApplied":  "pattern_ceiling_applied",
    "ForecastBelowRecent":    "forecast_below_recent",
    "ForecastDormant":        "forecast_dormant",
    "YaumiOpeningStock":      "yaumi_opening_stock",
    "YaumiFreshLoad":         "yaumi_fresh_load",
    "YaumiTotalVanLoad":      "yaumi_total_van_load",
    "YaumiLeftover":          "yaumi_leftover",
}

# Keys served from the DB-mirror CSV instead of local artifact files.
_DB_BACKED_PREDICTION_KEYS = {"test_predictions", "future_forecast"}


# ----------------------------------------------------------------------
# Inverse-mapping property check
# ----------------------------------------------------------------------
# The data flow for yf_sales_transactions is:
#   DB (snake_case) -> SQL AS aliases (PascalCase) -> CSV
#                   -> SALES_TRANSACTIONS_RENAME (PascalCase->snake_case)
#                   -> in-memory pandas (snake_case)
# The SQL aliases must therefore be the strict inverse of
# SALES_TRANSACTIONS_RENAME. If someone adds a column on one side and
# forgets the other, the data silently turns to NaN on read. We catch
# that here at import time -- one regex sweep over the SELECT clause,
# no live DB needed.

_SQL_AS_ALIAS_RX = re.compile(r"(\w+)\s+AS\s+(\w+)", re.IGNORECASE)


def _aliases_in_select(sql: str) -> Dict[str, str]:
    """Extract ``column AS Alias`` pairs from a SELECT clause.

    Returns ``{snake_db_col: PascalCaseWireAlias}``. Handles the
    ``CAST(x AS T) AS Alias`` form too -- only the trailing AS Alias is
    kept (regex is greedy-but-line-bounded, and the right-hand-side
    of the *outer* AS is always the wire name we care about).
    """
    aliases: Dict[str, str] = {}
    # Strip ``CAST(... AS <SqlType>)`` to leave just the outer ``... AS Alias``.
    cast_rx = re.compile(r"CAST\s*\(([^()]+?)\s+AS\s+\w+(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?\s*\)",
                         re.IGNORECASE)
    cleaned = cast_rx.sub(lambda m: m.group(1), sql)
    for src, dst in _SQL_AS_ALIAS_RX.findall(cleaned):
        aliases[src] = dst
    return aliases


def _assert_inverse_of_sales_transactions_aliases() -> None:
    """Cross-check SALES_TRANSACTIONS_RENAME against the SQL producer.

    Imported lazily so this module never fails to load when data_import
    isn't on the path (e.g. unit tests of unrelated artifact reads).
    Drift is a hard error -- silent NaN columns in van-load are exactly
    what this guard exists to prevent.
    """
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
    """Reads/writes artifacts as CSV/JSON files on disk.

    For ``test_predictions`` / ``future_forecast`` the read path bypasses
    the local artifact and pulls from the DB-mirror CSV maintained by
    data_import; see module docstring for the rationale.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        # Map artifact keys -> file paths
        models_dir = Path(self._s.models_dir)
        self._paths: Dict[str, Path] = {
            # Kept for write-side back-compat (training pipeline still emits
            # these; db_pusher reads them then pushes to yf_demand_forecast).
            # READS ignore these and use the DB mirror instead.
            "test_predictions": self._s.predictions_path(self._s.test_predictions_file),
            "future_forecast": self._s.predictions_path(self._s.future_forecast_file),
            "model_metrics": self._s.metrics_path(self._s.model_metrics_file),
            "training_summary": self._s.artifact_path(self._s.training_summary_file),
            "pair_model_lookup": self._s.artifact_path(self._s.pair_model_lookup_file),
            "pair_classes": self._s.explainability_path(self._s.pair_classes_file),
            "pair_explainability": self._s.explainability_path(self._s.pair_explainability_file),
            "data_quality": self._s.artifact_path(self._s.data_quality_file),
            # Auxiliary artifacts. ``outlier_bounds.csv`` lives next to
            # the model pickles (it's a fit artifact, not a metric).
            # ``conformal_offsets`` and ``pair_coverage`` live alongside
            # the schema-versioned summary in artifacts_dir.
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
        """Read predictions from the DB-mirror CSV maintained by data_import.

        ``key`` selects the slice via ``DataSplit``:
          * ``test_predictions``  -> rows with ``DataSplit == 'Test'``
          * ``future_forecast``   -> rows with ``DataSplit == 'Forecast'``

        The wire schema (PascalCase, e.g. ``Predicted``) is mapped to the
        snake_case shape the artifact_service consumes (``prediction``).
        Missing mirror file degrades to empty frame -- callers already
        tolerate that path.
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
        # Project PascalCase wire columns onto snake_case for downstream
        # consumers. Columns not in the rename map pass through as-is so
        # PascalCase keys the rest of the pipeline still references
        # (RouteCode, ItemCode, TrxDate, ItemName) survive.
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
        # Predictions are served from the DB-mirror; their "existence"
        # is the mirror's existence, not a local artifact file.
        if key in _DB_BACKED_PREDICTION_KEYS:
            return self._predictions_mirror.exists()
        path = self._paths.get(key)
        return path is not None and path.exists()

    # ------------------------------------------------------------------
    # Source path (drives upstream mtime-keyed caches)
    # ------------------------------------------------------------------

    def source_path(self, key: str) -> Optional[Path]:
        """Return the file the read path will consult for ``key``.

        For prediction keys this is the DB-mirror CSV (the actual source
        of truth at read time), NOT the legacy local artifact file the
        write path still touches. For every other key it is the single
        artifact file registered in ``self._paths``. ``None`` for an
        unknown key so upstream cache-key code can bail out cleanly."""
        if key in _DB_BACKED_PREDICTION_KEYS:
            return self._predictions_mirror
        return self._paths.get(key)
