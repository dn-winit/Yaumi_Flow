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
        self._scheduler = BackgroundScheduler(
            timezone=self._s.auto_visit_timezone, daemon=True,
        )
        self._scheduler.add_job(
            self._service.reconcile_all,
            trigger="interval",
            seconds=interval,
            id=self.JOB_ID,
            # ``max_instances=1`` + ``coalesce=True``: a slow tick skips
            # the next slot rather than queueing; a service restart that
            # crossed several slots only fires the make-up tick once.
            # ``misfire_grace_time=interval`` lets APScheduler still
            # fire after a brief stop instead of dropping the make-up
            # past the default 1s threshold.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=interval,
            next_run_time=datetime.now(),
        )
        self._scheduler.start()
        logger.info(
            "Auto-visit reconciler started -- interval=%ds tz=%s",
            interval, self._s.auto_visit_timezone,
        )

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        try:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        finally:
            self._scheduler = None
