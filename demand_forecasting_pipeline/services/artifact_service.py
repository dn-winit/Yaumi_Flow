"""
Artifact service -- reads/writes pipeline artifacts through file storage with TTL cache.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.cache import TTLCache
from demand_forecasting_pipeline.services.storage.base import ARTIFACT_KEYS, StorageBackend
from demand_forecasting_pipeline.services.storage.factory import create_storage
from demand_forecasting_pipeline.src.utils.config_loader import load_config, resolve_dtypes

logger = logging.getLogger(__name__)

# Shared with the API route default; callers can override.
DEFAULT_PAGE_LIMIT = 5000


class ArtifactService:
    """Serves pipeline artifacts via cached file reads.

    Dtype coercion is driven by the pipeline YAML (same source of truth the
    training/inference pipelines use) so that API responses match the types
    emitted upstream — no silent int↔string drift between CSV and JSON.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._cache = TTLCache(default_ttl=self._s.cache_ttl_seconds)
        self._storage: StorageBackend = create_storage(self._s)

        # Resolve dtype contract + key column names from the pipeline YAML
        # once. If the YAML is missing or can't be parsed we fall back to
        # the historical column names — the API still works, it just won't
        # adapt if a future config renames things.
        try:
            cfg = load_config(self._s.pipeline_config)
            self._dtypes: Dict[str, str] = resolve_dtypes(cfg)
            data_cfg = cfg.get("data", {}) or {}
            self._group_keys: List[str] = list(data_cfg.get("forecast_level") or [])
            self._date_col: str = data_cfg.get("date_col") or ""
            self._date_cols = {self._date_col} - {""}
            self._target_col: str = data_cfg.get("target_col") or ""
        except Exception as exc:
            logger.warning("ArtifactService: could not load pipeline config: %s", exc)
            self._dtypes = {}
            self._group_keys = []
            self._date_col = ""
            self._date_cols = set()
            self._target_col = ""

    @property
    def _route_key(self) -> str:
        """First forecast-level column ('RouteCode' in the default config)."""
        return self._group_keys[0] if len(self._group_keys) >= 1 else "RouteCode"

    @property
    def _item_key(self) -> str:
        """Second forecast-level column ('ItemCode' in the default config)."""
        return self._group_keys[1] if len(self._group_keys) >= 2 else "ItemCode"

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

    def get_future_forecast_meta(self) -> tuple[int, Optional[str]]:
        """Return ``(total_rows, max_TrxDate_iso)`` from the cached future
        forecast frame without slicing or copying. Lets summary endpoints
        derive both the count and the latest forecast date in one pass --
        replaces a pair of ``get_future_forecast(limit=1)`` +
        ``get_future_forecast(limit=10_000)`` calls."""
        df = self._read_df("future_forecast")
        if df.empty:
            return 0, None
        max_date: Optional[str] = None
        if "TrxDate" in df.columns:
            mx = df["TrxDate"].max()
            if pd.notna(mx):
                max_date = str(mx)
        return int(len(df)), max_date

    def get_future_route_summary(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-route aggregates from future_forecast (tiny payload for the grid).

        If ``date`` is given, summarises that date only; otherwise collapses the
        full horizon. Returns one row per route with SKU count, total predicted
        quantity, and peak day.
        """
        df = self._read_df("future_forecast")
        rk, ik, dk = self._route_key, self._item_key, self._date_col
        if df.empty or rk not in df.columns:
            return []
        if date and dk and dk in df.columns:
            df = df[df[dk] == date]
        if df.empty:
            return []

        grouped = df.groupby(rk)
        out: List[Dict[str, Any]] = []
        for rc, g in grouped:
            out.append({
                "route_code": str(rc),
                "skus": int(g[ik].nunique()) if ik in g.columns else 0,
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

    def get_data_quality(self) -> Dict[str, Any]:
        # data_quality.json is written by the data_processing step (~2 min
        # in), long before pair_classes.csv (end of training, ~18 min).
        # Reading it lets the dashboard surface the real pair count
        # mid-run instead of a misleading zero.
        return self._cache.get_or_load(
            "data_quality",
            lambda: self._storage.read_json("data_quality"),
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
        # Authoritative path: the persisted per-pair class assignment from
        # the most recent run.
        df = self.get_pair_classes()
        if not df.empty and "class" in df.columns:
            return {"total_pairs": len(df), "classes": df["class"].value_counts().to_dict()}
        # Early-visibility fallback: data_quality.json's split_balance has
        # the surviving pair count once data_processing finishes, even
        # before classification persists pair_classes.csv. No class breakdown
        # available here, but the headline number is honest.
        dq = self.get_data_quality()
        split = ((dq.get("post_processing") or {}).get("split_balance") or {})
        total = split.get("total_pairs")
        if isinstance(total, int) and total > 0:
            return {"total_pairs": total, "classes": {}}
        return {}

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

    @property
    def cache_keys(self) -> list[str]:
        """Non-expired cache keys — exposed so the health endpoint doesn't
        have to reach into private state."""
        return self._cache.keys

    @property
    def target_col(self) -> str:
        """Target column name from the pipeline config. Lets API routes
        (e.g. summary) avoid hardcoding ``TotalQuantity``."""
        return self._target_col

    @staticmethod
    def to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
        """JSON-safe DataFrame → list-of-dicts. Replaces NaN / ±Inf with
        ``None`` so the payload survives FastAPI / browser JSON strict mode
        (which rejects ``nan``). Centralizes the conversion so every API
        route gets identical serialization behaviour.
        """
        if df is None or df.empty:
            return []
        # ``np.nan`` + ``np.inf`` both fail JSON; one pass replaces both.
        cleaned = df.replace([np.nan, np.inf, -np.inf], None)
        return cleaned.to_dict("records")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _normalize_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the wire-level type contract: every column in ``self._dtypes``
        is coerced to the declared type; every column in ``self._date_cols``
        is emitted as a ``YYYY-MM-DD`` string. Both sets come from the
        pipeline YAML so UI and DB consumers see identical types regardless
        of how the CSV was re-inferred by pandas at read time."""
        if df.empty:
            return df
        for col, dt in self._dtypes.items():
            if col in df.columns:
                # ``string`` dtype is the pandas-nullable flavour; convert to
                # Python str so downstream JSON serialisation is predictable.
                df[col] = df[col].astype(str).str.strip() if dt == "string" else df[col].astype(dt)
        for col in self._date_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    def _read_df(self, key: str) -> pd.DataFrame:
        def _load() -> pd.DataFrame:
            df = self._storage.read_dataframe(key)
            return self._normalize_types(df) if df is not None else pd.DataFrame()
        result = self._cache.get_or_load(key, _load)
        return result if result is not None else pd.DataFrame()

    def _apply_filters(self, df: pd.DataFrame, route_code: Optional[str] = None, item_code: Optional[str] = None) -> pd.DataFrame:
        if df.empty:
            return df
        rk, ik = self._route_key, self._item_key
        # No `astype(str)` needed -- the loader already normalized these columns.
        if route_code and rk in df.columns:
            df = df[df[rk] == str(route_code).strip()]
        if item_code and ik in df.columns:
            df = df[df[ik] == str(item_code).strip()]
        return df
