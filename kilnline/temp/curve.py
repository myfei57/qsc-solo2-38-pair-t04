"""Firing curve and window comparisons.

The curve is a piecewise linear target over elapsed time, bounded by a ramp
rate so a setpoint jump cannot be followed instantly.  Window comparisons are
the single place where "is this value acceptable" is decided, so the same rule
is applied to live readings, baselines and historical snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from kilnline.errors import ValidationError

VERDICT_BELOW = "below"
VERDICT_WITHIN = "within"
VERDICT_ABOVE = "above"


@dataclass(frozen=True)
class WindowVerdict:
    """Result of comparing one value against a low/high window."""

    value: float
    low: float
    high: float
    verdict: str
    deviation: float

    @property
    def within(self) -> bool:
        return self.verdict == VERDICT_WITHIN

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "low": self.low,
            "high": self.high,
            "verdict": self.verdict,
            "deviation": self.deviation,
        }


def compare_window(value: float, *, low: float, high: float) -> WindowVerdict:
    if float(low) > float(high):
        raise ValidationError("window low bound must not exceed the high bound", low=low, high=high)
    measured = float(value)
    if measured < float(low):
        return WindowVerdict(measured, float(low), float(high), VERDICT_BELOW, float(low) - measured)
    if measured > float(high):
        return WindowVerdict(measured, float(low), float(high), VERDICT_ABOVE, measured - float(high))
    return WindowVerdict(measured, float(low), float(high), VERDICT_WITHIN, 0.0)


def within_band(value: float, *, target: float, band: float) -> bool:
    return compare_window(value, low=float(target) - float(band), high=float(target) + float(band)).within


def deviation(value: float, *, target: float, band: float) -> float:
    """Signed distance outside the band; zero while inside it."""

    verdict = compare_window(value, low=float(target) - float(band), high=float(target) + float(band))
    if verdict.verdict == VERDICT_BELOW:
        return verdict.deviation
    if verdict.verdict == VERDICT_ABOVE:
        return -verdict.deviation
    return 0.0


@dataclass(frozen=True)
class CurvePoint:
    """One breakpoint of the target curve."""

    elapsed_s: float
    target_c: float

    def as_dict(self) -> dict[str, Any]:
        return {"elapsed_s": self.elapsed_s, "target_c": self.target_c}


class FiringCurve:
    """Piecewise linear target with a ramp-rate ceiling."""

    def __init__(self, points: Sequence[CurvePoint], *, ramp_rate_c_per_min: float) -> None:
        if float(ramp_rate_c_per_min) <= 0.0:
            raise ValidationError("ramp rate must be positive")
        ordered = tuple(sorted(points, key=lambda item: item.elapsed_s))
        if len(ordered) < 2:
            raise ValidationError("a curve needs at least two points")
        previous = -1.0
        for point in ordered:
            if point.elapsed_s < 0.0:
                raise ValidationError("curve points must not be negative in time")
            if point.elapsed_s <= previous:
                raise ValidationError("curve points must be strictly ordered in time")
            previous = point.elapsed_s
        self._points = ordered
        self._ramp_c_per_s = float(ramp_rate_c_per_min) / 60.0

    @property
    def points(self) -> tuple[CurvePoint, ...]:
        return self._points

    @property
    def ramp_rate_c_per_min(self) -> float:
        return self._ramp_c_per_s * 60.0

    @property
    def duration_s(self) -> float:
        return self._points[-1].elapsed_s

    @property
    def peak_c(self) -> float:
        return max(point.target_c for point in self._points)

    def target_at(self, elapsed_s: float) -> float:
        moment = max(0.0, float(elapsed_s))
        if moment <= self._points[0].elapsed_s:
            return self._points[0].target_c
        for left, right in zip(self._points, self._points[1:]):
            if moment <= right.elapsed_s:
                span = right.elapsed_s - left.elapsed_s
                if span <= 0.0:  # pragma: no cover - guarded by construction
                    return right.target_c
                ratio = (moment - left.elapsed_s) / span
                return left.target_c + (right.target_c - left.target_c) * ratio
        return self._points[-1].target_c

    def slope_at(self, elapsed_s: float) -> float:
        """Target gradient in degrees per second, used to bound zone leads."""

        moment = max(0.0, float(elapsed_s))
        for left, right in zip(self._points, self._points[1:]):
            if moment <= right.elapsed_s:
                span = right.elapsed_s - left.elapsed_s
                if span <= 0.0:  # pragma: no cover - guarded by construction
                    return 0.0
                return (right.target_c - left.target_c) / span
        return 0.0

    def limited_step(self, current_c: float, target_c: float, dt: float) -> float:
        """Move ``current_c`` toward ``target_c`` without exceeding the ramp."""

        budget = self._ramp_c_per_s * max(0.0, float(dt))
        delta = float(target_c) - float(current_c)
        if abs(delta) <= budget:
            return float(target_c)
        return float(current_c) + budget * (1.0 if delta > 0 else -1.0)

    def window_at(self, elapsed_s: float, *, band_c: float) -> tuple[float, float]:
        target = self.target_at(elapsed_s)
        return target - float(band_c), target + float(band_c)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ramp_rate_c_per_min": self.ramp_rate_c_per_min,
            "duration_s": self.duration_s,
            "peak_c": self.peak_c,
            "points": [point.as_dict() for point in self._points],
        }

    @classmethod
    def firing_profile(cls, *, start_c: float, peak_c: float, ramp_rate_c_per_min: float, soak_s: float) -> "FiringCurve":
        """Build the standard heat-up / soak / cool-down profile."""

        rate = float(ramp_rate_c_per_min)
        heat_s = max(1.0, (float(peak_c) - float(start_c)) / rate * 60.0)
        cool_s = heat_s
        points = (
            CurvePoint(0.0, float(start_c)),
            CurvePoint(heat_s, float(peak_c)),
            CurvePoint(heat_s + float(soak_s), float(peak_c)),
            CurvePoint(heat_s + float(soak_s) + cool_s, float(start_c)),
        )
        return cls(points, ramp_rate_c_per_min=rate)
