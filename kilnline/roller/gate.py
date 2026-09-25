"""Roller start gate.

Starting the roller table on an unflushed temperature baseline is the classic
way to drag material through a kiln whose setpoint nobody trusts.  The gate
therefore refuses the start when the baseline was never persisted (durability)
or has aged out (expiry), and it names the single missing item in the error
context so the console can show it verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from kilnline.errors import DurabilityError, GenerationExpired, NotFoundError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.params.baseline import Baseline
from kilnline.roller.calibration import SpeedCalibration

GATE_TEMP_PERSISTED = "kiln.temp_persisted"
GATE_ROLLER_CALIBRATED = "roller.calibrated"


@dataclass(frozen=True)
class Precondition:
    """One checked precondition and how it was evaluated."""

    name: str
    satisfied: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "satisfied": self.satisfied, "detail": self.detail}


class RollerStartGate:
    """Evaluates everything the roller table needs before it may turn."""

    def __init__(
        self,
        gates: PreGateRegistry,
        latches: LatchRegistry,
        *,
        temp_gate: str = GATE_TEMP_PERSISTED,
        calibration_gate: str = GATE_ROLLER_CALIBRATED,
        blocking_latches: Sequence[str] = (),
    ) -> None:
        self._gates = gates
        self._latches = latches
        self._temp_gate = str(temp_gate)
        self._calibration_gate = str(calibration_gate)
        self._blocking_latches = tuple(str(name) for name in blocking_latches)
        for name, description in (
            (self._temp_gate, "temperature baseline written to disk"),
            (self._calibration_gate, "roller speed calibration current"),
        ):
            if name not in gates.names():
                gates.declare(name, description=description)

    @property
    def temp_gate(self) -> str:
        return self._temp_gate

    @property
    def calibration_gate(self) -> str:
        return self._calibration_gate

    @property
    def blocking_latches(self) -> tuple[str, ...]:
        return self._blocking_latches

    def mark_temp_persisted(self, *, at: float, detail: str = "") -> Precondition:
        gate = self._gates.satisfy(self._temp_gate, at=at, detail=detail)
        return Precondition(gate.name, gate.satisfied, gate.detail)

    def mark_calibrated(self, *, at: float, detail: str = "") -> Precondition:
        gate = self._gates.satisfy(self._calibration_gate, at=at, detail=detail)
        return Precondition(gate.name, gate.satisfied, gate.detail)

    def preconditions(
        self,
        *,
        temp_baseline: Baseline | None,
        calibration: SpeedCalibration | None,
        now: float,
    ) -> list[Precondition]:
        checks: list[Precondition] = []
        if temp_baseline is None:
            checks.append(
                Precondition(self._temp_gate, False, "no temperature baseline has been persisted")
            )
        else:
            checks.append(
                Precondition(
                    self._temp_gate,
                    not temp_baseline.expired(now),
                    f"generation {temp_baseline.generation}, age {temp_baseline.age_seconds(now):.1f}s",
                )
            )
        if calibration is None:
            checks.append(
                Precondition(self._calibration_gate, False, "no roller speed calibration recorded")
            )
        else:
            checks.append(
                Precondition(
                    self._calibration_gate,
                    not calibration.expired(now),
                    f"generation {calibration.generation}, age {calibration.age_seconds(now):.1f}s",
                )
            )
        for name in self._blocking_latches:
            state = self._latches.state(name)
            checks.append(Precondition(name, not state.tripped, state.reason or "clear"))
        return checks

    def require(
        self,
        *,
        temp_baseline: Baseline | None,
        calibration: SpeedCalibration | None,
        now: float,
    ) -> list[Precondition]:
        if temp_baseline is None:
            raise DurabilityError(
                "temperature baseline was never persisted; roller start refused",
                gate=self._temp_gate,
            )
        if temp_baseline.expired(now):
            raise GenerationExpired(
                "temperature baseline expired; roller start refused",
                gate=self._temp_gate,
                age_s=temp_baseline.age_seconds(now),
                max_lag_s=temp_baseline.max_lag_s,
            )
        if calibration is None:
            raise NotFoundError("no roller speed calibration recorded", gate=self._calibration_gate)
        if calibration.expired(now):
            raise GenerationExpired(
                "roller speed calibration expired; roller start refused",
                gate=self._calibration_gate,
                age_s=calibration.age_seconds(now),
                max_lag_s=calibration.max_lag_s,
            )
        self._gates.require_all((self._temp_gate, self._calibration_gate))
        for name in self._blocking_latches:
            self._latches.require_clear(name)
        return self.preconditions(temp_baseline=temp_baseline, calibration=calibration, now=now)
