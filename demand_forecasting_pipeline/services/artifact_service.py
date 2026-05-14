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
from demand_forecasting_pipeline.services.storage.file_storage import (
    SALES_TRANSACTIONS_RENAME,
)
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
        # Composite-key cache for derived frames whose freshness depends
        # on *several* on-disk files at once (e.g. the lazy-enrichment
        # fallback for van_load_view, which reads the forecast mirror +
        # sales_transactions + the three engine aux CSVs). Key shape is
        # a tuple of ``_CacheKey`` entries -- identical idiom to
        # ``_df_cache`` just compounded across files.
        self._derived_cache: Dict[str, Tuple[Tuple[Optional[_CacheKey], ...], pd.DataFrame]] = {}
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
        inference run).

        **Filter: carry-aware reconciled van load**. Keep rows where
        ``(recommended_load + opening_stock) > 0``. This is the rep's
        actual physical capacity for the day:

          * ``recommended_load`` -- the V5_b engine's fresh-issuance
            recommendation (after bias correction, envelope, etc.).
          * ``opening_stock``    -- yesterday's leftover already on the
            truck (forward-filled across calendar gaps).

        A row with ``prediction == 0`` (model said "no demand today")
        but ``opening_stock > 0`` is STILL on the van -- the leftover
        stock is real physical inventory the rep has to manage. The
        earlier ``prediction > 0`` filter silently dropped these rows
        and the tile under-reported by the carryover sum (route 9209
        on 2026-05-13 dropped ~4338 units that way).

        Cold-start fallback: when reconciliation columns are absent
        (fresh deploy, pre-cron), fall back to ``prediction > 0`` so
        the tile still shows something rather than a blank.

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
        # Reconciliation columns moved out of yf_demand_forecast into
        # yf_sales_transactions. We LEFT JOIN them in here at read time
        # and alias the new column names back to the legacy ones the
        # downstream consumers (page_views.py, predictions.py) already
        # understand. This keeps the refactor transparent: no other
        # caller needs to know the storage shape changed.
        df = self._merge_sales_transactions(df)
        has_recon = "recommended_load" in df.columns and "opening_stock" in df.columns
        if has_recon:
            rec = pd.to_numeric(df["recommended_load"], errors="coerce").fillna(0.0)
            op = pd.to_numeric(df["opening_stock"], errors="coerce").fillna(0.0)
            df = df[(rec + op) > 0]
        elif "prediction" in df.columns:
            df = df[pd.to_numeric(df["prediction"], errors="coerce").fillna(0) > 0]
        return df

    def van_load_view_enriched(self) -> pd.DataFrame:
        """Same as :meth:`van_load_view` but guarantees the reconciliation
        columns are populated.

        Read path:
          1. Call :meth:`van_load_view` -- already LEFT JOINs sales_transactions.csv
             so DB-stored reconciled values flow through when the cron has
             written them.
          2. If ``recommended_load`` exists AND is not uniformly zero, the
             DB mirror is authoritative -- return the merged frame as-is.
          3. Else (cold deploy, brand-new dates, pre-migration rows) run
             ``enrich_with_load`` ONCE on the full frame and cache the
             result keyed on the underlying file mtimes. Subsequent calls
             within the same mtime window are a dict lookup.

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
        merged = self.van_load_view()
        if merged.empty:
            return merged

        # Detection rule lifted verbatim from the three inline call
        # sites: the column exists AND its |sum| > 0. A column of all
        # zeros (migration default for un-pushed rows) doesn't count
        # as "stored" -- the fallback must still run.
        have_stored = (
            "recommended_load" in merged.columns
            and pd.to_numeric(merged["recommended_load"], errors="coerce")
            .fillna(0.0)
            .abs()
            .sum()
            > 0
        )
        if have_stored:
            return merged

        # Compute the composite snapshot key for the enrichment fallback.
        # Any of these files changing invalidates the cached frame.
        snapshot = self._van_load_enriched_snapshot_key()
        cache_key = "van_load_view_enriched"
        if snapshot is not None:
            with self._cache_lock:
                cached = self._derived_cache.get(cache_key)
                if cached is not None and cached[0] == snapshot:
                    return cached[1]

        # Cold path: pick the predicted column the same way the API
        # routes do, then run the canonical engine. Import is lazy so
        # this module can be imported without the engine being importable
        # (the cron's reconciliation context handles that path itself).
        from demand_forecasting_pipeline.services.reconciliation import (
            enrich_with_load,
        )

        pred_col: Optional[str] = None
        for cand in ("prediction", "Predicted"):
            if cand in merged.columns:
                pred_col = cand
                break
        if pred_col is None:
            # No predicted column -- nothing the engine can act on.
            # Return the merged frame; downstream callers already
            # handle the "no recon column" degraded path.
            return merged

        enriched = enrich_with_load(merged, predicted_col=pred_col)
        if snapshot is not None:
            with self._cache_lock:
                self._derived_cache[cache_key] = (snapshot, enriched)
        return enriched

    def _van_load_enriched_snapshot_key(
        self,
    ) -> Optional[Tuple[Optional[_CacheKey], ...]]:
        """Compose the mtime snapshot tuple for ``van_load_view_enriched``.

        Bundles the snapshot of every on-disk file the enrichment
        depends on. ``None`` entries are kept positionally so a missing
        file (later created) still flips the tuple and busts the cache;
        we only short-circuit to ``None`` when the *forecast mirror*
        itself is unavailable, because without it there's nothing to
        enrich and caching an empty fallback would pin staleness across
        the very first successful write."""
        forecast_key = self._file_snapshot_key("future_forecast")
        if forecast_key is None:
            return None
        # The remaining files live in shared imports; stat each directly.
        # Mirrors the cron's view of "what an enrichment reads".
        aux_filenames = (
            self._s.sales_transactions_file,
            self._s.sales_recent_file,
            self._s.customer_data_file,
            self._s.journey_plan_file,
        )
        aux_keys: list[Optional[_CacheKey]] = []
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

    def _merge_sales_transactions(self, df: pd.DataFrame) -> pd.DataFrame:
        """LEFT JOIN sales_transactions.csv onto the forecast frame.

        The sales-transactions CSV mirror carries the carry chain +
        engine math + envelope diagnostics + actual_sold for past +
        today. Future dates aren't in the mirror (no reconciliation for
        the future by design) -- those rows stay NaN/False for the
        reconciliation columns, which is the correct semantic.

        Column aliasing: the new schema renamed a few columns; we map
        them back to the legacy names downstream code expects:
           fresh_load              -> recommended_load
           total_van_load          -> recommended_van_load
           van_load_lower_bound    -> load_lower_bound
           van_load_upper_bound    -> load_upper_bound
           recent_daily_avg        -> recent_avg_per_selling_day
        All other reconciliation columns kept their names.
        """
        if df.empty:
            return df
        from pathlib import Path
        try:
            sales_path = self._s.shared_data_path(self._s.sales_transactions_file)
        except AttributeError:
            # Settings hasn't been wired with sales_transactions_file yet;
            # leave df untouched. Downstream falls back to prediction>0.
            return df
        if not Path(sales_path).exists():
            return df
        try:
            sx = pd.read_csv(sales_path, low_memory=False)
        except Exception as exc:
            logger.warning("sales_transactions CSV read failed: %s", exc)
            return df
        if sx.empty:
            return df

        # Normalise PascalCase wire columns -> snake_case via the
        # single source of truth in services.storage.file_storage
        # (SALES_TRANSACTIONS_RENAME). Keys missing from sx pass
        # through unchanged.
        sx = sx.rename(columns=SALES_TRANSACTIONS_RENAME)
        if not {"trx_date", "route_code", "item_code"}.issubset(sx.columns):
            return df
        sx["trx_date"]   = pd.to_datetime(sx["trx_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        sx["route_code"] = sx["route_code"].astype(str)
        sx["item_code"]  = sx["item_code"].astype(str)

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

        merged = work.merge(
            sx_work,
            on=["_join_date", "_join_route", "_join_item"],
            how="left",
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
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[pd.DataFrame, int]:
        # Use the enriched view so every consumer of paginated forecast
        # rows sees the reconciliation columns populated -- DB-stored
        # when the cron filled them, engine-derived (cached) on cold
        # start. Replaces the per-route inline ``enrich_with_load``
        # fallback the predictions API used to carry.
        df = self.van_load_view_enriched()
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
            self._derived_cache.clear()

    @property
    def cache_keys(self) -> list[str]:
        """Currently-cached artifact keys -- exposed so the health
        endpoint doesn't have to reach into private state."""
        with self._cache_lock:
            return sorted(
                set(self._df_cache)
                | set(self._json_cache)
                | set(self._derived_cache)
            )

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
