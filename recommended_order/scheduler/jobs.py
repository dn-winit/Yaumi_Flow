"""
Scheduled jobs -- daily recommendation generation.

One cron, runs at ``RO_SCHEDULER_GENERATION_HOUR`` (default 04:30 Asia/Dubai):
    1. Force a CSV re-read (covers any cascade-failed reconciliation refresh)
    2. For every route on today's journey plan, generate + save + DB-push recs
    3. Retry up to ``max_retries`` with backoff on transient failures

We deliberately do NOT keep a separate cache-refresh cron: the in-process
``DataManager.refresh()`` invoked at the start of generation, plus the
mtime-keyed ``ensure_fresh()`` that every endpoint calls, cover every
case the old 03:20 refresh job did.

If the cron fails or is missed, the API also auto-generates on first
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
    # Force a CSV re-read before generation. The 03:30 reconciliation
    # cascades a data_import refresh that updates demand_forecast.csv;
    # if that cascade failed silently, mtime-based ``ensure_fresh``
    # would no-op and we'd consume yesterday's reconciled values. An
    # explicit ``refresh()`` is idempotent (~100 ms) and guarantees we
    # see whatever is on disk right now, regardless of cascade health.
    dm.refresh()
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


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """Start the background scheduler with the daily generation job."""
    global _scheduler
    settings = settings or get_settings()
    sc = settings.scheduler

    _scheduler = BackgroundScheduler(timezone=sc.timezone)

    # Single cron: daily generation. The forced ``dm.refresh()`` inside
    # ``_run_daily_generation`` makes a separate "data refresh" cron
    # redundant -- the same call covers it and runs in the same context
    # as the generation it serves.
    _scheduler.add_job(
        _generate_daily,
        CronTrigger(hour=sc.generation_hour, minute=sc.generation_minute, timezone=sc.timezone),
        id="daily_recommendation_generation",
        name="Daily Recommendation Generation",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started -- generation %02d:%02d (%s), retries=%d",
        sc.generation_hour, sc.generation_minute,
        sc.timezone, sc.max_retries,
    )
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
