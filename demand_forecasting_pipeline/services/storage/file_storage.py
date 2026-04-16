"""
File-based storage backend -- reads/writes CSV and JSON from disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class FileStorage(StorageBackend):
    """Reads/writes artifacts as CSV/JSON files on disk."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        # Map artifact keys -> file paths
        self._paths: Dict[str, Path] = {
            "test_predictions": self._s.predictions_path(self._s.test_predictions_file),
            "future_forecast": self._s.predictions_path(self._s.future_forecast_file),
            "model_metrics": self._s.metrics_path(self._s.model_metrics_file),
            "training_summary": self._s.artifact_path(self._s.training_summary_file),
            "pair_model_lookup": self._s.artifact_path(self._s.pair_model_lookup_file),
            "pair_classes": self._s.explainability_path(self._s.pair_classes_file),
            "pair_explainability": self._s.explainability_path(self._s.pair_explainability_file),
        }
        # Keys stored as JSON (not CSV)
        self._json_keys = {"training_summary"}

    @property
    def name(self) -> str:
        return "file"

    # ------------------------------------------------------------------
    # DataFrame
    # ------------------------------------------------------------------

    def read_dataframe(self, key: str) -> pd.DataFrame:
        path = self._paths.get(key)
        if not path or not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path, low_memory=False)
        except Exception as exc:
            logger.error("Failed to read %s from %s: %s", key, path, exc)
            return pd.DataFrame()

    def write_dataframe(self, key: str, df: pd.DataFrame) -> int:
        path = self._paths.get(key)
        if not path:
            logger.error("Unknown artifact key: %s", key)
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
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
            with open(path, "r") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("Failed to read JSON %s from %s: %s", key, path, exc)
            return {}

    def write_json(self, key: str, data: Dict[str, Any]) -> bool:
        path = self._paths.get(key)
        if not path:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.info("Wrote JSON to %s", path)
        return True

    # ------------------------------------------------------------------
    # Existence
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        path = self._paths.get(key)
        return path is not None and path.exists()
