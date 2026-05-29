"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.route_registry import get_route_codes as _get_route_codes
from common.settings_base import data_root as _shared_data_root
from common.settings_base import read_allow_origins as _read_allow_origins

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _data_root() -> Path:
    return _shared_data_root(_PROJECT_ROOT)


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
    # Per-cursor timeout for the bulk push (large to handle thousands of
    # rows per executemany).
    query_timeout: int = Field(default=300, ge=10)
    # Server-triggered push retries transient blips (mirrors df pusher).
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)
    # executemany batch size: amortise round-trip cost, bound rollback.
    executemany_chunk_size: int = Field(default=1000, ge=1)
    # DELETE+INSERT bulk push isolation; SERIALIZABLE locks the range for
    # the whole txn so concurrent readers can't see the empty window.
    merge_isolation_level: str = Field(
        default="SERIALIZABLE",
        description="SQL Server txn isolation for the bulk recommendation push.",
    )
    # HOLDLOCK extends range lock to full txn; UPDLOCK avoids reader deadlock.
    merge_target_lock_hints: str = Field(
        default="HOLDLOCK, UPDLOCK",
        description="Lock hints injected into the DELETE phase of the bulk push.",
    )

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
    # 04:30 Dubai gives upstream crons room (03:00 data_import, 03:30
    # reconciliation); generation forces dm.refresh() at start.
    generation_hour: int = Field(default=4, ge=0, le=23)
    generation_minute: int = Field(default=30, ge=0, le=59)
    max_retries: int = Field(default=3, ge=1)
    retry_delay_seconds: int = Field(default=60, ge=10)
    # Leader-election lock: one worker per host fires the daily generation cron.
    # Without this, ``workers>1`` would have N workers all racing the same
    # ``yf_recommended_orders`` MERGEs.
    lock_path: str = Field(
        default_factory=lambda: str(_data_root() / "recommendations" / "scheduler.lock"),
        description="Filesystem path for the recommendation-generation leader-election lock.",
    )


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

    # Per-route generation parallelism (numpy/pandas releases the GIL).
    generation_concurrency: int = Field(default=4, ge=1, le=32,
                                        description="Threads for per-route generation")

    # Transient pre-DB working copy: one CSV per (date, route). Recs are
    # DB-canonical (yf_recommended_orders); CSVs are a push staging area.
    file_storage_dir: str = Field(
        default_factory=lambda: str(_data_root() / "recommendations"),
        description="Dir for file-based recommendation snapshots (transient pre-DB)",
    )

    # Shared data directory (CSVs owned by data_import) -- single source of truth.
    shared_data_dir: str = Field(
        default_factory=lambda: str(_data_root() / "imports"),
        description="Unified imports/ mirror written by data_import (DB-canonical CSVs)",
    )
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")
    # Carry-chain + reconciliation mirror (past + today only); future-horizon
    # rows fall through to DataManager's inline enrichment.
    sales_transactions_file: str = Field(default="sales_transactions.csv")

    # DB replication target (DbPusher writes to this table; reads come from file).
    recommendation_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_recommended_orders]")

    # demand_forecasting URL for the pre-generation carry-chain guard.
    # Empty disables the auto-heal (but missing row still raises).
    demand_forecasting_url: str = Field(default="", description="e.g. http://localhost:8002")
    reconciliation_preflight_timeout_seconds: float = Field(default=420.0, ge=10.0)

    # Route codes from shared registry (override via ``YF_ROUTE_CODES``).
    route_codes: list[str] = Field(default_factory=_get_route_codes)

    # Demand filter (applied to rows read from demand_forecast.csv)
    demand_probability_threshold: float = Field(default=0.99, ge=0.0, le=1.0)

    # Carry-chain lookback days for Tier-2 fallback in DataManager; covers
    # non-trip gaps (weekends/holidays). 1 disables walk-back; 0 disables seed.
    carry_chain_lookback_days: int = Field(default=14, ge=0, le=90,
        description="Days the Tier-2 carry seed walks back for the most recent leftover_to_next_day.")

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
