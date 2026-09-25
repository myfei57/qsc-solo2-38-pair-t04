"""Current-value versus historical-value comparisons.

The history keeps every recorded value of a key with the moment it was taken,
so a report can ask two different questions: what is the value now, and what
did the same key read at the moment a carrier entered the kiln.  A comparison
between the two is what turns a raw number into "the setpoint moved after this
batch was started".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import NotFoundError, ValidationError


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded value of a key."""

    key: str
    value: float
    at: float
    generation: int

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "at": self.at, "generation": self.generation}


@dataclass(frozen=True)
class StateDelta:
    """Comparison of the current value against the value at ``as_of``."""

    key: str
    as_of: float
    current: float
    historical: float
    delta: float
    changed: bool
    generation_current: int
    generation_historical: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "as_of": self.as_of,
            "current": self.current,
            "historical": self.historical,
            "delta": self.delta,
            "changed": self.changed,
            "generation_current": self.generation_current,
            "generation_historical": self.generation_historical,
        }


class StateHistory:
    """Bounded in-memory history of keyed scalar values."""

    def __init__(self, *, limit: int = 200, tolerance: float = 1e-9) -> None:
        if int(limit) <= 0:
            raise ValidationError("history limit must be positive", limit=limit)
        if float(tolerance) < 0.0:
            raise ValidationError("history tolerance must not be negative", tolerance=tolerance)
        self._limit = int(limit)
        self._tolerance = float(tolerance)
        self._entries: dict[str, list[HistoryEntry]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def record(self, key: str, value: float, *, at: float, generation: int = 0) -> HistoryEntry:
        label = self._label(key)
        entry = HistoryEntry(
            key=label,
            value=float(value),
            at=float(at),
            generation=int(generation),
        )
        bucket = self._entries.setdefault(label, [])
        bucket.append(entry)
        if len(bucket) > self._limit:
            del bucket[: len(bucket) - self._limit]
        return entry

    def history(self, key: str) -> list[HistoryEntry]:
        return list(self._entries.get(self._label(key), []))

    def current(self, key: str) -> HistoryEntry:
        bucket = self._entries.get(self._label(key), [])
        if not bucket:
            raise NotFoundError("no value has been recorded for that key", key=str(key))
        return bucket[-1]

    def historical(self, key: str, *, as_of: float) -> HistoryEntry:
        moment = float(as_of)
        found: HistoryEntry | None = None
        for entry in self._entries.get(self._label(key), []):
            if entry.at <= moment:
                found = entry
        if found is None:
            raise NotFoundError(
                "no value was recorded for that key before the requested moment",
                key=str(key),
                as_of=moment,
            )
        return found

    def compare(self, key: str, *, as_of: float) -> StateDelta:
        label = self._label(key)
        current = self.current(label)
        previous = self.historical(label, as_of=as_of)
        delta = current.value - previous.value
        return StateDelta(
            key=label,
            as_of=float(as_of),
            current=current.value,
            historical=previous.value,
            delta=delta,
            changed=abs(delta) > self._tolerance,
            generation_current=current.generation,
            generation_historical=previous.generation,
        )

    def changed_since(self, key: str, *, as_of: float) -> bool:
        return self.compare(key, as_of=as_of).changed

    def keys(self) -> list[str]:
        return sorted(self._entries)

    def entry_count(self, key: str) -> int:
        return len(self._entries.get(self._label(key), []))

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self._limit,
            "keys": {
                key: {
                    "entries": len(bucket),
                    "current": bucket[-1].as_dict() if bucket else None,
                }
                for key, bucket in sorted(self._entries.items())
            },
        }

    @staticmethod
    def _label(key: str) -> str:
        label = str(key).strip()
        if not label:
            raise ValidationError("history key must not be empty")
        return label
