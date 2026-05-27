"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from data_import.api.routes import router
from data_import.config.settings import Settings, get_settings


def _cascade_reconciliation_refresh(settings: Settings, log: logging.Logger) -> None:
    """POST demand_forecasting's ``/reconciliation/refresh`` after a fresh
    CSV import lands so CSV-only diagnostic columns (recent_avg,
    expected_demand, pattern_*_applied, forecast_below_recent) are patched
    in the same tick. Best-effort: an unreachable forecast service logs
    and returns; the scheduled cron picks it up later.
    """
    base = (settings.forecast_url or "").rstrip("/")
    if not base:
        log.info(
            "Reverse cascade skipped -- DI_FORECAST_URL unset, "
            "diagnostic columns will be patched on the next scheduled "
            "reconciliation cron",
        )
        return
    url = f"{base}/api/v1/forecast/reconciliation/refresh"
    try:
        # Local import: avoids paying httpx cost on every cold import.
        import httpx
        resp = httpx.post(
            url,
            timeout=float(settings.forecast_refresh_timeout_seconds),
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        cascade = payload.get("cascade") or {}
        log.info(
            "Reverse cascade ok: yf_sales_transactions refreshed %s rows, mirror cascade %s new rows",
            payload.get("rows_updated", "?"),
            cascade.get("new_rows", "?"),
        )
    except Exception as exc:
        log.warning(
            "Reverse cascade skipped -- /reconciliation/refresh "
            "unreachable at %s: %s. Diagnostic columns will be patched "
            "on the next scheduled cron.",
            url, exc,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _logger = logging.getLogger("data_import.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _logger.info("Data Import Service starting -- data_dir=%s", settings.data_dir)

        # Pre-warm EDA aggregations so first dashboard request is instant.
        # Leader election: only the worker that owns the scheduler lock runs
        # the full-import probe, otherwise ``workers>1`` would have N workers
        # all racing the same CSV ``os.replace`` and hammering YaumiLive.
        try:
            from data_import.api.dependencies import get_eda_service, get_importer
            from common.leader_election import try_acquire_leader_lock
            import threading
            from datetime import datetime, timedelta
            from pathlib import Path

            startup_is_leader = (
                settings.scheduler_enabled
                and try_acquire_leader_lock(settings.scheduler_lock_path)
            )
            if not startup_is_leader and settings.scheduler_enabled:
                _logger.info(
                    "data_import startup refresh skipped: another worker holds the leader lease",
                )

            def _startup_refresh() -> None:
                """Run a full import on boot if any dataset is stale (covers
                missed overnight cron), then warm the EDA cache. Off the
                request path so startup is not blocked."""
                # Default-stale: any probe failure falls through to refresh
                # rather than NameError'ing in the warm-up block below.
                stale = True
                try:
                    importer = get_importer()
                    today = datetime.now().strftime("%Y-%m-%d")

                    # Freshness probe: most recent date in sales_recent.csv
                    # (fastest-changing dataset).
                    sales_csv = Path(settings.data_dir) / "sales_recent.csv"
                    if sales_csv.exists():
                        import pandas as pd
                        try:
                            max_date = str(
                                pd.read_csv(sales_csv, usecols=["TrxDate"])["TrxDate"].max()
                            ).strip()[:10]
                            stale = max_date < today
                            _logger.info("Sales CSV max date: %s, today: %s, stale: %s", max_date, today, stale)
                        except Exception as exc:
                            _logger.warning("CSV freshness check failed, assuming stale: %s", exc)

                    if stale:
                        _logger.info("Data is stale — running full import on startup")
                        results = importer.import_all("full")
                        new_rows = sum(r.get("new_rows", 0) for r in results.values())
                        _logger.info("Startup import complete: %d new rows across all datasets", new_rows)
                        # Reverse cascade so diagnostic columns are re-patched in this tick.
                        _cascade_reconciliation_refresh(settings, _logger)
                    else:
                        _logger.info("Data is fresh — skipping startup import")

                except Exception as exc:
                    _logger.warning("Startup data refresh failed: %s", exc)

                # Warm EDA cache (always) using the same trailing-30-day
                # window the dashboard defaults to, so first request is a hit.
                try:
                    svc = get_eda_service()
                    if stale:
                        svc.invalidate()
                    today_dt = datetime.now()
                    today = today_dt.strftime("%Y-%m-%d")
                    start = (today_dt - timedelta(days=29)).strftime("%Y-%m-%d")
                    for label, runner in (
                        ("sales_overview",
                            lambda: svc.get_sales_overview(start, today)),
                        ("item_catalog",
                            lambda: svc.get_item_catalog()),
                        ("business_kpis",
                            lambda: svc.get_business_kpis(start, today)),
                        ("last_active_date",
                            lambda: svc.get_last_active_date()),
                    ):
                        try:
                            runner()
                        except Exception as exc:
                            _logger.warning("EDA warm-up skipped %s: %s", label, exc)
                    _logger.info("EDA cache warmed")
                except Exception as exc:
                    _logger.warning("EDA warm-up skipped: %s", exc)

            # Skip the import + cascade on followers; EDA cache warm-up still
            # runs because it only reads the local CSV mirror that the leader
            # populated.
            if startup_is_leader:
                threading.Thread(target=_startup_refresh, daemon=True, name="startup-refresh").start()
            else:
                # Followers warm EDA against whatever CSVs the leader produced;
                # the inline block matches the no-stale branch of _startup_refresh.
                def _warm_eda_only() -> None:
                    try:
                        svc = get_eda_service()
                        today_dt = datetime.now()
                        today = today_dt.strftime("%Y-%m-%d")
                        start = (today_dt - timedelta(days=29)).strftime("%Y-%m-%d")
                        for label, runner in (
                            ("sales_overview", lambda: svc.get_sales_overview(start, today)),
                            ("item_catalog", lambda: svc.get_item_catalog()),
                            ("business_kpis", lambda: svc.get_business_kpis(start, today)),
                            ("last_active_date", lambda: svc.get_last_active_date()),
                        ):
                            try:
                                runner()
                            except Exception as exc:
                                _logger.warning("EDA warm-up skipped %s: %s", label, exc)
                    except Exception as exc:
                        _logger.warning("EDA warm-up skipped: %s", exc)
                threading.Thread(target=_warm_eda_only, daemon=True, name="startup-warm-eda").start()
        except Exception as exc:
            _logger.warning("Startup refresh scheduling failed: %s", exc)

        sched = None
        # Only the leader (that already booted the import thread above) fires
        # the daily cron. Followers stay scheduler-less.
        if settings.scheduler_enabled and startup_is_leader:
            from data_import.scheduler import start_scheduler
            sched = start_scheduler(settings)
        yield
        if sched is not None:
            sched.shutdown(wait=False)
        _logger.info("Shutting down")

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )

    # CORS pinned to configured origins (never ``*`` with credentials).
    # Override via ``YF_ALLOW_ORIGINS``.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=f"{settings.api_prefix}/data")

    return app
