"""Ignition, shut-down and recovery ordering.

The design rule is short: combustion air first, fuel second, igniter third,
and the zone map is only trusted once the flame has been proved.  Shut-down
runs the mirror image -- fuel off before air off -- so unburnt gas is never
left in a hot duct.  Recovery adds a fourth rule: the alarm has to be reset
before the flame latch may even be asked to release.
"""

from __future__ import annotations

from typing import Any

from kilnline.burner.air import CombustionAirTrain
from kilnline.burner.gas import GasTrain
from kilnline.errors import OrderingViolation, StateConflict
from kilnline.interlock.latch import LatchRegistry
from kilnline.interlock.sequencer import Stage, StageSequencer

FLAME_LATCH = "burner.flame"

IGNITION_STAGES: tuple[Stage, ...] = (
    Stage("air_established", description="combustion air pressure established"),
    Stage("gas_open", ("air_established",), description="fuel admitted"),
    Stage("igniter_spark", ("gas_open",), description="igniter energised"),
    Stage("flame_confirmed", ("igniter_spark",), description="flame proof positive"),
    Stage("zone_map_refreshed", ("flame_confirmed",), description="zone map mapped onto the flame"),
)


class IgnitionSequence:
    """Drives the burner through its start, stop and recovery order."""

    def __init__(
        self,
        air: CombustionAirTrain,
        gas: GasTrain,
        latches: LatchRegistry,
        *,
        flame_proof_s: float = 5.0,
        latch_name: str = FLAME_LATCH,
    ) -> None:
        self._air = air
        self._gas = gas
        self._latches = latches
        self._latch_name = str(latch_name)
        if self._latch_name not in latches.snapshot():
            latches.declare(self._latch_name, description="flame proof lost")
        self._flame_proof_s = float(flame_proof_s)
        self.sequence = StageSequencer("ignition", IGNITION_STAGES)
        self._flame_proven = False
        self._lit = False

    @property
    def latch_name(self) -> str:
        return self._latch_name

    @property
    def flame_proof_s(self) -> float:
        return self._flame_proof_s

    @property
    def lit(self) -> bool:
        return self._lit

    def ignite(self, *, at: float) -> dict[str, Any]:
        self._latches.require_clear(self._latch_name)
        if self._lit:
            raise StateConflict("burner is already lit", at=float(at))
        if not self._air.established:
            raise OrderingViolation(
                "combustion air must be established before fuel is admitted",
                sequencer=self.sequence.name,
                stage="air_established",
                missing=["air_established"],
                completed=self.sequence.completed(),
            )
        if not self.sequence.is_complete("air_established"):
            self.sequence.complete("air_established", at=at)
        self._gas.open(at=at)
        self.sequence.complete("gas_open", at=at)
        self.sequence.complete("igniter_spark", at=at)
        self._flame_proven = True
        self._lit = True
        self.sequence.complete("flame_confirmed", at=at)
        return {
            "steps": self.sequence.completed(),
            "next": self.sequence.next_stage(),
            "air": self._air.snapshot(),
            "gas": self._gas.snapshot(),
        }

    def mark_zone_map_refreshed(self, *, at: float) -> dict[str, Any]:
        self._latches.require_clear(self._latch_name)
        self.sequence.complete("zone_map_refreshed", at=at)
        return {"steps": ["zone_map_refreshed"], "complete": True}

    def extinguish(self, *, at: float, reason: str = "requested") -> dict[str, Any]:
        if not self._gas.is_open:
            raise StateConflict("burner is not lit", reason=reason)
        self._gas.close(at=at)
        self._air.stop(at=at)
        self._flame_proven = False
        self._lit = False
        if self.sequence.completed():
            self.sequence.reset(at=at, reason=reason)
        return {"steps": ["gas_closed", "air_stopped"], "air": self._air.snapshot(), "gas": self._gas.snapshot()}

    def flame_lost(self, *, at: float, reason: str = "flame_proof_lost") -> dict[str, Any]:
        """Trip the flame latch and cut fuel; air stays on to purge the duct."""

        self._flame_proven = False
        self._lit = False
        self._gas.fault(reason, at=at)
        state = self._latches.trip(self._latch_name, reason, at=at)
        if self.sequence.completed():
            self.sequence.reset(at=at, reason=reason)
        return {
            "latch": state.as_dict(),
            "gas": self._gas.snapshot(),
            "air": self._air.snapshot(),
            "reason": str(reason),
        }

    def recover(self, *, at: float, now: float, alarm_reset: bool) -> dict[str, Any]:
        """Release the flame latch, but only after the alarm was reset."""

        if not alarm_reset:
            raise OrderingViolation(
                "the flame-loss alarm must be reset before the latch can be released",
                sequencer="recovery",
                stage="alarm_reset",
                missing=["alarm_reset"],
            )
        self._lit = False
        self._flame_proven = False
        if not self._latches.is_tripped(self._latch_name):
            return {
                "latch": self._latches.state(self._latch_name).as_dict(),
                "released": False,
                "conditions_ok": self._release_conditions_ok(),
                "hold_remaining_s": 0.0,
                "reason": "not_tripped",
            }
        self._latches.request_reset(self._latch_name, at=at)
        gas_latch_tripped = self._latches.is_tripped(self._gas.latch_name)
        if gas_latch_tripped:
            self._latches.request_reset(self._gas.latch_name, at=at)
        conditions_ok = self._release_conditions_ok()
        state = self._latches.evaluate(self._latch_name, now=now, conditions_ok=conditions_ok)
        gas_state = self._latches.evaluate(self._gas.latch_name, now=now, conditions_ok=conditions_ok)
        released = not state.tripped
        return {
            "latch": state.as_dict(),
            "gas_latch": gas_state.as_dict(),
            "released": released,
            "conditions_ok": conditions_ok,
            "hold_remaining_s": state.hold_remaining(now),
            "reason": "released" if released else "holding",
        }

    def _release_conditions_ok(self) -> bool:
        """Fuel cut and purge air still moving before the latch may release."""

        return not self._gas.is_open and self._air.running

    def ready(self) -> bool:
        return self._lit and not self.sequence.pending()

    def progress(self) -> dict[str, Any]:
        return self.sequence.progress()

    def snapshot(self) -> dict[str, Any]:
        return {
            "lit": self.lit,
            "flame_proven": self._flame_proven,
            "latch": self._latches.state(self._latch_name).as_dict(),
            "air": self._air.snapshot(),
            "gas": self._gas.snapshot(),
            "sequence": self.sequence.progress(),
        }
