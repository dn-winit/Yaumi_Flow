"""
Auto-retrain scheduler -- persisted config and drift detection.

The drift computation here is the single source of truth consumed by:
  * ``api/routes/retrain.py`` (interactive UI)
  * ``jobs/drift_check.py`` (Step Functions / Fargate scheduled job)

Retrain orchestration itself lives outside this module (Step Functions),
so there is no in-process scheduler tick here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import (
    DRIFT_PCT,
    LAST_TRAIN_AGE_SECONDS,
)
from demand_forecasting_pipeline.src.evaluation.metrics import (
    composite_kwargs_from_yaml,
    composite_summary,
)

logger = logging.getLogger(__name__)

def _utcnow() -> datetime:
    """All persisted timestamps in this module are UTC, ISO-8601, with
    timezone offset. Centralised so the offset never accidentally goes
    naive again (Python 3.12 deprecated ``datetime.utcnow``)."""
    return datetime.now(timezone.utc)

# Window resolution for "recent" accuracy. data_import is the canonical
# authority on what counts as a working day, so we ask it for the
# trailing-N-working-days span instead of guessing on calendar weeks.
# All three knobs (path, query name, timeout) are env-overridable via
# Settings so a deployment can point at a different upstream without
# code changes. Short timeout: this is on a cached UI hot path and we
# always have the calendar fallback ready.

# ---------------------------------------------------------------------------
#  AutoRetrainConfig -- thread-safe JSON persistence
# ---------------------------------------------------------------------------

class AutoRetrainConfig:
    """Loads / saves the retrain config JSON with atomic writes and a lock."""

    def __init__(self, path: Optional[str] = None, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._path = Path(path or s.retrain_config_path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        # Settings-driven defaults so deployments can adjust frequency
        # and history bounds without code changes.
        self._max_history: int = int(s.retrain_history_max)
        self._rotation_eps: float = float(s.baseline_rotation_convergence_pp)
        self._default_frequency_days: int = int(s.retrain_default_frequency_days)
        self._defaults: dict[str, Any] = {
            "enabled": False,
            "frequency_days": self._default_frequency_days,
            "last_auto_retrain": None,
            "next_scheduled": None,
            "auto_inference_after_train": True,
            "history": [],
            # Baseline tracking. ``baseline_accuracy_pct`` is the
            # reference against which "current" accuracy is compared
            # for drift; initialised on the first read
            # (``baseline_source='initialized'``) and later refreshed
            # to a rolling median of recent_accuracy values
            # (``baseline_source='rolling_median_30d'``).
            "baseline_accuracy_pct": None,
            "baseline_source": None,
            "baseline_set_at": None,
        }
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read retrain config, using defaults: %s", exc)
                    self._data = dict(self._defaults)
            else:
                self._data = dict(self._defaults)
            # Fill missing keys with defaults
            for k, v in self._defaults.items():
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

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    # -- computed -----------------------------------------------------------

    def _compute_next_scheduled(self) -> Optional[str]:
        last = self._data.get("last_auto_retrain")
        freq = self._data.get("frequency_days", self._default_frequency_days)
        if not self._data.get("enabled"):
            return None
        if last:
            try:
                dt = datetime.fromisoformat(str(last))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return (dt + timedelta(days=freq)).isoformat()
            except (ValueError, TypeError):
                pass
        # No previous run -- schedule from now
        return (_utcnow() + timedelta(days=freq)).isoformat()

    # -- queries ------------------------------------------------------------

    def is_due(self) -> bool:
        with self._lock:
            if not self._data.get("enabled"):
                return False
            ns = self._data.get("next_scheduled")
            if not ns:
                return True  # never run & enabled -> due now
            try:
                scheduled = datetime.fromisoformat(str(ns))
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=timezone.utc)
                return _utcnow() >= scheduled
            except (ValueError, TypeError):
                return False

    # -- baseline ----------------------------------------------------------

    def update_baseline(self, accuracy_pct: float, source: str) -> None:
        """Persist a new baseline. Idempotent: callers can invoke this
        unconditionally; only the new value (and a timestamp) are
        written. ``source`` is stored so consumers can tell whether the
        baseline came from a rolling median or a cold-start initialization.
        """
        with self._lock:
            self._data["baseline_accuracy_pct"] = float(accuracy_pct)
            self._data["baseline_source"] = source
            self._data["baseline_set_at"] = _utcnow().isoformat()
        self.save()

    def baseline(self) -> dict[str, Any]:
        with self._lock:
            return {
                "value": self._data.get("baseline_accuracy_pct"),
                "source": self._data.get("baseline_source"),
                "set_at": self._data.get("baseline_set_at"),
            }

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._data.get("history", []))

    def rotate_baseline_if_eligible(
        self, *, window: int, min_history: int,
    ) -> Optional[dict[str, Any]]:
        """Rotate the persisted baseline to a rolling median when enough
        successful runs are on record.

        Returns the new baseline dict on rotation, ``None`` otherwise.
        Idempotent within a tick: the rotation only fires when the
        median of the trailing ``window`` ``accuracy_after`` values
        differs from the current baseline by more than 0.01pp, so a
        flat history doesn't churn the persisted file.

        Median (not mean) so a single failed run with degenerate accuracy
        can't drag the reference; window-bounded so the baseline tracks
        the model's actual behaviour over time rather than freezing on
        the day-1 ``initialized`` value.
        """
        with self._lock:
            history = list(self._data.get("history", []))
        scored = [
            float(h["accuracy_after"]) for h in history[:window]
            if isinstance(h.get("accuracy_after"), (int, float))
            and h.get("status") == "success"
        ]
        if len(scored) < min_history:
            return None
        scored_sorted = sorted(scored)
        n = len(scored_sorted)
        median = (
            scored_sorted[n // 2] if n % 2
            else (scored_sorted[n // 2 - 1] + scored_sorted[n // 2]) / 2.0
        )
        current = self.baseline().get("value")
        if current is not None and abs(float(current) - median) < self._rotation_eps:
            return None
        self.update_baseline(median, source="rolling_median_30d")
        return self.baseline()

    # -- mutations ----------------------------------------------------------

    def update_settings(
        self,
        enabled: Optional[bool] = None,
        frequency_days: Optional[int] = None,
        auto_inference_after_train: Optional[bool] = None,
    ) -> dict[str, Any]:
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

    def record_run(self, entry: dict[str, Any]) -> None:
        with self._lock:
            history: list[dict[str, Any]] = self._data.get("history", [])
            history.insert(0, entry)
            self._data["history"] = history[: self._max_history]
            self._data["last_auto_retrain"] = entry.get("date", _utcnow().isoformat())
            self._data["next_scheduled"] = self._compute_next_scheduled()
        self.save()

# ---------------------------------------------------------------------------
#  Drift detection (live: predicted vs YaumiLive actuals)
# ---------------------------------------------------------------------------

_NO_DRIFT: dict[str, Any] = {
    "status": "stable",
    # ``recent_accuracy`` is the apples-to-apples number: raw model
    # forecast vs invoiced actuals, scored under the SAME composite
    # function the training-time baseline uses. ``delta`` is therefore a
    # pure model-quality signal -- not contaminated by the reconciliation
    # lift that the operational tile sees.
    "recent_accuracy": None,
    "baseline_accuracy": None,
    "delta": None,
    "source": "unavailable",
    # Operational lens on the same window: V5_b reconciled van-load vs
    # invoiced actuals. Always None on the test_set fallback path -- test
    # predictions don't have a van-load to reconcile against.
    "recent_reconciled_accuracy": None,
    # Sample size that fed the recent score (cells where actual > 0 AND
    # predicted > 0). Surfaced so the UI can render "n cells scored".
    "rows_compared": None,
}

# Drift result cache - avoids hammering YaumiLive on every UI page load.
# The Step Functions drift job calls compute_drift_status with
# ``bypass_cache=True``, so this TTL only applies to interactive API calls.
#
# Lock guards every read+TTL-check+write of the pair so two UI threads
# arriving within the same TTL window cannot both pass the staleness
# check and both run the (expensive) YaumiLive comparison; only the
# first wins and the second returns the freshly-cached value. Also
# eliminates the torn read where one thread sees the new ``_drift_cache``
# under the old ``_drift_cache_ts`` (or vice versa).
_drift_cache: dict[str, Any] = {}
_drift_cache_ts: float = 0.0
_drift_cache_lock = threading.Lock()

def compute_drift_status(
    artifact_svc: Any,
    accuracy_svc: Any = None,
    settings: Optional[Settings] = None,
    bypass_cache: bool = False,
) -> dict[str, Any]:
    """Compare live post-training accuracy against the training-time baseline.

    **Primary (live)**: queries the trailing ``drift_lookback_days``
    calendar days of predictions vs actual sales from YaumiLive via
    ``AccuracyService.get_comparison``. This is real drift detection --
    the model's predictions are scored against what customers actually
    bought AFTER training.

    **Fallback (test-set)**: if the live DB is unavailable, splits the static
    test predictions into recent vs baseline. Less meaningful but still a
    signal.

    Returns ``{"status", "recent_accuracy", "baseline_accuracy", "delta",
    "source": "live"|"test_set"|"unavailable"}``.
    """
    global _drift_cache, _drift_cache_ts

    s = settings or get_settings()
    if not bypass_cache:
        with _drift_cache_lock:
            if (
                _drift_cache
                and (time.time() - _drift_cache_ts) < s.drift_cache_ttl_seconds
            ):
                return dict(_drift_cache)

    warn = s.drift_warn_threshold
    alert = s.drift_alert_threshold

    # Baseline accuracy = training-time WAPE over the test split. Computed
    # inline so this module does not import from the API layer (services must
    # not depend on routes - the dependency runs the other direction).
    baseline_acc = _training_baseline_accuracy(artifact_svc)

    # --- Primary: live accuracy from YaumiLive ---
    recent_acc: Optional[float] = None
    recent_reconciled: Optional[float] = None
    rows_compared: Optional[int] = None
    source = "unavailable"

    if accuracy_svc is not None and getattr(accuracy_svc, "available", False):
        try:
            start, end = _recent_window(s)
            # ``limit=None`` returns every row in the window -- drift must
            # see the full population, not a TOP-N truncation, otherwise
            # ``recent_accuracy`` drifts from baseline by a sampling
            # artefact (rows beyond the cap silently skip the WAPE
            # numerator).
            result = accuracy_svc.get_comparison(start_date=start, end_date=end, limit=None)
            summary = result.get("summary") or {}
            if result.get("success") and summary:
                # ``model_accuracy_pct`` is the raw model forecast scored
                # under the same composite function as the baseline -- the
                # ONLY honest input to the recent-vs-baseline delta.
                # ``reconciled_accuracy_pct`` rides alongside as the
                # operational lens for the UI to surface separately.
                # ``None`` is "no data scored" (no overlapping rows in the
                # window). A real 0.0 is "model missed everything" -- a
                # genuine signal we must NOT suppress, otherwise drift
                # silently falls through to the test-set fallback when
                # the model is at its worst.
                live_acc = summary.get("model_accuracy_pct")
                if live_acc is not None:
                    recent_acc = round(float(live_acc), 2)
                    recon = summary.get("reconciled_accuracy_pct")
                    recent_reconciled = round(float(recon), 2) if recon is not None else None
                    rc = summary.get("rows_compared")
                    rows_compared = int(rc) if rc is not None else None
                    source = "live"
        except Exception as exc:
            logger.warning("Drift: live comparison failed, falling back to test-set: %s", exc)

    # --- Fallback: test-set split ---
    # No reconciliation in this path -- test predictions don't have a
    # van-load. ``recent_reconciled_accuracy`` stays None so the UI knows
    # to hide that row.
    if recent_acc is None:
        recent_acc = _test_set_recent_accuracy(artifact_svc)
        if recent_acc is not None:
            source = "test_set"

    if baseline_acc is None or recent_acc is None:
        result = {
            **_NO_DRIFT,
            "recent_accuracy": recent_acc,
            "baseline_accuracy": baseline_acc,
            "source": source,
            "recent_reconciled_accuracy": recent_reconciled,
            "rows_compared": rows_compared,
        }
        with _drift_cache_lock:
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
        "recent_reconciled_accuracy": recent_reconciled,
        "rows_compared": rows_compared,
    }
    DRIFT_PCT.set(float(delta) if delta is not None else 0.0)
    with _drift_cache_lock:
        # Track the prior status under the same lock so the webhook
        # below only fires on a state TRANSITION into "significant",
        # not on every poll that happens to land on the same status.
        # Without this the dashboard's 5-min polling cycle would
        # spam alerts.
        prior = _drift_cache.get("status") if _drift_cache else None
        _drift_cache, _drift_cache_ts = result, time.time()
    if status == "significant" and prior != "significant":
        _fire_drift_webhook(s, result)
    return result


def _fire_drift_webhook(settings: Settings, result: dict[str, Any]) -> None:
    """POST a drift-alert payload to ``YF_DRIFT_WEBHOOK_URL`` when the
    detector first transitions into ``significant`` state.

    Fire-and-forget by design: a slow or down webhook endpoint MUST
    NOT block drift computation (which is on the interactive UI path
    via /retrain/config). 5s timeout + caught exception covers the
    full surface; if the webhook fails, the drift state is already
    logged + exposed on the prometheus DRIFT_PCT gauge so ops still
    has signal through other channels.

    Body shape is intentionally minimal -- callers can wire to Slack
    incoming webhooks (which accept ``{"text": ...}``), PagerDuty
    Events API, or a custom endpoint. The structured ``drift`` field
    carries the full metric breakdown for downstream parsing.

    No-op when ``YF_DRIFT_WEBHOOK_URL`` is unset -- dev environments
    skip the webhook entirely without configuration noise.
    """
    url = (os.environ.get("YF_DRIFT_WEBHOOK_URL") or "").strip()
    if not url:
        return
    delta = result.get("delta")
    payload = {
        "text": (
            f"[demand_forecasting] DRIFT SIGNIFICANT: accuracy dropped "
            f"{abs(delta) if delta is not None else '?'} pts "
            f"(recent={result.get('recent_accuracy')}, "
            f"baseline={result.get('baseline_accuracy')}, "
            f"source={result.get('source')})"
        ),
        "drift": result,
    }
    try:
        import httpx
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json=payload)
        logger.info("Drift webhook fired -> %s", url)
    except Exception as exc:
        logger.warning(
            "Drift webhook POST failed (URL=%s): %s -- continuing "
            "(prometheus gauge + log line still carry the signal)",
            url, exc,
        )

def _recent_window(settings: Settings) -> tuple[str, str]:
    """Trailing N-calendar-day window used by the drift detector.

    N comes from ``settings.drift_lookback_days``. Returns ISO
    ``(start_date, end_date)`` inclusive, ending today. The previous
    implementation rounded to "working days" via a data_import HTTP
    round-trip; that endpoint is gone and the dashboard's reporting
    period is now an arbitrary user-chosen range, so drift consistently
    uses calendar days here -- one source of truth, no network hop.
    """
    now = _utcnow()
    return (
        (now - timedelta(days=settings.drift_lookback_days)).strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
    )

def _composite_accuracy(
    actual: pd.Series,
    pred: pd.Series,
    demand_class: Optional[pd.Series] = None,
    *,
    settings: Optional[Settings] = None,
) -> Optional[float]:
    """Coerce to float, run :func:`composite_summary` under the SAME
    config-driven tolerances training used, return accuracy_pct or None
    when nothing scored.

    ``settings`` is taken from ``get_settings()`` when not supplied; the
    optional kwarg keeps the function unit-testable without monkey
    patching the settings module."""
    a = pd.to_numeric(actual, errors="coerce").fillna(0).to_numpy()
    p = pd.to_numeric(pred, errors="coerce").fillna(0).to_numpy()
    cls = demand_class.astype(str).to_numpy() if demand_class is not None else None
    s = settings or get_settings()
    kwargs = composite_kwargs_from_yaml(s.pipeline_config)
    stats = composite_summary(a, p, cls, **kwargs)
    return stats["accuracy_pct"] if stats["rows_compared"] > 0 else None


def _training_baseline_accuracy(svc: Any) -> Optional[float]:
    """Composite accuracy over the full held-out test set.

    Column resolution lives on ``ArtifactService`` so this code path and
    the summary endpoint share one schema-discipline implementation --
    a future artifact rename only needs to update the resolver, not
    every caller."""
    try:
        test_df, _ = svc.get_test_predictions(
            limit=int(get_settings().summary_test_predictions_limit), offset=0,
        )
    except Exception as exc:
        logger.warning("Drift: baseline fetch failed: %s", exc)
        return None
    if test_df.empty:
        return None
    pred_col = svc.resolve_prediction_column(test_df)
    actual_col = svc.resolve_actual_column(test_df)
    if pred_col is None or actual_col is None:
        return None
    cls = test_df["class"] if "class" in test_df.columns else None
    return _composite_accuracy(test_df[actual_col], test_df[pred_col], cls)


def _test_set_recent_accuracy(svc: Any) -> Optional[float]:
    """Composite accuracy on the last 7 days of test_predictions.csv --
    fallback when the live DB is unreachable. Shares column resolution
    with the baseline path via ``ArtifactService``."""
    try:
        test_df, _ = svc.get_test_predictions(
            limit=int(get_settings().summary_test_predictions_limit), offset=0,
        )
    except Exception:
        return None
    if test_df.empty:
        return None
    pred_col = svc.resolve_prediction_column(test_df)
    actual_col = svc.resolve_actual_column(test_df)
    if pred_col is None or actual_col is None:
        return None

    if "TrxDate" in test_df.columns:
        dates = pd.to_datetime(test_df["TrxDate"], errors="coerce")
        max_date = dates.max()
        if pd.notna(max_date):
            test_df = test_df[dates >= (max_date - pd.Timedelta(days=7))]

    cls = test_df["class"] if "class" in test_df.columns else None
    return _composite_accuracy(test_df[actual_col], test_df[pred_col], cls)
# ---------------------------------------------------------------------------
#  Scheduler job: check_and_retrain
# ---------------------------------------------------------------------------

# Module-level state to track an in-progress auto-retrain
_auto_retrain_pending: dict[str, Any] = {}
_pending_lock = threading.Lock()

def _max_sales_recent_date(s: Settings) -> Optional[pd.Timestamp]:
    """Latest ``TrxDate`` in the data_import sales_recent CSV mirror.

    Returns None when the file is missing/empty or has no parseable
    dates -- the caller treats that as "freshness unknown" and lets
    the retrain proceed rather than blocking on a missing signal.
    """
    path = Path(getattr(s, "sales_recent_file", ""))
    if not path or not path.exists():
        return None
    try:
        df = pd.read_csv(path, usecols=["TrxDate"])
    except (ValueError, KeyError, OSError):
        return None
    if df.empty:
        return None
    parsed = pd.to_datetime(df["TrxDate"], errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().normalize()


def check_and_retrain(
    config: AutoRetrainConfig,
    pipeline_service: Any,
    artifact_service: Any,
    settings: Optional[Settings] = None,
) -> None:
    """Called periodically by APScheduler. Non-blocking."""
    s = settings or get_settings()

    global _auto_retrain_pending

    # Refresh the Prometheus gauge so the metric stays fresh between
    # interactive API calls.
    update_last_train_age(artifact_service)

    # Lazy baseline init: on first tick (or whenever a previous deployment
    # left the baseline unset) seed it from the current recent_accuracy
    # so drift starts measuring as soon as the service has data. The
    # ``initialized`` source label tells consumers this is a cold-start
    # baseline (vs the eventual ``rolling_median_30d``).
    persisted_baseline = config.baseline()
    if persisted_baseline.get("value") is None:
        try:
            from demand_forecasting_pipeline.api.routes.summary import forecast_summary
            current = forecast_summary(artifact_service).accuracy_pct
            if current is not None:
                config.update_baseline(float(current), source="initialized")
                logger.info(
                    "auto_retrain: baseline initialized at %.2f%% (source=initialized)",
                    current,
                )
        except Exception as exc:
            logger.warning("auto_retrain: baseline initialization deferred: %s", exc)
    else:
        # Rotation: once enough successful retrain runs are on record,
        # promote the baseline from ``initialized`` to a rolling median
        # of the trailing window. Idempotent and bounded -- only fires
        # when the median actually moves.
        try:
            rotated = config.rotate_baseline_if_eligible(
                window=int(s.baseline_history_window),
                min_history=int(s.baseline_min_history),
            )
            if rotated is not None:
                logger.info(
                    "auto_retrain: baseline rotated to %.2f%% (source=%s, set_at=%s)",
                    rotated.get("value"), rotated.get("source"), rotated.get("set_at"),
                )
        except Exception as exc:
            logger.warning("auto_retrain: baseline rotation skipped: %s", exc)

    # 1. Check if a previous auto-retrain completed and record it
    with _pending_lock:
        if _auto_retrain_pending:
            train_status = pipeline_service.get_status("train")
            st = train_status.get("status", "")
            if st in ("success", "failed"):
                entry: dict[str, Any] = {
                    "date": _auto_retrain_pending.get("started_at", _utcnow().isoformat()),
                    # ``trigger`` is carried over from the originating
                    # tick: "schedule" (time-based cadence) or "drift"
                    # (drift-accelerated). Defaults to "scheduled" for
                    # legacy rows persisted by older code -- so reading
                    # mixed history never crashes a UI summary.
                    "trigger": _auto_retrain_pending.get("trigger", "scheduled"),
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

    # 2. Decide whether to retrain. Two independent triggers:
    #    (a) Time floor -- enabled AND time-based cadence has elapsed.
    #    (b) Drift accelerator -- recent accuracy has dropped past the
    #        alert threshold AND the cooldown gate is open.
    # Either firing is enough; if both are true, the time-based trigger
    # wins for audit purposes (it's the primary path). The cooldown
    # prevents wobble around the alert threshold from causing a retrain
    # storm; it is computed dynamically below from the operator-chosen
    # ``frequency_days`` so it scales with the selected cadence.
    time_due = config.is_due()
    trigger: Optional[str] = "schedule" if time_due else None
    if not time_due and getattr(s, "retrain_on_drift_alert_enabled", True):
        drift = compute_drift_status(artifact_service, settings=s)
        # ``compute_drift_status`` emits one of three labels:
        #   "stable"      -- accuracy delta within ``drift_warn_threshold``
        #   "drifting"    -- delta between warn and alert thresholds
        #   "significant" -- delta exceeds ``drift_alert_threshold`` (this
        #                    is the firing condition for the accelerator)
        if (drift or {}).get("status") == "significant":
            # Adaptive cooldown: scales with the operator's chosen
            # ``frequency_days`` so the accelerator stays proportional
            # to the selected cadence. A 7-day operator gets a ~2-day
            # cooldown; a 21-day operator gets ~5 days; a 30-day
            # operator gets ~8 days. Floor at ``retrain_cooldown_min_days``
            # so the cooldown is never zero. Read ``frequency_days``
            # from the persisted config (operator-controlled via UI),
            # NOT from settings, so a runtime UI change takes effect on
            # the next tick without a service restart.
            cfg_now = config.get() or {}
            freq_days = int(cfg_now.get(
                "frequency_days",
                getattr(s, "retrain_default_frequency_days", 14),
            ))
            # ``math.ceil`` (not round/floor) for the cooldown:
            #   * matches the codebase's convention for safety/
            #     conservative thresholds (see page_views.py inventory
            #     loading: math.ceil on units_to_load + opening_stock).
            #   * avoids Python's banker's rounding for X.5 inputs
            #     (round(0.5)=0, round(1.5)=2, round(2.5)=2 -- not
            #     monotonic in a way an operator would expect).
            #   * symmetric with ``gap_days_observed`` below, which is
            #     ``timedelta.days`` (floor by definition). Combining
            #     floor-on-elapsed with ceil-on-threshold gives a
            #     strictly conservative gate -- the comparison
            #     ``gap_days_observed >= cooldown_days`` only opens
            #     after at least ceil(freq*fraction) FULL elapsed days.
            cooldown_days = max(
                int(getattr(s, "retrain_cooldown_min_days", 1)),
                math.ceil(freq_days * float(
                    getattr(s, "retrain_cooldown_fraction", 0.25)
                )),
            )
            last_iso = cfg_now.get("last_auto_retrain")
            gap_ok = True
            gap_days_observed: Optional[int] = None
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(str(last_iso))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    gap_days_observed = (_utcnow() - last_dt).days
                    gap_ok = gap_days_observed >= cooldown_days
                except (ValueError, TypeError):
                    gap_ok = True  # unparseable -> treat as cold start
            if gap_ok:
                trigger = "drift"
                logger.warning(
                    "auto_retrain: drift-accelerated trigger "
                    "(delta=%s%%, recent=%s%%, baseline=%s%%, "
                    "frequency=%dd, cooldown=%dd)",
                    drift.get("delta"),
                    drift.get("recent_accuracy"),
                    drift.get("baseline_accuracy"),
                    freq_days, cooldown_days,
                )
            else:
                logger.info(
                    "auto_retrain: drift alert seen but within cooldown "
                    "(observed_gap=%sd, cooldown=%dd, frequency=%dd); "
                    "deferring",
                    gap_days_observed, cooldown_days, freq_days,
                )
    if trigger is None:
        return

    # 3. Check if pipeline is already running
    train_status = pipeline_service.get_status("train")
    if train_status.get("status") == "running":
        logger.debug("Auto-retrain: training already running, skipping")
        return

    # 4. Data-freshness guard. Re-training on stale data wastes a full
    # pipeline cycle for no accuracy lift -- check that the upstream
    # data_import has produced new sales rows since the last successful
    # auto-retrain. The check is best-effort (any failure falls through
    # to allow the run); we only skip when we have positive evidence
    # that nothing changed.
    last_run_iso = (config.get() or {}).get("last_auto_retrain")
    if last_run_iso:
        try:
            max_dt = _max_sales_recent_date(s)
            last_run_dt = pd.Timestamp(last_run_iso).normalize()
            if max_dt is not None and max_dt <= last_run_dt:
                logger.info(
                    "Auto-retrain: skipping -- upstream sales_recent max_date=%s "
                    "not newer than last_auto_retrain=%s",
                    max_dt.date(), last_run_dt.date(),
                )
                return
        except Exception as exc:
            logger.warning("Auto-retrain: freshness check skipped: %s", exc)

    # 4. Record accuracy_before
    accuracy_before = None
    try:
        from demand_forecasting_pipeline.api.routes.summary import forecast_summary
        summary = forecast_summary(artifact_service)
        accuracy_before = summary.accuracy_pct
    except Exception as exc:
        logger.warning("Could not get pre-retrain accuracy: %s", exc)

    # 5. Start training
    logger.info(
        "Auto-retrain: starting training (trigger=%s accuracy_before=%.1f%%)",
        trigger, accuracy_before or 0,
    )
    result = pipeline_service.run_training()

    if result.get("success"):
        with _pending_lock:
            _auto_retrain_pending = {
                "started_at": _utcnow().isoformat(),
                "accuracy_before": accuracy_before,
                "trigger": trigger,
            }
    else:
        logger.warning("Auto-retrain: failed to start training: %s", result.get("message"))

# ---------------------------------------------------------------------------
#  APScheduler -- replaces the old hand-rolled retrain loop in api/app.py
# ---------------------------------------------------------------------------

def start_scheduler(
    *,
    interval_hours: int,
    job: Callable[[], None],
    logger: Any | None = None,
) -> Any:
    """Build and start a ``BackgroundScheduler`` that runs ``job`` every
    ``interval_hours``. Returns the scheduler so the caller can shut it
    down on app exit.

    APScheduler computes the next run time from a fixed wall-clock anchor,
    not from accumulated ``sleep`` durations -- so a slow job iteration
    no longer pushes every subsequent invocation later. ``misfire_grace_time``
    is generous (1 hour) so a short outage doesn't drop the next tick.

    Failure modes:
      * Scheduler thread dies -> APScheduler restarts the executor.
      * Job raises -> APScheduler logs and continues with the next tick.
      * Import-time fallback: if APScheduler isn't installed, we emit a
        warning and return a no-op stub. The service still boots; drift
        detection still works on demand via the API.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.warning(
            "APScheduler not installed -- auto-retrain check disabled. "
            "Install apscheduler>=3.10 to enable scheduled drift checks."
        )
        return None

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.add_job(
        _safe_run(job, log),
        trigger=IntervalTrigger(hours=int(interval_hours)),
        id="auto_retrain_check",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    return scheduler

def stop_scheduler(scheduler: Any) -> None:
    """Best-effort scheduler shutdown. Tolerates ``None`` so callers can
    pass through the value returned by ``start_scheduler`` without
    branching on the import-fallback path."""
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception as exc:  # pragma: no cover -- shutdown best-effort
        logger.warning("retrain_scheduler shutdown failed: %s", exc)

def _safe_run(job: Callable[[], None], log: Any) -> Callable[[], None]:
    """Wrap ``job`` so APScheduler never sees an unhandled exception
    (which would otherwise be logged but silently re-arm); we record
    a structured warning so ops monitoring sees the failure."""

    def _runner() -> None:
        try:
            job()
        except Exception as exc:  # noqa: BLE001
            log.warning("auto_retrain_job_failed", error=repr(exc))

    return _runner

def update_last_train_age(artifact_svc: Any) -> None:
    """Refresh the ``df_last_train_age_seconds`` Prometheus gauge.

    Called from the retrain check tick so the metric stays fresh
    without a separate poller. Safe to call at any time -- silently
    skips when the artifact summary isn't available.
    """
    try:
        summary = artifact_svc.get_training_summary() or {}
        meta = summary.get("metadata") or {}
        ts = meta.get("trained_at")
        if not ts:
            return
        trained_at = datetime.fromisoformat(str(ts))
        if trained_at.tzinfo is None:
            trained_at = trained_at.replace(tzinfo=timezone.utc)
        age = (_utcnow() - trained_at).total_seconds()
        LAST_TRAIN_AGE_SECONDS.set(max(0.0, age))
    except Exception:
        pass
