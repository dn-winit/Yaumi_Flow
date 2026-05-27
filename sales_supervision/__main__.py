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
    # Hard-fail boot on missing cross-service URLs; fail-soft would mean a silently-dark supervisor.
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
