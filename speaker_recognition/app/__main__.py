"""Container entry point."""

import logging

import uvicorn

from app.api import settings
from app.companion import install_integration, publish_discovery


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        install_integration()
    except (OSError, RuntimeError) as error:
        # Companion installation must never prevent the recognition GUI/API from starting.
        logging.getLogger(__name__).error("Could not install companion integration: %s", error)
    publish_discovery(settings.companion_token, settings.port)
    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG",
        # Keep request.client bound to the real socket peer; API authorization uses
        # it to distinguish Supervisor ingress from spoofed LAN headers.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
