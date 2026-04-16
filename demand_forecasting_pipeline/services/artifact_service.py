"""
Artifact service -- reads/writes pipeline artifacts through file storage with TTL cache.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.cache import TTLCache
from demand_forecasting_pipeline.services.storage.base import ARTIFACT_KEYS
from demand_forecasting_pipeline.services.storage.factory import create_storage
from demand_forecasting_pipeline.services.storage.file_storage import FileStorage

logger = logging.getLogger(__name__)

# Shared with the API route default; callers can override.
DEFAULT_PAGE_LIMIT = 5000


class ArtifactService:
    """Serves pipeline artifacts via cached file reads."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._cache = TTLCache(default_ttl=self._s.cache_ttl_seconds)
        self._storage: FileStorage = create_storage(self._s)

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def get_test_predictions(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        df = self._read_df("test_predictions")
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        return df.iloc[offset : offset + limit], total

    def get_future_forecast(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        df = self._read_df("future_forecast")
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        return df.iloc[offset : offset + limit], total

    def get_future_route_summary(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-route aggregates from future_forecast (tiny payload for the grid).

        If ``date`` is given, summarises that date only; otherwise collapses the
        full horizon. Returns one row per route with SKU count, total predicted
        quantity, and peak day.
        """
        df = self._read_df("future_forecast")
        if df.empty or "RouteCode" not in df.columns:
            return []
        if date and "TrxDate" in df.columns:
            df = df[df["TrxDate"] == date]
        if df.empty:
            return []

        grouped = df.groupby("RouteCode")
        out: List[Dict[str, Any]] = []
        for rc, g in grouped:
            out.append({
                "route_code": str(rc),
                "skus": int(g["ItemCode"].nunique()) if "ItemCode" in g.columns else 0,
                "predicted_qty": round(float(g["prediction"].sum()), 1) if "prediction" in g.columns else 0.0,
            })
        out.sort(key=lambda r: r["route_code"])
        return out

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_model_metrics(self, demand_class: Optional[str] = None) -> pd.DataFrame:
        df = self._read_df("model_metrics")
        if demand_class and "class" in df.columns:
            df = df[df["class"] == demand_class]
        return df

    # ------------------------------------------------------------------
    # Training summary
    # ------------------------------------------------------------------

    def get_training_summary(self) -> Dict[str, Any]:
        return self._cache.get_or_load(
            "training_summary",
            lambda: self._storage.read_json("training_summary"),
        ) or {}

    # ------------------------------------------------------------------
    # Pair model lookup
    # ------------------------------------------------------------------

    def get_pair_model_lookup(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
    ) -> pd.DataFrame:
        df = self._read_df("pair_model_lookup")
        return self._apply_filters(df, route_code=route_code, item_code=item_code)

    # ------------------------------------------------------------------
    # Explainability
    # ------------------------------------------------------------------

    def get_pair_classes(self, demand_class: Optional[str] = None) -> pd.DataFrame:
        df = self._read_df("pair_classes")
        if demand_class and "class" in df.columns:
            df = df[df["class"] == demand_class]
        return df

    def get_pair_explainability(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        demand_class: Optional[str] = None,
    ) -> pd.DataFrame:
        df = self._read_df("pair_explainability")
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        if demand_class and "class" in df.columns:
            df = df[df["class"] == demand_class]
        return df

    # ------------------------------------------------------------------
    # Model files (always from disk)
    # ------------------------------------------------------------------

    def list_model_files(self) -> List[Dict[str, Any]]:
        models_dir = Path(self._s.models_dir)
        if not models_dir.exists():
            return []
        return [
            {
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "type": "weights" if f.suffix == ".json" else "model",
            }
            for f in sorted(models_dir.iterdir())
            if f.suffix in (".pkl", ".json")
        ]

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def get_class_summary(self) -> Dict[str, Any]:
        df = self.get_pair_classes()
        if df.empty or "class" not in df.columns:
            return {}
        return {"total_pairs": len(df), "classes": df["class"].value_counts().to_dict()}

    # ------------------------------------------------------------------
    # Artifact checks
    # ------------------------------------------------------------------

    def check_artifacts(self) -> Dict[str, bool]:
        return {k: self._storage.exists(k) for k in ARTIFACT_KEYS}

    # ------------------------------------------------------------------
    # Write (for pipeline output saving)
    # ------------------------------------------------------------------

    def write_df(self, key: str, df: pd.DataFrame) -> int:
        rows = self._storage.write_dataframe(key, df)
        self._cache.invalidate(key)
        return rows

    def write_json(self, key: str, data: Dict[str, Any]) -> bool:
        ok = self._storage.write_json(key, data)
        self._cache.invalidate(key)
        return ok

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    # Wire-level type contract (single source of truth for the API):
    #   * Code columns are emitted as strings (no int route codes leaking out).
    #   * Date columns are emitted as YYYY-MM-DD strings.
    # Doing this once in the loader means every endpoint, every consumer, sees
    # consistent types -- no per-row coercion in the UI.
    _STRING_COLS = ("RouteCode", "ItemCode", "CustomerCode")
    _DATE_COLS = ("TrxDate", "trx_date", "JourneyDate", "ds", "date")

    def _normalize_types(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        for col in self._STRING_COLS:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip()
        for col in self._DATE_COLS:
            if col in df.columns:
                # Convert to YYYY-MM-DD string. Already-string values pass through.
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    def _read_df(self, key: str) -> pd.DataFrame:
        def _load() -> pd.DataFrame:
            df = self._storage.read_dataframe(key)
            return self._normalize_types(df) if df is not None else pd.DataFrame()
        result = self._cache.get_or_load(key, _load)
        return result if result is not None else pd.DataFrame()

    @staticmethod
    def _apply_filters(df: pd.DataFrame, route_code: Optional[str] = None, item_code: Optional[str] = None) -> pd.DataFrame:
        if df.empty:
            return df
        # No `astype(str)` needed -- the loader already normalized these columns.
        if route_code and "RouteCode" in df.columns:
            df = df[df["RouteCode"] == str(route_code).strip()]
        if item_code and "ItemCode" in df.columns:
            df = df[df["ItemCode"] == str(item_code).strip()]
        return df
