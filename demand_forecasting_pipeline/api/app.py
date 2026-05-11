"""
FastAPI application factory.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

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
    ERRORS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    configure_logging,
    get_logger,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(level=settings.log_level)
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

        # ---- Auto-retrain scheduler (APScheduler-managed) ----
        from demand_forecasting_pipeline.services.retrain_scheduler import (
            check_and_retrain,
            start_scheduler,
            stop_scheduler,
        )

        retrain_cfg = get_retrain_config()
        pipeline_svc = get_pipeline_service()

        scheduler = start_scheduler(
            interval_hours=settings.retrain_check_interval_hours,
            job=lambda: check_and_retrain(retrain_cfg, pipeline_svc, svc, settings),
            logger=log,
        )
        log.info("retrain_scheduler_started",
                 check_interval_hours=settings.retrain_check_interval_hours)

        # ---- Reconciliation-refresh scheduler (daily cron). Re-runs
        # ``enrich_with_load`` for the rolling forecast window using the
        # latest closing_stock + load_allocation values and UPDATEs the
        # four reconciliation columns in yf_demand_forecast. The API can
        # then read pre-computed reconciled van load straight from the DB
        # instead of recomputing on every request. Same canonical engine
        # that db_pusher uses, so values can never drift between paths.
        from demand_forecasting_pipeline.services.reconciliation_refresh import (
            start_reconciliation_scheduler,
            stop_reconciliation_scheduler,
        )

        recon_scheduler = start_reconciliation_scheduler(
            settings=settings, logger=log,
        )

        yield  # app is running

        stop_scheduler(scheduler)
        stop_reconciliation_scheduler(recon_scheduler)
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

    # ------------------------------------------------------------------
    # Request middleware: trace IDs, structured access logs, latency
    # histogram. Runs around every request so the same correlation id
    # appears on every log line emitted while the request is in flight.
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        # Honour an upstream-provided trace id (gateway, ALB, frontend
        # retry middleware) so the full call chain shares one id.
        request_id = (
            request.headers.get("x-request-id")
            or request.headers.get("x-correlation-id")
            or uuid.uuid4().hex
        )
        path = request.url.path
        method = request.method

        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=path,
            method=method,
        )
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration = time.perf_counter() - start
            HTTP_REQUEST_DURATION.labels(path=path).observe(duration)
            HTTP_REQUESTS.labels(
                method=method, path=path, status=str(status_code),
            ).inc()
            log.info(
                "http_request",
                method=method,
                path=path,
                status=status_code,
                duration_ms=round(duration * 1000.0, 2),
                request_id=request_id,
            )
            structlog.contextvars.clear_contextvars()

    # ------------------------------------------------------------------
    # Global exception handler. Converts unhandled errors into a stable
    # JSON envelope so clients can parse a uniform shape regardless of
    # which handler raised. Records the error in the Prometheus counter
    # and emits a structured log line with traceback.
    # ------------------------------------------------------------------

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        err_type = type(exc).__name__
        ERRORS.labels(type=err_type).inc()
        log.exception(
            "unhandled_error",
            error_type=err_type,
            error=str(exc),
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc) or err_type,
                "type": err_type,
                "request_id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            headers={"X-Request-ID": request_id},
        )

    # ------------------------------------------------------------------
    # /metrics/prometheus -- served on the same prefix as the rest of the
    # service so the existing reverse-proxy rule covers it.
    # ------------------------------------------------------------------

    prefix = f"{settings.api_prefix}/forecast"

    @app.get(f"{prefix}/metrics/prometheus", include_in_schema=False)
    def _prometheus_metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

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
