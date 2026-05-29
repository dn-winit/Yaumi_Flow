"""
Entry point: ``python -m data_import``
"""

from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# Load unified .env from project root before any settings import
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


from common.runtime import port_from_env, require_env
from data_import.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    # Production guard: the nightly cron cascades reconciliation via
    # ``DI_FORECAST_URL`` so downstream diagnostic columns stay aligned
    # with the freshly-mirrored CSVs. In dev/staging the soft-skip
    # branch logs a warning and the 03:30 backstop still fires;
    # production hard-fails so a half-configured deploy is loud at
    # boot, not silently noticed after the first missed cascade.
    require_env(["DI_FORECAST_URL"], service="data_import")
    uvicorn.run(
        "data_import.api.app:create_app",
        factory=True,
        host=settings.host,
        port=port_from_env(settings.port),
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
