"""Structured JSON logging utilities for the L5 service."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format Python log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record to a JSON string.

        Args:
            record: Log record to serialize.

        Returns:
            str: JSON log line.
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
    """Configure root logger for JSON output.

    Args:
        level: Minimum severity level for emitted logs.
    """

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """Return logger by module name.

    Args:
        name: Logger namespace.

    Returns:
        logging.Logger: Configured logger instance.
    """

    return logging.getLogger(name)
