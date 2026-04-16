"""
Auto-retrain scheduler -- persisted config, drift detection, and periodic check.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  AutoRetrainConfig -- thread-safe JSON persistence
# ---------------------------------------------------------------------------


class AutoRetrainConfig:
    """Loads / saves the retrain config JSON with atomic writes and a lock."""

    _DEFAULT: Dict[str, Any] = {
        "enabled": False,
        "frequency_days": 14,
        "last_auto_retrain": None,
        "next_scheduled": None,
        "auto_inference_after_train": True,
        "history": [],
    }

    _MAX_HISTORY = 10

    def __init__(self, path: Optional[str] = None, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._path = Path(path or s.retrain_config_path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read retrain config, using defaults: %s", exc)
                    self._data = dict(self._DEFAULT)
            else:
                self._data = dict(self._DEFAULT)
            # Fill missing keys with defaults
            for k, v in self._DEFAULT.items():
                self._data.setdefault(k, v)
            return dict(self._data)

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file then rename
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp", prefix="retrain_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, default=str)
                # On Windows, target must not exist for rename
                if self._path.exists():
                    self._path.unlink()
                os.rename(tmp, str(self._path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    # -- computed -----------------------------------------------------------

    def _compute_next_scheduled(self) -> Optional[str]:
        last = self._data.get("last_auto_retrain")
        freq = self._data.get("frequency_days", 14)
        if not self._data.get("enabled"):
            return None
        if last:
            try:
                dt = datetime.fromisoformat(str(last))
                return (dt + timedelta(days=freq)).isoformat()
            except (ValueError, TypeError):
                pass
        # No previous run -- schedule from now
        return (datetime.now() + timedelta(days=freq)).isoformat()

    # -- queries ------------------------------------------------------------

    def is_due(self) -> bool:
        with self._lock:
            if not self._data.get("enabled"):
                return False
            ns = self._data.get("next_scheduled")
            if not ns:
                return True  # never run & enabled -> due now
            try:
                return datetime.now() >= datetime.fromisoformat(str(ns))
            except (ValueError, TypeError):
                return False

    # -- mutations ----------------------------------------------------------

    def update_settings(
        self,
        enabled: Optional[bool] = None,
        frequency_days: Optional[int] = None,
        auto_inference_after_train: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if enabled is not None:
                self._data["enabled"] = enabled
            if frequency_days is not None:
                self._data["frequency_days"] = frequency_days
            if auto_inference_after_train is not None:
                self._data["auto_inference_after_train"] = auto_inference_after_train
            self._data["next_scheduled"] = self._compute_next_scheduled()
        self.save()
        return self.get()

    def record_run(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            history: List[Dict[str, Any]] = self._data.get("history", [])
            history.insert(0, entry)
            self._data["history"] = history[: self._MAX_HISTORY]
            self._data["last_auto_retrain"] = entry.get("date", datetime.now().isoformat())
            self._data["next_scheduled"] = self._compute_next_scheduled()
        self.save()


# ---------------------------------------------------------------------------
#  Drift detection (live: predicted vs YaumiLive actuals)
# ---------------------------------------------------------------------------

_NO_DRIFT: Dict[str, Any] = {
    "status": "stable",
    "recent_accuracy": None,
    "baseline_accuracy": None,
    "delta": None,
    "source": "unavailable",
}

# Drift result cache — avoids hammering YaumiLive on every page load.
# TTL matches the dashboard refresh tier (5 min). The scheduler's own
# 6-hour check also calls this, so worst case YaumiLive sees one drift
# query every 5 min (from the UI) + one every 6 hours (from the scheduler).
_drift_cache: Dict[str, Any] = {}
_drift_cache_ts: float = 0.0
_DRIFT_CACHE_TTL = 5 * 60  # seconds


def compute_drift_status(
    artifact_svc: Any,
    accuracy_svc: Any = None,
    settings: Optional[Settings] = None,
    bypass_cache: bool = False,
) -> Dict[str, Any]:
    """Compare live post-training accuracy against the training-time baseline.

    **Primary (live)**: queries the last 7 days of predictions vs actual sales
    from YaumiLive via ``AccuracyService.get_comparison``. This is real drift
    detection — the model's predictions are scored against what customers
    actually bought AFTER training.

    **Fallback (test-set)**: if the live DB is unavailable, splits the static
    test predictions into recent vs baseline. Less meaningful but still a
    signal.

    Returns ``{"status", "recent_accuracy", "baseline_accuracy", "delta",
    "source": "live"|"test_set"|"unavailable"}``.
    """
    global _drift_cache, _drift_cache_ts

    if not bypass_cache and _drift_cache and (time.time() - _drift_cache_ts) < _DRIFT_CACHE_TTL:
        return dict(_drift_cache)

    s = settings or get_settings()
    warn = s.drift_warn_threshold
    alert = s.drift_alert_threshold

    # Baseline accuracy from the summary (training-time WAPE, already stored).
    baseline_acc: Optional[float] = None
    try:
        from demand_forecasting_pipeline.api.routes.summary import forecast_summary
        summary = forecast_summary(artifact_svc)
        baseline_acc = summary.accuracy_pct
    except Exception as exc:
        logger.warning("Drift: cannot get baseline accuracy: %s", exc)

    # --- Primary: live accuracy from YaumiLive ---
    recent_acc: Optional[float] = None
    source = "unavailable"

    if accuracy_svc is not None and getattr(accuracy_svc, "available", False):
        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            result = accuracy_svc.get_comparison(start_date=start, end_date=end)
            if result.get("success") and result.get("summary"):
                live_acc = result["summary"].get("accuracy_pct")
                if live_acc is not None and live_acc > 0:
                    recent_acc = round(live_acc, 2)
                    source = "live"
        except Exception as exc:
            logger.warning("Drift: live comparison failed, falling back to test-set: %s", exc)

    # --- Fallback: test-set split ---
    if recent_acc is None:
        recent_acc = _test_set_recent_accuracy(artifact_svc)
        if recent_acc is not None:
            source = "test_set"

    if baseline_acc is None or recent_acc is None:
        result = {**_NO_DRIFT, "recent_accuracy": recent_acc, "baseline_accuracy": baseline_acc, "source": source}
        _drift_cache, _drift_cache_ts = result, time.time()
        return result

    delta = round(recent_acc - baseline_acc, 2)
    abs_drop = abs(min(0, delta))
    status = "significant" if abs_drop > alert else "drifting" if abs_drop > warn else "stable"

    result = {
        "status": status,
        "recent_accuracy": recent_acc,
        "baseline_accuracy": baseline_acc,
        "delta": delta,
        "source": source,
    }
    _drift_cache, _drift_cache_ts = result, time.time()
    return result


def _test_set_recent_accuracy(svc: Any) -> Optional[float]:
    """WAPE on the last 7 days of the static test-predictions CSV."""
    try:
        test_df, _ = svc.get_test_predictions(limit=50_000, offset=0)
    except Exception:
        return None
    if test_df.empty:
        return None

    actual_col = "TotalQuantity" if "TotalQuantity" in test_df.columns else "actual_qty"
    pred_col = "prediction" if "prediction" in test_df.columns else "predicted"
    date_col = "TrxDate" if "TrxDate" in test_df.columns else "trx_date"

    if actual_col not in test_df.columns or pred_col not in test_df.columns:
        return None

    actual = pd.to_numeric(test_df[actual_col], errors="coerce").fillna(0)
    pred = pd.to_numeric(test_df[pred_col], errors="coerce").fillna(0)

    if date_col in test_df.columns:
        dates = pd.to_datetime(test_df[date_col], errors="coerce")
        max_date = dates.max()
        if pd.notna(max_date):
            mask = dates >= (max_date - pd.Timedelta(days=7))
            actual = actual[mask]
            pred = pred[mask]

    scored = actual > 0
    total_actual = float(actual[scored].sum())
    if total_actual <= 0:
        return None
    total_err = float((actual[scored] - pred[scored]).abs().sum())
    return round(max(0.0, 100.0 - (total_err / total_actual) * 100), 2)


# ---------------------------------------------------------------------------
#  Scheduler job: check_and_retrain
# ---------------------------------------------------------------------------

# Module-level state to track an in-progress auto-retrain
_auto_retrain_pending: Dict[str, Any] = {}
_pending_lock = threading.Lock()


def check_and_retrain(
    config: AutoRetrainConfig,
    pipeline_service: Any,
    artifact_service: Any,
    settings: Optional[Settings] = None,
) -> None:
    """Called periodically by APScheduler. Non-blocking."""
    s = settings or get_settings()

    global _auto_retrain_pending

    # 1. Check if a previous auto-retrain completed and record it
    with _pending_lock:
        if _auto_retrain_pending:
            train_status = pipeline_service.get_status("train")
            st = train_status.get("status", "")
            if st in ("success", "failed"):
                entry: Dict[str, Any] = {
                    "date": _auto_retrain_pending.get("started_at", datetime.now().isoformat()),
                    "trigger": "scheduled",
                    "accuracy_before": _auto_retrain_pending.get("accuracy_before"),
                    "accuracy_after": None,
                    "duration_seconds": train_status.get("duration_seconds", 0),
                    "status": st,
                }
                # Get accuracy after
                if st == "success":
                    try:
                        from demand_forecasting_pipeline.api.routes.summary import forecast_summary
                        # Invalidate cache so we get fresh numbers
                        artifact_service._cache.clear()
                        summary = forecast_summary(artifact_service)
                        entry["accuracy_after"] = summary.accuracy_pct
                    except Exception as exc:
                        logger.warning("Could not compute post-retrain accuracy: %s", exc)

                config.record_run(entry)
                logger.info(
                    "Auto-retrain completed: status=%s, before=%.1f%%, after=%s",
                    st,
                    entry.get("accuracy_before") or 0,
                    entry.get("accuracy_after"),
                )

                # If auto_inference_after_train is set and training succeeded, run inference
                cfg = config.get()
                if st == "success" and cfg.get("auto_inference_after_train"):
                    inf_status = pipeline_service.get_status("inference")
                    if inf_status.get("status") != "running":
                        logger.info("Auto-retrain: triggering inference after successful training")
                        pipeline_service.run_inference()

                _auto_retrain_pending = {}
            # Still running -- do nothing this tick
            return

    # 2. Check if enabled and due
    if not config.is_due():
        return

    # 3. Check if pipeline is already running
    train_status = pipeline_service.get_status("train")
    if train_status.get("status") == "running":
        logger.debug("Auto-retrain: training already running, skipping")
        return

    # 4. Record accuracy_before
    accuracy_before = None
    try:
        from demand_forecasting_pipeline.api.routes.summary import forecast_summary
        summary = forecast_summary(artifact_service)
        accuracy_before = summary.accuracy_pct
    except Exception as exc:
        logger.warning("Could not get pre-retrain accuracy: %s", exc)

    # 5. Start training
    logger.info("Auto-retrain: starting scheduled training (accuracy_before=%.1f%%)", accuracy_before or 0)
    result = pipeline_service.run_training()

    if result.get("success"):
        with _pending_lock:
            _auto_retrain_pending = {
                "started_at": datetime.now().isoformat(),
                "accuracy_before": accuracy_before,
            }
    else:
        logger.warning("Auto-retrain: failed to start training: %s", result.get("message"))
