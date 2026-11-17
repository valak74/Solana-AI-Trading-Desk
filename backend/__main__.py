"""Entry point: ``python -m backend`` starts the service."""

import uvicorn

from backend.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "backend.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
