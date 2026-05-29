"""
FastAPI application factory.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.api_middleware import install_all as _install_observability_middleware
from demand_forecasting_pipeline.api.routes import (
    explainability_router,
    health_router,
    metrics_router,
    page_views_router,
    pipeline_router,
    predictions_router,
    reconciliation_router,
    retrain_router,
    summary_router,
)
from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import (
    SERVICE_NAME,
    configure_logging,
    get_logger,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(level=settings.log_level, timezone=settings.log_timezone)
    log = get_logger("demand_forecasting_pipeline.api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("service_starting",
                 config=settings.pipeline_config,
                 artifacts_dir=settings.artifacts_dir)

        from demand_forecasting_pipeline.api.dependencies import (
            get_artifact_service,
            get_pipeline_service,
            get_retrain_config,
        )
        svc = get_artifact_service()
        artifacts = svc.check_artifacts()
        present = sum(1 for v in artifacts.values() if v)
        log.info("artifacts_check", present=present, total=len(artifacts))

        # Leader election: only one worker per host starts schedulers
        # (otherwise N workers => N concurrent writes against the same tables).
        from common.leader_election import try_acquire_leader_lock
        is_leader = try_acquire_leader_lock(settings.scheduler_lock_path)

        retrain_cfg = get_retrain_config()
        pipeline_svc = get_pipeline_service()
        scheduler = None
        recon_scheduler = None

        if is_leader:
            # Auto-retrain scheduler (APScheduler-managed)
            from demand_forecasting_pipeline.services.retrain_scheduler import (
                check_and_retrain,
                start_scheduler,
                stop_scheduler,
            )

            scheduler = start_scheduler(
                interval_hours=settings.retrain_check_interval_hours,
                job=lambda: check_and_retrain(retrain_cfg, pipeline_svc, svc, settings),
                logger=log,
            )
            log.info("retrain_scheduler_started",
                     check_interval_hours=settings.retrain_check_interval_hours)

            # Reconciliation-refresh daily cron: re-runs enrich_with_load and UPDATEs
            # the four reconciliation columns; reuses db_pusher's engine.
            from demand_forecasting_pipeline.services.reconciliation_refresh import (
                start_reconciliation_scheduler,
                stop_reconciliation_scheduler,
            )

            recon_scheduler = start_reconciliation_scheduler(
                settings=settings, logger=log,
            )
        else:
            log.info("scheduler_boot_skipped reason=not_leader")
            # Followers need stop_* symbols defined for shutdown; pass None for no-ops.
            from demand_forecasting_pipeline.services.reconciliation_refresh import (
                stop_reconciliation_scheduler,
            )
            from demand_forecasting_pipeline.services.retrain_scheduler import (
                stop_scheduler,
            )

        # Cache warm-up in daemon thread: pay enrich_with_load cold-start at boot.
        # mtime-keyed cache in ArtifactService dedupes concurrent first requests.
        import threading

        def _warm_caches() -> None:
            try:
                t0 = time.perf_counter()
                svc.van_load_view_enriched()
                log.info(
                    "cache_warmup_complete",
                    duration_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                )
            except Exception as exc:  # pragma: no cover -- defensive
                log.warning(
                    "cache_warmup_skipped",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        threading.Thread(
            target=_warm_caches,
            name="df-cache-warmup",
            daemon=True,
        ).start()

        yield  # app is running

        # Shutdown order: stop schedulers first (no new runs), then drain via shutdown()
        # with bounded join timeout; abandoned fits leave prior-stage artifacts intact.
        stop_scheduler(scheduler)
        stop_reconciliation_scheduler(recon_scheduler)
        try:
            pipeline_svc.shutdown()
        except Exception as exc:
            log.warning("pipeline_shutdown_failed", error=str(exc))
        log.info("service_shutting_down")

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Single source of truth for request-id propagation, access logs,
    # latency histograms, structured error envelope, and Prometheus scrape.
    # All five services mount the same primitives from common/ so the
    # observability contract cannot diverge.
    prefix = f"{settings.api_prefix}/forecast"
    _install_observability_middleware(
        app,
        service_name=SERVICE_NAME,
        metrics_path=f"{prefix}/metrics/prometheus",
        logger=log,
    )

    app.include_router(health_router, prefix=prefix)
    app.include_router(predictions_router, prefix=prefix)
    app.include_router(metrics_router, prefix=prefix)
    app.include_router(explainability_router, prefix=prefix)
    app.include_router(pipeline_router, prefix=prefix)
    app.include_router(summary_router, prefix=prefix)
    app.include_router(retrain_router, prefix=prefix)
    app.include_router(reconciliation_router, prefix=prefix)
    app.include_router(page_views_router, prefix=prefix)

    return app
