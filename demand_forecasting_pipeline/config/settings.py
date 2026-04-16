"""
Application settings from environment variables.
Pipeline-specific ML params stay in config.yaml -- this handles server/API/paths.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PIPELINE_ROOT.parent


class DbSettings(BaseSettings):
    """DB connection for pushing results to YaumiAIML."""

    model_config = {"env_prefix": "DF_DB_", "extra": "ignore"}

    host: str = Field(default="")
    port: int = Field(default=1433)
    database: str = Field(default="YaumiAIML")
    username: str = Field(default="")
    password: str = Field(default="")
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    connection_timeout: int = Field(default=120, ge=10)
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)

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
    """Server and path settings -- all from env vars with sensible defaults."""

    model_config = {"env_prefix": "DF_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="Demand Forecasting Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8002, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # Pipeline config (YAML path)
    pipeline_config: str = Field(default=str(_PIPELINE_ROOT / "config" / "config.yaml"))

    # Paths
    raw_data_path: str = Field(default=str(_PROJECT_ROOT / "data" / "sales_recent.csv"))
    artifacts_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts"))
    models_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts" / "models"))
    predictions_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts" / "predictions"))
    metrics_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts" / "metrics"))
    explainability_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts" / "explainability"))
    logs_dir: str = Field(default=str(_PIPELINE_ROOT / "artifacts" / "logs"))

    # Artifact filenames
    test_predictions_file: str = Field(default="test_predictions.csv")
    future_forecast_file: str = Field(default="future_forecast.csv")
    model_metrics_file: str = Field(default="model_metrics.csv")
    training_summary_file: str = Field(default="training_summary.json")
    pair_model_lookup_file: str = Field(default="pair_model_lookup.csv")
    pair_classes_file: str = Field(default="pair_classes.csv")
    pair_explainability_file: str = Field(default="pair_explainability.csv")

    # Cache
    cache_ttl_seconds: int = Field(default=300, ge=0)

    # Auto-retrain
    retrain_check_interval_hours: int = Field(default=6, ge=1)
    retrain_config_path: str = Field(default=str(_PIPELINE_ROOT / "data" / "retrain_config.json"))
    drift_warn_threshold: float = Field(default=3.0, ge=0)
    drift_alert_threshold: float = Field(default=7.0, ge=0)

    # DB push (target table for demand predictions)
    db: DbSettings = Field(default_factory=DbSettings)
    demand_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_demand_forecast]")

    # YaumiLive (read-only) -- for live actual sales lookup
    live_db_host: str = Field(default="")
    live_db_port: int = Field(default=1433)
    live_db_database: str = Field(default="YaumiLive")
    live_db_username: str = Field(default="")
    live_db_password: str = Field(default="")
    live_sales_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_SALES_DETAILS]")
    live_route_codes: list[str] = Field(default=[])

    @property
    def live_db_configured(self) -> bool:
        return bool(self.live_db_host and self.live_db_username)

    def live_connection_string(self) -> str:
        return (
            f"DRIVER={self.db.driver};"
            f"SERVER={self.live_db_host},{self.live_db_port};"
            f"DATABASE={self.live_db_database};"
            f"UID={self.live_db_username};"
            f"PWD={self.live_db_password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.db.connection_timeout};"
        )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v

    def predictions_path(self, filename: str) -> Path:
        return Path(self.predictions_dir) / filename

    def metrics_path(self, filename: str) -> Path:
        return Path(self.metrics_dir) / filename

    def explainability_path(self, filename: str) -> Path:
        return Path(self.explainability_dir) / filename

    def artifact_path(self, filename: str) -> Path:
        return Path(self.artifacts_dir) / filename


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("DF_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
