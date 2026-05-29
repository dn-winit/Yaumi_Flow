"""Abstract storage interface; file and DB backends implement this contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pandas as pd

# Canonical artifact keys used throughout the system
ARTIFACT_KEYS = (
    "test_predictions",
    "future_forecast",
    "model_metrics",
    "training_summary",
    "pair_model_lookup",
    "pair_classes",
    "pair_explainability",
    "data_quality",
    # Auxiliary artifacts; registering here routes them through atomic write/read + health.
    "outlier_bounds",
    "conformal_offsets",
    "pair_coverage",
    "target_encoding",
)


class StorageBackend(ABC):
    """Interface for reading/writing pipeline artifacts."""

    # ------------------------------------------------------------------
    # DataFrame artifacts (predictions, metrics, explainability)
    # ------------------------------------------------------------------

    @abstractmethod
    def read_dataframe(self, key: str) -> pd.DataFrame:
        """Read a tabular artifact by key. Return empty DataFrame if missing."""

    @abstractmethod
    def write_dataframe(self, key: str, df: pd.DataFrame) -> int:
        """Write a tabular artifact. Return rows written."""

    # ------------------------------------------------------------------
    # JSON artifacts (training summary)
    # ------------------------------------------------------------------

    @abstractmethod
    def read_json(self, key: str) -> dict[str, Any]:
        """Read a JSON artifact by key. Return empty dict if missing."""

    @abstractmethod
    def write_json(self, key: str, data: dict[str, Any]) -> bool:
        """Write a JSON artifact. Return success."""

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if an artifact exists."""

    def check_all(self) -> dict[str, bool]:
        """Check existence of all known artifacts."""
        return {k: self.exists(k) for k in ARTIFACT_KEYS}

    # ------------------------------------------------------------------
    # Source path (for mtime-keyed caching upstream)
    # ------------------------------------------------------------------

    @abstractmethod
    def source_path(self, key: str) -> Path | None:
        """On-disk path for ``key`` so callers can mtime-cache; None for unknown keys."""

    # ------------------------------------------------------------------
    # Name
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name for logging."""
