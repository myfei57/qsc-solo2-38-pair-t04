"""Kiln entry spacing and carrier feeding."""

from kilnline.conv.line import (
    GATE_FEED_ALLOWED,
    GATE_SPACING_CONFIRMED,
    ConveyorLine,
    EntryRecord,
)
from kilnline.conv.spacing import GAP_OK, GAP_TOO_CLOSE, GAP_TOO_FAR, EntryWindow, GapVerdict

__all__ = [
    "GAP_OK",
    "GAP_TOO_CLOSE",
    "GAP_TOO_FAR",
    "GATE_FEED_ALLOWED",
    "GATE_SPACING_CONFIRMED",
    "ConveyorLine",
    "EntryRecord",
    "EntryWindow",
    "GapVerdict",
]
