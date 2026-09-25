"""In-process control service for a roller line."""

from __future__ import annotations

__all__ = ["__version__", "build_service"]

__version__ = "1.0.0"


def build_service(*args, **kwargs):
    """Import the console service lazily so light tooling skips heavy imports."""

    from kilnline.console.service import ControlService

    return ControlService(*args, **kwargs)
