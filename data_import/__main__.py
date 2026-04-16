"""
Entry point: ``python -m data_import``
"""

from pathlib import Path
from dotenv import load_dotenv
import uvicorn

# Load unified .env from project root before any settings import
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


from data_import.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "data_import.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
