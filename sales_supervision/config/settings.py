"""
Settings from environment variables.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.settings_base import data_root as _shared_data_root, read_allow_origins as _read_allow_origins

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _data_root() -> Path:
    return _shared_data_root(_PROJECT_ROOT)


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
    # Per-cursor timeout: bounds how long a hung warehouse can stall save_session.
    query_timeout: int = Field(default=60, ge=5)
    # Caps worst-case payload size in a single executemany chunk.
    executemany_chunk_size: int = Field(default=1000, ge=1, le=10000)

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
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # In-memory session registry. Env-overridable for longer shifts.
    session_registry_max: int = Field(default=256, ge=16, le=4096,
        description="LRU cap on concurrent in-memory supervision sessions.")
    session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60 * 60,
        description="Idle TTL before an in-memory session is reclaimed.")

    # Upstream data_import owns all DB access; called over HTTP for live actuals.
    data_import_url: str = Field(
        default="http://localhost:8005",
        description="Base URL for data_import service",
    )
    data_import_timeout: int = Field(default=15, ge=1)

    # CORS allow-list -- shared ``YF_ALLOW_ORIGINS`` env var.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # Auto-visit reconciler: background job that mirrors live YaumiLive
    # invoices into yf_supervision_* tables independent of the browser.
    auto_visit_enabled: bool = Field(default=True,
        description="Master switch for the supervision auto-fill background job.")
    auto_visit_timezone: str = Field(default="Asia/Dubai",
        description="Scheduler timezone (matches sibling services for log alignment).")
    auto_visit_poll_seconds: int = Field(default=60, ge=30, le=3600,
        description="Reconciliation tick interval. Min 30s to avoid hammering YaumiLive.")
    auto_visit_route_codes: list[str] = Field(default_factory=list,
        description="Routes to reconcile each tick. Empty -> falls back to demand-forecasting's live_route_codes.")
    auto_visit_data_phase_workers: int = Field(default=8, ge=1, le=64,
        description="Concurrency cap for the data-sync phase of reconcile_route.")
    # Pool must exceed data_phase_workers so UI hot paths never block behind the cron.
    db_pool_max_connections: int = Field(default=16, ge=1, le=64,
        description="Total DB connection slots. Must be >= auto_visit_data_phase_workers with UI headroom.")
    auto_visit_session_ttl_seconds: int = Field(default=14400, ge=300, le=86400,
        description="Eviction TTL for cached (route, date) sessions so _sessions can't grow unbounded across days.")
    # Leader-election lock: one worker per host runs the auto-visit reconciler.
    # ``workers>1`` without this would race N copies of the same YaumiAIML MERGEs.
    scheduler_lock_path: str = Field(
        default_factory=lambda: str(_data_root() / "supervision" / "scheduler.lock"),
        description="Filesystem path for the auto-visit reconciler leader-election lock.",
    )
    # Saved-visits hot-path cache: absorbs UI polling without explicit invalidation; staleness bounded by cron cadence.
    saved_visits_cache_ttl_seconds: float = Field(default=0.0, ge=0.0, le=600.0,
        description="In-process TTL for load_session_visits. 0 = adaptive (auto_visit_poll_seconds / 2).")
    saved_visits_warmup_enabled: bool = Field(default=True,
        description="Pre-load /session/saved per configured route at startup so first poll hits warm cache.")

    # Recommended-order client (used by the reconciler to scope a session).
    recommended_order_url: str = Field(default="http://localhost:8001",
        description="Base URL of the recommended_order service.")
    recommended_order_timeout: int = Field(default=30, ge=1)
    recommendation_cache_seconds: int = Field(default=300, ge=0,
        description="Per-(route, date) recommendation cache TTL.")
    recommendation_fetch_limit: int = Field(default=10000, ge=1, le=100000)

    # LLM analytics client (best-effort during the reconciler).
    llm_analytics_url: str = Field(default="http://localhost:8003",
        description="Base URL of llm_analytics. Empty -> the reconciler skips all LLM calls.")
    llm_analytics_timeout: int = Field(default=120, ge=10, le=600,
        description="Per-call timeout in seconds.")

    @field_validator("auto_visit_route_codes", mode="before")
    @classmethod
    def _coerce_auto_routes(cls, v):
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return v

    @property
    def effective_saved_visits_cache_ttl(self) -> float:
        """Resolve load_session_visits cache TTL; 0 -> half of auto_visit_poll_seconds."""
        return float(
            self.saved_visits_cache_ttl_seconds
            or (self.auto_visit_poll_seconds / 2.0)
        )

    # DB (optional -- saves to DB in addition to file)
    db: DbSettings = Field(default_factory=DbSettings)
    route_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_route_summary]")
    customer_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_customer_summary]")
    item_details_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_item_details]")
    # Current-plan source for alsoBought dedupe vs older recommendation snapshots; bare name resolves via SS_DB_DATABASE.
    recommendations_table: str = Field(
        default="yf_recommended_orders",
        description="Current-plan source for alsoBought dedupe (default: yf_recommended_orders).",
    )

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
