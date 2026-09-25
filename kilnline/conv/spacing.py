"""Kiln entry spacing window.

Too close and carriers collide at the kiln mouth; too far and the kiln runs
half empty and the curve never settles.  Both failures are the same window
comparison, so they share one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from kilnline.errors import ThresholdExceeded, ValidationError
from kilnline.temp.curve import VERDICT_ABOVE, VERDICT_BELOW, compare_window

GAP_TOO_CLOSE = "too_close"
GAP_OK = "ok"
GAP_TOO_FAR = "too_far"


@dataclass(frozen=True)
class GapVerdict:
    """Spacing decision for one carrier."""

    car_id: str
    gap_mm: float
    low_mm: float
    high_mm: float
    verdict: str
    deviation: float

    @property
    def ok(self) -> bool:
        return self.verdict == GAP_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "car_id": self.car_id,
            "gap_mm": self.gap_mm,
            "low_mm": self.low_mm,
            "high_mm": self.high_mm,
            "verdict": self.verdict,
            "deviation": self.deviation,
        }


class EntryWindow:
    """The accepted gap band at the kiln mouth."""

    def __init__(self, *, min_mm: float, max_mm: float) -> None:
        if float(min_mm) <= 0.0:
            raise ValidationError("minimum entry gap must be positive", min_mm=min_mm)
        if float(min_mm) >= float(max_mm):
            raise ValidationError("entry gap band is inverted", min_mm=min_mm, max_mm=max_mm)
        self._low = float(min_mm)
        self._high = float(max_mm)

    @property
    def low_mm(self) -> float:
        return self._low

    @property
    def high_mm(self) -> float:
        return self._high

    @property
    def width_mm(self) -> float:
        return self._high - self._low

    def evaluate(self, gap_mm: float, *, car_id: str = "") -> GapVerdict:
        window = compare_window(float(gap_mm), low=self._low, high=self._high)
        if window.verdict == VERDICT_BELOW:
            verdict = GAP_TOO_CLOSE
        elif window.verdict == VERDICT_ABOVE:
            verdict = GAP_TOO_FAR
        else:
            verdict = GAP_OK
        return GapVerdict(
            car_id=str(car_id),
            gap_mm=window.value,
            low_mm=window.low,
            high_mm=window.high,
            verdict=verdict,
            deviation=window.deviation,
        )

    def require(self, gap_mm: float, *, car_id: str = "") -> GapVerdict:
        verdict = self.evaluate(gap_mm, car_id=car_id)
        if not verdict.ok:
            raise ThresholdExceeded(
                "carrier spacing is outside the entry window",
                car_id=str(car_id),
                gap_mm=verdict.gap_mm,
                low_mm=self._low,
                high_mm=self._high,
                verdict=verdict.verdict,
                deviation=verdict.deviation,
            )
        return verdict

    def plan(self, gaps: Sequence[float], *, prefix: str = "") -> list[GapVerdict]:
        return [
            self.evaluate(gap, car_id=f"{prefix}{index + 1}")
            for index, gap in enumerate(gaps)
        ]

    def as_dict(self) -> dict[str, Any]:
        return {"low_mm": self._low, "high_mm": self._high, "width_mm": self.width_mm}
