"""Slurry density: readings, the acceptance band and the calibration baseline.

The density band is an absolute process window, while the calibration is a
generation-stamped baseline.  Both have to hold: a reading inside the band but
measured against a superseded calibration is rejected, and a fresh calibration
cannot rescue a reading outside the band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import ThresholdExceeded, ValidationError
from kilnline.params.baseline import Baseline, BaselineRegistry
from kilnline.temp.curve import WindowVerdict, compare_window

GLAZE_DENSITY_KEY = "glaze.density"
DENSITY_VALUE = "density_g_cm3"


@dataclass(frozen=True)
class SlurryReading:
    """One density measurement with its window verdict."""

    density_g_cm3: float
    read_at: float
    verdict: WindowVerdict

    @property
    def in_band(self) -> bool:
        return self.verdict.within

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.read_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "density_g_cm3": self.density_g_cm3,
            "read_at": self.read_at,
            "in_band": self.in_band,
            "window": self.verdict.as_dict(),
        }


class SlurryStation:
    """Density readings plus the calibration baseline they are judged against."""

    def __init__(
        self,
        baselines: BaselineRegistry,
        *,
        low: float,
        high: float,
        max_age_s: float,
        key: str = GLAZE_DENSITY_KEY,
    ) -> None:
        if float(low) >= float(high):
            raise ValidationError("slurry density band is inverted", low=low, high=high)
        if float(max_age_s) <= 0.0:
            raise ValidationError("slurry calibration age budget must be positive")
        self._baselines = baselines
        self._key = str(key)
        self._low = float(low)
        self._high = float(high)
        self._max_age_s = float(max_age_s)
        self._latest: SlurryReading | None = None

    @property
    def key(self) -> str:
        return self._key

    @property
    def low(self) -> float:
        return self._low

    @property
    def high(self) -> float:
        return self._high

    @property
    def max_age_s(self) -> float:
        return self._max_age_s

    def classify(self, density_g_cm3: float) -> WindowVerdict:
        return compare_window(float(density_g_cm3), low=self._low, high=self._high)

    def in_band(self, density_g_cm3: float) -> bool:
        return self.classify(density_g_cm3).within

    def require_in_band(self, density_g_cm3: float) -> WindowVerdict:
        verdict = self.classify(density_g_cm3)
        if not verdict.within:
            raise ThresholdExceeded(
                "slurry density is outside the acceptance band",
                density_g_cm3=verdict.value,
                low=self._low,
                high=self._high,
                verdict=verdict.verdict,
                deviation=verdict.deviation,
            )
        return verdict

    def record(self, density_g_cm3: float, *, at: float) -> SlurryReading:
        reading = SlurryReading(
            density_g_cm3=float(density_g_cm3),
            read_at=float(at),
            verdict=self.classify(density_g_cm3),
        )
        self._latest = reading
        return reading

    def latest(self) -> SlurryReading | None:
        return self._latest

    def calibrate(
        self,
        *,
        density_g_cm3: float,
        at: float,
        generation: int,
        author: str,
        max_lag_s: float | None = None,
    ) -> Baseline:
        self.require_in_band(density_g_cm3)
        return self._baselines.capture(
            self._key,
            {DENSITY_VALUE: float(density_g_cm3)},
            captured_at=at,
            generation=generation,
            author=author,
            max_lag_s=self._max_age_s if max_lag_s is None else float(max_lag_s),
            reason="slurry_calibration",
        )

    def reference(self, *, now: float, generation: int) -> Baseline:
        return self._baselines.require_current(self._key, now=now, generation=int(generation))

    def require_usable(
        self,
        *,
        density_g_cm3: float,
        now: float,
        generation: int,
    ) -> tuple[Baseline, WindowVerdict]:
        baseline = self.reference(now=now, generation=generation)
        verdict = self.require_in_band(density_g_cm3)
        self.record(density_g_cm3, at=now)
        return baseline, verdict

    def snapshot(self, *, now: float) -> dict[str, Any]:
        baseline = self._baselines.latest(self._key)
        return {
            "band": {"low": self._low, "high": self._high},
            "max_age_s": self._max_age_s,
            "reference": None if baseline is None else baseline.as_dict(),
            "reference_expired": None if baseline is None else baseline.expired(now),
            "latest": None if self._latest is None else self._latest.as_dict(),
        }
