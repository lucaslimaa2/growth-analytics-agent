"""Structured JSON logging built on stdlib `logging`.

Emits one JSON object per log event to stdout (Vercel captures stdout into its
log viewer; aggregators like Datadog or Loki parse JSON natively).

Usage:
    from lib.logging import get_logger
    log = get_logger(__name__)

    log.info("agent_turn_start", question="why did mrr flatline?")
    log.warning("query_truncated", rows=500, sql=sql[:200])
    log.error("tool_failed", exc_info=True)

Pass extra fields as keyword args using the standard `extra=` form, or use the
project's small helper `log.info(msg, extra={...})`. Anything in `extra` gets
merged into the JSON event.

Log level is taken from the LOG_LEVEL env var (default INFO).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Fields LogRecord always sets; we don't want to repeat them in the JSON payload.
_RESERVED = {
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
    "message",
    "module",
    "msecs",
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


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge any extra={"key": value} kwargs passed to the log call.
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        return json.dumps(payload, default=str, ensure_ascii=False)


_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured for JSON output. Idempotent across imports."""
    _configure_root()
    return logging.getLogger(name)
