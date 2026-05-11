"""
Pipeline service -- runs training and inference in background threads.
Tracks status so the API can report progress.

Every successful run cascades through the same two steps so each
prediction split lands where its consumers expect it without manual
intervention:

  1. ``DbPusher`` writes the run's prediction CSV into
     ``YaumiAIML.yf_demand_forecast``. Each pipeline owns one
     ``data_split`` value, and ``DbPusher`` does
     ``DELETE WHERE data_split=?  ;  INSERT`` so splits coexist
     permanently in the same table -- pushing one split never
     touches the rows of the other.
        train     -> test_predictions.csv  -> data_split='Test'
        inference -> future_forecast.csv   -> data_split='Forecast'
  2. The ``data_import`` service is asked to mirror the table into
     ``data/demand_forecast.csv`` so recommended_order reads the
     fresh rows on its next pass.

Each cascade step is opportunistic: missing DB credentials, an unset
``DF_DATA_IMPORT_URL``, or a network blip never fail the pipeline run
-- the on-disk artifacts under ``artifacts/predictions/`` are always
the contract of record. Step outcomes surface in
``/pipeline/status`` so the UI can show what landed and what didn't.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

import httpx

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import PIPELINE_RUNS
from demand_forecasting_pipeline.services.db_pusher import DbPusher

logger = logging.getLogger(__name__)

# Each pipeline owns the ``data_split`` label it pushes into
# yf_demand_forecast. Adding a new pipeline that emits a prediction
# CSV is a single entry here; ``_publish`` picks the right split,
# ``DbPusher.push_predictions`` selects the right CSV under
# ``predictions_dir``, and the rest of the cascade flows automatically.
_PIPELINE_DATASPLIT: dict[str, str] = {
    "train": "test",
    "inference": "forecast",
}

# Pipelines whose start is blocked while a listed dependency runs.
# Inference reads training artifacts, so concurrent execution risks
# stale or partially-written reads. Atomic file writes prevent torn
# files but a freshly-spawned inference can still load the *previous*
# training's artifacts and silently push outdated forecasts to the DB.
# Single source of truth for both server-side guard and any UI hint.
_PIPELINE_BLOCKING_DEPS: dict[str, tuple[str, ...]] = {
    "inference": ("train",),
}

# Pipeline-specific precondition artifacts. Each value is a tuple of
# ``(filename, hint_for_user)`` pairs resolved against
# ``Settings.explainability_path``. Inference cannot run until training
# has produced these on disk; checked synchronously so the trigger API
# returns an actionable error instead of a failed background-thread
# status the user has to poll for.
_PIPELINE_PREFLIGHT_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "inference": ("pair_classes.csv",),
}

class PipelineStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

@dataclass
class PipelineRun:
    pipeline: str  # "train" or "inference"
    status: PipelineStatus = PipelineStatus.IDLE
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    # Duration of the most recent SUCCESSFUL run, surviving across new
    # runs. The UI uses it as an ETA hint while a fresh run is in flight.
    last_success_duration_seconds: Optional[float] = None
    error: Optional[str] = None
    result: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, str] = field(default_factory=dict)

class PipelineService:
    """Manages background pipeline execution with status tracking."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        self._runs: dict[str, PipelineRun] = {
            "train": PipelineRun(pipeline="train"),
            "inference": PipelineRun(pipeline="inference"),
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self, pipeline: str) -> Mapping[str, Any]:
        """Return an immutable snapshot of the run state.

        Snapshot semantics:
          * ``deepcopy`` of the underlying mutable fields (``result``,
            ``steps``) so a caller that pokes the dict can't perturb
            the live run state.
          * Wrapped in ``MappingProxyType`` so direct attribute writes
            on the returned object also fail loudly instead of silently
            mutating a copy.
        """
        with self._lock:
            run = self._runs.get(pipeline)
            if not run:
                return MappingProxyType({"error": f"Unknown pipeline: {pipeline}"})
            payload = {
                "pipeline": run.pipeline,
                "status": run.status.value,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": run.duration_seconds,
                "last_success_duration_seconds": run.last_success_duration_seconds,
                "error": run.error,
                "result": copy.deepcopy(run.result),
                "steps": dict(run.steps),
            }
        return MappingProxyType(payload)

    def get_all_status(self) -> dict[str, Mapping[str, Any]]:
        return {k: self.get_status(k) for k in self._runs}

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run_training(self, config_path: Optional[str] = None) -> dict[str, Any]:
        return self._run_pipeline("train", config_path)

    def run_inference(self, config_path: Optional[str] = None) -> dict[str, Any]:
        return self._run_pipeline("inference", config_path)

    # ------------------------------------------------------------------
    # Trigger-time guards. Caller must hold ``self._lock`` -- they read
    # the in-memory run table.
    # ------------------------------------------------------------------

    def _check_run_guards(self, pipeline: str) -> Optional[str]:
        """Return a user-facing reason if ``pipeline`` cannot start now.

        Two layered checks:
          1. Self-running: the same pipeline is already in flight.
          2. Blocking dependency: another pipeline this one depends on
             is in flight (declared in ``_PIPELINE_BLOCKING_DEPS``).
        """
        run = self._runs.get(pipeline)
        if run and run.status == PipelineStatus.RUNNING:
            return f"{pipeline} is already running"
        for dep in _PIPELINE_BLOCKING_DEPS.get(pipeline, ()):
            dep_run = self._runs.get(dep)
            if dep_run and dep_run.status == PipelineStatus.RUNNING:
                return f"{dep} is in progress; wait for it to finish"
        return None

    def _check_preflight(self, pipeline: str) -> Optional[str]:
        """Return a user-facing reason if pipeline preconditions are unmet.

        Checks each artifact declared in ``_PIPELINE_PREFLIGHT_ARTIFACTS``
        via ``Settings.explainability_path`` -- the canonical, config-
        driven location, no hardcoded paths.
        """
        for filename in _PIPELINE_PREFLIGHT_ARTIFACTS.get(pipeline, ()):
            if not self._s.explainability_path(filename).exists():
                return "no trained model found - run training first"
        return None

    def _run_pipeline(self, pipeline: str, config_path: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            blocked = self._check_run_guards(pipeline)
            if blocked:
                return {"success": False, "message": blocked}
            missing = self._check_preflight(pipeline)
            if missing:
                return {"success": False, "message": missing}
            run = self._runs[pipeline]
            run.status = PipelineStatus.RUNNING
            run.started_at = datetime.now(timezone.utc).isoformat()
            run.finished_at = None
            run.error = None
            run.result = {}
            run.steps = {}

        cfg = config_path or self._s.pipeline_config
        thread = threading.Thread(
            target=self._execute,
            args=(pipeline, cfg),
            name=f"pipeline-{pipeline}",
            daemon=True,
        )
        thread.start()
        return {"success": True, "message": f"{pipeline} started", "config": cfg}

    def _update_step(self, pipeline: str, step: str, status: str) -> None:
        """Thread-safe step progress update. Called from the pipeline callback."""
        with self._lock:
            self._runs[pipeline].steps[step] = status

    def _execute(self, pipeline: str, config_path: str) -> None:
        t0 = time.time()

        def on_step(step: str, status: str = "completed") -> None:
            self._update_step(pipeline, step, status)

        try:
            if pipeline == "train":
                from demand_forecasting_pipeline.src.pipelines.train_pipeline import run_training
                result = run_training(config_path, on_step=on_step)
            else:
                from demand_forecasting_pipeline.src.pipelines.inference_pipeline import run_inference
                result = run_inference(config_path, on_step=on_step)

            cascade = self._publish(pipeline, on_step)

            # Train and inference both refresh artifacts on disk. Drop the
            # ArtifactService cache so /summary, /predictions/*, /metrics
            # serve the new numbers on the next request instead of waiting
            # for the TTL to expire. VanLoadService caches the per-(route,
            # date) van composition off the same CSVs, so it gets the
            # same treatment -- otherwise /workflow/plan keeps showing
            # the prior van composition for up to 5 minutes after refresh.
            self._invalidate_artifact_cache()
            self._invalidate_van_load_cache()

            duration = round(time.time() - t0, 2)
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.SUCCESS
                run.finished_at = datetime.now(timezone.utc).isoformat()
                run.duration_seconds = duration
                run.last_success_duration_seconds = duration
                run.result = {"output_type": type(result).__name__} if result is not None else {}
                if cascade:
                    run.result["cascade"] = cascade

            PIPELINE_RUNS.labels(pipeline=pipeline, status="success").inc()
            logger.info("%s pipeline completed in %.1fs", pipeline, duration)

            # Mirror the auto-retrain cron: when training succeeds and the
            # operator has opted into auto-inference (the "Auto-generate
            # forecasts after retrain" checkbox on the Forecasting tab),
            # chain a fresh inference so the future-forecast rows are in
            # the DB without a second manual click. ``run_inference`` is
            # itself non-blocking (spawns a daemon), and the inference
            # status guard ensures we no-op cleanly if one is already in
            # flight.
            if pipeline == "train":
                self._maybe_chain_inference()

        except Exception as exc:
            duration = round(time.time() - t0, 2)
            tb = traceback.format_exc()
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.FAILED
                run.finished_at = datetime.now(timezone.utc).isoformat()
                run.duration_seconds = duration
                run.error = str(exc)

            PIPELINE_RUNS.labels(pipeline=pipeline, status="failed").inc()
            logger.error("%s pipeline failed after %.1fs: %s\n%s", pipeline, duration, exc, tb)

    def _maybe_chain_inference(self) -> None:
        """Run inference after a successful train when the operator has
        ``auto_inference_after_train`` enabled. Same behavior the auto-
        retrain cron honors -- without this the checkbox label was a
        half-truth (cron yes, manual click no)."""
        try:
            from demand_forecasting_pipeline.services.retrain_scheduler import (
                AutoRetrainConfig,
            )
            cfg = AutoRetrainConfig(settings=self._s).get()
        except Exception as exc:
            logger.warning("auto_inference_chain_skipped reason=config_read: %s", exc)
            return
        if not cfg.get("auto_inference_after_train"):
            return
        if self.get_status("inference").get("status") == "running":
            logger.info("auto_inference_chain_skipped reason=already_running")
            return
        logger.info("auto_inference_chaining train->inference")
        self.run_inference()

    def _publish(self, pipeline: str, on_step: Callable[[str, str], None]) -> dict[str, Any]:
        """Cascade a successful run through DB push then data_import refresh.

        Pipelines without a publishable prediction CSV (anything not in
        ``_PIPELINE_DATASPLIT``) return ``{}`` and skip the cascade
        entirely. Both steps are opportunistic: failures land in the
        step status, never raised. The on-disk artifacts under
        ``artifacts/predictions/`` are the source of truth either way.
        """
        split = _PIPELINE_DATASPLIT.get(pipeline)
        if split is None:
            return {}
        push = self._push_to_db(on_step, datasplit=split)
        refresh = self._refresh_data_import(on_step, push_succeeded=bool(push.get("success")))
        return {"db_push": push, "data_import_refresh": refresh}

    def _push_to_db(self, on_step: Callable[[str, str], None], *, datasplit: str) -> dict[str, Any]:
        on_step("db_push", "running")
        pusher = DbPusher(self._s)
        if not pusher.available:
            on_step("db_push", "skipped")
            logger.info("db_push skipped (split=%s): DB not configured (set DF_DB_HOST + DF_DEMAND_TABLE)", datasplit)
            return {"success": False, "skipped": True, "reason": "db_not_configured", "datasplit": datasplit}

        try:
            result = pusher.push_predictions(datasplit=datasplit)
        except Exception as exc:
            on_step("db_push", "failed")
            logger.warning("db_push raised (split=%s): %s", datasplit, exc)
            return {"success": False, "error": str(exc), "datasplit": datasplit}

        on_step("db_push", "completed" if result.get("success") else "failed")
        if result.get("success"):
            logger.info(
                "db_push wrote %d rows to %s (split=%s)",
                result.get("rows", 0), result.get("table"), datasplit,
            )
        else:
            logger.warning("db_push did not succeed (split=%s): %s", datasplit, result.get("error"))
        return {**result, "datasplit": datasplit}

    def _refresh_data_import(
        self, on_step: Callable[[str, str], None], *, push_succeeded: bool,
    ) -> dict[str, Any]:
        """Trigger data_import to re-read the YaumiAIML demand table.

        Skipped when the upstream push didn't land (nothing new to mirror)
        or when ``DF_DATA_IMPORT_URL`` is unset (production deployments
        that orchestrate data_import on their own schedule). Errors are
        logged and reported but never raised.
        """
        if not push_succeeded:
            on_step("data_import_refresh", "skipped")
            return {"success": False, "skipped": True, "reason": "db_push_did_not_land"}

        base = (self._s.data_import_url or "").rstrip("/")
        if not base:
            on_step("data_import_refresh", "skipped")
            logger.info("data_import_refresh skipped: DF_DATA_IMPORT_URL not set")
            return {"success": False, "skipped": True, "reason": "url_not_configured"}

        dataset = self._s.data_import_dataset
        url = f"{base}{self._s.data_import_path}"
        on_step("data_import_refresh", "running")
        try:
            # ``lookback_days`` opts into the importer's refresh-window
            # mode. Training rewrites a rolling block (test-split rows
            # back, forecast-horizon rows forward); pure-append
            # incremental would miss every UPDATE on a date already in
            # the CSV. ``training_cascade_lookback_days`` covers both
            # ends of that block with a buffer.
            resp = httpx.post(
                url,
                json={
                    "dataset": dataset,
                    "mode": "incremental",
                    "lookback_days": int(self._s.training_cascade_lookback_days),
                },
                timeout=self._s.data_import_cascade_timeout_seconds,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            on_step("data_import_refresh", "failed")
            logger.warning("data_import_refresh failed: %s", exc)
            return {"success": False, "error": str(exc)}

        ok = bool(payload.get("success"))
        on_step("data_import_refresh", "completed" if ok else "failed")
        if ok:
            logger.info(
                "data_import_refresh ok: %s rows for %s",
                payload.get("new_rows", payload.get("total_rows", "?")),
                payload.get("dataset", dataset),
            )
        else:
            logger.warning("data_import_refresh did not succeed: %s", payload.get("error"))
        return {"success": ok, **payload}

    @staticmethod
    def _invalidate_artifact_cache() -> None:
        """Clear the singleton ArtifactService cache after a successful run.

        Lazily imported because ``api.dependencies`` is the layer above this
        service; the function runs in a worker thread once per pipeline
        completion, so the import cost is negligible.
        """
        try:
            from demand_forecasting_pipeline.api.dependencies import get_artifact_service
            get_artifact_service().invalidate_cache()
        except Exception as exc:
            logger.warning("artifact_cache_invalidate_failed: %s", exc)

    @staticmethod
    def _invalidate_van_load_cache() -> None:
        """Clear the singleton VanLoadService TTL cache after a refresh.

        VanLoadService caches per-(route, date) van composition for up
        to ``van_load_csv_cache_ttl_seconds`` (CSV) / ``..._live`` (live).
        After a successful db_pusher + data_import remirror the
        underlying CSVs change but those entries would keep serving the
        old composition until TTL elapses -- five minutes of stale van
        data the supervisor sees on /workflow/plan. Busting the cache
        on cascade success closes that window.
        """
        try:
            from demand_forecasting_pipeline.api.dependencies import get_van_load_service
            get_van_load_service().invalidate()
        except Exception as exc:
            logger.warning("van_load_cache_invalidate_failed: %s", exc)
