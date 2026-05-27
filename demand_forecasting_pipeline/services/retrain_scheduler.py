"""Auto-retrain scheduler: persisted config + drift detection.

Drift computation is the single source of truth for retrain.py + jobs/drift_check.py.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import (
    DRIFT_PCT,
    LAST_TRAIN_AGE_SECONDS,
)

logger = logging.getLogger(__name__)

def _utcnow() -> datetime:
    """UTC ISO-8601 with offset; centralised post-utcnow() deprecation."""
    return datetime.now(timezone.utc)


# Champion-challenger promotion gate; pure function (no I/O, deterministic).

@dataclass(frozen=True)
class PromotionDecision:
    """Gate outcome; comparison details audited via history + manifest."""
    action: str             # "promote" | "reject" | "cold_start"
    champion_accuracy: Optional[float]
    challenger_accuracy: Optional[float]
    delta_pp: Optional[float]      # challenger - champion (positive = improvement)
    threshold_pp: float            # configured max_regression_pp at evaluation time
    reason: str

    def update_pointer(self) -> bool:
        """Maps action to ModelRegistry.update_pointer; only ``reject`` holds back."""
        return self.action != "reject"


def _finite_score(x: Optional[float]) -> Optional[float]:
    """Coerce to finite float else None; prevents NaN-silent promotion."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def evaluate_challenger(
    challenger_acc: Optional[float],
    champion_acc: Optional[float],
    *,
    max_regression_pp: float,
) -> PromotionDecision:
    """Pure decision: cold_start when champion non-finite; reject when challenger non-finite
    OR < champion - tolerance; else promote. One-sided gate (flat delta still promotes)."""
    threshold = max(0.0, float(max_regression_pp))
    champ = _finite_score(champion_acc)
    chall = _finite_score(challenger_acc)
    if champ is None:
        return PromotionDecision(
            action="cold_start",
            champion_accuracy=None,
            challenger_accuracy=chall,
            delta_pp=None,
            threshold_pp=threshold,
            reason="no comparable champion score on record; promoting unconditionally",
        )
    if chall is None:
        return PromotionDecision(
            action="reject",
            champion_accuracy=champ,
            challenger_accuracy=None,
            delta_pp=None,
            threshold_pp=threshold,
            reason="challenger accuracy missing or non-finite; refusing to promote an unknown",
        )
    delta = chall - champ
    if delta < -threshold:
        return PromotionDecision(
            action="reject",
            champion_accuracy=champ,
            challenger_accuracy=chall,
            delta_pp=delta,
            threshold_pp=threshold,
            reason=(f"regression {-delta:.2f}pp exceeds tolerance "
                    f"{threshold:.2f}pp (champion={champ:.2f}%, "
                    f"challenger={chall:.2f}%)"),
        )
    return PromotionDecision(
        action="promote",
        champion_accuracy=champ,
        challenger_accuracy=chall,
        delta_pp=delta,
        threshold_pp=threshold,
        reason=(f"delta {delta:+.2f}pp within tolerance {threshold:.2f}pp "
                f"(champion={champ:.2f}%, challenger={chall:.2f}%)"),
    )

# AutoRetrainConfig: thread-safe JSON persistence; env-overridable timeout for cached UI hot path.

