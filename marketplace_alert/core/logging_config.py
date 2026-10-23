"""Structured (JSON) logging setup.

Kept dependency-free (stdlib ``logging`` only) - swap in a library like
``structlog`` later if richer structured logging is needed.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from types import TracebackType


def exception_summary(exc: BaseException) -> str:
    """A log-safe description of an exception: its type and where it was raised.

    Deliberately never includes ``str(exc)``/``repr(exc)`` or a traceback's
    exception line: an arbitrary exception's message can contain anything the
    failing code was handling - including a saved-search keyword (e.g. a URL
    with ``?query=...`` in an HTTP-library error). Use this instead of
    ``logger.exception(...)`` wherever user-entered text may be in scope.
    """
    frame: TracebackType | None = exc.__traceback__
    location = "unknown location"
    while frame is not None:
        code = frame.tb_frame.f_code
        location = f"{code.co_filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{frame.tb_lineno} in {code.co_name}"
        frame = frame.tb_next
    return f"{type(exc).__name__} at {location}"


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
    # Same reasoning for the transport layer underneath it: at DEBUG it traces
    # per-request details, and connector requests carry the user's search
    # keyword as a query parameter - keep it out even when LOG_LEVEL=DEBUG.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
