"""
Pipeline service -- runs training and inference in background threads.
Tracks status so the API can report progress.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from demand_forecasting_pipeline.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


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
            pipeline_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if pipeline_root not in sys.path:
                sys.path.insert(0, pipeline_root)

            if pipeline == "train":
                from src.pipelines.train_pipeline import run_training
                result = run_training(config_path, on_step=on_step)
            else:
                from src.pipelines.inference_pipeline import run_inference
                result = run_inference(config_path, on_step=on_step)

            duration = round(time.time() - t0, 2)
            with self._lock:
                run = self._runs[pipeline]
                run.status = PipelineStatus.SUCCESS
                run.finished_at = datetime.now().isoformat()
                run.duration_seconds = duration
                run.result = {"output_type": type(result).__name__} if result is not None else {}

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
