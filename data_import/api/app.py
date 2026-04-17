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
            from datetime import datetime
            from pathlib import Path

            def _startup_refresh() -> None:
                """Check CSV freshness on boot. If any dataset is stale (max
                date < today), run a full import before warming the EDA cache.
                This covers the case where the server was down overnight and
                the scheduled cron missed its window. Runs off the request
                path so startup isn't blocked."""
                try:
                    importer = get_importer()
                    today = datetime.now().strftime("%Y-%m-%d")

                    # Quick freshness check: peek at the most recent date in
                    # sales_recent.csv (the fastest-changing dataset).
                    sales_csv = Path(settings.data_dir) / "sales_recent.csv"
                    stale = True
                    if sales_csv.exists():
                        import pandas as pd
                        try:
                            df = pd.read_csv(sales_csv, usecols=["TrxDate"], nrows=0)
                            # Read last 100 rows for max date (cheaper than full scan)
                            tail = pd.read_csv(sales_csv, usecols=["TrxDate"]).tail(500)
                            max_date = str(tail["TrxDate"].max()).strip()[:10]
                            # Stale if max date is more than 1 day behind today
                            stale = max_date < today
                            _logger.info("Sales CSV max date: %s, today: %s, stale: %s", max_date, today, stale)
                        except Exception as exc:
                            _logger.warning("CSV freshness check failed, assuming stale: %s", exc)

                    if stale:
                        _logger.info("Data is stale — running full import on startup")
                        results = importer.import_all("full")
                        new_rows = sum(r.get("new_rows", 0) for r in results.values())
                        _logger.info("Startup import complete: %d new rows across all datasets", new_rows)
                    else:
                        _logger.info("Data is fresh — skipping startup import")

                except Exception as exc:
                    _logger.warning("Startup data refresh failed: %s", exc)

                # Warm the EDA cache (always, regardless of import)
                try:
                    svc = get_eda_service()
                    if stale:
                        svc.invalidate()
                    for label, fn in (
                        ("sales_overview", svc.get_sales_overview),
                        ("item_catalog", svc.get_item_catalog),
                        ("business_kpis", svc.get_business_kpis),
                        ("customer_overview_90", lambda: svc.get_customer_overview(90)),
                    ):
                        try:
                            fn()
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=f"{settings.api_prefix}/data")

    return app
