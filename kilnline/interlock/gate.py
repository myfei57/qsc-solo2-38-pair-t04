"""Named precondition gates.

A gate is the coarse "has this already happened" flag a later step asks about:
the grate must be on disk before feeding, combustion air must be established
before the gas train opens.  The component that owns the fact satisfies the
gate; the component that depends on it calls :meth:`PreGateRegistry.require`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from kilnline.errors import InterlockActive, NotFoundError, ValidationError


@dataclass(frozen=True)
class Gate:
    """One precondition with its last transition stamp."""

    name: str
    description: str
    satisfied: bool
    detail: str
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "updated_at": self.updated_at,
        }


class PreGateRegistry:
    """Registry of named preconditions shared across subsystems."""

    def __init__(self) -> None:
        self._gates: dict[str, Gate] = {}

    def declare(self, name: str, *, description: str = "") -> Gate:
        label = self._label(name)
        gate = Gate(
            name=label,
            description=str(description),
            satisfied=False,
            detail="",
            updated_at=0.0,
        )
        self._gates[label] = gate
        return gate

    def declare_many(self, names: Iterable[str], *, prefix: str = "") -> list[Gate]:
        declared: list[Gate] = []
        for name in names:
            declared.append(self.declare(f"{prefix}{name}", description=name))
        return declared

    def satisfy(self, name: str, *, at: float, detail: str = "") -> Gate:
        current = self._require(name)
        gate = Gate(
            name=current.name,
            description=current.description,
            satisfied=True,
            detail=str(detail),
            updated_at=float(at),
        )
        self._gates[current.name] = gate
        return gate

    def unsatisfy(self, name: str, *, at: float, detail: str = "") -> Gate:
        current = self._require(name)
        gate = Gate(
            name=current.name,
            description=current.description,
            satisfied=False,
            detail=str(detail),
            updated_at=float(at),
        )
        self._gates[current.name] = gate
        return gate

    def satisfies(self, name: str) -> bool:
        return self._require(name).satisfied

    def require(self, name: str) -> Gate:
        gate = self._require(name)
        if not gate.satisfied:
            raise InterlockActive(
                "precondition gate is not satisfied",
                gate=gate.name,
                description=gate.description,
                detail=gate.detail,
            )
        return gate

    def require_all(self, names: Iterable[str]) -> list[Gate]:
        return [self.require(name) for name in names]

    def get(self, name: str) -> Gate:
        return self._require(name)

    def names(self) -> list[str]:
        return sorted(self._gates)

    def closed(self) -> list[str]:
        return sorted(name for name, gate in self._gates.items() if not gate.satisfied)

    def open_gates(self) -> list[str]:
        return sorted(name for name, gate in self._gates.items() if gate.satisfied)

    def snapshot(self) -> dict[str, Any]:
        return {name: gate.as_dict() for name, gate in sorted(self._gates.items())}

    def _require(self, name: str) -> Gate:
        label = self._label(name)
        gate = self._gates.get(label)
        if gate is None:
            raise NotFoundError("unknown precondition gate", name=label)
        return gate

    @staticmethod
    def _label(name: str) -> str:
        label = str(name).strip()
        if not label:
            raise ValidationError("gate name must not be empty")
        return label
