"""
Settings -- DB credentials, paths, route codes. All from env vars.
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
    """Resolve the unified on-disk data root for every Yaumi Flow service.

    Reads ``YF_DATA_ROOT`` from the environment so a single env var moves
    every service's filesystem layout in lockstep. Falls back to
    ``<project>/data`` so a fresh checkout works without ops setup.
    """
    raw = os.getenv("YF_DATA_ROOT", "").strip()
    return Path(raw).resolve() if raw else _PROJECT_ROOT / "data"


class _BaseDbSettings(BaseSettings):
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    host: str = Field(default="", description="DB server IP/hostname")
    port: int = Field(default=1433)
    database: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    connection_timeout: int = Field(default=120, ge=10)
    # Short connect timeout used by interactive live-sales queries served
    # to the supervisor UI. The bulk import flow still uses the full
    # connection_timeout above so a slow initial handshake during a
    # nightly refresh does not abort.
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

    # Output paths -- the unified ``imports/`` mirror under ``YF_DATA_ROOT``.
    # Every service that needs DB-mirrored data reads from this same dir,
    # so a single env override moves the whole stack to a different volume.
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

    # TrxType vocabulary in VW_GET_SALES_DETAILS. Returns are recorded as
    # separate rows with negative QuantityInPCs; Bad Return = damaged
    # write-off, Good Return = salable stock back to depot.
    sales_invoice_trx_type: str = Field(default="SalesInvoice")
    bad_return_trx_type: str = Field(default="Bad Return")
    good_return_trx_type: str = Field(default="Good Return")
    sales_item_type: str = Field(default="OrderItem")

    # Route codes -- sourced from the shared registry so data_import,
    # recommended_order, and the verification scripts all run against
    # the same fleet. Override via the ``YF_ROUTE_CODES`` env var (read
    # by the registry) so adding or retiring a route is a one-line ops
    # change, not a code edit in three files.
    route_codes: list[str] = Field(default_factory=_get_route_codes)

    # Lookback defaults (for full refresh)
    customer_data_lookback_days: int = Field(default=365, ge=30)
    journey_plan_window_days: int = Field(default=90, ge=7)
    sales_recent_lookback_days: int = Field(default=365, ge=30)

    # ``import_all`` fan-out. Each dataset query is independent and
    # I/O-bound; pyodbc releases the GIL, so a small thread pool turns
    # the 7-dataset cron into one connection-bounded round trip.
    # Conservative default keeps OLTP load gentle.
    import_concurrency: int = Field(default=4, ge=1, le=16)

    # Live YaumiLive cut-through cache window. Short by design so the
    # supervisor UI sees fresh-enough actuals while still absorbing
    # rapid-fire visit clicks on the same (route, date) cell.
    live_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)

    # CORS allow-list. ``YF_ALLOW_ORIGINS`` (shared across all services)
    # is read at boot via a default_factory so the value can be a plain
    # comma- or semicolon-separated string -- pydantic-settings' default
    # env handler insists on JSON for list-typed fields, which is too
    # strict for a config knob ops will set as a single string.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # Scheduler -- daily incremental import
    scheduler_enabled: bool = Field(default=True)
    scheduler_timezone: str = Field(default="Asia/Dubai")
    scheduler_hour: int = Field(default=3, ge=0, le=23)
    scheduler_minute: int = Field(default=0, ge=0, le=59)

    # Reverse cascade: after a fresh full import lands the raw forecast
    # mirror (yf_demand_forecast -> demand_forecast.csv), trigger the
    # forecast service to refresh ``yf_sales_transactions`` for today
    # so the UI's carry chain + actual_sold are aligned with the latest
    # raw forecasts. Without this hop, today's row in
    # ``yf_sales_transactions`` would lag by up to 24h (until the next
    # 03:30 cron). Set DI_FORECAST_URL to enable; unset = silent skip.
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
