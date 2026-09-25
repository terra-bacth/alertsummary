"""Container entrypoint: ``python -m tsagent`` (or the ``tsagent`` console script).

Every server option comes from TSAGENT_* environment variables (see ``tsagent.config.Settings``),
so the image never needs a different CMD per environment — a ConfigMap is enough.
"""

from __future__ import annotations

import uvicorn

from tsagent.config import get_settings
from tsagent.logging_setup import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    ssl: dict = {}
    if settings.ssl_certfile and settings.ssl_keyfile:
        ssl = {"ssl_certfile": settings.ssl_certfile, "ssl_keyfile": settings.ssl_keyfile}

    uvicorn.run(
        "tsagent.main:app",
        host=settings.host,
        port=settings.port,
        # One worker by design: the SSE event bus is in-process and the stores are SQLite.
        workers=1,
        # root_path is deliberately NOT given to uvicorn: uvicorn assumes the proxy stripped the
        # prefix and prepends it again, but the OpenShift router forwards the path unchanged.
        # FastAPI gets it instead (tsagent.main), which accepts both prefixed and bare paths.
        proxy_headers=settings.proxy_headers,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_keep_alive=settings.timeout_keep_alive,
        timeout_graceful_shutdown=settings.timeout_graceful_shutdown,
        access_log=settings.access_log,
        log_level=settings.log_level.lower(),
        log_config=None,  # keep the handlers configure_logging() installed
        **ssl,
    )


if __name__ == "__main__":
    main()
