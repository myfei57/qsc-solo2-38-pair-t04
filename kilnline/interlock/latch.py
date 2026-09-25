"""Latches that hold a subsystem down until the cause clears and a hold runs out.

A latch is a two part condition: the trip is immediate and stays on, and the
release needs three things at once -- a reset request from the operator, a
cleared cause, and the hold time elapsed since that request.  ``at``/``now``
arguments are monotonic seconds so a wall-clock jump cannot shorten a hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import InterlockActive, NotFoundError, StateConflict, ValidationError


@dataclass(frozen=True)
class LatchState:
    """Current condition of one latch."""

    name: str
    description: str
    tripped: bool
    reason: str
    tripped_at: float | None
    reset_requested_at: float | None
    released_at: float | None
    hold_s: float
    trips: int

    def held_for(self, now: float) -> float:
        if self.reset_requested_at is None:
            return 0.0
        return max(0.0, float(now) - self.reset_requested_at)

    def hold_remaining(self, now: float) -> float:
        if not self.tripped:
            return 0.0
        if self.reset_requested_at is None:
            return float(self.hold_s)
        return max(0.0, float(self.hold_s) - self.held_for(now))

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tripped": self.tripped,
            "reason": self.reason,
            "tripped_at": self.tripped_at,
            "reset_requested_at": self.reset_requested_at,
            "released_at": self.released_at,
            "hold_s": self.hold_s,
            "trips": self.trips,
        }


class LatchRegistry:
    """Owns every latch in the line and the rules that release them."""

    def __init__(self, *, default_hold_s: float = 0.0) -> None:
        if float(default_hold_s) < 0.0:
            raise ValidationError("default latch hold must not be negative", hold_s=default_hold_s)
        self._default_hold_s = float(default_hold_s)
        self._states: dict[str, LatchState] = {}

    def declare(self, name: str, *, description: str = "", hold_s: float | None = None) -> LatchState:
        label = self._label(name)
        if label in self._states:
            raise StateConflict("latch is already declared", name=label)
        hold = self._default_hold_s if hold_s is None else float(hold_s)
        if hold < 0.0:
            raise ValidationError("latch hold must not be negative", name=label, hold_s=hold)
        state = LatchState(
            name=label,
            description=str(description),
            tripped=False,
            reason="",
            tripped_at=None,
            reset_requested_at=None,
            released_at=None,
            hold_s=hold,
            trips=0,
        )
        self._states[label] = state
        return state

    def trip(self, name: str, reason: str, *, at: float) -> LatchState:
        current = self._require(name)
        state = LatchState(
            name=current.name,
            description=current.description,
            tripped=True,
            reason=str(reason),
            tripped_at=current.tripped_at if current.tripped else float(at),
            reset_requested_at=None,
            released_at=current.released_at,
            hold_s=current.hold_s,
            trips=current.trips + (0 if current.tripped else 1),
        )
        self._states[current.name] = state
        return state

    def request_reset(self, name: str, *, at: float) -> LatchState:
        current = self._require(name)
        if not current.tripped:
            raise StateConflict("latch is not tripped", name=current.name)
        if current.reset_requested_at is not None:
            return current
        state = LatchState(
            name=current.name,
            description=current.description,
            tripped=True,
            reason=current.reason,
            tripped_at=current.tripped_at,
            reset_requested_at=float(at),
            released_at=current.released_at,
            hold_s=current.hold_s,
            trips=current.trips,
        )
        self._states[current.name] = state
        return state

    def evaluate(self, name: str, *, now: float, conditions_ok: bool) -> LatchState:
        """Release the latch when every release condition is satisfied."""

        current = self._require(name)
        if not current.tripped:
            return current
        if not conditions_ok or current.reset_requested_at is None:
            return current
        if current.held_for(now) < current.hold_s:
            return current
        state = LatchState(
            name=current.name,
            description=current.description,
            tripped=False,
            reason="",
            tripped_at=None,
            reset_requested_at=None,
            released_at=float(now),
            hold_s=current.hold_s,
            trips=current.trips,
        )
        self._states[current.name] = state
        return state

    def clear(self, name: str, *, at: float) -> LatchState:
        """Force a latch back to normal; used by the bench self test."""

        current = self._require(name)
        state = LatchState(
            name=current.name,
            description=current.description,
            tripped=False,
            reason="",
            tripped_at=None,
            reset_requested_at=None,
            released_at=float(at),
            hold_s=current.hold_s,
            trips=current.trips,
        )
        self._states[current.name] = state
        return state

    def state(self, name: str) -> LatchState:
        return self._require(name)

    def is_tripped(self, name: str) -> bool:
        return self._require(name).tripped

    def any_tripped(self, *names: str) -> bool:
        scope = names or tuple(self._states)
        return any(self._require(name).tripped for name in scope)

    def tripped_names(self) -> list[str]:
        return sorted(name for name, state in self._states.items() if state.tripped)

    def require_clear(self, name: str) -> None:
        state = self._require(name)
        if state.tripped:
            raise InterlockActive(
                "latch is tripped and blocks this action",
                latch=state.name,
                reason=state.reason,
                tripped_at=state.tripped_at,
            )

    def snapshot(self) -> dict[str, Any]:
        return {name: state.as_dict() for name, state in sorted(self._states.items())}

    def _require(self, name: str) -> LatchState:
        label = self._label(name)
        state = self._states.get(label)
        if state is None:
            raise NotFoundError("unknown latch", name=label)
        return state

    @staticmethod
    def _label(name: str) -> str:
        label = str(name).strip()
        if not label:
            raise ValidationError("latch name must not be empty")
        return label
