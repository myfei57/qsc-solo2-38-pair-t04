"""Generation-stamped baselines that expire.

A baseline captures measured behaviour (zone temperatures, roller speed,
slurry density) that later steps are allowed to rely on.  Once it is older
than its lag budget the service must refuse the dependent action instead of
quietly reusing stale numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kilnline.errors import GenerationExpired, GenerationMismatch, NotFoundError, ValidationError
from kilnline.ledger.records import LedgerRecord
from kilnline.ledger.stream import EventStream
from kilnline.params.generation import digest_of
from kilnline.store.json_store import JsonFileStore

BASELINE_DOCUMENT = "baselines"


@dataclass(frozen=True)
class Baseline:
    """A measured reference valid for a bounded time and one generation."""

    key: str
    generation: int
    values: dict[str, float]
    captured_at: float
    author: str
    digest: str
    max_lag_s: float

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.captured_at)

    def expired(self, now: float) -> bool:
        return self.age_seconds(now) > self.max_lag_s

    def value(self, name: str) -> float:
        if name not in self.values:
            raise NotFoundError("baseline does not carry that value", key=self.key, name=name)
        return float(self.values[name])

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "generation": int(self.generation),
            "values": {str(name): float(value) for name, value in self.values.items()},
            "captured_at": float(self.captured_at),
            "author": self.author,
            "digest": self.digest,
            "max_lag_s": float(self.max_lag_s),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Baseline":
        raw = payload.get("values", {})
        if not isinstance(raw, Mapping):
            raise ValidationError("baseline values must be a mapping")
        return cls(
            key=str(payload.get("key", "")),
            generation=int(payload.get("generation", 0)),
            values={str(name): float(value) for name, value in raw.items()},
            captured_at=float(payload.get("captured_at", 0.0)),
            author=str(payload.get("author", "")),
            digest=str(payload.get("digest", "")),
            max_lag_s=float(payload.get("max_lag_s", 0.0)),
        )


class BaselineRegistry:
    """Keeps the newest baseline per key and enforces its age budget."""

    def __init__(
        self,
        store: JsonFileStore,
        stream: EventStream,
        *,
        document: str = BASELINE_DOCUMENT,
        key_prefix: str = "baseline:",
    ) -> None:
        self._store = store
        self._stream = stream
        self._document = document
        self._key_prefix = key_prefix
        self._entries: dict[str, Baseline] = self._load()

    def ledger_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def capture(
        self,
        key: str,
        values: Mapping[str, float],
        *,
        captured_at: float,
        generation: int,
        author: str,
        max_lag_s: float,
        reason: str = "capture",
    ) -> Baseline:
        label = str(key).strip()
        if not label:
            raise ValidationError("baseline key must not be empty")
        if not values:
            raise ValidationError("baseline must carry at least one value", key=label)
        if float(max_lag_s) <= 0.0:
            raise ValidationError("baseline lag budget must be positive", key=label)
        normalised = {str(name): float(value) for name, value in values.items()}
        baseline = Baseline(
            key=label,
            generation=int(generation),
            values=normalised,
            captured_at=float(captured_at),
            author=str(author),
            digest=digest_of(normalised),
            max_lag_s=float(max_lag_s),
        )
        self._entries[label] = baseline
        self._persist(captured_at)
        self._stream.put(
            self.ledger_key(label),
            baseline.as_dict(),
            written_at=captured_at,
            generation=int(generation),
            reason=reason,
        )
        self._stream.commit(committed_at=captured_at)
        return baseline

    def latest(self, key: str) -> Baseline | None:
        return self._entries.get(str(key))

    def require_fresh(self, key: str, *, now: float) -> Baseline:
        baseline = self.latest(key)
        if baseline is None:
            raise NotFoundError("no baseline captured for that key", key=str(key))
        if baseline.expired(now):
            raise GenerationExpired(
                "baseline is older than its lag budget",
                key=baseline.key,
                age_s=baseline.age_seconds(now),
                max_lag_s=baseline.max_lag_s,
            )
        return baseline

    def require_current(self, key: str, *, now: float, generation: int) -> Baseline:
        baseline = self.require_fresh(key, now=now)
        if baseline.generation != int(generation):
            raise GenerationMismatch(
                "baseline was captured against a superseded generation",
                key=baseline.key,
                baseline_generation=baseline.generation,
                current_generation=int(generation),
            )
        return baseline

    def keys(self) -> list[str]:
        return sorted(self._entries)

    def expired_keys(self, *, now: float) -> list[str]:
        return sorted(key for key, entry in self._entries.items() if entry.expired(now))

    def ages(self, *, now: float) -> dict[str, float]:
        return {key: entry.age_seconds(now) for key, entry in self._entries.items()}

    def restore(self, records: Sequence[LedgerRecord]) -> list[str]:
        """Rebuild baselines from a ledger replay, returning restored keys."""

        restored: list[str] = []
        for record in records:
            if record.is_tombstone or not record.key.startswith(self._key_prefix):
                continue
            baseline = Baseline.from_dict(record.payload)
            self._entries[baseline.key] = baseline
            restored.append(baseline.key)
        return restored

    def _persist(self, at: float) -> None:
        payload = {"entries": {key: entry.as_dict() for key, entry in sorted(self._entries.items())}}
        self._store.write(self._document, payload, written_at=at)

    def _load(self) -> dict[str, Baseline]:
        document = self._store.read_or_none(self._document)
        if document is None:
            return {}
        raw = document.data.get("entries", {})
        if not isinstance(raw, Mapping):
            return {}
        recovered: dict[str, Baseline] = {}
        for key, entry in raw.items():
            if not isinstance(entry, Mapping):
                continue
            recovered[str(key)] = Baseline.from_dict(entry)
        return recovered
