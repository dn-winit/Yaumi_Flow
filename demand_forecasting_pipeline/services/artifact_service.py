"""
Artifact service -- reads/writes pipeline artifacts through file storage
with an mtime-keyed cache.

Caching contract
----------------
Every artifact this service vends is backed by an on-disk file managed
by the storage layer. The cache is keyed on
``(path, st_mtime_ns, st_size)`` -- one ``stat()`` per request, no TTL.
The moment the daily reconciliation cron rewrites the DB mirror or any
other artifact, the next read here observes the new ``(mtime, size)``
tuple and re-parses the file. There is no stale window -- not 5 minutes,
not 5 seconds. The freshness signal is the filesystem itself, the same
pattern used by ``VanLoadService._load_csv``, ``BiasService``, and the
enrich-side ``_recent_stats_per_selling_day_index`` /
``_concentrated_buyers_index`` / ``_journey_index`` helpers.

Dtype coercion is driven by the pipeline YAML (same source of truth the
training/inference pipelines use) so that API responses match the types
emitted upstream -- no silent int<->string drift between CSV and JSON.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.storage.base import ARTIFACT_KEYS, StorageBackend
from demand_forecasting_pipeline.services.storage.factory import create_storage
from demand_forecasting_pipeline.src.utils.config_loader import load_config, resolve_dtypes

logger = logging.getLogger(__name__)

# Cache key shape used everywhere: identifies a unique on-disk file
# snapshot. ``mtime_ns`` flips on every write (atomic tmp+rename
# preserves this); ``size`` is a cheap second discriminator that catches
# the rare case where two atomic rewrites land in the same nanosecond
# with different content. Both come from a single ``stat()`` call so
# the per-request overhead is one syscall.
_CacheKey = Tuple[str, int, int]


class ArtifactService:
    """Serves pipeline artifacts via mtime-keyed file reads.

    The cache is a plain dict guarded by a ``threading.Lock``; entries
    are keyed by an opaque service-level name (``test_predictions``,
    ``future_forecast``, ``training_summary``, ...) and store
    ``(file_snapshot_key, payload)``. A read is a ``stat()`` + dict
    lookup on the hot path; the file is only re-parsed when the
    snapshot key changes. No TTL -- correctness comes from observing
    the filesystem, not from a clock.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._storage: StorageBackend = create_storage(self._s)
        # Two parallel maps so a DataFrame artifact and a JSON artifact
        # can share a key (unlikely in practice, defensive). Same lock
        # guards both -- contention is negligible because the hot path
        # is a dict lookup.
        self._df_cache: Dict[str, Tuple[_CacheKey, pd.DataFrame]] = {}
        self._json_cache: Dict[str, Tuple[_CacheKey, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

        # Resolve dtype contract + key column names from the pipeline YAML
        # once. If the YAML is missing or can't be parsed we fall back to
        # the historical column names - the API still works, it just won't
        # adapt if a future config renames things.
        try:
            cfg = load_config(self._s.pipeline_config)
            self._dtypes: dict[str, str] = resolve_dtypes(cfg)
            data_cfg = cfg.get("data", {}) or {}
            self._group_keys: list[str] = list(data_cfg.get("forecast_level") or [])
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
    # Test-predictions schema resolvers
    #
    # Two scorers (drift baseline + summary KPI) read the same
    # test_predictions.csv and need the same column-name discipline.
    # Centralised here so a future artifact-schema rename only has to
    # update one resolver, not every caller. Returns None when the
    # column is absent so callers can render an em-dash instead of
    # crashing or silently scoring against a zero column.
    # ------------------------------------------------------------------

    def resolve_actual_column(self, df: pd.DataFrame) -> Optional[str]:
        """Pick the column carrying realised quantity in test_predictions.

        Tries the configured ``target_col`` first (legacy artifacts), then
        the canonical ``actual_qty`` the current pipeline writes. Same
        precedence the prior inline implementations used in summary.py
        and retrain_scheduler.py."""
        configured = (self._target_col or "").strip()
        for cand in (configured, "actual_qty"):
            if cand and cand in df.columns:
                return cand
        return None

    def resolve_prediction_column(self, df: pd.DataFrame) -> Optional[str]:
        """Pick the column carrying the model's point forecast.

        ``prediction`` is the canonical name the current pipeline writes;
        ``predicted`` is kept as a fallback for legacy snapshots so an
        older test_predictions.csv still scores rather than silently
        evaluating to zero."""
        for cand in ("prediction", "predicted"):
            if cand in df.columns:
                return cand
        return None

    @property
    def composite_accuracy_kwargs(self) -> dict:
        """Composite-WAPE kwargs (per-class tolerances) read from the same
        pipeline YAML training used. Returns an empty dict if the config
        is missing/invalid -- callers fall through to module defaults.

        Cached at the metrics-module level (one parse per YAML path),
        so this property is effectively free to re-read."""
        from demand_forecasting_pipeline.src.evaluation.metrics import (
            composite_kwargs_from_yaml,
        )
        return composite_kwargs_from_yaml(self._s.pipeline_config)

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def get_test_predictions(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        df = self._read_df("test_predictions")
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        eff_limit = int(limit if limit is not None else self._s.default_page_limit)
        return df.iloc[offset : offset + eff_limit], total

    def van_load_view(self) -> pd.DataFrame:
        """Unified "what's on the van" frame -- the single source of truth
        every operational consumer reads (VanLoad page, route-summary tile,
        recommendation engine).

        Concatenates Forecast + Test splits then de-dupes by
        (RouteCode, ItemCode, TrxDate) preferring the Forecast row when
        both exist (it's the freshest output, regenerated by every
        inference run). Filters ``prediction > 0`` because rows with
        zero prediction mean "model said skip" -- counting them as van
        load would be misleading on the tile and the table.

        Why both splits? A date inside the test horizon (e.g. today)
        gets predictions in BOTH artifacts but for *different* (route,
        item) pairs -- the model assigns each pair to exactly one split
        per inference run. The recommendation engine already unions them
        upstream (data_manager.get_van_items); doing the same here keeps
        VanLoad's tile, table, and recommendation count aligned.
        """
        forecast = self._read_df("future_forecast")
        test = self._read_df("test_predictions")
        if forecast.empty and test.empty:
            return pd.DataFrame()
        if forecast.empty:
            df = test.copy()
        elif test.empty:
            df = forecast.copy()
        else:
            common = sorted(set(forecast.columns) & set(test.columns))
            df = pd.concat(
                [forecast[common].assign(_split="Forecast"),
                 test[common].assign(_split="Test")],
                ignore_index=True,
            )
            df = (
                df.sort_values("_split")
                .drop_duplicates(["RouteCode", "ItemCode", "TrxDate"], keep="first")
                .drop(columns="_split")
                .reset_index(drop=True)
            )
        if "prediction" in df.columns:
            df = df[pd.to_numeric(df["prediction"], errors="coerce").fillna(0) > 0]
        return df

    def get_future_forecast(
        self,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        df = self.van_load_view()
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        eff_limit = int(limit if limit is not None else self._s.default_page_limit)
        return df.iloc[offset : offset + eff_limit], total

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

    def get_training_summary(self) -> dict[str, Any]:
        return self._read_json("training_summary")

    def get_data_quality(self) -> dict[str, Any]:
        # data_quality.json is written by the data_processing step (~2 min
        # in), long before pair_classes.csv (end of training, ~18 min).
        # Reading it lets the dashboard surface the real pair count
        # mid-run instead of a misleading zero.
        return self._read_json("data_quality")

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

    def list_model_files(self) -> list[dict[str, Any]]:
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

    def get_class_summary(self) -> dict[str, Any]:
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

    def check_artifacts(self) -> dict[str, bool]:
        return {k: self._storage.exists(k) for k in ARTIFACT_KEYS}

    # ------------------------------------------------------------------
    # Write (for pipeline output saving)
    # ------------------------------------------------------------------

    def write_df(self, key: str, df: pd.DataFrame) -> int:
        # No explicit invalidation: the next read will see the new
        # mtime/size and re-parse automatically. The cached entry
        # (if any) is replaced lazily on that next read.
        return self._storage.write_dataframe(key, df)

    def write_json(self, key: str, data: dict[str, Any]) -> bool:
        # Same lazy mtime-driven invalidation as ``write_df``.
        return self._storage.write_json(key, data)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Drop every cached entry. The mtime-keyed cache already
        re-parses on file change, so this is purely a memory hygiene
        hook for callers that know they want a hard reset (e.g. the
        pipeline-completed hook in ``PipelineService``). Cheap; safe to
        call from any thread."""
        with self._cache_lock:
            self._df_cache.clear()
            self._json_cache.clear()

    @property
    def cache_keys(self) -> list[str]:
        """Currently-cached artifact keys -- exposed so the health
        endpoint doesn't have to reach into private state."""
        with self._cache_lock:
            return sorted(set(self._df_cache) | set(self._json_cache))

    @property
    def target_col(self) -> str:
        """Target column name from the pipeline config. Lets API routes
        (e.g. summary) avoid hardcoding ``TotalQuantity``."""
        return self._target_col

    @staticmethod
    def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
        """JSON-safe DataFrame -> list-of-dicts. Replaces NaN / +/-Inf with
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

    def _file_snapshot_key(self, key: str) -> Optional[_CacheKey]:
        """Build the ``(path, mtime_ns, size)`` snapshot tuple for the
        on-disk file backing ``key``. ``None`` when the storage layer
        has no path for the key OR the file is missing -- both cases
        skip caching so a transient missing file doesn't pin an empty
        frame across a later write."""
        path = self._storage.source_path(key)
        if path is None or not path.exists():
            return None
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)

    def _read_df(self, key: str) -> pd.DataFrame:
        """Mtime-keyed DataFrame read.

        Hot path: one ``stat()`` + one dict lookup. Cold path (file
        changed or first read): re-parse via the storage backend and
        apply the YAML-driven dtype contract. Lock window is tight --
        only the dict mutation is guarded, the parse runs lock-free."""
        snapshot = self._file_snapshot_key(key)
        if snapshot is not None:
            with self._cache_lock:
                cached = self._df_cache.get(key)
                if cached is not None and cached[0] == snapshot:
                    return cached[1]
        df = self._storage.read_dataframe(key)
        df = self._normalize_types(df) if df is not None else pd.DataFrame()
        if snapshot is not None:
            with self._cache_lock:
                self._df_cache[key] = (snapshot, df)
        return df

    def _read_json(self, key: str) -> dict[str, Any]:
        """Mtime-keyed JSON read. Same contract as ``_read_df`` -- one
        stat + dict lookup on the hot path; re-parse on file change."""
        snapshot = self._file_snapshot_key(key)
        if snapshot is not None:
            with self._cache_lock:
                cached = self._json_cache.get(key)
                if cached is not None and cached[0] == snapshot:
                    return cached[1]
        data = self._storage.read_json(key) or {}
        if snapshot is not None:
            with self._cache_lock:
                self._json_cache[key] = (snapshot, data)
        return data

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
