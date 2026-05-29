"""Shared structured-logging + Prometheus primitives.

Single source of truth for the operational observability contract across all
five backend services. Each service calls :func:`configure_logging` with its
own ``service_name`` + log path, then mounts :func:`install_request_middleware`
and :func:`install_exception_handler` from ``common.api_middleware`` to get
identical request-id propagation, latency histograms, structured access logs,
and a stable error envelope.

Metric names are deliberately ``yf_*`` (not service-specific) and carry a
``service`` label so a single Grafana panel can break down latency, error
rate, and traffic per service across the whole fleet.

Keep this module dependency-free relative to the service layer -- it must be
importable before any service's settings load. The only third-party deps are
``structlog`` and ``prometheus_client``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Cross-service Prometheus metrics (singletons on the default REGISTRY)
# ---------------------------------------------------------------------------
# Module-import time so a service that imports this then mounts /metrics
# always sees the same series. ``service`` label namespaces every metric so
# the same panel works for any combination of services.

HTTP_REQUESTS = Counter(
    "yf_http_requests_total",
    "API requests handled.",
    labelnames=("service", "method", "path", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "yf_http_request_duration_seconds",
    "API request latency.",
    labelnames=("service", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
ERRORS = Counter(
    "yf_errors_total",
    "Unhandled errors caught by the global exception handler.",
    labelnames=("service", "type"),
)
PIPELINE_STEP_DURATION = Histogram(
    "yf_pipeline_step_duration_seconds",
    "Wall-clock duration of a pipeline step.",
    labelnames=("service", "step", "status"),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)
INFO_GAUGE = Gauge(
    "yf_service_info",
    "Static service metadata (always 1; labels carry the info).",
    labelnames=("service", "version"),
)


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

_configured = False


def _resolve_format(fmt: str | None) -> str:
    if fmt:
        return fmt.lower()
    env = os.getenv("YF_LOG_FORMAT")
    if env:
        return env.lower()
    return "console" if sys.stdout.isatty() else "json"


def _resolve_int_env(name: str, default: int) -> int:
    """Read int env var; default on absence/parse failure (avoids importing Settings)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _make_local_timestamper(tz_name: str):
    """ISO-8601 timestamps in the given IANA TZ; avoids structlog's UTC fallback in containers."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)

    def _proc(_logger, _method, event_dict):
        event_dict["timestamp"] = datetime.now(tz).isoformat(timespec="microseconds")
        return event_dict

    return _proc


def configure_logging(
    service_name: str,
    *,
    level: str = "INFO",
    fmt: str | None = None,
    logs_dir: Path | str | None = None,
    log_filename: str | None = None,
    timezone: str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """Configure structlog + stdlib logging exactly once per process.

    Idempotent across re-invocation (FastAPI lifespan can call this from
    multiple workers safely; only the first call wins).

    Args:
        service_name: short name used in the rotating filename and as the
            ``service`` field bound into every structlog event.
        level: stdlib log level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
        fmt: ``json`` or ``console``; auto-detected on TTY when None.
        logs_dir: directory for the rotating file handler. None disables file
            logging (stdout only) -- useful for container deployments where
            stdout capture is the canonical log destination.
        log_filename: defaults to ``<service_name>.log``.
        timezone: IANA TZ for log timestamps. None -> UTC.
        max_bytes / backup_count: rotation config. Defaults to 10MB x 5.
    """
    global _configured
    if _configured:
        return

    resolved = _resolve_format(fmt)
    lvl = getattr(logging, level.upper(), logging.INFO)

    if timezone is None or timezone.upper() == "UTC":
        timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    else:
        timestamper = _make_local_timestamper(timezone)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if resolved == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]

    # Rotating file handler; downgrade to stdout-only on filesystem failure.
    if logs_dir is not None:
        try:
            logs_path = Path(logs_dir)
            logs_path.mkdir(parents=True, exist_ok=True)
            mb = int(max_bytes) if max_bytes is not None else _resolve_int_env(
                "YF_LOG_FILE_MAX_BYTES", 10 * 1024 * 1024,
            )
            bc = int(backup_count) if backup_count is not None else _resolve_int_env(
                "YF_LOG_FILE_BACKUP_COUNT", 5,
            )
            fname = log_filename or f"{service_name}.log"
            file_handler = logging.handlers.RotatingFileHandler(
                filename=str(logs_path / fname),
                maxBytes=mb,
                backupCount=bc,
                encoding="utf-8",
            )
            # File copy always JSON for grepability.
            if resolved != "json":
                file_handler.setFormatter(
                    structlog.stdlib.ProcessorFormatter(
                        foreign_pre_chain=shared,
                        processors=[
                            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                            structlog.processors.JSONRenderer(),
                        ],
                    )
                )
            else:
                file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except Exception as exc:  # pragma: no cover -- best-effort fallback
            sys.stderr.write(
                f"observability[{service_name}]: rotating file handler disabled "
                f"({exc!r}); stdout only\n"
            )

    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(lvl)

    # Bind service-name into every log event so cross-service aggregation works.
    structlog.contextvars.bind_contextvars(service=service_name)

    # Dampen noisy third-party loggers unless DEBUG is requested explicitly.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    # Stamp the static info gauge so /metrics carries running version.
    version = os.getenv("GIT_COMMIT") or os.getenv("CI_COMMIT_SHA") or "unknown"
    INFO_GAUGE.labels(service=service_name, version=version).set(1)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Safe to call before configure_logging."""
    return structlog.get_logger(name)


@contextmanager
def step_timer(
    step: str,
    *,
    service_name: str,
    run_id: str | None = None,
    pipeline: str | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Time a pipeline step; logs + records to ``yf_pipeline_step_duration_seconds``.

    Mutate the yielded dict to attach payload. Re-raises after recording status=failed.
    """
    log = logger or get_logger(f"{service_name}.pipeline")
    rid = run_id or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    payload: dict[str, Any] = dict(extra or {})
    payload.update({"step": step, "run_id": rid})
    if pipeline:
        payload["pipeline"] = pipeline
    log.info("step_started", **payload)
    status = "completed"
    try:
        yield payload
    except Exception as exc:  # noqa: BLE001 -- record then re-raise
        status = "failed"
        payload["error"] = repr(exc)
        raise
    finally:
        duration = time.perf_counter() - start
        payload["duration_ms"] = round(duration * 1000.0, 2)
        payload["status"] = status
        PIPELINE_STEP_DURATION.labels(
            service=service_name, step=step, status=status,
        ).observe(duration)
        if status == "completed":
            log.info("step_completed", **payload)
        else:
            log.error("step_failed", **payload)


__all__ = [
    "configure_logging",
    "get_logger",
    "step_timer",
    # Metrics (re-exported so service code doesn't import prometheus_client directly)
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "ERRORS",
    "PIPELINE_STEP_DURATION",
    "INFO_GAUGE",
]
