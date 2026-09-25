"""Entry conveyor.

Feeding is gated twice: the spacing for the carrier has to be confirmed against
the entry window, and the grate placement has to be on disk.  The second rule
is expressed as a named gate so the refusal message says which of the two is
missing instead of failing with a generic conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.conv.spacing import EntryWindow, GapVerdict
from kilnline.errors import InterlockActive, StateConflict, ValidationError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.kiln.zones import GATE_GRATE_PERSISTED

GATE_SPACING_CONFIRMED = "conv.spacing_confirmed"
GATE_FEED_ALLOWED = "conv.feed_allowed"
CONV_LATCH = "conv.line"


@dataclass(frozen=True)
class EntryRecord:
    """A carrier that has crossed onto the entry conveyor."""

    car_id: str
    gap_mm: float
    verdict: str
    fed_at: float
    position_m: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "car_id": self.car_id,
            "gap_mm": self.gap_mm,
            "verdict": self.verdict,
            "fed_at": self.fed_at,
            "position_m": self.position_m,
        }


class ConveyorLine:
    """Entry conveyor with spacing confirmation and feed gating."""

    def __init__(
        self,
        gates: PreGateRegistry,
        latches: LatchRegistry,
        window: EntryWindow,
        *,
        speed_m_per_s: float,
        spacing_gate: str = GATE_SPACING_CONFIRMED,
        feed_gate: str = GATE_FEED_ALLOWED,
        grate_gate: str = GATE_GRATE_PERSISTED,
        latch_name: str = CONV_LATCH,
    ) -> None:
        if float(speed_m_per_s) <= 0.0:
            raise ValidationError("conveyor speed must be positive")
        self._gates = gates
        self._latches = latches
        self._window = window
        self._speed = float(speed_m_per_s)
        self._spacing_gate = str(spacing_gate)
        self._feed_gate = str(feed_gate)
        self._grate_gate = str(grate_gate)
        self._latch_name = str(latch_name)
        for name, description in (
            (self._spacing_gate, "entry spacing confirmed against the window"),
            (self._feed_gate, "conveyor may admit carriers"),
        ):
            if name not in gates.names():
                gates.declare(name, description=description)
        if self._latch_name not in latches.snapshot():
            latches.declare(self._latch_name, description="entry conveyor faulted")
        self._position_m = 0.0
        self._pending: tuple[str, float, GapVerdict] | None = None
        self._entries: list[EntryRecord] = []

    @property
    def spacing_gate(self) -> str:
        return self._spacing_gate

    @property
    def feed_gate(self) -> str:
        return self._feed_gate

    @property
    def grate_gate(self) -> str:
        return self._grate_gate

    @property
    def window(self) -> EntryWindow:
        return self._window

    @property
    def latch_name(self) -> str:
        return self._latch_name

    @property
    def position_m(self) -> float:
        return self._position_m

    def confirm_spacing(self, *, car_id: str, gap_mm: float, at: float) -> GapVerdict:
        label = self._label(car_id)
        verdict = self._window.require(gap_mm, car_id=label)
        self._pending = (label, float(gap_mm), verdict)
        self._gates.satisfy(
            self._spacing_gate,
            at=at,
            detail=f"carrier {label} gap {verdict.gap_mm:.1f} mm",
        )
        return verdict

    def feed(self, *, car_id: str, at: float) -> EntryRecord:
        label = self._label(car_id)
        if self._pending is None or self._pending[0] != label:
            raise StateConflict(
                "carrier spacing has not been confirmed",
                car_id=label,
                gate=self._spacing_gate,
            )
        self._gates.require(self._spacing_gate)
        missing = [name for name in (self._grate_gate,) if not self._gates.satisfies(name)]
        if missing:
            raise InterlockActive(
                "grate placement is not on disk; feeding is refused",
                car_id=label,
                gates=missing,
            )
        self._latches.require_clear(self._latch_name)
        _, gap_mm, verdict = self._pending
        record = EntryRecord(
            car_id=label,
            gap_mm=gap_mm,
            verdict=verdict.verdict,
            fed_at=float(at),
            position_m=self._position_m,
        )
        self._entries.append(record)
        self._pending = None
        self._gates.unsatisfy(self._spacing_gate, at=at, detail="carrier admitted")
        self._gates.satisfy(self._feed_gate, at=at, detail=f"carrier {label} admitted")
        return record

    def advance(self, *, dt: float) -> float:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("conveyor advance step must be positive", dt=dt)
        self._position_m += self._speed * step
        return self._position_m

    def fault(self, reason: str, *, at: float) -> None:
        self._latches.trip(self._latch_name, reason, at=at)
        self._gates.unsatisfy(self._feed_gate, at=at, detail=reason)

    def pending(self) -> str | None:
        return None if self._pending is None else self._pending[0]

    def entries(self) -> list[EntryRecord]:
        return list(self._entries)

    def snapshot(self) -> dict[str, Any]:
        return {
            "position_m": self._position_m,
            "speed_m_per_s": self._speed,
            "window": self._window.as_dict(),
            "pending": None if self._pending is None else self._pending[2].as_dict(),
            "entries": [entry.as_dict() for entry in self._entries],
        }

    @staticmethod
    def _label(car_id: str) -> str:
        label = str(car_id).strip()
        if not label:
            raise ValidationError("carrier id must not be empty")
        return label
