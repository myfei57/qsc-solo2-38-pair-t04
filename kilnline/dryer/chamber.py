"""Tunnel dryer.

Carriers are loaded with a moisture content and dry at a fixed rate while the
bank advances, so drying is reproducible without a real clock.  A carrier may
only be declared dry once its moisture has fallen to target *and* the minimum
residence time has elapsed; the second rule is what stops a car that was only
just loaded from being treated as ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import (
    DuplicateRecord,
    NotFoundError,
    OrderingViolation,
    StateConflict,
    ThresholdExceeded,
    ValidationError,
)
from kilnline.interlock.gate import PreGateRegistry

DRYER_COMPLETE_GATE = "dryer.complete"

STATE_DRYING = "drying"
STATE_DONE = "done"
STATE_UNLOADED = "unloaded"


@dataclass(frozen=True)
class DryingRun:
    """One carrier inside the dryer."""

    car_id: str
    state: str
    loaded_at: float
    elapsed_s: float
    moisture_pct: float
    target_moisture_pct: float
    completed_at: float | None

    @property
    def dry(self) -> bool:
        return self.state == STATE_DONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "car_id": self.car_id,
            "state": self.state,
            "loaded_at": self.loaded_at,
            "elapsed_s": self.elapsed_s,
            "moisture_pct": self.moisture_pct,
            "target_moisture_pct": self.target_moisture_pct,
            "completed_at": self.completed_at,
            "dry": self.dry,
        }


class DryerBank:
    """Deterministic moisture model plus the completion gate."""

    def __init__(
        self,
        gates: PreGateRegistry,
        *,
        target_moisture_pct: float,
        rate_pct_per_s: float,
        min_duration_s: float,
        gate_name: str = DRYER_COMPLETE_GATE,
    ) -> None:
        if float(rate_pct_per_s) <= 0.0:
            raise ValidationError("dryer rate must be positive")
        if float(target_moisture_pct) < 0.0:
            raise ValidationError("dryer target moisture must not be negative")
        if float(min_duration_s) < 0.0:
            raise ValidationError("dryer minimum duration must not be negative")
        self._gates = gates
        self._gate_name = str(gate_name)
        if self._gate_name not in gates.names():
            gates.declare(self._gate_name, description="at least one carrier is dry")
        self._target = float(target_moisture_pct)
        self._rate = float(rate_pct_per_s)
        self._min_duration = float(min_duration_s)
        self._runs: dict[str, dict[str, Any]] = {}

    @property
    def gate_name(self) -> str:
        return self._gate_name

    @property
    def target_moisture_pct(self) -> float:
        return self._target

    @property
    def min_duration_s(self) -> float:
        return self._min_duration

    def load(self, car_id: str, *, at: float, initial_moisture_pct: float = 6.0) -> DryingRun:
        label = self._label(car_id)
        if label in self._runs:
            raise DuplicateRecord("carrier is already in the dryer", car_id=label)
        moisture = float(initial_moisture_pct)
        if moisture <= self._target:
            raise ValidationError(
                "carrier cannot be loaded already dry",
                car_id=label,
                moisture_pct=moisture,
                target_moisture_pct=self._target,
            )
        self._runs[label] = {
            "car_id": label,
            "state": STATE_DRYING,
            "loaded_at": float(at),
            "elapsed_s": 0.0,
            "moisture_pct": moisture,
            "completed_at": None,
        }
        return self.run(label)

    def advance(self, *, dt: float) -> list[DryingRun]:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("dryer advance step must be positive", dt=dt)
        for run in self._runs.values():
            if run["state"] != STATE_DRYING:
                continue
            run["elapsed_s"] = float(run["elapsed_s"]) + step
            run["moisture_pct"] = max(0.0, float(run["moisture_pct"]) - self._rate * step)
        return self.active()

    def complete(self, car_id: str, *, at: float) -> DryingRun:
        label = self._label(car_id)
        run = self._runs.get(label)
        if run is None:
            raise NotFoundError("unknown carrier in the dryer", car_id=label)
        if run["state"] == STATE_DONE:
            raise StateConflict("carrier is already dry", car_id=label)
        if run["state"] != STATE_DRYING:
            raise StateConflict("carrier is not drying", car_id=label, state=run["state"])
        if float(run["elapsed_s"]) < self._min_duration:
            raise OrderingViolation(
                "carrier has not stayed in the dryer long enough",
                car_id=label,
                elapsed_s=run["elapsed_s"],
                required_s=self._min_duration,
                stage="dwell",
            )
        moisture = float(run["moisture_pct"])
        if moisture > self._target:
            raise ThresholdExceeded(
                "carrier moisture is still above target",
                car_id=label,
                moisture_pct=moisture,
                target_moisture_pct=self._target,
            )
        run["state"] = STATE_DONE
        run["completed_at"] = float(at)
        self._gates.satisfy(self._gate_name, at=at, detail=f"carrier {label} dry")
        return self.run(label)

    def unload(self, car_id: str, *, at: float) -> DryingRun:
        label = self._label(car_id)
        run = self._runs.get(label)
        if run is None:
            raise NotFoundError("unknown carrier in the dryer", car_id=label)
        if run["state"] != STATE_DONE:
            raise StateConflict("carrier is not dry yet", car_id=label, state=run["state"])
        run["state"] = STATE_UNLOADED
        run["completed_at"] = float(at)
        if not self.active():
            self._gates.unsatisfy(self._gate_name, at=at, detail="dryer empty")
        return self.run(label)

    def run(self, car_id: str) -> DryingRun:
        label = self._label(car_id)
        run = self._runs.get(label)
        if run is None:
            raise NotFoundError("unknown carrier in the dryer", car_id=label)
        return DryingRun(
            car_id=run["car_id"],
            state=run["state"],
            loaded_at=float(run["loaded_at"]),
            elapsed_s=float(run["elapsed_s"]),
            moisture_pct=float(run["moisture_pct"]),
            target_moisture_pct=self._target,
            completed_at=None if run["completed_at"] is None else float(run["completed_at"]),
        )

    def require_dry(self, car_id: str) -> DryingRun:
        run = self.run(car_id)
        if not run.dry:
            raise OrderingViolation(
                "carrier must finish drying before the next wet process",
                car_id=run.car_id,
                state=run.state,
                moisture_pct=run.moisture_pct,
                stage="dry",
            )
        return run

    def active(self) -> list[DryingRun]:
        return sorted(
            (self.run(key) for key, run in self._runs.items() if run["state"] == STATE_DRYING),
            key=lambda item: item.car_id,
        )

    def completed(self) -> list[DryingRun]:
        return sorted(
            (self.run(key) for key, run in self._runs.items() if run["state"] == STATE_DONE),
            key=lambda item: item.car_id,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "gate": self._gate_name,
            "target_moisture_pct": self._target,
            "rate_pct_per_s": self._rate,
            "min_duration_s": self._min_duration,
            "runs": [self.run(key).as_dict() for key in sorted(self._runs)],
        }

    @staticmethod
    def _label(car_id: str) -> str:
        label = str(car_id).strip()
        if not label:
            raise ValidationError("carrier id must not be empty")
        return label
