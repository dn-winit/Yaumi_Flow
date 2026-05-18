"""
Entry point: ``python -m sales_supervision``
"""

from pathlib import Path
from dotenv import load_dotenv
import uvicorn

# Load unified .env from project root before any settings import
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


from common.runtime import port_from_env, require_env
from sales_supervision.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    # Production guard: supervision can technically boot without any
    # cross-service URL (the cron fail-soft path silently logs and
    # returns empty), but in production this would mean zero visit
    # data, zero LLM briefings, and zero recommendations -- the entire
    # supervisor surface goes dark. Hard-fail at boot keeps the
    # configuration error from looking like "feature is broken".
    require_env(
        [
            "SS_DATA_IMPORT_URL",
            "SS_RECOMMENDED_ORDER_URL",
            "SS_LLM_ANALYTICS_URL",
        ],
        service="sales_supervision",
    )
    uvicorn.run(
        "sales_supervision.api.app:create_app",
        factory=True,
        host=settings.host,
        port=port_from_env(settings.port),
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
