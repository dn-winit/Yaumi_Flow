"""
Abstract storage interface.
Both file and database backends implement this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

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
    def read_json(self, key: str) -> Dict[str, Any]:
        """Read a JSON artifact by key. Return empty dict if missing."""

    @abstractmethod
    def write_json(self, key: str, data: Dict[str, Any]) -> bool:
        """Write a JSON artifact. Return success."""

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if an artifact exists."""

    def check_all(self) -> Dict[str, bool]:
        """Check existence of all known artifacts."""
        return {k: self.exists(k) for k in ARTIFACT_KEYS}

    # ------------------------------------------------------------------
    # Name
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name for logging."""
