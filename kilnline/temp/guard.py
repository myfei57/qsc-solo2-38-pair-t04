"""Temperature protection: overload trip, stale baseline trip, held reset.

Two conditions can hold the line down.  An over-temperature trip is raised the
moment a zone leaves ``target + margin``; a stale-baseline trip is raised when
the service has no fresh baseline to judge against.  Both stay on until the
cause clears *and* the operator asked for a reset *and* the hold time elapsed.
"""

from __future__ import annotations

from typing import Any

from kilnline.errors import ValidationError
from kilnline.interlock.latch import LatchRegistry, LatchState
from kilnline.temp.curve import compare_window, within_band

LATCH_OVER_TEMP = "temp.over_temp"
LATCH_BASELINE_STALE = "temp.baseline_stale"

REASON_OVER_TEMP = "over_temperature"
REASON_BASELINE_STALE = "baseline_stale"
REASON_PROBE_STALE = "probe_calibration_stale"


class TemperatureGuard:
    """Trips and releases the temperature latches."""

    def __init__(
        self,
        latches: LatchRegistry,
        *,
        over_temp_margin_c: float,
        hold_s: float,
        band_c: float,
        latch_over_temp: str = LATCH_OVER_TEMP,
        latch_baseline_stale: str = LATCH_BASELINE_STALE,
    ) -> None:
        if float(over_temp_margin_c) < 0.0:
            raise ValidationError("over temperature margin must not be negative")
        if float(hold_s) < 0.0:
            raise ValidationError("latch hold must not be negative")
        self._margin_c = float(over_temp_margin_c)
        self._band_c = float(band_c)
        self._latches = latches
        self._over_temp_latch = str(latch_over_temp)
        self._stale_latch = str(latch_baseline_stale)
        if self._over_temp_latch not in latches.snapshot():
            latches.declare(self._over_temp_latch, description="zone left its over-temperature margin")
        if self._stale_latch not in latches.snapshot():
            latches.declare(self._stale_latch, description="baseline or probe calibration expired")

    @property
    def over_temp_latch(self) -> str:
        return self._over_temp_latch

    @property
    def stale_latch(self) -> str:
        return self._stale_latch

    @property
    def margin_c(self) -> float:
        return self._margin_c

    def over_limit_c(self, value_c: float, *, target_c: float) -> float:
        """How far a reading is above target + margin; zero while inside."""

        ceiling = float(target_c) + self._margin_c
        return max(0.0, float(value_c) - ceiling)

    def evaluate(
        self,
        *,
        zone: str,
        value_c: float,
        target_c: float,
        baseline_fresh: bool,
        probe_fresh: bool,
        now: float,
    ) -> dict[str, Any]:
        zone_name = str(zone)
        violation = self.over_limit_c(value_c, target_c=target_c)
        result: dict[str, Any] = {
            "zone": zone_name,
            "value_c": float(value_c),
            "target_c": float(target_c),
            "over_limit_c": violation,
            "window": compare_window(
                value_c,
                low=float(target_c) - self._band_c,
                high=float(target_c) + self._band_c,
            ).as_dict(),
            "tripped": [],
            "released": [],
        }
        if violation > 0.0:
            state = self._latches.trip(
                self._over_temp_latch,
                REASON_OVER_TEMP,
                at=now,
            )
            result["tripped"].append(state.name)
        if not baseline_fresh or not probe_fresh:
            state = self._latches.trip(
                self._stale_latch,
                REASON_BASELINE_STALE if not baseline_fresh else REASON_PROBE_STALE,
                at=now,
            )
            result["tripped"].append(state.name)
        in_band = within_band(value_c, target=target_c, band=self._band_c)
        for name, conditions_ok in (
            (self._over_temp_latch, in_band),
            (self._stale_latch, baseline_fresh and probe_fresh),
        ):
            before = self._latches.state(name)
            after = self._latches.evaluate(name, now=now, conditions_ok=conditions_ok)
            if before.tripped and not after.tripped:
                result["released"].append(name)
        result["latches"] = self.snapshot()
        return result

    def request_reset(self, *, at: float) -> list[LatchState]:
        requested: list[LatchState] = []
        for name in (self._over_temp_latch, self._stale_latch):
            if self._latches.is_tripped(name):
                requested.append(self._latches.request_reset(name, at=at))
        return requested

    def blocked(self) -> bool:
        return self._latches.any_tripped(self._over_temp_latch, self._stale_latch)

    def tripped(self) -> list[str]:
        return [
            name
            for name in (self._over_temp_latch, self._stale_latch)
            if self._latches.is_tripped(name)
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "over_temp_latch": self._latches.state(self._over_temp_latch).as_dict(),
            "stale_latch": self._latches.state(self._stale_latch).as_dict(),
            "margin_c": self._margin_c,
        }
