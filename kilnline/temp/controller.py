"""Ramp-limited zone controller with a bounded integral term.

The controller is deliberately simple and fully deterministic: it walks the
ramp-limited setpoint toward the requested target, integrates only while the
error is near the band (so a long cold start cannot wind the integral up), and
clamps the output.  Feed-forward from the curve keeps the power demand stable
through the soak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import ValidationError
from kilnline.temp.curve import VERDICT_ABOVE, VERDICT_BELOW, VERDICT_WITHIN, compare_window


@dataclass(frozen=True)
class ControlOutput:
    """One control step, fully described for the audit trail."""

    target_c: float
    ramp_target_c: float
    measured_c: float
    error_c: float
    heater_power: float
    integral: float
    band_state: str
    at_target: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_c": self.target_c,
            "ramp_target_c": self.ramp_target_c,
            "measured_c": self.measured_c,
            "error_c": self.error_c,
            "heater_power": self.heater_power,
            "integral": self.integral,
            "band_state": self.band_state,
            "at_target": self.at_target,
        }


class TemperatureController:
    """Deterministic single-zone controller."""

    def __init__(
        self,
        *,
        band_c: float,
        ramp_rate_c_per_min: float,
        gain: float = 0.4,
        integral_gain: float = 0.002,
        integral_limit: float = 100.0,
        max_power: float = 1.0,
    ) -> None:
        if float(band_c) <= 0.0:
            raise ValidationError("control band must be positive", band_c=band_c)
        if float(ramp_rate_c_per_min) <= 0.0:
            raise ValidationError("ramp rate must be positive", ramp_rate_c_per_min=ramp_rate_c_per_min)
        if float(max_power) <= 0.0:
            raise ValidationError("max power must be positive", max_power=max_power)
        self._band_c = float(band_c)
        self._ramp_c_per_s = float(ramp_rate_c_per_min) / 60.0
        self._gain = float(gain)
        self._integral_gain = float(integral_gain)
        self._integral_limit = abs(float(integral_limit))
        self._max_power = float(max_power)
        self._target_c = 0.0
        self._ramp_target_c: float | None = None
        self._integral = 0.0
        self._steps = 0
        self._saturated_steps = 0

    @property
    def target_c(self) -> float:
        return self._target_c

    @property
    def ramp_target_c(self) -> float:
        return self._target_c if self._ramp_target_c is None else self._ramp_target_c

    @property
    def at_setpoint(self) -> bool:
        """True once the ramp-limited setpoint has reached the commanded target."""

        return abs(self.ramp_target_c - self._target_c) <= self._band_c

    @property
    def integral(self) -> float:
        return self._integral

    @property
    def band_c(self) -> float:
        return self._band_c

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def saturated_steps(self) -> int:
        return self._saturated_steps

    def set_target(self, target_c: float) -> float:
        value = float(target_c)
        if value != value:
            raise ValidationError("controller target must be a number", target_c=target_c)
        self._target_c = value
        return self._target_c

    def reset(self) -> None:
        self._ramp_target_c = None
        self._integral = 0.0
        self._steps = 0
        self._saturated_steps = 0

    def update(self, measured_c: float, *, dt: float) -> ControlOutput:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("control step must be positive", dt=dt)
        measured = float(measured_c)
        if self._ramp_target_c is None:
            self._ramp_target_c = measured
        budget = self._ramp_c_per_s * step
        delta = self._target_c - self._ramp_target_c
        if abs(delta) <= budget:
            self._ramp_target_c = self._target_c
        else:
            self._ramp_target_c += budget * (1.0 if delta > 0 else -1.0)
        error = self._ramp_target_c - measured
        pre_power = self._gain * error + self._integral_gain * self._integral
        saturated = pre_power > self._max_power or pre_power < 0.0
        if not saturated and abs(error) <= self._band_c * 4.0:
            self._integral = _clamp(self._integral + error * step, -self._integral_limit, self._integral_limit)
        power = _clamp(self._gain * error + self._integral_gain * self._integral, 0.0, self._max_power)
        verdict = compare_window(
            measured,
            low=self._ramp_target_c - self._band_c,
            high=self._ramp_target_c + self._band_c,
        )
        self._steps += 1
        if saturated:
            self._saturated_steps += 1
        return ControlOutput(
            target_c=self._target_c,
            ramp_target_c=self._ramp_target_c,
            measured_c=measured,
            error_c=error,
            heater_power=power,
            integral=self._integral,
            band_state=verdict.verdict,
            at_target=verdict.verdict == VERDICT_WITHIN,
        )

    def in_band(self, measured_c: float) -> bool:
        verdict = compare_window(
            float(measured_c),
            low=self.ramp_target_c - self._band_c,
            high=self.ramp_target_c + self._band_c,
        )
        return verdict.verdict == VERDICT_WITHIN

    def snapshot(self) -> dict[str, Any]:
        return {
            "target_c": self._target_c,
            "ramp_target_c": self.ramp_target_c,
            "band_c": self._band_c,
            "integral": self._integral,
            "steps": self._steps,
            "saturated_steps": self._saturated_steps,
            "ramp_rate_c_per_min": self._ramp_c_per_s * 60.0,
        }

    @staticmethod
    def classify(error: float, *, band: float) -> str:
        if error > float(band):
            return VERDICT_BELOW
        if error < -float(band):
            return VERDICT_ABOVE
        return VERDICT_WITHIN


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
