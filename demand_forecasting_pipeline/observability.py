"""Structured logging + Prometheus metrics. Logs to stdout + rotating file.

Format via DF_LOG_FORMAT (json | console); console auto-selected on TTY.
Metrics exposed at /api/v1/forecast/metrics/prometheus.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import structlog

from prometheus_client import Counter, Gauge, Histogram

_configured = False

# Prometheus singletons on default REGISTRY; module-import time for ordering-safe import.

PIPELINE_RUNS = Counter(
    "df_pipeline_runs_total",
    "Total pipeline runs.",
    labelnames=("pipeline", "status"),
)
DB_PUSH_ROWS = Counter(
    "df_db_push_rows_total",
    "Rows pushed into yf_demand_forecast.",
    labelnames=("datasplit",),
)
LIVE_FETCH_TIMEOUTS = Counter(
    "df_live_fetch_timeout_total",
    "Live van-composition HTTP fetch timeouts (fell back to CSV).",
)
L4_DISABLED = Counter(
    "df_l4_disabled_total",
    "Reconciliation calls where L4 quantile loading degraded to V5_b "
    "because q_low/q_high columns were absent.",
)
HTTP_REQUESTS = Counter(
    "df_http_requests_total",
    "API requests handled.",
    labelnames=("method", "path", "status"),
)
ERRORS = Counter(
    "df_errors_total",
    "Unhandled errors caught by the global exception handler.",
    labelnames=("type",),
)

PIPELINE_STEP_DURATION = Histogram(
    "df_pipeline_step_duration_seconds",
    "Wall-clock duration of a pipeline step.",
    labelnames=("step", "status"),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)
HTTP_REQUEST_DURATION = Histogram(
    "df_http_request_duration_seconds",
    "API request latency.",
    labelnames=("path",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

DRIFT_PCT = Gauge(
    "df_drift_pct",
    "Most recent drift percentage from the auto-retrain check.",
)
LAST_TRAIN_AGE_SECONDS = Gauge(
    "df_last_train_age_seconds",
    "Seconds since the last successful training run.",
)
FORECASTED_PAIRS = Gauge(
    "df_forecasted_pairs",
    "Pairs included in the most recent inference run.",
)
DROPPED_PAIRS = Gauge(
    "df_dropped_pairs",
    "Pairs dropped from the most recent inference run.",
    labelnames=("reason",),
)

def _resolve_format(fmt: str | None) -> str:
    if fmt:
        return fmt.lower()
    env = os.getenv("DF_LOG_FORMAT")
    if env:
        return env.lower()
    return "console" if sys.stdout.isatty() else "json"

def _resolve_logs_dir() -> Path:
    """Logs dir without importing settings (avoid bootstrap cycle); DF_LOGS_DIR > YF_DATA_ROOT/forecast/logs."""
    env = os.getenv("DF_LOGS_DIR")
    if env:
        return Path(env)
    root_env = os.getenv("YF_DATA_ROOT", "").strip()
    root = Path(root_env).resolve() if root_env else Path(__file__).resolve().parent.parent / "data"
    return root / "forecast" / "logs"

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
    level: str = "INFO",
    fmt: str | None = None,
    timezone: str | None = None,
) -> None:
    """Configure structlog + stdlib logging (idempotent).

    Adds stdout StreamHandler + rotating pipeline.log; file copy is byte-identical
    modulo TTY color codes. ``timezone`` is an IANA zone (default UTC).
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

    # stdlib LoggerFactory; PrintLoggerFactory lacks .name and breaks add_logger_name.
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
    try:
        logs_dir = _resolve_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = _resolve_int_env("DF_LOG_FILE_MAX_BYTES", 10 * 1024 * 1024)
        backup_count = _resolve_int_env("DF_LOG_FILE_BACKUP_COUNT", 5)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(logs_dir / "pipeline.log"),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        # File copy is always JSON (forensic logs are easier to grep).
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
            f"observability: rotating file handler disabled ({exc!r}); stdout only\n"
        )

    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(lvl)

    # Dampen noisy third-party loggers unless DEBUG is requested explicitly.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    _configured = True

def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Safe to call before configure_logging."""
    return structlog.get_logger(name)

@contextmanager
def step_timer(
    step: str,
    *,
    run_id: str | None = None,
    pipeline: str | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Time a pipeline step; logs + records to df_pipeline_step_duration_seconds.

    Mutate the yielded dict to attach payload. Re-raises after recording status=failed.
    """
    log = logger or get_logger("demand_forecasting_pipeline.pipeline")
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
        PIPELINE_STEP_DURATION.labels(step=step, status=status).observe(duration)
        if status == "completed":
            log.info("step_completed", **payload)
        else:
            log.error("step_failed", **payload)
