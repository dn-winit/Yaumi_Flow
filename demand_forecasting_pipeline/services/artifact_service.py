"""Artifact service: mtime-keyed reads through file storage; no TTL.

Cache key is (path, st_mtime_ns, st_size) -- one stat() per request. Atomic
tmp+rename writes upstream make the read observe either prior or new file fully.
Dtype coercion driven by pipeline YAML (same source as training/inference).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.reconciliation.enrich import activity_mask
from demand_forecasting_pipeline.services.storage.base import ARTIFACT_KEYS, StorageBackend
from demand_forecasting_pipeline.services.storage.factory import create_storage
from demand_forecasting_pipeline.services.storage.file_storage import (
    SALES_TRANSACTIONS_RENAME,
)
from demand_forecasting_pipeline.src.utils.config_loader import load_config, resolve_dtypes

logger = logging.getLogger(__name__)

# Cache key: (path, mtime_ns, size); one stat() per request.
_CacheKey = tuple[str, int, int]


class ArtifactService:
    """Serves pipeline artifacts via mtime-keyed file reads; no TTL."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._storage: StorageBackend = create_storage(self._s)
        # Parallel DF/JSON maps so the same key in each surface stays isolated.
        self._df_cache: dict[str, tuple[_CacheKey, pd.DataFrame]] = {}
        self._json_cache: dict[str, tuple[_CacheKey, dict[str, Any]]] = {}
        # Composite-key cache for derived frames spanning multiple on-disk files.
        self._derived_cache: dict[str, tuple[tuple[_CacheKey | None, ...], pd.DataFrame]] = {}
        # Cache invariants: atomic tmp+rename upstream + (path, mtime_ns, size) key;
        # _cache_lock serialises concurrent miss-path readers on parse.
        self._sales_tx_cache: tuple[_CacheKey, pd.DataFrame] | None = None
        self._cache_lock = threading.Lock()

        # Dtype contract + key column names from pipeline YAML; falls back to historical defaults.
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

    # Test-predictions schema resolvers (shared by drift + summary KPI scorers).

    def resolve_actual_column(self, df: pd.DataFrame) -> str | None:
        """Realised-quantity column; configured target_col first, then ``actual_qty``."""
        configured = (self._target_col or "").strip()
        for cand in (configured, "actual_qty"):
            if cand and cand in df.columns:
                return cand
        return None

    def resolve_prediction_column(self, df: pd.DataFrame) -> str | None:
        """``prediction`` (canonical) or legacy ``predicted``; None if absent."""
        for cand in ("prediction", "predicted"):
            if cand in df.columns:
                return cand
        return None

    @property
    def composite_accuracy_kwargs(self) -> dict:
        """Composite-WAPE per-class tolerance kwargs from training YAML; empty dict on miss."""
        from demand_forecasting_pipeline.src.evaluation.metrics import (
            composite_kwargs_from_yaml,
        )
        return composite_kwargs_from_yaml(self._s.pipeline_config)

    def score_test_predictions(
        self, *, recent_window_days: int | None = None,
    ) -> tuple[float | None, int]:
        """Score test_predictions; single source of truth for Pipeline-summary + drift.

        ``recent_window_days`` (positive int) restricts to N days from max; None = full.
        Returns (accuracy_pct, rows_compared) or (None, 0) if unscoreable.
        """
        if recent_window_days is not None and recent_window_days < 1:
            raise ValueError(
                f"recent_window_days must be a positive int or None; got {recent_window_days!r}"
            )
        from demand_forecasting_pipeline.src.evaluation.metrics import (
            composite_summary,
            resolve_class_array,
        )
        try:
            test_df, _total = self.get_test_predictions(limit=None, offset=0)
        except Exception as exc:
            logger.warning("score_test_predictions: fetch failed: %s", exc)
            return None, 0
        if test_df.empty:
            return None, 0
        pred_col = self.resolve_prediction_column(test_df)
        actual_col = self.resolve_actual_column(test_df)
        if pred_col is None or actual_col is None:
            return None, 0
        if recent_window_days is not None and "TrxDate" in test_df.columns:
            dates = pd.to_datetime(test_df["TrxDate"], errors="coerce")
            max_date = dates.max()
            if pd.notna(max_date):
                test_df = test_df[dates >= (max_date - pd.Timedelta(days=recent_window_days))]
            if test_df.empty:
                return None, 0
        actual = pd.to_numeric(test_df[actual_col], errors="coerce").fillna(0).to_numpy()
        predicted = pd.to_numeric(test_df[pred_col], errors="coerce").fillna(0).to_numpy()
        cls = resolve_class_array(test_df)
        stats = composite_summary(actual, predicted, cls, **self.composite_accuracy_kwargs)
        rows_compared = int(stats.get("rows_compared", 0))
        if rows_compared <= 0:
            return None, 0
        return float(stats["accuracy_pct"]), rows_compared

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def get_test_predictions(
        self,
        route_code: str | None = None,
        item_code: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        """Filtered test predictions; (DataFrame, total). limit=None means no cap (positive int else)."""
        if limit is not None and limit < 1:
            raise ValueError(
                f"limit must be a positive int or None (got {limit!r})"
            )
        df = self._read_df("test_predictions")
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        if limit is None:
            return df.iloc[offset:], total
        return df.iloc[offset : offset + int(limit)], total

    def _van_load_unfiltered(self) -> pd.DataFrame:
        """Forecast + Test deduped with sales_transactions LEFT JOIN; NO activity_mask
        so future rows survive for van_load_view_enriched to engine-enrich."""
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
        return self._merge_sales_transactions(df)

    def van_load_view(self) -> pd.DataFrame:
        """Unified "what's on the van" frame; single source of truth for VanLoad surfaces.

        Forecast + Test deduped (Forecast wins). Carry-aware filter:
        (recommended_load + opening_stock) > 0 so rows with leftover but zero forecast survive.
        Cold-start fallback to ``prediction > 0`` when reconciliation cols absent.
        """
        df = self._van_load_unfiltered()
        if df.empty:
            return df
        has_recon = "recommended_load" in df.columns and "opening_stock" in df.columns
        if has_recon:
            # Shared activity predicate (see enrich.py); every consumer uses the same mask.
            df = df[activity_mask(df)]
        elif "prediction" in df.columns:
            df = df[pd.to_numeric(df["prediction"], errors="coerce").fillna(0) > 0]
        return df

    def van_load_view_enriched(self) -> pd.DataFrame:
        """van_load_view with reconciliation cols guaranteed populated.

        Returns DB-stored values when cron has written them; else runs enrich_with_load
        once and caches on the underlying file mtimes (composite key).

        Replaces the three inline lazy-fallback blocks the API routes
        used to carry (``van_load_page_view``, ``forecast_drawer_page_view``,
        ``get_future_forecast``); each used to re-run the same engine on
        every request. Centralising here means one cache, one detection
        rule, one source of truth.

        Cache invalidation is driven by file mtimes, same idiom every
        other cache in this service uses. The composite key bundles the
        snapshots of every file the enrichment reads:

          * ``demand_forecast.csv``     (predictions mirror -- input frame)
          * ``sales_transactions.csv``  (recon columns -- DB-mirrored)
          * ``sales_recent.csv``        (actuals for opening_stock sim)
          * ``customer_data.csv``       (whale guard input)
          * ``journey_plan.csv``        (journey guard input)

        Any one of those changing (the cron rewrites sales_transactions
        on each tick) invalidates the cached enrichment so the next call
        re-derives against the fresh inputs.
        """
        # IMPORTANT: read from the UNFILTERED source. ``van_load_view``
        # applies ``activity_mask`` which requires
        # ``recommended_load + opening_stock > 0`` on the row -- but
        # those columns are NaN for future-date rows that the
        # past+today-only reconciliation cron doesn't cover. Using the
        # filtered source would drop every future row BEFORE the engine
        # could enrich it, breaking VanLoad / ForecastDrawer / route-
        # summary for any forward date. We do our own enrich-then-filter
        # so future rows get real engine-computed values BEFORE the mask
        # decides whether to keep them.
        merged = self._van_load_unfiltered()
        if merged.empty:
            return merged

        # Trigger engine if recommended_load is NaN on ANY row (future rows always NaN
        # from past+today-only cron); mtime cache absorbs repeat-call cost.
        recon_col = merged["recommended_load"] if "recommended_load" in merged.columns else None
        have_stored = (
            recon_col is not None
            and recon_col.notna().all()
            and pd.to_numeric(recon_col, errors="coerce").fillna(0.0).abs().sum() > 0
        )
        if have_stored:
            # activity_mask now matches van_load_view's public contract.
            return merged[activity_mask(merged)]

        # Composite snapshot key; any input file change invalidates the cached frame.
        snapshot = self._van_load_enriched_snapshot_key()
        cache_key = "van_load_view_enriched"
        if snapshot is not None:
            with self._cache_lock:
                cached = self._derived_cache.get(cache_key)
                if cached is not None and cached[0] == snapshot:
                    return cached[1]

        # Cold path: pick prediction column + run canonical engine (lazy import).
        from demand_forecasting_pipeline.services.reconciliation import (
            enrich_with_load,
        )

        pred_col: str | None = None
        for cand in ("prediction", "Predicted"):
            if cand in merged.columns:
                pred_col = cand
                break
        if pred_col is None:
            # No predicted column; downstream handles degraded path.
            return merged

        enriched = enrich_with_load(merged, predicted_col=pred_col)

        # Apply activity_mask AFTER enrichment so engine can populate future rows first.
        enriched = enriched[activity_mask(enriched)]
        if snapshot is not None:
            with self._cache_lock:
                self._derived_cache[cache_key] = (snapshot, enriched)
        return enriched

    def _van_load_enriched_snapshot_key(
        self,
    ) -> tuple[_CacheKey | None, ...] | None:
        """mtime snapshot tuple for van_load_view_enriched; None when forecast mirror absent."""
        forecast_key = self._file_snapshot_key("future_forecast")
        if forecast_key is None:
            return None
        # Shared-imports aux files; stat each (mirrors cron's enrichment inputs).
        aux_filenames = (
            self._s.sales_transactions_file,
            self._s.sales_recent_file,
            self._s.customer_data_file,
            self._s.journey_plan_file,
        )
        aux_keys: list[_CacheKey | None] = []
        for fname in aux_filenames:
            try:
                path = self._s.shared_data_path(fname)
            except AttributeError:
                aux_keys.append(None)
                continue
            if not path.exists():
                aux_keys.append(None)
                continue
            stat = path.stat()
            aux_keys.append((str(path), stat.st_mtime_ns, stat.st_size))
        return (forecast_key, *aux_keys)

    def _load_sales_transactions_normalized(self) -> pd.DataFrame:
        """Cached mtime-keyed read of sales_transactions.csv; PascalCase -> snake_case applied.

        stat+read+parse run under _cache_lock so concurrent miss-path readers serialise on parse.
        """
        from pathlib import Path
        try:
            sales_path = self._s.shared_data_path(self._s.sales_transactions_file)
        except AttributeError:
            return pd.DataFrame()
        if not Path(sales_path).exists():
            return pd.DataFrame()
        with self._cache_lock:
            stat = sales_path.stat()
            key: _CacheKey = (str(sales_path), stat.st_mtime_ns, stat.st_size)
            cached = self._sales_tx_cache
            if cached is not None and cached[0] == key:
                return cached[1]
            try:
                sx = pd.read_csv(sales_path, low_memory=False)
            except Exception as exc:
                logger.warning("sales_transactions CSV read failed: %s", exc)
                return pd.DataFrame()
            if not sx.empty:
                # PascalCase -> snake_case via SALES_TRANSACTIONS_RENAME (single source of truth).
                sx = sx.rename(columns=SALES_TRANSACTIONS_RENAME)
                if {"trx_date", "route_code", "item_code"}.issubset(sx.columns):
                    sx["trx_date"]   = pd.to_datetime(sx["trx_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                    sx["route_code"] = sx["route_code"].astype(str)
                    sx["item_code"]  = sx["item_code"].astype(str)
            self._sales_tx_cache = (key, sx)
            return sx

    def _merge_sales_transactions(self, df: pd.DataFrame) -> pd.DataFrame:
        """LEFT/OUTER merge sales_transactions.csv onto forecast frame; future rows stay NaN.

        Column aliases mapped to legacy names:
          fresh_load -> recommended_load, total_van_load -> recommended_van_load,
          van_load_lower/upper_bound -> load_lower/upper_bound,
          recent_daily_avg -> recent_avg_per_selling_day.
        """
        if df.empty:
            return df
        sx = self._load_sales_transactions_normalized()
        if sx.empty:
            return df
        if not {"trx_date", "route_code", "item_code"}.issubset(sx.columns):
            return df

        # Forecast df side -- build a working copy with normalised
        # join keys. The forecast frame uses PascalCase. Drop any
        # leftover join columns from prior calls (defensive).
        work = df.copy()
        for stale in ("_join_date", "_join_route", "_join_item"):
            if stale in work.columns:
                work = work.drop(columns=stale)
        work["_join_date"]  = pd.to_datetime(work["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")
        work["_join_route"] = work["RouteCode"].astype(str)
        work["_join_item"]  = work["ItemCode"].astype(str)

        sx_work = sx.rename(columns={
            "trx_date":   "_join_date",
            "route_code": "_join_route",
            "item_code":  "_join_item",
        })
        # Drop any recon column from work that ALSO exists on sx_work --
        # the right side wins (sales_transactions is the source of truth
        # for reconciliation). This handles cold-CSV cases where the
        # forecast mirror happened to retain the old recon columns.
        recon_on_sx = [c for c in sx_work.columns
                       if c not in {"_join_date", "_join_route", "_join_item"}]
        for c in recon_on_sx:
            if c in work.columns:
                work = work.drop(columns=c)

        # Scope sx_work to the routes / dates the forecast frame already
        # covers. The outer merge below is then bounded by the same
        # universe -- it adds rep-only SKUs (items the rep stocked but
        # the model did not predict for) WITHIN the active scope, never
        # rep activity from unrelated routes or dates outside the
        # working window.
        sx_work = sx_work[
            sx_work["_join_route"].astype(str).isin(
                work["_join_route"].astype(str).unique()
            )
            & sx_work["_join_date"].astype(str).isin(
                work["_join_date"].astype(str).unique()
            )
        ]

        # OUTER so rep-only items survive. Forecast columns stay NaN
        # for those rows; the activity filter in ``van_load_view``
        # keeps them only when the rep side carries real signal. Pure
        # reflection of every observed cell, no scope-narrowing at the
        # merge boundary.
        merged = work.merge(
            sx_work,
            on=["_join_date", "_join_route", "_join_item"],
            how="outer",
        )
        # Rep-only (outer-side) rows arrive with the PascalCase keys
        # NaN -- the forecast side had no row. Backfill from the
        # ``_join_*`` columns (always populated, because they are the
        # merge keys) so every row downstream has TrxDate / RouteCode
        # / ItemCode populated. Done before the drop below so no row
        # ever leaves this function without a key triple.
        merged["TrxDate"] = pd.to_datetime(merged["TrxDate"], errors="coerce").fillna(
            pd.to_datetime(merged["_join_date"], errors="coerce")
        )
        for col, jkey in (("RouteCode", "_join_route"), ("ItemCode", "_join_item")):
            if col not in merged.columns:
                merged[col] = None
            merged[col] = (
                merged[col].astype(object).where(merged[col].notna(), merged[jkey])
                .astype(str)
            )
        merged = merged.drop(columns=["_join_date", "_join_route", "_join_item"])

        # Alias new column names back to the legacy ones downstream uses.
        alias_map = {
            "fresh_load":           "recommended_load",
            "total_van_load":       "recommended_van_load",
            "van_load_lower_bound": "load_lower_bound",
            "van_load_upper_bound": "load_upper_bound",
            "recent_daily_avg":     "recent_avg_per_selling_day",
        }
        rename_now = {n: o for n, o in alias_map.items()
                      if n in merged.columns and o not in merged.columns}
        if rename_now:
            merged = merged.rename(columns=rename_now)
        return merged

    def get_future_forecast(
        self,
        route_code: str | None = None,
        item_code: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        # Enriched view so all paginated consumers see reconciliation cols populated.
        df = self.van_load_view_enriched()
        df = self._apply_filters(df, route_code=route_code, item_code=item_code)
        total = len(df)
        eff_limit = int(limit if limit is not None else self._s.default_page_limit)
        return df.iloc[offset : offset + eff_limit], total

    def get_future_forecast_meta(self) -> tuple[int, str | None]:
        """(total_rows, max_TrxDate_iso) from the cached frame; no slice/copy."""
        df = self._read_df("future_forecast")
        if df.empty:
            return 0, None
        max_date: str | None = None
        if "TrxDate" in df.columns:
            mx = df["TrxDate"].max()
            if pd.notna(mx):
                max_date = str(mx)
        return int(len(df)), max_date

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_model_metrics(self, demand_class: str | None = None) -> pd.DataFrame:
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
        # Written by data_processing (~2min), well before pair_classes (~18min) -- surfaces mid-run.
        return self._read_json("data_quality")

    # ------------------------------------------------------------------
    # Pair model lookup
    # ------------------------------------------------------------------

    def get_pair_model_lookup(
        self,
        route_code: str | None = None,
        item_code: str | None = None,
    ) -> pd.DataFrame:
        df = self._read_df("pair_model_lookup")
        return self._apply_filters(df, route_code=route_code, item_code=item_code)

    # ------------------------------------------------------------------
    # Explainability
    # ------------------------------------------------------------------

    def get_pair_classes(self, demand_class: str | None = None) -> pd.DataFrame:
        df = self._read_df("pair_classes")
        if demand_class and "class" in df.columns:
            df = df[df["class"] == demand_class]
        return df

    def get_pair_explainability(
        self,
        route_code: str | None = None,
        item_code: str | None = None,
        demand_class: str | None = None,
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
        # Authoritative: pair_classes.csv from the latest run.
        df = self.get_pair_classes()
        if not df.empty and "class" in df.columns:
            return {"total_pairs": len(df), "classes": df["class"].value_counts().to_dict()}
        # Early-visibility fallback: data_quality.json (post-processing) for headline count.
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
        # No explicit invalidation; next read picks up new mtime/size.
        return self._storage.write_dataframe(key, df)

    def write_json(self, key: str, data: dict[str, Any]) -> bool:
        # Same lazy mtime-driven invalidation as write_df.
        return self._storage.write_json(key, data)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Drop every cached entry; cheap, thread-safe (memory hygiene only -- mtime check would catch it anyway)."""
        with self._cache_lock:
            self._df_cache.clear()
            self._json_cache.clear()
            self._derived_cache.clear()

    @property
    def cache_keys(self) -> list[str]:
        """Currently-cached artifact keys; exposed for the health endpoint."""
        with self._cache_lock:
            return sorted(
                set(self._df_cache)
                | set(self._json_cache)
                | set(self._derived_cache)
            )

    @property
    def target_col(self) -> str:
        """Target column name from pipeline config; avoids hardcoding TotalQuantity."""
        return self._target_col

    @staticmethod
    def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
        """JSON-safe records: NaN/+-Inf -> None for FastAPI strict JSON."""
        if df is None or df.empty:
            return []
        # One pass replaces NaN + +/-Inf.
        cleaned = df.replace([np.nan, np.inf, -np.inf], None)
        return cleaned.to_dict("records")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _normalize_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply YAML wire-type contract; date_cols emit ``YYYY-MM-DD`` strings."""
        if df.empty:
            return df
        for col, dt in self._dtypes.items():
            if col in df.columns:
                # ``string`` (pandas-nullable) -> Python str for predictable JSON.
                df[col] = df[col].astype(str).str.strip() if dt == "string" else df[col].astype(dt)
        for col in self._date_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    def _file_snapshot_key(self, key: str) -> _CacheKey | None:
        """(path, mtime_ns, size) tuple; None for unknown/missing keys to skip caching."""
        path = self._storage.source_path(key)
        if path is None or not path.exists():
            return None
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)

    def _read_df(self, key: str) -> pd.DataFrame:
        """Mtime-keyed DataFrame read; hot path is one stat() + dict lookup."""
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
        """Mtime-keyed JSON read; same contract as ``_read_df``."""
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

    def _apply_filters(self, df: pd.DataFrame, route_code: str | None = None, item_code: str | None = None) -> pd.DataFrame:
        if df.empty:
            return df
        rk, ik = self._route_key, self._item_key
        # Loader pre-normalized; no astype(str) needed.
        if route_code and rk in df.columns:
            df = df[df[rk] == str(route_code).strip()]
        if item_code and ik in df.columns:
            df = df[df[ik] == str(item_code).strip()]
        return df
