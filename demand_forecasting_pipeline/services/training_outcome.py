"""Shared training-completed post-processing: history, version registry, champion-challenger gate.

Used by both check_and_retrain (auto) and PipelineService._execute (manual) so
behaviour is identical regardless of trigger. Failures are logged + swallowed
(the training run already succeeded; audit trail is best-effort).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def record_training_outcome(
    *,
    pipeline_service: Any,
    artifact_service: Any,
    retrain_config: Any,
    settings: Any,
    trigger: str,
    accuracy_before: float | None = None,
    started_at: str | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Post-process a successful training run; returns persisted entry or None on non-success.

    trigger: "manual" / "schedule" / "drift". accuracy_before optional (scheduler
    snapshots pre-run; manual passes None). started_at / duration_seconds default
    to pipeline_service.get_status('train').
    """
    train_status = pipeline_service.get_status("train")
    st = train_status.get("status", "")
    if st != "success":
        # Failed runs surface via /pipeline/status; no artifacts to version, no history row.
        logger.debug(
            "record_training_outcome: train status=%s (not success); "
            "skipping audit hook", st,
        )
        return None

    if duration_seconds is None:
        duration_seconds = float(train_status.get("duration_seconds") or 0.0)
    if started_at is None:
        started_at = (
            train_status.get("started_at")
            or datetime.now(UTC).isoformat()
        )

    entry: dict[str, Any] = {
        "date": started_at,
        "trigger": trigger,
        "accuracy_before": accuracy_before,
        "accuracy_after": None,
        "duration_seconds": duration_seconds,
        "status": "success",
    }

    # accuracy_after via the UI's KPI helper (no divergence from dashboard).
    try:
        from demand_forecasting_pipeline.services.forecast_kpi import (
            compute_forecast_summary,
        )
        artifact_service.invalidate_cache()
        summary = compute_forecast_summary(artifact_service)
        entry["accuracy_after"] = summary.accuracy_pct
    except Exception as exc:
        logger.warning(
            "record_training_outcome: post-train accuracy probe failed: %s",
            exc,
        )

    # Version snapshot + champion-challenger gate + reject-path rollback.
    try:
        # Lazy imports keep this module cheap on the hot paths.
        from demand_forecasting_pipeline.services.model_registry import (
            get_model_registry,
        )
        from demand_forecasting_pipeline.services.retrain_scheduler import (
            PromotionDecision,
            evaluate_challenger,
        )
        registry = get_model_registry()

        # Capture champion BEFORE record_version so gate sees pre-promotion state.
        champion = registry.current_version()
        gate_enabled = bool(
            getattr(settings, "champion_challenger_enabled", True),
        )
        if gate_enabled:
            decision = evaluate_challenger(
                challenger_acc=entry.get("accuracy_after"),
                champion_acc=(
                    champion.accuracy_after if champion else None
                ),
                max_regression_pp=float(getattr(
                    settings, "challenger_max_regression_pp", 1.0,
                )),
            )
        else:
            # Gate disabled -- promote unconditionally (decision distinguishes "no gate ran").
            decision = PromotionDecision(
                action="promote",
                champion_accuracy=(
                    champion.accuracy_after if champion else None
                ),
                challenger_accuracy=entry.get("accuracy_after"),
                delta_pp=None,
                threshold_pp=0.0,
                reason="champion-challenger gate disabled",
            )

        version_id = registry.record_version(
            entry,
            trigger=trigger,
            promoted_by=trigger,
            update_pointer=decision.update_pointer(),
            decision=(decision.action if gate_enabled else None),
            champion_accuracy=decision.champion_accuracy,
            delta_pp=decision.delta_pp,
        )
        entry["version_id"] = version_id
        entry["promotion_decision"] = (
            decision.action if gate_enabled else "promote"
        )
        entry["promotion_reason"] = decision.reason

        if decision.action == "reject":
            logger.warning(
                "training_outcome[%s]: challenger REJECTED -- %s "
                "(snapshot=%s)", trigger, decision.reason, version_id,
            )
            if champion is not None:
                try:
                    restored = registry.rollback_if_current(
                        champion.version_id,
                        expected_current=champion.version_id,
                        promoted_by="gate-rejected",
                    )
                    if restored is not None:
                        logger.warning(
                            "training_outcome[%s]: live files restored "
                            "to champion %s",
                            trigger, champion.version_id,
                        )
                    else:
                        logger.warning(
                            "training_outcome[%s]: reject-path rollback "
                            "deferred -- pointer moved during decision "
                            "(operator action?)", trigger,
                        )
                except Exception as exc:
                    logger.error(
                        "training_outcome[%s]: rollback to champion %s "
                        "FAILED after reject: %s -- live state "
                        "inconsistent; next successful retrain will fix",
                        trigger, champion.version_id, exc,
                    )
        else:
            logger.info(
                "training_outcome[%s]: challenger %s -- %s (version=%s)",
                trigger, decision.action.upper(),
                decision.reason, version_id,
            )
    except Exception as exc:
        logger.warning(
            "training_outcome[%s]: version recording failed: %s",
            trigger, exc,
        )

    # Persist history LAST so failures above still surface a row.
    try:
        retrain_config.record_run(entry)
    except Exception as exc:
        logger.error(
            "training_outcome[%s]: failed to persist history entry: %s",
            trigger, exc,
        )

    # Invalidate drift cache (applies on REJECT too; test_predictions was rewritten).
    try:
        from demand_forecasting_pipeline.services.retrain_scheduler import (
            invalidate_drift_cache,
        )
        invalidate_drift_cache()
    except Exception as exc:
        logger.warning(
            "training_outcome[%s]: drift cache invalidation failed: %s",
            trigger, exc,
        )
    return entry
