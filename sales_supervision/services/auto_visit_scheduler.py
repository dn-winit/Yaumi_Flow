"""APScheduler wrapper for the auto-visit reconciler.

Runs ``AutoVisitService.reconcile_all`` on a fixed interval. One job,
one in-memory dedup set inside the service, max one instance running
at any time so a slow tick can never overlap itself.

Lifecycle:
  * ``start()``  -- called from the FastAPI lifespan on app startup.
                    No-op if ``auto_visit_enabled=False``.
  * ``shutdown()`` -- called on app shutdown. Idempotent.

This is the only place that imports APScheduler in sales_supervision,
keeping the dependency surface tight.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from sales_supervision.config.settings import Settings, get_settings
from sales_supervision.services.auto_visit_service import AutoVisitService

logger = logging.getLogger(__name__)


class AutoVisitScheduler:
    """Background tick driver for the auto-visit reconciler."""

    JOB_ID = "supervision.auto_visit_reconcile"

    def __init__(
        self,
        service: AutoVisitService,
        *,
        settings: Optional[Settings] = None,
    ) -> None:
        self._s = settings or get_settings()
        self._service = service
        self._scheduler: Optional[BackgroundScheduler] = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        if not self._s.auto_visit_enabled:
            logger.info("Auto-visit reconciler disabled via settings.")
            return
        if self.running:
            return
        interval = max(int(self._s.auto_visit_poll_seconds), 30)
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self._service.reconcile_all,
            trigger="interval",
            seconds=interval,
            id=self.JOB_ID,
            # Don't queue overlapping ticks: if a tick takes longer than
            # the interval, the scheduler skips the next slot rather than
            # piling up workers on a slow warehouse.
            max_instances=1,
            coalesce=True,
            # Run immediately on start so the first tick doesn't wait
            # ``interval`` seconds after a server restart. APScheduler
            # treats ``next_run_time=None`` as "use the trigger default",
            # which on IntervalTrigger is start + interval -- a full
            # cadence wait. Setting an explicit ``datetime.now()``
            # actually fires on next event-loop turn.
            next_run_time=datetime.now(),
        )
        self._scheduler.start()
        logger.info("Auto-visit reconciler started -- interval=%ds", interval)

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        try:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        finally:
            self._scheduler = None
