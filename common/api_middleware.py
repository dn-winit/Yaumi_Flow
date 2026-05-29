"""FastAPI middleware + exception handlers shared across every service.

Single contract for:

* **Request-ID propagation** -- honour ``X-Request-ID`` / ``X-Correlation-ID``
  from upstream, generate one when absent, echo on the response, bind into the
  structlog context so every log line under the request carries it.
* **Access log + latency histogram** -- one ``http_request`` log line and one
  ``yf_http_request_duration_seconds`` observation per request, no exceptions.
* **Stable error envelope** -- uncaught exceptions surface as
  ``{"error", "type", "request_id", "timestamp"}`` instead of FastAPI's default
  500 body. ``yf_errors_total`` increments by exception type.
* **Prometheus scrape endpoint** -- exposes the default REGISTRY on a
  service-supplied path.

Every primitive takes ``service_name`` so the metrics label-namespace works
across the fleet. Importable before any service settings load; depends only
on ``fastapi``, ``starlette`` and ``common.observability``.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from common.observability import (
    ERRORS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    get_logger,
)

_REQUEST_ID_HEADER = "X-Request-ID"
_CORRELATION_ID_HEADER = "X-Correlation-ID"


def _extract_or_generate_request_id(request: Request) -> str:
    """Honour upstream-supplied trace id (case-insensitive header lookup),
    fall back to a fresh UUID4 hex. Centralising the rule means
    cross-service traces stay correlated end-to-end without each handler
    re-implementing the lookup."""
    return (
        request.headers.get(_REQUEST_ID_HEADER)
        or request.headers.get(_CORRELATION_ID_HEADER)
        or uuid.uuid4().hex
    )


def install_request_middleware(
    app: FastAPI,
    *,
    service_name: str,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Install the structured access-log + latency-histogram middleware.

    Idempotent on ``app`` (FastAPI rejects duplicate middleware on the same
    function so reinstalling raises -- safe by construction). Call exactly
    once from each service's ``create_app``.
    """
    log = logger or get_logger(f"{service_name}.api")

    # Keys this middleware owns -- only these are unbound on request end so
    # the boot-time ``bind_contextvars(service=...)`` survives untouched.
    # Wiping the whole context would strip ``service`` for any background
    # work that runs after the request returns (FastAPI BackgroundTasks,
    # daemon threads sharing the request's contextvar snapshot, etc.).
    _OWNED_KEYS = ("request_id", "path", "method")

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Response:
        request_id = _extract_or_generate_request_id(request)
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
            response.headers[_REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration = time.perf_counter() - start
            HTTP_REQUEST_DURATION.labels(
                service=service_name, path=path,
            ).observe(duration)
            HTTP_REQUESTS.labels(
                service=service_name, method=method, path=path,
                status=str(status_code),
            ).inc()
            log.info(
                "http_request",
                method=method,
                path=path,
                status=status_code,
                duration_ms=round(duration * 1000.0, 2),
                request_id=request_id,
            )
            # Targeted unbind so the boot-time ``service`` binding survives.
            structlog.contextvars.unbind_contextvars(*_OWNED_KEYS)


def install_exception_handler(
    app: FastAPI,
    *,
    service_name: str,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Install the global ``Exception`` handler.

    Returns the stable envelope ``{error, type, request_id, timestamp}`` on
    any uncaught error, increments ``yf_errors_total{service, type}``, and
    logs the full traceback via ``log.exception``.

    Mounted via ``@app.exception_handler(Exception)`` -- FastAPI's
    HTTPException path (4xx with structured details) is preserved because
    HTTPException is a subclass of Exception but routed by its dedicated
    handler, which FastAPI installs by default.
    """
    log = logger or get_logger(f"{service_name}.api")

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _extract_or_generate_request_id(request)
        err_type = type(exc).__name__
        ERRORS.labels(service=service_name, type=err_type).inc()
        # FULL message goes to the structured log -- ops still has the
        # detail. The response body returns the type only because
        # ``str(exc)`` on pyodbc / SQLAlchemy errors typically embeds the
        # DSN (SERVER, UID, etc.) and other internal context that must NOT
        # leak to a public client.
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
                # Generic message keyed by ``type`` so the client can
                # distinguish error classes without seeing internals.
                "error": "Internal Server Error",
                "type": err_type,
                "request_id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            headers={_REQUEST_ID_HEADER: request_id},
        )


def install_prometheus_endpoint(
    app: FastAPI,
    *,
    path: str,
) -> None:
    """Expose ``/metrics/prometheus`` on the supplied path.

    Service-specific path so an HTTP proxy fronting multiple services can
    route ``/api/v1/<service>/metrics/prometheus`` to the right backend
    without rewriting headers.
    """

    @app.get(path, include_in_schema=False)
    def _prometheus_metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_all(
    app: FastAPI,
    *,
    service_name: str,
    metrics_path: str,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Convenience: install middleware + exception handler + metrics endpoint
    in one call. Use this from ``create_app`` unless a service needs to
    selectively skip one of the three (none currently do)."""
    install_request_middleware(app, service_name=service_name, logger=logger)
    install_exception_handler(app, service_name=service_name, logger=logger)
    install_prometheus_endpoint(app, path=metrics_path)


__all__ = [
    "install_request_middleware",
    "install_exception_handler",
    "install_prometheus_endpoint",
    "install_all",
]
