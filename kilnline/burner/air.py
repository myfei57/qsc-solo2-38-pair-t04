"""Combustion air train.

Air has to be moving before any fuel is admitted: the train ramps its duct
pressure over a spin-up period and only then satisfies the
``burner.air_established`` gate that the gas train waits on.
"""

from __future__ import annotations

from typing import Any

from kilnline.errors import InterlockActive, StateConflict, ValidationError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry

AIR_ESTABLISHED_GATE = "burner.air_established"
AIR_LATCH = "burner.air"


class CombustionAirTrain:
    """Deterministic first order pressure ramp for the combustion air fan."""

    def __init__(
        self,
        gates: PreGateRegistry,
        latches: LatchRegistry,
        *,
        min_pressure_kpa: float,
        spin_up_s: float,
        max_pressure_kpa: float = 8.0,
        gate_name: str = AIR_ESTABLISHED_GATE,
        latch_name: str = AIR_LATCH,
    ) -> None:
        if float(min_pressure_kpa) <= 0.0:
            raise ValidationError("air pressure threshold must be positive")
        if float(spin_up_s) <= 0.0:
            raise ValidationError("air spin-up period must be positive")
        if float(max_pressure_kpa) < float(min_pressure_kpa):
            raise ValidationError("air maximum pressure must reach the threshold")
        self._min_pressure = float(min_pressure_kpa)
        self._max_pressure = float(max_pressure_kpa)
        self._ramp_kpa_per_s = self._max_pressure / float(spin_up_s)
        self._gates = gates
        self._latches = latches
        self._gate_name = str(gate_name)
        self._latch_name = str(latch_name)
        if self._gate_name not in gates.names():
            gates.declare(self._gate_name, description="combustion air pressure established")
        if self._latch_name not in latches.snapshot():
            latches.declare(self._latch_name, description="combustion air train faulted")
        self._running = False
        self._started_at: float | None = None
        self._pressure_kpa = 0.0

    @property
    def gate_name(self) -> str:
        return self._gate_name

    @property
    def latch_name(self) -> str:
        return self._latch_name

    @property
    def min_pressure_kpa(self) -> float:
        return self._min_pressure

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pressure_kpa(self) -> float:
        return self._pressure_kpa

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def established(self) -> bool:
        return self._running and self._pressure_kpa >= self._min_pressure

    def start(self, *, at: float) -> dict[str, Any]:
        self._latches.require_clear(self._latch_name)
        if self._running:
            raise StateConflict("combustion air train is already running", at=float(at))
        self._running = True
        self._started_at = float(at)
        self._pressure_kpa = 0.0
        return self.snapshot()

    def stop(self, *, at: float) -> dict[str, Any]:
        self._running = False
        self._pressure_kpa = 0.0
        self._gates.unsatisfy(self._gate_name, at=at, detail="air stopped")
        return self.snapshot()

    def fault(self, reason: str, *, at: float) -> dict[str, Any]:
        self._latches.trip(self._latch_name, reason, at=at)
        self._running = False
        self._pressure_kpa = 0.0
        self._gates.unsatisfy(self._gate_name, at=at, detail=reason)
        return self.snapshot()

    def advance(self, *, dt: float, at: float) -> float:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("air advance step must be positive", dt=dt)
        if not self._running:
            return self._pressure_kpa
        self._pressure_kpa = min(self._max_pressure, self._pressure_kpa + self._ramp_kpa_per_s * step)
        if self.established and not self._gates.satisfies(self._gate_name):
            self._gates.satisfy(
                self._gate_name,
                at=at,
                detail=f"pressure {self._pressure_kpa:.2f} kPa",
            )
        return self._pressure_kpa

    def require_established(self) -> None:
        if not self.established:
            raise InterlockActive(
                "combustion air is not established",
                gate=self._gate_name,
                pressure_kpa=self._pressure_kpa,
                required_kpa=self._min_pressure,
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "pressure_kpa": self._pressure_kpa,
            "min_pressure_kpa": self._min_pressure,
            "established": self.established,
            "started_at": self._started_at,
            "gate": self._gate_name,
        }
