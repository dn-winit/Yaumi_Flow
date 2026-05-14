"""
FastAPI application factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from data_import.api.routes import router
from data_import.config.settings import Settings, get_settings


def _cascade_reconciliation_refresh(settings: Settings, log: logging.Logger) -> None:
    """POST demand_forecasting's ``/reconciliation/refresh`` after a fresh
    CSV import lands. The forecast service's reconciliation cron is the
    sole writer of the CSV-only diagnostic columns
    (``recent_avg_per_selling_day``, ``expected_demand``,
    ``pattern_floor_applied`` / ``pattern_ceiling_applied``,
    ``forecast_below_recent``). Without this hop, those columns stay
    absent on the freshly-imported mirror until the next 03:30 Dubai
    cron, and the van-load explainability modal shows misleading zeros
    next to a real ``recommended_load``.

    Best-effort: a missing or unreachable forecast service logs a
    warning and returns -- the import itself succeeded and the
    diagnostic columns will eventually be patched by the scheduled cron.
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
        # httpx is already in the venv (used by demand_forecasting). We
        # only need a POST + JSON parse so the import is local; avoids
        # paying its import cost on every cold dependency import.
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

        # Pre-warm EDA aggregations so the first dashboard request is instant
        # (otherwise the user pays for the 50MB CSV parse on initial load).
        try:
            from data_import.api.dependencies import get_eda_service, get_importer
            import threading
            from datetime import datetime, timedelta
            from pathlib import Path

            def _startup_refresh() -> None:
                """Check CSV freshness on boot. If any dataset is stale (max
                date < today), run a full import before warming the EDA cache.
                This covers the case where the server was down overnight and
                the scheduled cron missed its window. Runs off the request
                path so startup isn't blocked."""
                # Default-stale so any failure in the freshness probe falls
                # through to a refresh + cache invalidate, never to NameError
                # in the warm-up block below.
                stale = True
                try:
                    importer = get_importer()
                    today = datetime.now().strftime("%Y-%m-%d")

                    # Quick freshness check: peek at the most recent date in
                    # sales_recent.csv (the fastest-changing dataset).
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
                        # Reverse cascade: after the fresh CSV mirror lands,
                        # ask demand_forecasting to re-patch the CSV-only
                        # diagnostic columns (recent_avg, expected_demand,
                        # pattern_floor/ceiling_applied, forecast_below_recent)
                        # so the van-load explainability modal renders the
                        # real engine intermediates, not the silent zeros
                        # that ``_first_present`` falls back to when those
                        # columns are missing. Best-effort: a warning if the
                        # forecast service is unreachable, never a hard fail.
                        _cascade_reconciliation_refresh(settings, _logger)
                    else:
                        _logger.info("Data is fresh — skipping startup import")

                except Exception as exc:
                    _logger.warning("Startup data refresh failed: %s", exc)

                # Warm the EDA cache (always, regardless of import). The two
                # date-windowed surfaces (sales_overview, business_kpis) need
                # a real (start_date, end_date) anchor now -- warm them with
                # the same trailing-30-day window the dashboard defaults to
                # on cold mount, so the first user request is a cache hit.
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

            threading.Thread(target=_startup_refresh, daemon=True, name="startup-refresh").start()
        except Exception as exc:
            _logger.warning("Startup refresh scheduling failed: %s", exc)

        sched = None
        if settings.scheduler_enabled:
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

    # CORS pinned to the configured webapp origins -- never ``*`` with
    # credentials, which browsers silently block. Override via the
    # shared ``YF_ALLOW_ORIGINS`` env var.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=f"{settings.api_prefix}/data")

    return app
