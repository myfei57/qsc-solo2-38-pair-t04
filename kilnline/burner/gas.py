"""Gas valve train.

Opening the train is gated on established combustion air.  The gate is read
from the shared registry rather than passed in, so the ordering rule holds
even when the train is driven directly from the console.
"""

from __future__ import annotations

from typing import Any

from kilnline.burner.air import AIR_ESTABLISHED_GATE
from kilnline.errors import StateConflict, ValidationError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry

GAS_OPEN_GATE = "burner.gas_open"
GAS_LATCH = "burner.gas"


class GasTrain:
    """Ramped fuel flow behind the air interlock."""

    def __init__(
        self,
        gates: PreGateRegistry,
        latches: LatchRegistry,
        *,
        settle_s: float,
        nominal_flow_nm3h: float = 120.0,
        air_gate: str = AIR_ESTABLISHED_GATE,
        gate_name: str = GAS_OPEN_GATE,
        latch_name: str = GAS_LATCH,
    ) -> None:
        if float(settle_s) <= 0.0:
            raise ValidationError("gas settle period must be positive")
        if float(nominal_flow_nm3h) <= 0.0:
            raise ValidationError("nominal gas flow must be positive")
        self._settle_s = float(settle_s)
        self._nominal_flow = float(nominal_flow_nm3h)
        self._flow_per_s = self._nominal_flow / self._settle_s
        self._gates = gates
        self._latches = latches
        self._air_gate = str(air_gate)
        self._gate_name = str(gate_name)
        self._latch_name = str(latch_name)
        if self._gate_name not in gates.names():
            gates.declare(self._gate_name, description="gas valve train open")
        if self._latch_name not in latches.snapshot():
            latches.declare(self._latch_name, description="gas valve train faulted")
        self._is_open = False
        self._opened_at: float | None = None
        self._flow_nm3h = 0.0

    @property
    def gate_name(self) -> str:
        return self._gate_name

    @property
    def latch_name(self) -> str:
        return self._latch_name

    @property
    def air_gate(self) -> str:
        return self._air_gate

    @property
    def settle_s(self) -> float:
        return self._settle_s

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def opened_at(self) -> float | None:
        return self._opened_at

    @property
    def flow_nm3h(self) -> float:
        return self._flow_nm3h

    @property
    def settled(self) -> bool:
        return self._is_open and self._flow_nm3h >= self._nominal_flow

    def open(self, *, at: float) -> dict[str, Any]:
        self._latches.require_clear(self._latch_name)
        self._gates.require(self._air_gate)
        if self._is_open:
            raise StateConflict("gas valve train is already open", at=float(at))
        self._is_open = True
        self._opened_at = float(at)
        self._flow_nm3h = 0.0
        return self.snapshot()

    def close(self, *, at: float) -> dict[str, Any]:
        if not self._is_open:
            raise StateConflict("gas valve train is already closed", at=float(at))
        self._is_open = False
        self._opened_at = None
        self._flow_nm3h = 0.0
        self._gates.unsatisfy(self._gate_name, at=at, detail="gas closed")
        return self.snapshot()

    def fault(self, reason: str, *, at: float) -> dict[str, Any]:
        self._latches.trip(self._latch_name, reason, at=at)
        self._is_open = False
        self._opened_at = None
        self._flow_nm3h = 0.0
        self._gates.unsatisfy(self._gate_name, at=at, detail=reason)
        return self.snapshot()

    def advance(self, *, dt: float, at: float) -> float:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("gas advance step must be positive", dt=dt)
        if not self._is_open:
            return self._flow_nm3h
        self._flow_nm3h = min(self._nominal_flow, self._flow_nm3h + self._flow_per_s * step)
        if self.settled and not self._gates.satisfies(self._gate_name):
            self._gates.satisfy(
                self._gate_name,
                at=at,
                detail=f"flow {self._flow_nm3h:.1f} Nm3/h",
            )
        return self._flow_nm3h

    def snapshot(self) -> dict[str, Any]:
        return {
            "open": self._is_open,
            "flow_nm3h": self._flow_nm3h,
            "nominal_flow_nm3h": self._nominal_flow,
            "settled": self.settled,
            "opened_at": self._opened_at,
            "air_gate": self._air_gate,
            "gate": self._gate_name,
        }
