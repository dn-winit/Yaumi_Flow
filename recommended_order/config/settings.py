"""
Application settings loaded from environment variables.
No hardcoded connection strings, secrets, or environment-specific values.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """Database connection settings -- all from env vars."""

    model_config = {"env_prefix": "RO_DB_", "extra": "ignore"}

    driver: str = Field(default="{ODBC Driver 17 for SQL Server}", description="ODBC driver name")
    host: str = Field(default="", description="Database server hostname")
    port: int = Field(default=1433)
    aiml_database: str = Field(default="YaumiAIML", description="AIML database name")
    live_database: str = Field(default="YaumiLive", description="Live database name")
    username: str = Field(default="", description="Database username")
    password: str = Field(default="", description="Database password")
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=10, ge=0, le=50)
    connection_timeout: int = Field(default=120, ge=10)
    retry_attempts: int = Field(default=3, ge=1, le=10)
    retry_delay: int = Field(default=2, ge=1)

    def connection_string(self, database: str) -> str:
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.connection_timeout};"
        )

    @property
    def aiml_connection_string(self) -> str:
        return self.connection_string(self.aiml_database)

    @property
    def live_connection_string(self) -> str:
        return self.connection_string(self.live_database)


class SchedulerSettings(BaseSettings):
    """Scheduler configuration."""

    model_config = {"env_prefix": "RO_SCHEDULER_", "extra": "ignore"}

    enabled: bool = Field(default=True)
    timezone: str = Field(default="Asia/Dubai")
    generation_hour: int = Field(default=4, ge=0, le=23)
    generation_minute: int = Field(default=0, ge=0, le=59)
    cache_refresh_hour: int = Field(default=3, ge=0, le=23)
    cache_refresh_minute: int = Field(default=0, ge=0, le=59)
    max_retries: int = Field(default=3, ge=1)
    retry_delay_seconds: int = Field(default=60, ge=10)


class Settings(BaseSettings):
    """Root settings -- aggregates all sub-settings."""

    model_config = {"env_prefix": "RO_", "extra": "ignore"}

    # General
    app_name: str = Field(default="Recommended Order Service")
    api_prefix: str = Field(default="/api/v1")
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    workers: int = Field(default=1, ge=1)

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8001, ge=1024, le=65535)

    # Canonical recommendation store -- one CSV per (date, route).
    # DB replication happens orthogonally through DbPusher when configured.
    file_storage_dir: str = Field(default="recommended_order/output", description="Dir for file-based storage")

    # Shared data directory (CSVs owned by data_import) -- single source of truth
    shared_data_dir: str = Field(default="data", description="Project-root data/ folder written by data_import")
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")

    # DB replication target (DbPusher writes to this table; reads come from file).
    recommendation_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_recommended_orders]")

    # Route codes (configurable, not hardcoded)
    route_codes: list[str] = Field(
        default=[
            "9105", "9108", "9114", "9115", "9126", "9142",
            "9202", "9204", "9209", "9218", "9219", "9221",
        ]
    )

    # Demand filter (applied to rows read from demand_forecast.csv)
    demand_probability_threshold: float = Field(default=0.99, ge=0.0, le=1.0)

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
