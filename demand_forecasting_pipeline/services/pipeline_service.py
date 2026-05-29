"""Pipeline service: train + inference in background threads.

After each successful run: (1) DbPusher writes predictions to yf_demand_forecast
under the run's data_split; (2) data_import mirrors the table to demand_forecast.csv.
Cascades are opportunistic -- on-disk artifacts are the contract of record.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import httpx

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import PIPELINE_RUNS
from demand_forecasting_pipeline.services.cache_invalidation import (
    invalidate_artifact_cache,
    invalidate_van_load_cache,
)
from demand_forecasting_pipeline.services.db_pusher import DbPusher

logger = logging.getLogger(__name__)

# Pipeline -> data_split label pushed into yf_demand_forecast.
_PIPELINE_DATASPLIT: dict[str, str] = {
    "train": "test",
    "inference": "forecast",
}

# Pipelines blocked from starting while a dependency runs (prevents stale-read races).
_PIPELINE_BLOCKING_DEPS: dict[str, tuple[str, ...]] = {
    "inference": ("train",),
}

# Required precondition artifacts under explainability_path; synchronous trigger-time check.
_PIPELINE_PREFLIGHT_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "inference": ("pair_classes.csv",),
}

class PipelineStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

@dataclass
class PipelineRun:
    pipeline: str  # "train" or "inference"
    status: PipelineStatus = PipelineStatus.IDLE
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float = 0.0
    # Duration of last successful run; UI uses it as ETA hint during in-flight runs.
    last_success_duration_seconds: float | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, str] = field(default_factory=dict)
    # Audit-hook inputs. Populated at trigger time so the post-success
    # outcome recorder knows who started the run ("manual" via API,
    # "schedule"/"drift" via auto-retrain cron) and what accuracy the
    # caller captured before the run started. The auto-retrain
    # scheduler captures accuracy_before; manual triggers leave it
    # None and the history row shows N/A for that column.
    trigger: str = "manual"
    # True only when this run was launched by ``_maybe_chain_inference``
    # as part of the auto-cascade (train -> inference -> reconciliation).
    # Standalone ``POST /pipeline/inference`` debug calls stay False and
    # the auto-cascade reconciliation hook NO-OPs on them.
    chained_from_train: bool = False
    accuracy_before: float | None = None

class PipelineService:
    """Manages background pipeline execution with status tracking.

    Shutdown semantics
    ------------------
    The two pipeline threads were previously ``daemon=True`` so SIGTERM
    killed them mid-run with zero cleanup -- a 4-hour XGBoost retrain
    95% done at deploy time would just disappear. We now hold real
    (non-daemon) threads with a per-thread ``threading.Event`` cancel
    signal. ``shutdown()`` (called from the FastAPI lifespan) sets the
    event and joins the threads with a bounded timeout. The pipeline
    code itself doesn't expose a mid-fit interrupt (LightGBM/XGBoost
    aren't cooperatively cancellable), but it CAN check the event at
    stage boundaries via the ``on_step`` callback -- enough to skip
    db_push + cascade if shutdown was requested mid-run.
    """

    # Bounded wait for in-flight pipelines to finish on shutdown. Long
    # enough for the current STAGE to complete (training has multi-
    # Env-tunable bound; long enough for typical fits, short enough not to hang shutdown.
    _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        self._runs: dict[str, PipelineRun] = {
            "train": PipelineRun(pipeline="train"),
            "inference": PipelineRun(pipeline="inference"),
        }
        # Per-pipeline cancel event + thread handle; checked at stage boundaries.
        self._cancel: dict[str, threading.Event] = {
            "train": threading.Event(),
            "inference": threading.Event(),
        }
        self._threads: dict[str, threading.Thread | None] = {
            "train": None, "inference": None,
        }

    def _cancel_requested(self, pipeline: str) -> bool:
        return self._cancel[pipeline].is_set()

    def shutdown(self, timeout_seconds: float | None = None) -> None:
        """Cancel in-flight pipelines; per-pipeline join with bounded timeout."""
        wait = float(timeout_seconds or self._DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        for name in ("train", "inference"):
            self._cancel[name].set()
        for name in ("train", "inference"):
            t = self._threads.get(name)
            if t is None or not t.is_alive():
                continue
            logger.info(
                "pipeline_service.shutdown: waiting up to %.1fs for %s thread",
                wait, name,
            )
            t.join(timeout=wait)
            if t.is_alive():
                logger.warning(
                    "pipeline_service.shutdown: %s thread did not exit "
                    "within %.1fs; process will terminate anyway",
                    name, wait,
                )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self, pipeline: str) -> Mapping[str, Any]:
        """Immutable snapshot of run state (deepcopy + MappingProxyType)."""
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

    def run_training(
        self,
        config_path: str | None = None,
        *,
        trigger: str = "manual",
        accuracy_before: float | None = None,
    ) -> dict[str, Any]:
        """Spawn a training run; ``trigger`` records origin (manual/schedule/drift)."""
        return self._run_pipeline(
            "train", config_path,
            trigger=trigger, accuracy_before=accuracy_before,
        )

    def run_inference(
        self,
        config_path: str | None = None,
        *,
        chained_from_train: bool = False,
    ) -> dict[str, Any]:
        """Trigger inference; chained_from_train=True opts into downstream auto-cascade."""
        if not chained_from_train:
            logger.info(
                "standalone /pipeline/inference triggered -- auto-cascade "
                "(reconciliation + recommendation regen) WILL NOT fire. "
                "POST /reconciliation/refresh explicitly if you need it.",
            )
        return self._run_pipeline(
            "inference", config_path, chained_from_train=chained_from_train,
        )

    # Trigger-time guards (caller must hold self._lock).

    def _check_run_guards(self, pipeline: str) -> str | None:
        """Reason pipeline can't start: self-running OR blocking dep is running."""
        run = self._runs.get(pipeline)
        if run and run.status == PipelineStatus.RUNNING:
            return f"{pipeline} is already running"
        for dep in _PIPELINE_BLOCKING_DEPS.get(pipeline, ()):
            dep_run = self._runs.get(dep)
            if dep_run and dep_run.status == PipelineStatus.RUNNING:
                return f"{dep} is in progress; wait for it to finish"
        return None

    def _check_preflight(self, pipeline: str) -> str | None:
        """Reason if precondition artifacts are missing under explainability_path."""
        for filename in _PIPELINE_PREFLIGHT_ARTIFACTS.get(pipeline, ()):
            if not self._s.explainability_path(filename).exists():
                return "no trained model found - run training first"
        return None

    def _run_pipeline(
        self,
        pipeline: str,
        config_path: str | None = None,
        *,
        trigger: str = "manual",
        accuracy_before: float | None = None,
        chained_from_train: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            # Shutdown check first so we surface "shutting down" not "no trained model".
            # Inside _lock to close race with shutdown.set() + concurrent triggers.
            # a fresh trigger could interleave and lose the shutdown
            # signal. The event is set ONLY by ``shutdown()``; we
            # never clear it (the process is about to exit anyway, so
            # a sticky-once-set semantic is the right contract).
            if self._cancel[pipeline].is_set():
                return {
                    "success": False,
                    "message": "service is shutting down; trigger refused",
                }
            blocked = self._check_run_guards(pipeline)
            if blocked:
                return {"success": False, "message": blocked}
            missing = self._check_preflight(pipeline)
            if missing:
                return {"success": False, "message": missing}
            run = self._runs[pipeline]
            run.status = PipelineStatus.RUNNING
            run.started_at = datetime.now(UTC).isoformat()
            run.finished_at = None
            run.error = None
            run.result = {}
            run.steps = {}
            # Stash trigger + accuracy_before for the audit hook to
            # read after the run completes. Per-run state on the
            # PipelineRun dataclass; cleared (with the rest of run
            # state) on the next trigger.
            run.trigger = trigger
            run.accuracy_before = accuracy_before
            # ``chained_from_train`` distinguishes the auto-cascade
            # (train -> inference -> reconciliation) from a standalone
            # ``/pipeline/inference`` debug call. The auto-cascade hook
            # in ``_execute`` reads this so an operator probing
            # inference in isolation doesn't accidentally trigger the
            # full 30-day reconciliation + recommendation regeneration.
            run.chained_from_train = chained_from_train

        cfg = config_path or self._s.pipeline_config
        # Non-daemon thread so the process won't exit until the
        # pipeline finishes the current stage on shutdown (paired with
        # ``shutdown()`` joining the thread). Note that we do NOT
        # clear the cancel event here -- it's per-pipeline and is set
        # ONLY by ``shutdown()`` once per process lifetime. Clearing
        # it would create the race we just guarded against above. The
        # event stays false in normal operation; the only time it
        # flips true is on a single shutdown call after which we
        # refuse new triggers.
        thread = threading.Thread(
            target=self._execute,
            args=(pipeline, cfg),
            name=f"pipeline-{pipeline}",
            daemon=False,
        )
        self._threads[pipeline] = thread
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
            # Pre-train data refresh closes the "cron hasn't fired" gap; opt-out via auto_data_refresh_before_train.
            if pipeline == "train":
                self._maybe_pre_refresh_data(on_step)

            if pipeline == "train":
                from demand_forecasting_pipeline.src.pipelines.train_pipeline import run_training
                result = run_training(config_path, on_step=on_step)
            else:
                from demand_forecasting_pipeline.src.pipelines.inference_pipeline import run_inference
                result = run_inference(config_path, on_step=on_step)

            # Shutdown checkpoint: skip downstream cascade on SIGTERM (artifacts already valid).
            if self._cancel_requested(pipeline):
                logger.warning(
                    "%s pipeline: shutdown requested mid-run; skipping "
                    "publish cascade (on-disk artifacts preserved)",
                    pipeline,
                )
                cascade = {"skipped": True, "reason": "shutdown_requested"}
            else:
                cascade = self._publish(pipeline, on_step)

            # Drop artifact + van-load caches so endpoints serve fresh numbers next request.
            invalidate_artifact_cache(reason=f"pipeline={pipeline}")
            invalidate_van_load_cache(reason=f"pipeline={pipeline}")

            duration = round(time.time() - t0, 2)
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.SUCCESS
                run.finished_at = datetime.now(UTC).isoformat()
                run.duration_seconds = duration
                run.last_success_duration_seconds = duration
                run.result = {"output_type": type(result).__name__} if result is not None else {}
                if cascade:
                    run.result["cascade"] = cascade

            PIPELINE_RUNS.labels(pipeline=pipeline, status="success").inc()
            logger.info("%s pipeline completed in %.1fs", pipeline, duration)

            # Audit trail + version snapshot + gate; idempotent against the scheduler's tick.
            if pipeline == "train" and cascade and not cascade.get("skipped"):
                self._record_training_outcome()

            # If operator opted into auto-inference, chain a fresh inference run.
            if pipeline == "train":
                self._maybe_chain_inference()

            # Post-inference reconciliation fires only when inference was auto-chained from train.
            inference_run = self._runs.get("inference")
            inference_was_chained = bool(
                inference_run is not None and inference_run.chained_from_train
            )
            if (
                pipeline == "inference"
                and cascade
                and not cascade.get("skipped")
                and inference_was_chained
            ):
                self._maybe_chain_reconciliation()

        except Exception as exc:
            duration = round(time.time() - t0, 2)
            tb = traceback.format_exc()
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.FAILED
                run.finished_at = datetime.now(UTC).isoformat()
                run.duration_seconds = duration
                run.error = str(exc)

            PIPELINE_RUNS.labels(pipeline=pipeline, status="failed").inc()
            logger.error("%s pipeline failed after %.1fs: %s\n%s", pipeline, duration, exc, tb)

    def _record_training_outcome(self) -> None:
        """Best-effort audit trail entry via shared training_outcome helper."""
        try:
            from demand_forecasting_pipeline.api.dependencies import (
                get_artifact_service,
                get_retrain_config,
            )
            from demand_forecasting_pipeline.services.training_outcome import (
                record_training_outcome,
            )
            with self._lock:
                run = self._runs["train"]
                trigger = run.trigger
                accuracy_before = run.accuracy_before
            record_training_outcome(
                pipeline_service=self,
                artifact_service=get_artifact_service(),
                retrain_config=get_retrain_config(),
                settings=self._s,
                trigger=trigger,
                accuracy_before=accuracy_before,
            )
        except Exception as exc:
            logger.warning(
                "pipeline_service._record_training_outcome failed: %s -- "
                "training run itself was successful, but the audit "
                "trail entry was not recorded", exc,
            )

    def _post_data_import_dataset(
        self, *, dataset: str, mode: str, lookback_days: int,
    ) -> dict[str, Any]:
        """Canonical POST /api/v1/data/import; never raises, returns structured success/skipped/error.

        Path comes from ``settings.data_import_path`` (same setting the
        reconciliation cascade reads). Hardcoded path here previously meant
        an env override of ``DF_DATA_IMPORT_PATH`` silently bypassed retrain
        cascades while reconciliation cascades honoured it.
        """
        base = (self._s.data_import_url or "").rstrip("/")
        if not base:
            return {"success": False, "skipped": True, "reason": "url_not_configured"}
        url = f"{base}{self._s.data_import_path}"
        try:
            resp = httpx.post(
                url,
                json={"dataset": dataset, "mode": mode, "lookback_days": int(lookback_days)},
                timeout=self._s.data_import_cascade_timeout_seconds,
            )
            resp.raise_for_status()
            return {"success": True, **resp.json()}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _maybe_pre_refresh_data(self, on_step: Callable[[str, str], None]) -> None:
        """Pull a fresh sales_recent mirror before training.

        Without this hook, training reads whatever the 03:00 cron last
        wrote -- if the operator retrains mid-day or before the cron
        fires, the model trains on stale actuals. Failures log and
        continue: a data_import outage shouldn't block training.
        """
        if not getattr(self._s, "auto_data_refresh_before_train", True):
            return
        lookback = int(getattr(self._s, "auto_data_refresh_lookback_days", 30))
        on_step("data_refresh_pre_train", "running")
        result = self._post_data_import_dataset(
            dataset="sales_recent", mode="incremental", lookback_days=lookback,
        )
        if result.get("skipped"):
            on_step("data_refresh_pre_train", "skipped")
            logger.info("auto_data_refresh skipped: %s", result.get("reason"))
            return
        if not result.get("success"):
            on_step("data_refresh_pre_train", "failed")
            logger.warning(
                "auto_data_refresh failed (%s) -- training proceeds against "
                "the existing CSV mirror", result.get("error"),
            )
            return
        on_step("data_refresh_pre_train", "completed")
        logger.info(
            "auto_data_refresh ok: sales_recent +%d rows (total=%d, %.1fs)",
            int(result.get("new_rows") or 0),
            int(result.get("total_rows") or 0),
            float(result.get("duration_seconds") or 0.0),
        )

    def _maybe_chain_reconciliation(self) -> None:
        """Run reconciliation_refresh after a successful inference push.

        Closes the last manual step in the auto-cascade. After
        inference lands fresh predictions in yf_demand_forecast and
        demand_forecast.csv, the V5_b engine + bias_service still need
        to re-run before yf_sales_transactions and yf_recommended_orders
        reflect the new model. Operators previously had to click
        /reconciliation/refresh themselves; this hook fires it.

        ``run_refresh`` is called in-process (not via HTTP self-call)
        to avoid an HTTP round-trip and to surface failures directly
        in the train pipeline's log stream. Failures don't raise -- a
        reconciliation outage shouldn't poison the just-completed
        inference (operator can retry separately).
        """
        if not getattr(self._s, "auto_reconcile_after_inference", True):
            return
        horizon = int(getattr(self._s, "auto_reconcile_horizon_days", 30))
        logger.info("auto_reconcile chaining inference->reconciliation (horizon_days=%d)", horizon)
        try:
            from demand_forecasting_pipeline.services.reconciliation_refresh import refresh_reconciliation
            result = refresh_reconciliation(
                settings=self._s, horizon_days_behind=horizon, force=True,
            )
            logger.info(
                "auto_reconcile result: success=%s rows_updated=%s duration=%ss",
                result.get("success"),
                result.get("rows_updated"),
                result.get("duration_seconds"),
            )
        except Exception as exc:
            logger.warning("auto_reconcile failed (%s) -- inference output is in DB; "
                           "operator can retry POST /reconciliation/refresh manually", exc)

    def _maybe_chain_inference(self) -> None:
        """Chain inference after train when auto_inference_after_train is set."""
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
        logger.info("auto_inference_chaining train->inference (with reconciliation cascade)")
        # chained_from_train=True opts into downstream reconciliation cascade.
        self.run_inference(chained_from_train=True)

    def _publish(self, pipeline: str, on_step: Callable[[str, str], None]) -> dict[str, Any]:
        """Cascade DB push -> data_import refresh; opportunistic, never raises."""
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
            # Post-push magnitude guard (Forecast split only); logs CRITICAL, never fails push.
            if datasplit == "forecast":
                try:
                    from demand_forecasting_pipeline.services.production_sanity import (
                        record_and_check,
                    )
                    sanity = record_and_check(self._s)
                    result["production_sanity"] = sanity
                except Exception as exc:
                    logger.warning("production_sanity check failed: %s", exc)
        else:
            logger.warning("db_push did not succeed (split=%s): %s", datasplit, result.get("error"))
        return {**result, "datasplit": datasplit}

    def _refresh_data_import(
        self, on_step: Callable[[str, str], None], *, push_succeeded: bool,
    ) -> dict[str, Any]:
        """Trigger data_import re-read; skipped if push didn't land or URL unset."""
        if not push_succeeded:
            on_step("data_import_refresh", "skipped")
            return {"success": False, "skipped": True, "reason": "db_push_did_not_land"}

        # training_cascade_lookback_days covers Test+Forecast span; refresh-window mode dedup-merges.
        dataset = self._s.data_import_dataset
        on_step("data_import_refresh", "running")
        result = self._post_data_import_dataset(
            dataset=dataset,
            mode="incremental",
            lookback_days=int(self._s.training_cascade_lookback_days),
        )
        if result.get("skipped"):
            on_step("data_import_refresh", "skipped")
            logger.info("data_import_refresh skipped: %s", result.get("reason"))
            return result
        if not result.get("success"):
            on_step("data_import_refresh", "failed")
            logger.warning("data_import_refresh failed: %s", result.get("error"))
            return result
        # data_import body's ``success`` is final ground truth (may be False on a 2xx).
        body_ok = bool(result.get("success"))
        on_step("data_import_refresh", "completed" if body_ok else "failed")
        if body_ok:
            logger.info(
                "data_import_refresh ok: %s rows for %s",
                result.get("new_rows", result.get("total_rows", "?")),
                result.get("dataset", dataset),
            )
        else:
            logger.warning("data_import_refresh did not succeed: %s", result.get("error"))
        return result

