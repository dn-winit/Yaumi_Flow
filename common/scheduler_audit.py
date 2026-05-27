"""Cron-fire audit trail for APScheduler-backed daily jobs.

Writes one ``yf_scheduler_log`` row per job execution from an
``EVENT_JOB_EXECUTED|EVENT_JOB_ERROR`` listener; ``fired_at`` uses
``scheduled_run_time`` so it answers "did this cron fire at its time?"
independent of job duration. Best-effort writes -- a DB hiccup on the
audit table must never crash the scheduler. Schema: see
``scripts/create_tables.sql`` (search ``yf_scheduler_log``).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from common.db_pool import get_pool

logger = logging.getLogger(__name__)

_TABLE = "yf_scheduler_log"
_ERR_MSG_MAX = 2000
_SERVICE_MAX = 50
_JOB_ID_MAX = 100
_STATUS_MAX = 20

# Throttle: at most one WARNING per (service, error-type) per hour, so a sustained
# schema-drift or wrong-table outage is surfaced exactly once an hour instead of
# spamming the log on every cron fire.
_WARN_THROTTLE_SECONDS = 3600.0
_WARN_LAST: dict[tuple[str, str], float] = {}
_WARN_LOCK = threading.Lock()


def _should_warn(service: str, exc: BaseException) -> bool:
    """True iff the (service, exception-type) pair hasn't logged a WARNING
    in the past ``_WARN_THROTTLE_SECONDS``."""
    key = (service, type(exc).__name__)
    now = time.monotonic()
    with _WARN_LOCK:
        last = _WARN_LAST.get(key, 0.0)
        if now - last >= _WARN_THROTTLE_SECONDS:
            _WARN_LAST[key] = now
            return True
    return False


def _write(
    conn_str: str,
    service: str,
    job_id: str,
    fired_at: datetime,
    status: str,
    error_message: str | None,
) -> None:
    """Insert one audit row; DB errors are swallowed (never propagate to
    APScheduler's job-event dispatch loop)."""
    try:
        pool = get_pool(conn_str)
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {_TABLE} "
                f"(service, job_id, fired_at, status, error_message) "
                f"VALUES (?, ?, ?, ?, ?)",
                service[:_SERVICE_MAX],
                job_id[:_JOB_ID_MAX],
                fired_at,
                status[:_STATUS_MAX],
                (error_message[:_ERR_MSG_MAX] if error_message else None),
            )
            conn.commit()
    except Exception as exc:
        # First failure (or first per hour) gets a WARNING so schema drift
        # / bad table name / DSN rot is actually visible; sustained failure
        # downgrades to INFO so we don't drown the log. The audit trail is
        # best-effort -- the job's own success/fail path remains the source
        # of truth -- but a silent break is still a break.
        if _should_warn(service, exc):
            logger.warning(
                "scheduler_audit_write_failed service=%s job=%s err=%r "
                "(throttled to 1/hr per error type)",
                service, job_id, exc,
            )
        else:
            logger.info(
                "scheduler_audit_write_skipped service=%s job=%s err=%r",
                service, job_id, exc,
            )


def attach_audit(scheduler: Any, service: str, conn_str: str) -> None:
    """Wire the audit listener to ``scheduler`` (call once after
    ``add_job`` and before ``scheduler.start()``). ``conn_str`` is the
    AIML DSN -- passed in to avoid pulling a settings dep into common/.
    """
    # Lazy import: keeps APScheduler off common/'s import path for
    # callers that don't use a scheduler.
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

    def _listener(event: Any) -> None:
        fired_at = event.scheduled_run_time or datetime.now()
        if getattr(event, "exception", None) is not None:
            _write(conn_str, service, event.job_id, fired_at,
                   "failed", repr(event.exception))
        else:
            _write(conn_str, service, event.job_id, fired_at,
                   "success", None)

    scheduler.add_listener(_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
