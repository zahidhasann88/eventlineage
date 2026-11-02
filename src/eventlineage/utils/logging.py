"""Structured logging configuration.

Logs go to stderr so stdout stays machine-parsable (JSON exports, DOT, ...).
``--log-format json`` switches to one JSON object per line, which is what you
want in CI or when collecting logs from a container.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, TextIO

LOGGER_NAME = "eventlineage"

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter (no external dependency)."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record for the terminal or a JSON pipeline."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable formatter: ``level  logger  message``."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record for the terminal or a JSON pipeline."""
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{record.levelname.lower():7} {record.name.replace('eventlineage.', '')}: {message}"


def configure_logging(
    verbosity: int = 0,
    *,
    quiet: bool = False,
    json_output: bool = False,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure the ``eventlineage`` logger hierarchy and return the root logger.

    ``verbosity`` follows the usual CLI convention: 0 = WARNING, 1 = INFO,
    2 = DEBUG, 3+ = DEBUG including noisy libraries.
    """
    env_level = logging.getLevelName(os.environ.get("EVENTLINEAGE_LOG_LEVEL", "").upper())
    if quiet:
        level = logging.ERROR
    elif verbosity <= 0:
        # An unknown name comes back as the string "Level X", not as a number.
        level = env_level if isinstance(env_level, int) else logging.WARNING
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    logger.addHandler(handler)
    if verbosity >= 3:  # pragma: no cover - debugging aid
        logging.getLogger("eventlineage").setLevel(logging.DEBUG)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger inside the ``eventlineage`` hierarchy."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


__all__ = ["ConsoleFormatter", "JsonFormatter", "configure_logging", "get_logger"]
