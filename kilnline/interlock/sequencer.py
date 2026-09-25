"""Ordered multi-stage machines.

The sequencer never performs work itself; it records that a stage finished and
refuses to let a stage start before every prerequisite of that stage has
finished.  A stage may declare more than one prerequisite, which is how the
recovery path says "only after the alarm is reset *and* the latch released".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from kilnline.errors import NotFoundError, OrderingViolation, StateConflict, ValidationError


@dataclass(frozen=True)
class Stage:
    """One step of an ordered machine."""

    name: str
    prerequisites: tuple[str, ...] = ()
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prerequisites": list(self.prerequisites),
            "description": self.description,
        }


@dataclass(frozen=True)
class StageState:
    name: str
    order: int
    completed: bool
    completed_at: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "order": self.order,
            "completed": self.completed,
            "completed_at": self.completed_at,
        }


@dataclass
class StageSequencer:
    """Enforces the declared order of one process."""

    name: str
    stages: tuple[Stage, ...]
    completions: dict[str, float] = field(default_factory=dict)
    resets: int = 0
    last_reset_at: float | None = None
    last_reset_reason: str = ""

    def __init__(self, name: str, stages: Sequence[Stage]) -> None:
        label = str(name).strip()
        if not label:
            raise ValidationError("sequencer name must not be empty")
        ordered = tuple(stages)
        if not ordered:
            raise ValidationError("a sequencer needs at least one stage", name=label)
        names = [stage.name for stage in ordered]
        if len(set(names)) != len(names):
            raise ValidationError("stage names must be unique", name=label, stages=names)
        for stage in ordered:
            for prerequisite in stage.prerequisites:
                if prerequisite not in names:
                    raise ValidationError(
                        "stage prerequisite is not part of this sequence",
                        name=label,
                        stage=stage.name,
                        prerequisite=prerequisite,
                    )
                if names.index(prerequisite) >= names.index(stage.name):
                    raise ValidationError(
                        "stage prerequisite must be declared earlier",
                        name=label,
                        stage=stage.name,
                        prerequisite=prerequisite,
                    )
        self.name = label
        self.stages = ordered
        self.completions = {}
        self.resets = 0
        self.last_reset_at = None
        self.last_reset_reason = ""

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages)

    def stage(self, name: str) -> Stage:
        for stage in self.stages:
            if stage.name == str(name):
                return stage
        raise NotFoundError("unknown stage", sequencer=self.name, stage=str(name))

    def unfinished_prerequisites(self, name: str) -> list[str]:
        stage = self.stage(name)
        return [item for item in stage.prerequisites if item not in self.completions]

    def require(self, name: str) -> Stage:
        """Raise unless ``name`` is the next legal stage to complete."""

        stage = self.stage(name)
        if stage.name in self.completions:
            raise StateConflict("stage already completed", sequencer=self.name, stage=stage.name)
        missing = self.unfinished_prerequisites(stage.name)
        if missing:
            raise OrderingViolation(
                "stage requested before its prerequisites completed",
                sequencer=self.name,
                stage=stage.name,
                missing=missing,
                completed=sorted(self.completions),
            )
        return stage

    def complete(self, name: str, *, at: float) -> StageState:
        stage = self.require(name)
        self.completions[stage.name] = float(at)
        return self.state(stage.name)

    def complete_all(self, *, at: float) -> list[StageState]:
        states: list[StageState] = []
        for stage in self.stages:
            if stage.name not in self.completions:
                states.append(self.complete(stage.name, at=at))
        return states

    def is_complete(self, name: str) -> bool:
        return str(name) in self.completions

    def completed(self) -> list[str]:
        return [stage.name for stage in self.stages if stage.name in self.completions]

    def pending(self) -> list[str]:
        return [stage.name for stage in self.stages if stage.name not in self.completions]

    def next_stage(self) -> str | None:
        for stage in self.stages:
            if stage.name in self.completions:
                continue
            if self.unfinished_prerequisites(stage.name):
                return None
            return stage.name
        return None

    def state(self, name: str) -> StageState:
        stage = self.stage(name)
        return StageState(
            name=stage.name,
            order=self.order.index(stage.name),
            completed=stage.name in self.completions,
            completed_at=self.completions.get(stage.name),
        )

    def reset(self, *, at: float, reason: str = "") -> None:
        if not self.completions:
            raise StateConflict("sequence has not started", sequencer=self.name)
        self.completions = {}
        self.resets += 1
        self.last_reset_at = float(at)
        self.last_reset_reason = str(reason)

    def progress(self) -> dict[str, Any]:
        return {
            "sequencer": self.name,
            "order": list(self.order),
            "completed": self.completed(),
            "pending": self.pending(),
            "next": self.next_stage(),
            "complete": not self.pending(),
            "resets": self.resets,
        }
