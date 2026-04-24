"""Structured JSON logging utilities for L6 services."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format logging records as compact JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize one log record to JSON.

        Args:
            record: Native Python log record.

        Returns:
            str: JSON string for log output.
        """

        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def setup_logger(level: int = logging.INFO) -> None:
    """Configure root logger to emit structured JSON logs.

    Args:
        level: Minimum logging level for root logger.
    """

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger instance.

    Args:
        name: Logger namespace.

    Returns:
        logging.Logger: Configured logger.
    """

    return logging.getLogger(name)
