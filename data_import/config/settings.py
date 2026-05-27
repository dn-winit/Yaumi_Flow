"""Settings -- DB credentials, paths, route codes; all env-driven."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.route_registry import get_route_codes as _get_route_codes
from common.sales_vocab import SALES_VOCAB as _VOCAB
from common.settings_base import data_root as _shared_data_root, read_allow_origins as _read_allow_origins

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Production training window for sales_recent (5y), shared by full-refresh
# pull so it matches what the model was trained on.
FULL_REFRESH_LOOKBACK_DAYS = 1825


def _data_root() -> Path:
    return _shared_data_root(_PROJECT_ROOT)


class _BaseDbSettings(BaseSettings):
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    host: str = Field(default="", description="DB server IP/hostname")
    port: int = Field(default=1433)
    database: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    connection_timeout: int = Field(default=120, ge=10)
    # Short timeouts for interactive live-sales queries (supervisor UI).
    # Bulk import keeps the full connection_timeout above.
    live_connection_timeout: int = Field(default=10, ge=1)
    live_query_timeout: int = Field(default=15, ge=1)
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)

    def connection_string(self, *, live: bool = False) -> str:
        timeout = self.live_connection_timeout if live else self.connection_timeout
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={timeout};"
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
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # Databases
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    aiml_db: AimlDatabaseSettings = Field(default_factory=AimlDatabaseSettings)

    # Unified DB-mirror dir; single env override moves the whole stack.
    data_dir: str = Field(default_factory=lambda: str(_data_root() / "imports"))
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    sales_recent_file: str = Field(default="sales_recent.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")
    sales_transactions_file: str = Field(default="sales_transactions.csv")
    # Van-stock reconciliation inputs (refreshed nightly with the rest)
    closing_stock_file: str = Field(default="closing_stock.csv")
    load_allocation_file: str = Field(default="load_allocation.csv")
    returns_recent_file: str = Field(default="returns_recent.csv")

    # Source views/tables (configurable)
    sales_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_SALES_DETAILS]")
    journey_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_JOURNEYPLAN_DETAILS]")
    closing_stock_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_CLOSING_STOCK]")
    load_allocation_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_LOAD_ALLOCATION_DETAILS]")
    demand_forecast_table: str = Field(default="[YaumiAIML].[dbo].[yf_demand_forecast]")
    sales_transactions_table: str = Field(default="[YaumiAIML].[dbo].[yf_sales_transactions]")

    # TrxType vocabulary in VW_GET_SALES_DETAILS. Defaults come from the
    # shared ``common.sales_vocab.SALES_VOCAB`` so this service and
    # demand_forecasting can't drift on the strings; env overrides still work.
    # Bad Return = damaged write-off; Good Return = salable stock back to depot.
    sales_invoice_trx_type: str = Field(default=_VOCAB.sales_invoice_trx_type)
    bad_return_trx_type:    str = Field(default=_VOCAB.bad_return_trx_type)
    good_return_trx_type:   str = Field(default=_VOCAB.good_return_trx_type)
    sales_item_type:        str = Field(default=_VOCAB.sales_item_type)

    # Route codes from shared registry; override via ``YF_ROUTE_CODES``.
    route_codes: list[str] = Field(default_factory=_get_route_codes)

    # Per-table FULL-refresh lookback windows. Sized to actual downstream
    # usage (recon max=90, ffill=7, forecast horizon=60) plus a buffer.
    # sales_recent stays at the full training window for model retraining.

    # Customer identity + catalog dimension data.
    customer_data_lookback_days: int = Field(default=180, ge=30, le=3650)

    # Forward-looking visit plan; covers UI 7d + planner horizon.
    journey_plan_window_days: int = Field(default=90, ge=7, le=365)

    # Training-grade history: lag_{30,60,90} + 28d rolling features.
    sales_recent_lookback_days: int = Field(default=FULL_REFRESH_LOOKBACK_DAYS, ge=30)

    # AIML-side mirrors sized to 2x downstream usage for safety buffer.
    demand_forecast_lookback_days: int = Field(default=60, ge=14, le=365,
        description="Forecast horizon (today + future) the FE renders.")
    sales_transactions_lookback_days: int = Field(default=180, ge=30, le=730,
        description="Reconciliation history; 2x past-performance max (90).")
    closing_stock_lookback_days: int = Field(default=180, ge=30, le=730,
        description="2x past-performance max; covers 7d ffill window.")
    load_allocation_lookback_days: int = Field(default=180, ge=30, le=730,
        description="2x past-performance max (90).")
    sales_returns_lookback_days: int = Field(default=180, ge=30, le=730,
        description="Returns drive bad/good rate drawer + past-performance.")

    # sales_recent refresh-window for the daily cron: re-pulls and dedup-merges
    # to overwrite gross rows with netted ones once late returns arrive.
    # 0 disables (pure since_date incremental, not recommended with netting).
    sales_recent_refresh_window_days: int = Field(default=30, ge=0, le=365)

    # CSV prune retention: drop rows older than per-dataset lookback in the
    # same atomic write as the merge, so file size stays bounded.
    prune_to_lookback: bool = Field(default=True,
        description="Drop CSV rows older than the dataset's configured lookback after each merge.")

    # import_all fan-out: bounded thread pool over I/O-bound pyodbc.
    import_concurrency: int = Field(default=4, ge=1, le=16)

    # Live cut-through cache for the supervisor UI.
    live_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)

    # Shared CORS allow-list (``YF_ALLOW_ORIGINS``, plain CSV).
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # Scheduler -- daily incremental import
    scheduler_enabled: bool = Field(default=True)
    scheduler_timezone: str = Field(default="Asia/Dubai")
    scheduler_hour: int = Field(default=3, ge=0, le=23)
    scheduler_minute: int = Field(default=0, ge=0, le=59)
    # Leader-election lock: one worker per host fires the daily cron.
    # Without this, ``workers>1`` would have N workers all racing the same
    # YaumiAIML writes on the 03:00 fire.
    scheduler_lock_path: str = Field(
        default_factory=lambda: str(_data_root() / "imports" / "scheduler.lock"),
        description="Filesystem path for scheduler leader-election lock.",
    )

    # Reverse cascade: after a fresh full import, trigger forecast service
    # to refresh yf_sales_transactions so the UI's carry chain + actual_sold
    # stay aligned. Unset = silent skip (next 03:30 cron picks it up).
    forecast_url: str = Field(default="", description="demand_forecasting base URL for the import->refresh cascade.")
    forecast_refresh_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)

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