class AutoRetrainConfig:
    """Loads / saves the retrain config JSON with atomic writes and a lock."""

    def __init__(self, path: Optional[str] = None, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._path = Path(path or s.retrain_config_path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        # Settings-driven defaults; env-tunable without code changes.
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
            # Baseline tracking; init on first read, later rolling-median of recent_accuracy.
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
            # Shared atomic save_json (tmp + os.replace; single-syscall atomic).
            from demand_forecasting_pipeline.src.utils.io_utils import save_json
            save_json(self._data, str(self._path))

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
        """Persist baseline + source + timestamp; idempotent."""
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
        """Rotate baseline to trailing rolling-median when min_history is met; None else.
        Median + epsilon-bounded write so flat history doesn't churn the file."""
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
    # recent_accuracy: raw model vs actuals (drift-comparable to baseline).
    "recent_accuracy": None,
    "baseline_accuracy": None,
    "delta": None,
    "source": "unavailable",
    # Operational reconciled lens; None on test_set fallback (no van-load).
    "recent_reconciled_accuracy": None,
    # Cells where actual>0 AND predicted>0 (UI shows "n cells scored").
    "rows_compared": None,
}

# Drift cache; UI TTL only (Step Functions bypasses).
# Lock guards read+TTL+write so concurrent UI threads share one compute.
_drift_cache: dict[str, Any] = {}
_drift_cache_ts: float = 0.0
_drift_cache_lock = threading.Lock()


def invalidate_drift_cache() -> None:
    """Clear cached drift snapshot; called from training_outcome so the next read is fresh.
    """
    global _drift_cache, _drift_cache_ts
    with _drift_cache_lock:
        _drift_cache, _drift_cache_ts = {}, 0.0


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

    # Baseline = shared scorer over full test split (same call as Pipeline-summary tile).
    baseline_acc, _ = artifact_svc.score_test_predictions()

    # Primary: live accuracy from YaumiLive.
    recent_acc: Optional[float] = None
    recent_reconciled: Optional[float] = None
    rows_compared: Optional[int] = None
    source = "unavailable"

    if accuracy_svc is not None and getattr(accuracy_svc, "available", False):
        try:
            start, end = _recent_window(s)
            # limit=None: drift sees full population (TOP-N would sampling-bias the WAPE).
            result = accuracy_svc.get_comparison(start_date=start, end_date=end, limit=None)
            summary = result.get("summary") or {}
            if result.get("success") and summary:
                # model_accuracy_pct: raw forecast under baseline's composite (only honest delta input).
                # reconciled_accuracy_pct: operational lens shown separately.
                # None = no data scored; 0.0 = real miss (don't suppress -- drift signal).
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

    # Fallback: test-set split (no van-load -> recent_reconciled_accuracy stays None).
    if recent_acc is None:
        recent_acc, _ = artifact_svc.score_test_predictions(
            recent_window_days=s.drift_lookback_days,
        )
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
        # Track prior status under lock so webhook only fires on TRANSITION into "significant".
        prior = _drift_cache.get("status") if _drift_cache else None
        _drift_cache, _drift_cache_ts = result, time.time()
    if status == "significant" and prior != "significant":
        _fire_drift_webhook(s, result)
    return result


def _fire_drift_webhook(settings: Settings, result: dict[str, Any]) -> None:
    """POST drift alert to YF_DRIFT_WEBHOOK_URL on first transition into significant.

    Fire-and-forget with 5s timeout; Slack/PagerDuty compatible.
    No-op when URL unset.
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
    """Trailing N-calendar-day (start, end) window from drift_lookback_days."""
    now = _utcnow()
    return (
        (now - timedelta(days=settings.drift_lookback_days)).strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
    )

# ---------------------------------------------------------------------------
#  Scheduler job: check_and_retrain
# ---------------------------------------------------------------------------

# Module-level state to track an in-progress auto-retrain
_auto_retrain_pending: dict[str, Any] = {}
_pending_lock = threading.Lock()

def _max_sales_recent_date(s: Settings) -> Optional[pd.Timestamp]:
    """Latest TrxDate in sales_recent CSV mirror; None on missing/empty (freshness unknown)."""
    # ``sales_recent_file`` is a bare filename; resolve through shared_data_path
    # so the existence check actually hits the mirrored CSV under <data-root>/imports.
    filename = getattr(s, "sales_recent_file", "") or ""
    if not filename:
        return None
    path = s.shared_data_path(filename)
    if not path.exists():
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

    # Refresh Prometheus gauge between interactive calls.
    update_last_train_age(artifact_service)

    # Lazy baseline init on first tick; source='initialized' vs eventual 'rolling_median_30d'.
    persisted_baseline = config.baseline()
    if persisted_baseline.get("value") is None:
        try:
            from demand_forecasting_pipeline.services.forecast_kpi import compute_forecast_summary
            current = compute_forecast_summary(artifact_service).accuracy_pct
            if current is not None:
                config.update_baseline(float(current), source="initialized")
                logger.info(
                    "auto_retrain: baseline initialized at %.2f%% (source=initialized)",
                    current,
                )
        except Exception as exc:
            logger.warning("auto_retrain: baseline initialization deferred: %s", exc)
    else:
        # Rotation: promote initialized -> rolling-median when min_history is met.
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

    # 1. Check if a previous auto-retrain completed and clear the
    #    single-flight latch. Post-completion recording (history entry,
    #    version snapshot, champion-challenger gate, reject-path
    #    rollback) is owned by ``PipelineService._execute`` via the
    #    shared ``services.training_outcome.record_training_outcome``
    #    helper -- this scheduler tick just observes the completion to
    #    release the latch and optionally chain inference. Manual
    #    triggers reach the same helper through ``_execute`` too, so
    #    the audit trail is uniform regardless of who started the run.
    with _pending_lock:
        if _auto_retrain_pending:
            train_status = pipeline_service.get_status("train")
            st = train_status.get("status", "")
            if st in ("success", "failed"):
                if st == "failed":
                    # Failed runs aren't versioned (no new artifacts to
                    # snapshot) but still get a history row so the
                    # operator can see the failure. Success rows are
                    # written by PipelineService -- this branch is the
                    # only path that records failures.
                    failure_entry: dict[str, Any] = {
                        "date": _auto_retrain_pending.get(
                            "started_at", _utcnow().isoformat(),
                        ),
                        "trigger": _auto_retrain_pending.get(
                            "trigger", "scheduled",
                        ),
                        "accuracy_before": _auto_retrain_pending.get(
                            "accuracy_before",
                        ),
                        "accuracy_after": None,
                        "duration_seconds": train_status.get(
                            "duration_seconds", 0,
                        ),
                        "status": "failed",
                    }
                    try:
                        config.record_run(failure_entry)
                    except Exception as exc:
                        logger.warning(
                            "auto_retrain: failed to record failure entry: %s",
                            exc,
                        )
                logger.info("Auto-retrain completed: status=%s", st)

                # Inference auto-chain is owned by ``PipelineService._
                # maybe_chain_inference`` for both manual AND auto runs.
                # We deliberately do NOT fire ``run_inference()`` from
                # this branch: after the train completes, _execute's
                # chain already ran. If we fired again here, the check
                # ``inference.status != "running"`` would PASS (status
                # is "success" by now, not "running") and we'd spawn a
                # DUPLICATE inference run.
                #
                # This branch's sole remaining job is to release the
                # ``_auto_retrain_pending`` single-flight latch so the
                # next tick is free to evaluate a fresh trigger.

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
        from demand_forecasting_pipeline.services.forecast_kpi import compute_forecast_summary
        summary = compute_forecast_summary(artifact_service)
        accuracy_before = summary.accuracy_pct
    except Exception as exc:
        logger.warning("Could not get pre-retrain accuracy: %s", exc)

    # 5. Start training. Pass trigger + accuracy_before through so the
    # audit hook (in pipeline_service._execute) records the right
    # origin and pre-training accuracy. Single-flight is enforced by
    # PipelineService's own status guard; we still set the local
    # ``_auto_retrain_pending`` latch so the next scheduler tick can
    # tell auto-initiated runs apart from manual ones (for
    # post-completion housekeeping like the auto_inference_after_train
    # chain).
    logger.info(
        "Auto-retrain: starting training (trigger=%s accuracy_before=%.1f%%)",
        trigger, accuracy_before or 0,
    )
    result = pipeline_service.run_training(
        trigger=trigger, accuracy_before=accuracy_before,
    )

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
    # Wire the same audit-log sink the other crons use so silent
    # failures of the drift check show up in ``yf_scheduler_log``
    # alongside the data_import / reconciliation_refresh /
    # recommended_order_generation entries. Without this hook the
    # 6-hour drift detector could die quietly and operators would only
    # notice when ``recent_accuracy`` stopped advancing. Mirrors the
    # call in ``reconciliation_refresh.py:1242-1243``.
    # Narrow except to environmental causes only -- ImportError if the
    # audit module isn't on PYTHONPATH, AttributeError if the settings
    # shape changed. Real DB connection errors propagate so misconfig
    # surfaces loudly at startup instead of silently degrading
    # observability across every other audited job.
    try:
        from common.scheduler_audit import attach_audit
        from demand_forecasting_pipeline.config.settings import get_settings
        s = get_settings()
        attach_audit(scheduler, "demand_forecasting", s.db.connection_string())
    except (ImportError, AttributeError) as exc:
        log.warning(
            "auto_retrain_check audit hook not attached (%s); "
            "scheduler runs but yf_scheduler_log will not record its fires",
            exc,
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
