"""Glaze station.

Coating a carrier that has not finished drying traps moisture under the glaze
layer, so the station reads the dryer state before it will accept a carrier.
It also refuses to coat against a superseded calibration or an out-of-band
density, which is the second half of the "calibrate before you use it" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.dryer.chamber import DryerBank
from kilnline.errors import NotFoundError, StateConflict, ValidationError
from kilnline.glaze.slurry import SlurryStation
from kilnline.interlock.gate import PreGateRegistry

GATE_GLAZE_READY = "glaze.ready"

STATE_COATED = "coated"
STATE_FINISHED = "finished"


@dataclass(frozen=True)
class CoatingRun:
    car_id: str
    state: str
    started_at: float
    finished_at: float | None
    density_g_cm3: float
    parameter_generation: int
    reference_generation: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "car_id": self.car_id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "density_g_cm3": self.density_g_cm3,
            "parameter_generation": self.parameter_generation,
            "reference_generation": self.reference_generation,
        }


class GlazeStation:
    """Applies glaze to carriers that left the dryer dry."""

    def __init__(
        self,
        gates: PreGateRegistry,
        dryer: DryerBank,
        slurry: SlurryStation,
        *,
        gate_name: str = GATE_GLAZE_READY,
    ) -> None:
        self._gates = gates
        self._dryer = dryer
        self._slurry = slurry
        self._gate_name = str(gate_name)
        if self._gate_name not in gates.names():
            gates.declare(self._gate_name, description="slurry calibrated and carrier dry")
        self._runs: dict[str, dict[str, Any]] = {}

    @property
    def gate_name(self) -> str:
        return self._gate_name

    def start(
        self,
        car_id: str,
        *,
        at: float,
        now: float,
        density_g_cm3: float,
        parameter_generation: int,
    ) -> CoatingRun:
        label = self._label(car_id)
        if label in self._runs and self._runs[label]["state"] == STATE_COATED:
            raise StateConflict("carrier is already on the glaze line", car_id=label)
        self._dryer.require_dry(label)
        self._gates.require(self._dryer.gate_name)
        baseline, _ = self._slurry.require_usable(
            density_g_cm3=density_g_cm3,
            now=now,
            generation=int(parameter_generation),
        )
        self._runs[label] = {
            "car_id": label,
            "state": STATE_COATED,
            "started_at": float(at),
            "finished_at": None,
            "density_g_cm3": float(density_g_cm3),
            "parameter_generation": int(parameter_generation),
            "reference_generation": baseline.generation,
        }
        self._gates.satisfy(self._gate_name, at=at, detail=f"carrier {label} coated")
        return self.run(label)

    def finish(self, car_id: str, *, at: float) -> CoatingRun:
        label = self._label(car_id)
        run = self._runs.get(label)
        if run is None:
            raise NotFoundError("carrier is not on the glaze line", car_id=label)
        if run["state"] != STATE_COATED:
            raise StateConflict("carrier coating is already finished", car_id=label)
        run["state"] = STATE_FINISHED
        run["finished_at"] = float(at)
        return self.run(label)

    def run(self, car_id: str) -> CoatingRun:
        label = self._label(car_id)
        run = self._runs.get(label)
        if run is None:
            raise NotFoundError("carrier is not on the glaze line", car_id=label)
        return CoatingRun(
            car_id=run["car_id"],
            state=run["state"],
            started_at=float(run["started_at"]),
            finished_at=None if run["finished_at"] is None else float(run["finished_at"]),
            density_g_cm3=float(run["density_g_cm3"]),
            parameter_generation=int(run["parameter_generation"]),
            reference_generation=int(run["reference_generation"]),
        )

    def coated(self) -> list[CoatingRun]:
        return sorted(
            (self.run(key) for key, run in self._runs.items() if run["state"] == STATE_COATED),
            key=lambda item: item.car_id,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "gate": self._gate_name,
            "runs": [self.run(key).as_dict() for key in sorted(self._runs)],
        }

    @staticmethod
    def _label(car_id: str) -> str:
        label = str(car_id).strip()
        if not label:
            raise ValidationError("carrier id must not be empty")
        return label
