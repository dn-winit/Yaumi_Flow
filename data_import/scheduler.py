"""
data_import scheduler -- daily incremental pull at 03:00 Dubai time.

Runs ``importer.import_all('incremental')`` so only new rows since the last
import are pulled. The CSV files under ``data/`` stay the single source of
truth for all downstream services.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from data_import.config.settings import Settings, get_settings
from data_import.core.importer import DataImporter

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _incremental_import() -> None:
    settings = get_settings()
    logger.info("[cron] Incremental import starting")
    try:
        results = DataImporter(settings).import_all(mode="incremental")
        logger.info("[cron] Incremental import done: %s", results)
        # CSVs may have changed -- bust the EDA aggregation cache so the next
        # request recomputes against the fresh data.
        try:
            from data_import.api.dependencies import get_eda_service
            get_eda_service().invalidate()
            logger.info("[cron] EDA cache invalidated")
        except Exception as exc:
            logger.warning("[cron] EDA invalidate skipped: %s", exc)
    except Exception as exc:
        logger.error("[cron] Incremental import failed: %s", exc, exc_info=True)


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    global _scheduler
    settings = settings or get_settings()

    _scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
    _scheduler.add_job(
        _incremental_import,
        CronTrigger(
            hour=settings.scheduler_hour,
            minute=settings.scheduler_minute,
            timezone=settings.scheduler_timezone,
        ),
        id="daily_incremental_import",
        name="Daily Incremental Import",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started -- incremental import at %02d:%02d (%s)",
        settings.scheduler_hour, settings.scheduler_minute, settings.scheduler_timezone,
    )
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
