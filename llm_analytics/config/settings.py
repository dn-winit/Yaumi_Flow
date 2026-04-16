"""
Settings from environment variables -- provider-agnostic LLM config.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_MODULE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = {"env_prefix": "LLM_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="LLM Analytics Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8003, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # LLM Provider -- provider-agnostic
    provider: str = Field(default="groq", description="groq | openai | anthropic")
    api_key: str = Field(default="", description="LLM provider API key")
    model: str = Field(default="llama-3.1-8b-instant", description="Model name/ID")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=256, le=32000)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    timeout: int = Field(default=45, ge=5, le=300)
    max_retries: int = Field(default=2, ge=1, le=5)
    seed: int = Field(default=42)

    # Prompts
    prompts_dir: str = Field(default=str(_MODULE_ROOT / "config" / "prompts"))

    # Cache
    cache_enabled: bool = Field(default=True)
    cache_dir: str = Field(default=str(_MODULE_ROOT / "cache"))
    cache_ttl_hours: int = Field(default=24, ge=1)

    # Rate limiting
    rate_limit_max_requests: int = Field(default=10, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=10)

    # Data limits (prevent oversized prompts)
    max_items_per_customer: int = Field(default=12)
    max_customers_per_analysis: int = Field(default=12)
    max_van_load_items: int = Field(default=15)
    max_items_per_customer_detail: int = Field(default=5)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        v = v.lower()
        if v not in {"groq", "openai", "anthropic"}:
            raise ValueError(f"Unsupported provider: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("LLM_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
