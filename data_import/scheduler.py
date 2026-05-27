"""data_import scheduler -- daily incremental pull at 03:00 Dubai.

Runs ``importer.import_all('incremental')``. CSVs under ``data/`` stay the
single source of truth for downstream services.
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
        # Sales-recent SQL nets returns at source; late-arriving returns
        # would miss the netting under a pure since-date incremental. Re-pull
        # last N days and dedup-merge so the netted row overwrites the gross.
        overrides = {}
        if settings.sales_recent_refresh_window_days > 0:
            overrides["sales_recent"] = settings.sales_recent_refresh_window_days
        results = DataImporter(settings).import_all(
            mode="incremental",
            dataset_lookback_overrides=overrides or None,
        )
        logger.info("[cron] Incremental import done: %s", results)
        # Bust the EDA cache so next request recomputes against fresh CSVs.
        try:
            from data_import.api.dependencies import get_eda_service
            get_eda_service().invalidate()
            logger.info("[cron] EDA cache invalidated")
        except Exception as exc:
            logger.warning("[cron] EDA invalidate skipped: %s", exc)
        # Reverse cascade: re-patch diagnostic columns + carry chain in the
        # same tick. Lazy import keeps FastAPI app deps off this module.
        try:
            from data_import.api.app import _cascade_reconciliation_refresh
            _cascade_reconciliation_refresh(settings, logger)
        except Exception as exc:
            logger.warning(
                "[cron] Reverse cascade after import skipped: %s -- "
                "diagnostic columns will be patched by the next "
                "scheduled reconciliation cron",
                exc,
            )
    except Exception as exc:
        logger.error("[cron] Incremental import failed: %s", exc, exc_info=True)


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler | None:
    global _scheduler
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        logger.info("data_import scheduler disabled via settings.")
        return None

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
        # coalesce: collapse missed slots into one fire on recovery.
        # max_instances=1: prevent the job from racing itself.
        coalesce=True,
        max_instances=1,
    )
    # Audit every fire to yf_scheduler_log (timing-only).
    from common.scheduler_audit import attach_audit
    attach_audit(_scheduler, "data_import",
                 settings.aiml_db.connection_string())
    _scheduler.start()
    logger.info(
        "Scheduler started -- incremental import at %02d:%02d (%s)",
        settings.scheduler_hour, settings.scheduler_minute, settings.scheduler_timezone,
    )
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
