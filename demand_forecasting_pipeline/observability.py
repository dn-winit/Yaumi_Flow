"""DF service observability -- thin adapter over ``common.observability``.

Cross-service primitives (structlog setup, request middleware, HTTP/error
counters, step_timer) live in ``common.observability`` /
``common.api_middleware`` so all five services share one contract.

This module only adds the DF-specific Prometheus series the cross-cutting
common cannot know about (pipeline runs, DB push, live-fetch timeouts,
drift gauge, etc.) and re-exports the common primitives under their
historical names so existing imports keep working without churn.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import structlog
from prometheus_client import Counter, Gauge

# Cross-service primitives -- DO NOT redefine these here.
from common.observability import (
    ERRORS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    INFO_GAUGE,
    PIPELINE_STEP_DURATION,
    configure_logging as _common_configure_logging,
    get_logger,
)
from common.observability import step_timer as _common_step_timer

SERVICE_NAME = "demand_forecasting"


# ---------------------------------------------------------------------------
# DF-only Prometheus metrics (no analogue in any other service)
# ---------------------------------------------------------------------------

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


def configure_logging(
    level: str = "INFO",
    fmt: str | None = None,
    timezone: str | None = None,
) -> None:
    """DF-facing wrapper around :func:`common.observability.configure_logging`.

    Resolves the DF-specific logs dir (``DF_LOGS_DIR`` env -> ``<data-root>/
    forecast/logs`` default) so callers don't need to know the path layout.
    Keeps the public signature stable for legacy callers.
    """
    import os

    logs_dir_env = os.getenv("DF_LOGS_DIR")
    if logs_dir_env:
        logs_dir: Path | None = Path(logs_dir_env)
    else:
        root_env = os.getenv("YF_DATA_ROOT", "").strip()
        root = (
            Path(root_env).resolve()
            if root_env
            else Path(__file__).resolve().parent.parent / "data"
        )
        logs_dir = root / "forecast" / "logs"

    # Per-service env vars are honoured if set (legacy contract); otherwise the
    # common YF_LOG_* fallbacks apply.
    def _int_env(name: str) -> int | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    _common_configure_logging(
        service_name=SERVICE_NAME,
        level=level,
        fmt=fmt or os.getenv("DF_LOG_FORMAT"),
        logs_dir=logs_dir,
        log_filename="pipeline.log",  # historical name; kept for log-aggregator continuity
        timezone=timezone,
        max_bytes=_int_env("DF_LOG_FILE_MAX_BYTES"),
        backup_count=_int_env("DF_LOG_FILE_BACKUP_COUNT"),
    )


@contextmanager
def step_timer(
    step: str,
    *,
    run_id: str | None = None,
    pipeline: str | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """DF-facing wrapper: forwards to common, fixing ``service_name``."""
    with _common_step_timer(
        step,
        service_name=SERVICE_NAME,
        run_id=run_id,
        pipeline=pipeline,
        logger=logger,
        extra=extra,
    ) as payload:
        yield payload


__all__ = [
    # Cross-cutting re-exports (preserve legacy import sites)
    "configure_logging",
    "get_logger",
    "step_timer",
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "ERRORS",
    "PIPELINE_STEP_DURATION",
    "INFO_GAUGE",
    # DF-only series
    "PIPELINE_RUNS",
    "DB_PUSH_ROWS",
    "LIVE_FETCH_TIMEOUTS",
    "L4_DISABLED",
    "DRIFT_PCT",
    "LAST_TRAIN_AGE_SECONDS",
    "FORECASTED_PAIRS",
    "DROPPED_PAIRS",
]
