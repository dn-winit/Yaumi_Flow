"""
Entry point: ``python -m demand_forecasting_pipeline``
"""

from pathlib import Path
from dotenv import load_dotenv
import uvicorn

# Load unified .env from project root before any settings import
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


from common.runtime import port_from_env, require_env
from demand_forecasting_pipeline.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    # Production guard: reconciliation cascades to data_import (CSV
    # mirror) and recommended_order (regenerate) on every refresh.
    # Missing URLs silently no-op in dev/staging; production must fail
    # boot so the dashboard never serves stale carry-chain or
    # recommendations after a successful DB write.
    require_env(
        ["DF_DATA_IMPORT_URL", "DF_RECOMMENDED_ORDER_URL"],
        service="demand_forecasting",
    )
    uvicorn.run(
        "demand_forecasting_pipeline.api.app:create_app",
        factory=True,
        host=settings.host,
        port=port_from_env(settings.port),
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
