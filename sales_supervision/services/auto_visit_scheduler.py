"""APScheduler wrapper for the auto-visit reconciler.

Single job (max_instances=1) on a fixed interval; sole importer of APScheduler
within sales_supervision. start()/shutdown() are wired into the FastAPI lifespan.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from common.scheduler_audit import attach_audit
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
        settings: Settings | None = None,
    ) -> None:
        self._s = settings or get_settings()
        self._service = service
        self._scheduler: BackgroundScheduler | None = None

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
        # next_run_time uses the scheduler's tz so the first tick fires immediately on cross-tz hosts.
        tz = ZoneInfo(self._s.auto_visit_timezone)
        self._scheduler = BackgroundScheduler(timezone=tz, daemon=True)
        self._scheduler.add_job(
            self._service.reconcile_all,
            trigger="interval",
            seconds=interval,
            id=self.JOB_ID,
            # max_instances=1 + coalesce=True: slow ticks skip rather than queue.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=interval,
            next_run_time=datetime.now(tz=tz),
        )
        # Audit listener -> yf_scheduler_log. Without this, "did supervision
        # tick last hour?" is unanswerable from one query; every other service's
        # scheduler is audited and we keep parity.
        try:
            attach_audit(self._scheduler, "sales_supervision", self._s.db.connection_string())
        except Exception as exc:
            logger.warning("attach_audit failed for supervision scheduler: %s", exc)
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
