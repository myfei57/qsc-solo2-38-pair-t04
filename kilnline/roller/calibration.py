"""Roller speed calibration stored as a generation-stamped baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import ValidationError
from kilnline.params.baseline import Baseline, BaselineRegistry

SPEED_BASELINE_KEY = "roller.speed"
PULSES_PER_METER = "pulses_per_meter"


@dataclass(frozen=True)
class SpeedCalibration:
    """Pulse rate per metre, valid for one generation and a bounded age."""

    generation: int
    pulses_per_meter: float
    captured_at: float
    author: str
    max_lag_s: float
    digest: str

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.captured_at)

    def expired(self, now: float) -> bool:
        return self.age_seconds(now) > self.max_lag_s

    def speed_mpm(self, pulses_per_s: float) -> float:
        return float(pulses_per_s) / self.pulses_per_meter * 60.0

    def pulses_per_s(self, speed_mpm: float) -> float:
        return float(speed_mpm) / 60.0 * self.pulses_per_meter

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation),
            "pulses_per_meter": self.pulses_per_meter,
            "captured_at": float(self.captured_at),
            "author": self.author,
            "max_lag_s": float(self.max_lag_s),
            "digest": self.digest,
        }

    @classmethod
    def from_baseline(cls, baseline: Baseline) -> "SpeedCalibration":
        return cls(
            generation=baseline.generation,
            pulses_per_meter=baseline.value(PULSES_PER_METER),
            captured_at=baseline.captured_at,
            author=baseline.author,
            max_lag_s=baseline.max_lag_s,
            digest=baseline.digest,
        )


class SpeedCalibrationRegistry:
    """Thin, typed view over the shared baseline registry."""

    def __init__(self, baselines: BaselineRegistry, *, key: str = SPEED_BASELINE_KEY) -> None:
        self._baselines = baselines
        self._key = str(key)

    @property
    def key(self) -> str:
        return self._key

    def capture(
        self,
        *,
        pulses_per_meter: float,
        at: float,
        generation: int,
        author: str,
        max_lag_s: float,
    ) -> SpeedCalibration:
        value = float(pulses_per_meter)
        if value <= 0.0:
            raise ValidationError("pulses per metre must be positive", pulses_per_meter=pulses_per_meter)
        baseline = self._baselines.capture(
            self._key,
            {PULSES_PER_METER: value},
            captured_at=at,
            generation=generation,
            author=author,
            max_lag_s=max_lag_s,
            reason="speed_calibration",
        )
        return SpeedCalibration.from_baseline(baseline)

    def latest(self) -> SpeedCalibration | None:
        baseline = self._baselines.latest(self._key)
        return None if baseline is None else SpeedCalibration.from_baseline(baseline)

    def require_fresh(self, *, now: float) -> SpeedCalibration:
        return SpeedCalibration.from_baseline(self._baselines.require_fresh(self._key, now=now))

    def require_current(self, *, now: float, generation: int) -> SpeedCalibration:
        baseline = self._baselines.require_current(self._key, now=now, generation=generation)
        return SpeedCalibration.from_baseline(baseline)

    def measure_speed_mpm(self, pulses_per_s: float, *, now: float) -> float:
        return self.require_fresh(now=now).speed_mpm(pulses_per_s)
