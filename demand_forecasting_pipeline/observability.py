"""
Structured logging setup for the pipeline and API.

All logs go to stdout so that container log drivers (ECS awslogs,
Kubernetes fluent-bit, Docker json-file) can collect them. No file I/O.

Usage:
    from demand_forecasting_pipeline.observability import configure_logging, get_logger

    configure_logging(level="INFO")       # once, at startup
    log = get_logger(__name__)
    log.info("training_started", pairs=len(pairs), config=cfg_path)

Format is controlled by ``DF_LOG_FORMAT``:
  - ``json`` (default in prod) — one JSON object per line, ready for CloudWatch.
  - ``console`` — colored, human-readable; auto-selected when stdout is a TTY.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_configured = False


def _resolve_format(fmt: str | None) -> str:
    if fmt:
        return fmt
    env = os.getenv("DF_LOG_FORMAT")
    if env:
        return env.lower()
    return "console" if sys.stdout.isatty() else "json"


def configure_logging(level: str = "INFO", fmt: str | None = None) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _configured
    if _configured:
        return

    resolved = _resolve_format(fmt)
    lvl = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

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
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)

    # Dampen noisy third-party loggers unless user explicitly asks for DEBUG.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Safe to call before configure_logging."""
    return structlog.get_logger(name)
