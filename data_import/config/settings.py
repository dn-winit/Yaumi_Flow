"""
Settings -- DB credentials, paths, route codes. All from env vars.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_MODULE_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _MODULE_ROOT.parent


class _BaseDbSettings(BaseSettings):
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    host: str = Field(default="", description="DB server IP/hostname")
    port: int = Field(default=1433)
    database: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    connection_timeout: int = Field(default=120, ge=10)
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)

    def connection_string(self) -> str:
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.connection_timeout};"
        )


class DatabaseSettings(_BaseDbSettings):
    """Live OLTP (YaumiLive) -- source for raw sales / journey data."""
    model_config = {"env_prefix": "DI_DB_", "extra": "ignore"}
    database: str = Field(default="YaumiLive")


class AimlDatabaseSettings(_BaseDbSettings):
    """AIML results DB -- source for forecast outputs written by the pipeline."""
    model_config = {"env_prefix": "DI_AIML_DB_", "extra": "ignore"}
    database: str = Field(default="YaumiAIML")


class Settings(BaseSettings):
    model_config = {"env_prefix": "DI_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="Data Import Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8005, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # Databases
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    aiml_db: AimlDatabaseSettings = Field(default_factory=AimlDatabaseSettings)

    # Output paths
    data_dir: str = Field(default=str(_PROJECT_ROOT / "data"))
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    sales_recent_file: str = Field(default="sales_recent.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")

    # Source views/tables (configurable)
    sales_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_SALES_DETAILS]")
    journey_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_JOURNEYPLAN_DETAILS]")
    demand_forecast_table: str = Field(default="[YaumiAIML].[dbo].[yf_demand_forecast]")

    # Route codes
    route_codes: list[str] = Field(default=[
        "9105", "9108", "9114", "9115", "9126", "9142",
        "9202", "9204", "9209", "9218", "9219", "9221",
    ])

    # Lookback defaults (for full refresh)
    customer_data_lookback_days: int = Field(default=365, ge=30)
    journey_plan_window_days: int = Field(default=90, ge=7)
    sales_recent_lookback_days: int = Field(default=365, ge=30)

    # Scheduler -- daily incremental import
    scheduler_enabled: bool = Field(default=True)
    scheduler_timezone: str = Field(default="Asia/Dubai")
    scheduler_hour: int = Field(default=3, ge=0, le=23)
    scheduler_minute: int = Field(default=0, ge=0, le=59)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v

    def data_path(self, filename: str) -> Path:
        return Path(self.data_dir) / filename


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("DI_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
