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

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional

import httpx

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.db_pusher import DbPusher

logger = logging.getLogger(__name__)

# Cascade target. The dataset key matches data_import's registry
# (``data_import.core.importer._DATASETS``); the path is the public
# import endpoint under data_import's ``api_prefix``. Both are
# contracts between the two services -- keep them in one place so
# changes there are a single search away.
_DATA_IMPORT_DATASET = "demand_forecast"
_DATA_IMPORT_PATH = "/api/v1/data/import"
# Long enough for data_import to pull a small forecast table from
# YaumiAIML, short enough that a stuck downstream doesn't trap the
# inference worker thread indefinitely.
_DATA_IMPORT_TIMEOUT_SECONDS = 60.0

# Each pipeline owns the ``data_split`` label it pushes into
# yf_demand_forecast. Adding a new pipeline that emits a prediction
# CSV is a single entry here; ``_publish`` picks the right split,
# ``DbPusher.push_predictions`` selects the right CSV under
# ``predictions_dir``, and the rest of the cascade flows automatically.
_PIPELINE_DATASPLIT: Dict[str, str] = {
    "train": "test",
    "inference": "forecast",
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
    result: Dict[str, Any] = field(default_factory=dict)
    steps: Dict[str, str] = field(default_factory=dict)


class PipelineService:
    """Manages background pipeline execution with status tracking."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        self._runs: Dict[str, PipelineRun] = {
            "train": PipelineRun(pipeline="train"),
            "inference": PipelineRun(pipeline="inference"),
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self, pipeline: str) -> Dict[str, Any]:
        with self._lock:
            run = self._runs.get(pipeline)
            if not run:
                return {"error": f"Unknown pipeline: {pipeline}"}
            return {
                "pipeline": run.pipeline,
                "status": run.status.value,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": run.duration_seconds,
                "last_success_duration_seconds": run.last_success_duration_seconds,
                "error": run.error,
                "result": run.result,
                "steps": dict(run.steps),
            }

    def get_all_status(self) -> Dict[str, Any]:
        return {k: self.get_status(k) for k in self._runs}

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run_training(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        return self._run_pipeline("train", config_path)

    def run_inference(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        return self._run_pipeline("inference", config_path)

    def _run_pipeline(self, pipeline: str, config_path: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            run = self._runs[pipeline]
            if run.status == PipelineStatus.RUNNING:
                return {"success": False, "message": f"{pipeline} is already running"}
            run.status = PipelineStatus.RUNNING
            run.started_at = datetime.now().isoformat()
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
            # for the TTL to expire.
            self._invalidate_artifact_cache()

            duration = round(time.time() - t0, 2)
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.SUCCESS
                run.finished_at = datetime.now().isoformat()
                run.duration_seconds = duration
                run.last_success_duration_seconds = duration
                run.result = {"output_type": type(result).__name__} if result is not None else {}
                if cascade:
                    run.result["cascade"] = cascade

            logger.info("%s pipeline completed in %.1fs", pipeline, duration)

        except Exception as exc:
            duration = round(time.time() - t0, 2)
            tb = traceback.format_exc()
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.FAILED
                run.finished_at = datetime.now().isoformat()
                run.duration_seconds = duration
                run.error = str(exc)

            logger.error("%s pipeline failed after %.1fs: %s\n%s", pipeline, duration, exc, tb)

    def _publish(self, pipeline: str, on_step: Callable[[str, str], None]) -> Dict[str, Any]:
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

    def _push_to_db(self, on_step: Callable[[str, str], None], *, datasplit: str) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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

        url = f"{base}{_DATA_IMPORT_PATH}"
        on_step("data_import_refresh", "running")
        try:
            resp = httpx.post(
                url,
                json={"dataset": _DATA_IMPORT_DATASET, "mode": "incremental"},
                timeout=_DATA_IMPORT_TIMEOUT_SECONDS,
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
                payload.get("dataset", _DATA_IMPORT_DATASET),
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
