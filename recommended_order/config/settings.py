"""
Application settings loaded from environment variables.
No hardcoded connection strings, secrets, or environment-specific values.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.route_registry import get_route_codes as _get_route_codes
from common.settings_base import read_allow_origins as _read_allow_origins

_MODULE_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _MODULE_ROOT.parent


def _data_root() -> Path:
    """Resolve the unified on-disk data root. ``YF_DATA_ROOT`` env var
    moves every service's filesystem layout in lockstep; defaults to
    ``<project>/data`` for fresh checkouts."""
    raw = os.getenv("YF_DATA_ROOT", "").strip()
    return Path(raw).resolve() if raw else _PROJECT_ROOT / "data"


class DatabaseSettings(BaseSettings):
    """Database connection settings -- all from env vars."""

    model_config = {"env_prefix": "RO_DB_", "extra": "ignore"}

    driver: str = Field(default="{ODBC Driver 17 for SQL Server}", description="ODBC driver name")
    host: str = Field(default="", description="Database server hostname")
    port: int = Field(default=1433)
    aiml_database: str = Field(default="YaumiAIML", description="AIML database name")
    username: str = Field(default="", description="Database username")
    password: str = Field(default="", description="Database password")
    connection_timeout: int = Field(default=120, ge=10)
    # Per-query (cursor) timeout for the bulk recommendation push -- bounds
    # how long the writer can stall on a slow warehouse before we abort and
    # retry. Larger than the supervision query budget because pushes are
    # expected to handle thousands of rows in a single executemany.
    query_timeout: int = Field(default=300, ge=10)
    # Bulk push is server-triggered (no human waiting), so a transient
    # warehouse blip should retry rather than fail the run -- mirrors
    # the demand-forecast pusher's retry envelope.
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)
    # Rows per ``cursor.executemany`` batch. Large enough to amortise the
    # round-trip cost, small enough that a transient failure inside one
    # batch leaves a bounded amount of pending work to roll back.
    executemany_chunk_size: int = Field(default=1000, ge=1)

    @property
    def aiml_connection_string(self) -> str:
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.aiml_database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.connection_timeout};"
        )


class SchedulerSettings(BaseSettings):
    """Scheduler configuration."""

    model_config = {"env_prefix": "RO_SCHEDULER_", "extra": "ignore"}

    enabled: bool = Field(default=True)
    timezone: str = Field(default="Asia/Dubai")
    # Daily schedule (Asia/Dubai). The 04:30 slot is not arbitrary --
    # it leaves room for upstream crons:
    #   03:00  data_import        (writes 7 mirror CSVs in parallel)
    #   03:30  reconciliation     (df pipeline; writes reconciled DB cols + cascade)
    #   04:30  generation         (this) -- forced ``dm.refresh()`` at start
    #                              picks up any cascade-late mirror updates
    # Override per environment via ``RO_SCHEDULER_GENERATION_HOUR`` /
    # ``..._MINUTE``.
    generation_hour: int = Field(default=4, ge=0, le=23)
    generation_minute: int = Field(default=30, ge=0, le=59)
    max_retries: int = Field(default=3, ge=1)
    retry_delay_seconds: int = Field(default=60, ge=10)


class Settings(BaseSettings):
    """Root settings -- aggregates all sub-settings."""

    model_config = {"env_prefix": "RO_", "extra": "ignore"}

    # General
    app_name: str = Field(default="Recommended Order Service")
    api_prefix: str = Field(default="/api/v1")
    log_level: str = Field(default="INFO")
    workers: int = Field(default=1, ge=1)

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8001, ge=1024, le=65535)

    # How many routes to generate in parallel inside one /generate or
    # cron-fired pass. The engine is mostly numpy/pandas (GIL releases)
    # so threads give a real win. Cap at the configured route count to
    # avoid spawning idle workers on small fleets.
    generation_concurrency: int = Field(default=4, ge=1, le=32,
                                        description="Threads for per-route generation")

    # Canonical recommendation store -- one CSV per (date, route).
    # DB replication happens orthogonally through DbPusher when configured.
    # File-based storage now lives under the unified data root. Recs are
    # DB-canonical (yf_recommended_orders); the local CSVs are a transient
    # working copy for the push pipeline. Same env var moves every service.
    file_storage_dir: str = Field(
        default_factory=lambda: str(_data_root() / "recommendations"),
        description="Dir for file-based recommendation snapshots (transient pre-DB)",
    )

    # Shared data directory (CSVs owned by data_import) -- single source of truth
    shared_data_dir: str = Field(
        default_factory=lambda: str(_data_root() / "imports"),
        description="Unified imports/ mirror written by data_import (DB-canonical CSVs)",
    )
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")
    # Carry-chain + reconciliation outputs (opening_stock, fresh_load,
    # total_van_load, leftover_to_next_day, ...) moved out of
    # ``yf_demand_forecast`` into ``yf_sales_transactions``. The mirror CSV
    # is written by data_import alongside ``demand_forecast.csv``. Carries
    # past + today only -- future-horizon rows fall through to the inline
    # enrichment path in DataManager.
    sales_transactions_file: str = Field(default="sales_transactions.csv")

    # DB replication target (DbPusher writes to this table; reads come from file).
    recommendation_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_recommended_orders]")

    # demand_forecasting URL for the pre-generation carry-chain guard.
    # Empty disables the auto-heal (but missing row still raises).
    demand_forecasting_url: str = Field(default="", description="e.g. http://localhost:8002")
    reconciliation_preflight_timeout_seconds: float = Field(default=420.0, ge=10.0)

    # Route codes -- sourced from the shared registry so data_import,
    # recommended_order, and the verification scripts all run against
    # the same fleet. Override via the ``YF_ROUTE_CODES`` env var.
    route_codes: list[str] = Field(default_factory=_get_route_codes)

    # Demand filter (applied to rows read from demand_forecast.csv)
    demand_probability_threshold: float = Field(default=0.99, ge=0.0, le=1.0)

    # CORS allow-list -- shared ``YF_ALLOW_ORIGINS`` env var.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # Sub-settings
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings instance."""
    env_file = os.getenv("RO_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
