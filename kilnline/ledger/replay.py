"""Deterministic replay of the committed record stream.

A projection is the materialised view a service rebuilds on start-up: it walks
committed records in sequence order and folds them into ``key -> payload``.  A
tombstone hides the record it targets only when that record is still the live
value of its key, so voiding never erases a newer write by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from kilnline.ledger.records import LedgerRecord
from kilnline.ledger.stream import EventStream


@dataclass
class Projection:
    """Materialised view built from committed records."""

    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)
    sequences: dict[str, int] = field(default_factory=dict)
    voided: list[int] = field(default_factory=list)
    applied: list[int] = field(default_factory=list)

    def apply(self, record: LedgerRecord) -> bool:
        """Fold one committed record in; returns whether the view changed."""

        if record.is_tombstone:
            target = record.voided_sequence
            if target is None:
                return False
            self.voided.append(target)
            if self.sequences.get(record.key) != target:
                return False
            self.values.pop(record.key, None)
            self.generations.pop(record.key, None)
            self.sequences.pop(record.key, None)
            self.applied.append(record.sequence)
            return True
        self.values[record.key] = dict(record.payload)
        self.generations[record.key] = record.generation
        self.sequences[record.key] = record.sequence
        self.applied.append(record.sequence)
        return True

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.values.get(key)
        return None if value is None else dict(value)

    def keys(self) -> list[str]:
        return sorted(self.values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "keys": self.keys(),
            "values": {key: dict(value) for key, value in self.values.items()},
            "generations": dict(self.generations),
            "voided": list(self.voided),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Projection":
        """Rebuild a projection from a snapshot so recovery can resume mid-stream."""

        view = cls()
        raw_values = payload.get("values", {})
        if isinstance(raw_values, Mapping):
            for key, value in raw_values.items():
                if isinstance(value, Mapping):
                    view.values[str(key)] = {str(name): item for name, item in value.items()}
        raw_generations = payload.get("generations", {})
        if isinstance(raw_generations, Mapping):
            for key, value in raw_generations.items():
                view.generations[str(key)] = int(value)
        view.sequences = {key: 0 for key in view.values}
        view.voided = [int(item) for item in payload.get("voided", [])]
        return view


@dataclass(frozen=True)
class ReplayOutcome:
    """Everything a caller needs to explain what recovery did."""

    from_watermark: int
    to_watermark: int
    applied: tuple[int, ...]
    voided: tuple[int, ...]
    projection: dict[str, Any]

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_watermark": self.from_watermark,
            "to_watermark": self.to_watermark,
            "applied": list(self.applied),
            "voided": list(self.voided),
            "applied_count": self.applied_count,
            "keys": sorted(self.projection.get("values", {})),
        }


def replay(
    stream: EventStream,
    *,
    after_watermark: int = 0,
    projection: Projection | None = None,
) -> ReplayOutcome:
    """Re-apply committed records newer than ``after_watermark``."""

    view = Projection() if projection is None else projection
    records = stream.replay(after_watermark=after_watermark)
    for record in records:
        view.apply(record)
    return ReplayOutcome(
        from_watermark=int(after_watermark),
        to_watermark=stream.watermark,
        applied=tuple(view.applied),
        voided=tuple(view.voided),
        projection=view.as_dict(),
    )


def rebuild(stream: EventStream) -> ReplayOutcome:
    """Rebuild the whole projection from sequence zero."""

    return replay(stream, after_watermark=0)
