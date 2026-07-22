"""Structured JSONL logging into the working tree's ``logs/`` directory.

Each record is one JSON object per line with timestamp, level, stage, event
message, and any extra fields passed via ``logger.info(..., extra={...})``.
A plain-text handler mirrors events to the console.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.logs import configure_logging
    >>> log = configure_logging(Path("logs"), stage="ingest")  # doctest: +SKIP
    >>> log.info("hashed", extra={"count": 128})  # doctest: +SKIP
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

LOG_FILENAME = "archive.jsonl"

#: LogRecord attributes that are not user-supplied extras.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
        "stage",
    }
)


class JsonlFormatter(logging.Formatter):
    """Format each record as one JSON object per line.

    Usage:
        >>> import logging
        >>> rec = logging.LogRecord("t", logging.INFO, "p", 1, "hello", None, None)
        >>> '"event": "hello"' in JsonlFormatter().format(rec)
        True
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "stage": getattr(record, "stage", None),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=False)


class _StageFilter(logging.Filter):
    """Attach the current pipeline stage to every record."""

    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    def filter(self, record: logging.LogRecord) -> bool:
        record.stage = self.stage
        return True


def configure_logging(logs_dir: Path, stage: str, *, console: bool = True) -> logging.Logger:
    """Configure the package logger to append JSONL to ``logs_dir``; return it.

    Reconfigures idempotently: prior handlers on the package logger are replaced,
    so repeated calls (e.g. one per CLI invocation) never duplicate output.

    Usage:
        >>> log = configure_logging(Path("logs"), "init")  # doctest: +SKIP
        >>> log.info("working tree initialized")  # doctest: +SKIP
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("archive_pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    # The filter lives on the handlers (not the logger) so records propagated
    # from child loggers also receive the stage field.
    stage_filter = _StageFilter(stage)
    file_handler = logging.FileHandler(logs_dir / LOG_FILENAME, encoding="utf-8")
    file_handler.setFormatter(JsonlFormatter())
    file_handler.addFilter(stage_filter)
    logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        stream_handler.addFilter(stage_filter)
        logger.addHandler(stream_handler)
    return logger
