"""Cron-fire audit trail for APScheduler-backed daily jobs.

One row in ``yf_scheduler_log`` per job execution, written from an
APScheduler event listener. The listener fires AFTER the job returns
(or raises), so the row's ``fired_at`` is the scheduler's planned
``scheduled_run_time`` -- which is the correct timestamp for the
"did this cron fire at its time?" question, independent of how long
the job itself took.

Why a listener (not a per-job decorator):
  * APScheduler exposes ``EVENT_JOB_EXECUTED`` and ``EVENT_JOB_ERROR``
    natively; using them is the idiomatic, zero-glue approach.
  * No job function needs to import this module -- the wiring is a
    single ``attach_audit(scheduler, service, conn_str)`` call at
    scheduler startup, immediately before ``scheduler.start()``.
  * Adding a new cron in any service automatically gets audited the
    moment it's registered on the audited scheduler; no risk of
    forgetting to decorate the new job function.

Why best-effort writes:
  * The scheduler must never crash because the audit table is
    momentarily unreachable. Every audit write is swallowed at the
    INFO log level on failure; the scheduled job itself still runs
    normally either way.

Schema lives in ``scripts/create_tables.sql`` (search for
``yf_scheduler_log``). The table is intentionally narrow: service,
job_id, fire timestamp, status, and (only on failure) the error
message. Duration, route counts, and per-job side effects already
live in each service's own structured logs -- this table is the
single source of truth for the *fire timing* question, nothing more.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from common.db_pool import get_pool

logger = logging.getLogger(__name__)

_TABLE = "yf_scheduler_log"
_ERR_MSG_MAX = 2000
_SERVICE_MAX = 50
_JOB_ID_MAX = 100
_STATUS_MAX = 20


def _write(
    conn_str: str,
    service: str,
    job_id: str,
    fired_at: datetime,
    status: str,
    error_message: str | None,
) -> None:
    """Insert one audit row. Swallows DB errors -- this must never
    propagate back into APScheduler's job-event dispatch loop."""
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
        # INFO not WARNING: a transient DB hiccup during the audit is
        # not actionable on its own. If the underlying scheduled job
        # also failed, its own error path will log loudly with the real
        # root cause.
        logger.info(
            "scheduler_audit_write_skipped service=%s job=%s err=%r",
            service, job_id, exc,
        )


def attach_audit(scheduler: Any, service: str, conn_str: str) -> None:
    """Wire the audit listener to ``scheduler``.

    Call once per BackgroundScheduler, after ``add_job`` calls and
    immediately before ``scheduler.start()``. Every subsequent
    successful or failed job execution writes one ``yf_scheduler_log``
    row attributed to ``service``.

    ``conn_str`` is the AIML DSN string the row will be written to;
    each service passes its own settings.db.connection_string() (or
    equivalent) so we don't pull a settings dependency into common/.
    """
    # Lazy import keeps APScheduler off the common/ import path for
    # callers that don't use a scheduler (tests, the live data API).
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
