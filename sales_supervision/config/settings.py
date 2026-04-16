"""
Settings from environment variables.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_MODULE_ROOT = Path(__file__).resolve().parent.parent


class DbSettings(BaseSettings):
    """DB connection for saving supervision sessions to YaumiAIML."""

    model_config = {"env_prefix": "SS_DB_", "extra": "ignore"}

    host: str = Field(default="")
    port: int = Field(default=1433)
    database: str = Field(default="YaumiAIML")
    username: str = Field(default="")
    password: str = Field(default="")
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    connection_timeout: int = Field(default=120, ge=10)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username)

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


class Settings(BaseSettings):
    model_config = {"env_prefix": "SS_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="Sales Supervision Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8004, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # Storage
    storage_dir: str = Field(default=str(_MODULE_ROOT / "data"))

    # Upstream: data_import owns all DB access (single source of truth).
    # We call it over HTTP to fetch live actuals when a visit is processed.
    data_import_url: str = Field(
        default="http://localhost:8005",
        description="Base URL for data_import service",
    )
    data_import_timeout: int = Field(default=15, ge=1)

    # DB (optional -- saves to DB in addition to file)
    db: DbSettings = Field(default_factory=DbSettings)
    route_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_route_summary]")
    customer_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_customer_summary]")
    item_details_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_item_details]")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("SS_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
