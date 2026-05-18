"""
Entry point: ``python -m recommended_order``
"""

from pathlib import Path
from dotenv import load_dotenv
import uvicorn

# Load unified .env from project root before any settings import
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


from common.runtime import port_from_env, require_env
from recommended_order.config.settings import get_settings


def main() -> None:
    settings = get_settings()

    # Production guard: the lazy auto-heal in /get + /generate calls
    # demand_forecasting's /reconciliation/refresh when the carry chain
    # is missing for the target date. With ``RO_DEMAND_FORECASTING_URL``
    # unset, /get raises a clear RuntimeError -> structured 200 envelope
    # (correct for dev), but a production deploy without it means the
    # nightly cron has no recovery path at all.
    require_env(["RO_DEMAND_FORECASTING_URL"], service="recommended_order")

    # For workers > 1, uvicorn needs a factory string (it forks processes)
    uvicorn.run(
        "recommended_order.api.app:create_app",
        factory=True,
        host=settings.host,
        port=port_from_env(settings.port),
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
