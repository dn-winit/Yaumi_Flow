"""
Scheduled jobs -- daily recommendation generation and data refresh.

Flow (runs at ``RO_SCHEDULER_GENERATION_HOUR``, default 04:00 Asia/Dubai):
    1. Refresh cached data from source DBs (journey plan, demand forecast, customers).
    2. For every route on today's journey plan, generate + save + DB-push recs.
    3. Retry up to ``max_retries`` with backoff on transient failures.

If the cron job fails or is missed, the API also auto-generates on first
``POST /get`` for a date -- so the UI never sees empty data.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _today_in_tz(tz_name: str) -> str:
    """`today` interpreted in the configured scheduler timezone (e.g. Asia/Dubai),
    so the daily cron lines up with what the supervisor sees on their clock."""
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _routes_for_today(dm, today: str) -> list[str]:
    """Routes appearing in today's journey plan (source of truth)."""
    journey = dm.get_journey_plan(date=today)
    if journey.empty:
        return []
    return sorted(journey["RouteCode"].dropna().astype(str).str.strip().unique().tolist())


def _run_daily_generation(settings: Settings) -> dict:
    """Generate for today. Returns result summary."""
    from recommended_order.api.dependencies import (
        get_data_manager, get_engine, get_store, get_db_pusher,
    )
    from recommended_order.api.routes import _generate_routes

    today = _today_in_tz(settings.scheduler.timezone)
    dm = get_data_manager()
    routes = _routes_for_today(dm, today) or dm.get_route_codes()

    if not routes:
        logger.warning("[cron] No routes resolved for %s", today)
        return {"date": today, "routes_generated": 0, "total_records": 0}

    result = _generate_routes(
        today, routes,
        dm=dm, engine=get_engine(), store=get_store(), pusher=get_db_pusher(),
        skip_existing=True,
    )
    result["date"] = today
    return result


def _generate_daily(settings: Settings | None = None) -> None:
    """Retry wrapper around _run_daily_generation."""
    settings = settings or get_settings()
    sc = settings.scheduler

    last_error: Exception | None = None
    for attempt in range(1, sc.max_retries + 1):
        try:
            logger.info("[cron] Daily generation attempt %d/%d", attempt, sc.max_retries)
            res = _run_daily_generation(settings)
            logger.info(
                "[cron] Daily generation done for %s: %d routes, %d records in %.2fs",
                res.get("date"), res.get("routes_generated", 0),
                res.get("total_records", 0), res.get("duration_seconds", 0),
            )
            return
        except Exception as exc:
            last_error = exc
            logger.error("[cron] Attempt %d failed: %s", attempt, exc, exc_info=True)
            if attempt < sc.max_retries:
                time.sleep(sc.retry_delay_seconds)

    logger.error("[cron] Daily generation FAILED after %d attempts: %s", sc.max_retries, last_error)


def _refresh_data() -> None:
    """Incremental refresh of cached data from databases (03:00 Dubai).

    Pulls only new dates since the last successful refresh and prunes rows
    outside the lookback window. Falls back to full refresh if cache is empty.
    """
    from recommended_order.api.dependencies import get_data_manager

    logger.info("[cron] Incremental data refresh starting")
    result = get_data_manager().refresh_incremental()
    # Sprint-1: drop cached per-route calibration so next generate recomputes
    try:
        from recommended_order.core.calibration import invalidate_cache
        invalidate_cache()
    except Exception:  # pragma: no cover -- non-fatal
        logger.debug("calibration cache invalidate failed", exc_info=True)
    if result["success"]:
        logger.info("[cron] Data refresh done: %s", result["data"])
    else:
        logger.error("[cron] Data refresh errors: %s", result["errors"])


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """Start the background scheduler with configured jobs."""
    global _scheduler
    settings = settings or get_settings()
    sc = settings.scheduler

    _scheduler = BackgroundScheduler(timezone=sc.timezone)

    # Data refresh first (default 03:30), then generation (default 04:00)
    _scheduler.add_job(
        _refresh_data,
        CronTrigger(hour=sc.cache_refresh_hour, minute=sc.cache_refresh_minute, timezone=sc.timezone),
        id="daily_data_refresh",
        name="Daily Data Refresh",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.add_job(
        _generate_daily,
        CronTrigger(hour=sc.generation_hour, minute=sc.generation_minute, timezone=sc.timezone),
        id="daily_recommendation_generation",
        name="Daily Recommendation Generation",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started -- refresh %02d:%02d, generation %02d:%02d (%s), retries=%d",
        sc.cache_refresh_hour, sc.cache_refresh_minute,
        sc.generation_hour, sc.generation_minute,
        sc.timezone, sc.max_retries,
    )
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
