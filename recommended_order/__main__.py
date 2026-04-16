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


from recommended_order.config.settings import get_settings


def main() -> None:
    settings = get_settings()

    # For workers > 1, uvicorn needs a factory string (it forks processes)
    uvicorn.run(
        "recommended_order.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
