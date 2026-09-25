"""Roller drive with a ramp limited speed setpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import StateConflict, ValidationError
from kilnline.interlock.latch import LatchRegistry

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"

ROLLER_SPEED_LATCH = "roller.speed"


@dataclass(frozen=True)
class DriveState:
    """Snapshot of the drive, safe to serialise."""

    state: str
    speed_mpm: float
    target_mpm: float
    started_at: float | None
    stopped_at: float | None
    stop_reason: str

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "speed_mpm": self.speed_mpm,
            "target_mpm": self.target_mpm,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "running": self.running,
        }


class RollerDrive:
    """Speed controlled roller table drive."""

    def __init__(
        self,
        latches: LatchRegistry,
        *,
        min_mpm: float,
        max_mpm: float,
        ramp_mpm_per_s: float,
        latch_name: str = ROLLER_SPEED_LATCH,
    ) -> None:
        if float(min_mpm) <= 0.0 or float(max_mpm) <= float(min_mpm):
            raise ValidationError("roller speed range is invalid", min_mpm=min_mpm, max_mpm=max_mpm)
        if float(ramp_mpm_per_s) <= 0.0:
            raise ValidationError("roller ramp must be positive")
        self._latches = latches
        self._latch_name = str(latch_name)
        if self._latch_name not in latches.snapshot():
            latches.declare(self._latch_name, description="roller drive faulted")
        self._min_mpm = float(min_mpm)
        self._max_mpm = float(max_mpm)
        self._ramp_mpm_per_s = float(ramp_mpm_per_s)
        self._state = STATE_IDLE
        self._speed_mpm = 0.0
        self._target_mpm = 0.0
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._stop_reason = ""

    @property
    def latch_name(self) -> str:
        return self._latch_name

    @property
    def min_mpm(self) -> float:
        return self._min_mpm

    @property
    def max_mpm(self) -> float:
        return self._max_mpm

    @property
    def ramp_mpm_per_s(self) -> float:
        return self._ramp_mpm_per_s

    @property
    def state(self) -> str:
        return self._state

    @property
    def speed_mpm(self) -> float:
        return self._speed_mpm

    @property
    def target_mpm(self) -> float:
        return self._target_mpm

    @property
    def running(self) -> bool:
        return self._state == STATE_RUNNING

    def clamp_speed(self, speed_mpm: float) -> float:
        value = float(speed_mpm)
        if value != value:
            raise ValidationError("roller speed must be a number", speed_mpm=speed_mpm)
        return max(self._min_mpm, min(self._max_mpm, value))

    def start(self, *, at: float, target_mpm: float | None = None) -> DriveState:
        self._latches.require_clear(self._latch_name)
        if self._state == STATE_RUNNING:
            raise StateConflict("roller drive is already running")
        self._state = STATE_RUNNING
        self._started_at = float(at)
        self._stopped_at = None
        self._stop_reason = ""
        if target_mpm is not None:
            self._target_mpm = self.clamp_speed(target_mpm)
        return self.snapshot()

    def set_speed(self, *, target_mpm: float, at: float) -> DriveState:
        if self._state != STATE_RUNNING:
            raise StateConflict("roller drive is not running", state=self._state)
        self._target_mpm = self.clamp_speed(target_mpm)
        return self.snapshot()

    def stop(self, *, at: float, reason: str = "requested") -> DriveState:
        if self._state == STATE_IDLE:
            raise StateConflict("roller drive is already idle")
        self._state = STATE_STOPPING
        self._target_mpm = 0.0
        self._stopped_at = float(at)
        self._stop_reason = str(reason)
        if self._speed_mpm <= 0.0:
            self._state = STATE_IDLE
        return self.snapshot()

    def fault(self, reason: str, *, at: float) -> DriveState:
        self._latches.trip(self._latch_name, reason, at=at)
        self._state = STATE_IDLE
        self._speed_mpm = 0.0
        self._target_mpm = 0.0
        self._stopped_at = float(at)
        self._stop_reason = str(reason)
        return self.snapshot()

    def advance(self, *, dt: float) -> float:
        step = float(dt)
        if step <= 0.0:
            raise ValidationError("roller advance step must be positive", dt=dt)
        if self._state == STATE_IDLE:
            return self._speed_mpm
        budget = self._ramp_mpm_per_s * step
        delta = self._target_mpm - self._speed_mpm
        if abs(delta) <= budget:
            self._speed_mpm = self._target_mpm
        else:
            self._speed_mpm += budget * (1.0 if delta > 0 else -1.0)
        if self._state == STATE_STOPPING and self._speed_mpm <= 0.0:
            self._state = STATE_IDLE
        return self._speed_mpm

    def at_target(self, *, tolerance_mpm: float = 0.05) -> bool:
        return abs(self._speed_mpm - self._target_mpm) <= float(tolerance_mpm)

    def snapshot(self) -> DriveState:
        return DriveState(
            state=self._state,
            speed_mpm=self._speed_mpm,
            target_mpm=self._target_mpm,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
            stop_reason=self._stop_reason,
        )
