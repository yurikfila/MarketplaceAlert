"""Structured (JSON) logging setup.

Kept dependency-free (stdlib ``logging`` only) - swap in a library like
``structlog`` later if richer structured logging is needed.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger to emit JSON lines to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # httpx logs "HTTP Request: {method} {url} ..." at INFO by default. Our
    # only httpx caller today is the Telegram Bot API, whose URL embeds the
    # bot token (https://api.telegram.org/bot<TOKEN>/...) - never let that
    # reach our logs regardless of the configured app log level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
