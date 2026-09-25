"""Process logging, configured from TSAGENT_LOG_LEVEL / TSAGENT_LOG_FORMAT.

``json`` emits one object per line, which OpenShift's cluster logging (Vector/Loki or
Elasticsearch) can index field by field; ``text`` is easier to read in ``oc logs``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install one stdout handler on the root logger and route uvicorn's loggers through it."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # uvicorn installs its own handlers when given a log_config; we pass log_config=None and
    # let its loggers propagate here instead, so access logs share the chosen format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # LiteLLM is very chatty at INFO; keep it one level quieter than the app unless debugging.
    if root.level > logging.DEBUG:
        logging.getLogger("LiteLLM").setLevel(max(root.level, logging.WARNING))
