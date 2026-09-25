"""Logging configuration for the command line entry point."""

from __future__ import annotations

import logging
import sys
from typing import IO

_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> logging.Logger:
    """Attach a single stderr handler and return the service logger."""

    resolved = str(level).upper()
    if resolved not in _LEVELS:
        resolved = "INFO"
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("kilnline")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = False
    return logger
