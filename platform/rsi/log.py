"""Structured logging (config doc §2 logging, design 5.2 / NFR5).

structlog with JSON output; ``RSI_LOG_FORMAT=console`` switches to colored
console rendering. Run/batch/experiment context flows via structlog
contextvars so it appears on every log line once bound.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

CONTEXT_KEYS = ("run_id", "batch_id", "experiment_id")


def _is_console() -> bool:
    return os.environ.get("RSI_LOG_FORMAT", "").lower() == "console"


class _CurrentStdout:
    """File-like proxy that resolves ``sys.stdout`` at write time.

    Avoids capturing a stale/stdout wrapper at configure time (colorama on
    Windows wraps stdout and pytest's capsys replaces it per-test).
    """

    def write(self, s: str) -> int:
        return sys.stdout.write(s)

    def flush(self) -> None:
        sys.stdout.flush()


def configure_logging(level: str | None = None) -> None:
    """(Re)configure structlog + stdlib logging. JSON by default."""
    level = level or os.environ.get("RSI_LOG_LEVEL", "info")
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if _is_console():
        renderer: structlog.typing.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared, structlog.processors.UnicodeDecoder(), renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStdout()),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound with ``logger=name``."""
    return structlog.get_logger(name).bind(logger=name)


def bind_contextvars(**kwargs) -> None:
    """Bind run/batch/experiment (or any) context onto all future logs."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_contextvars() -> None:
    structlog.contextvars.clear_contextvars()


configure_logging()
