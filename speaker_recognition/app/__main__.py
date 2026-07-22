"""Container entry point."""

import logging

import uvicorn

from app.api import settings


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
