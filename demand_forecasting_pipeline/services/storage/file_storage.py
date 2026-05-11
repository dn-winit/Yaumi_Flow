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
    "RecommendedLoad":    "recommended_load",
    "ForecastCorrected":  "forecast_corrected",
    "BiasPct":            "bias_pct",
    "OpeningStock":       "opening_stock",
    "LoadLowerBound":     "load_lower_bound",
    "LoadUpperBound":     "load_upper_bound",
}

# Keys served from the DB-mirror CSV instead of local artifact files.
_DB_BACKED_PREDICTION_KEYS = {"test_predictions", "future_forecast"}


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
