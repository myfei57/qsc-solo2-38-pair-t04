"""Zone map: the generation-stamped link between sections and zones.

The map is what the burner and the temperature guard read when they decide
which zone a section belongs to.  It is stamped with a generation and with the
geometry revision it was built from, so an extension makes every existing map
stale in one step and the service has to refresh before it may use one again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kilnline.errors import GenerationExpired, GenerationMismatch, NotFoundError, ValidationError
from kilnline.kiln.zones import KilnBody
from kilnline.ledger.records import LedgerRecord
from kilnline.ledger.stream import EventStream
from kilnline.params.generation import digest_of
from kilnline.store.json_store import JsonFileStore

ZONE_MAP_DOCUMENT = "zone-map"
ZONE_MAP_KEY = "kiln:zone_map"


@dataclass(frozen=True)
class ZoneMap:
    """Section to zone mapping for one geometry revision and generation."""

    generation: int
    geometry_revision: int
    captured_at: float
    author: str
    max_lag_s: float
    digest: str
    sections: dict[str, list[int]]

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.captured_at)

    def expired(self, now: float) -> bool:
        return self.age_seconds(now) > self.max_lag_s

    def section_count(self) -> int:
        return sum(len(ids) for ids in self.sections.values())

    def zone_of_section(self, section: int) -> str | None:
        wanted = int(section)
        for name, ids in self.sections.items():
            if wanted in ids:
                return name
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation),
            "geometry_revision": int(self.geometry_revision),
            "captured_at": float(self.captured_at),
            "author": self.author,
            "max_lag_s": float(self.max_lag_s),
            "digest": self.digest,
            "sections": {name: list(ids) for name, ids in sorted(self.sections.items())},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoneMap":
        raw = payload.get("sections", {})
        if not isinstance(raw, Mapping):
            raise ValidationError("zone map sections must be a mapping")
        sections: dict[str, list[int]] = {}
        for name, ids in raw.items():
            if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
                raise ValidationError("zone map section list is malformed", zone=str(name))
            sections[str(name)] = [int(item) for item in ids]
        return cls(
            generation=int(payload.get("generation", 0)),
            geometry_revision=int(payload.get("geometry_revision", 0)),
            captured_at=float(payload.get("captured_at", 0.0)),
            author=str(payload.get("author", "")),
            max_lag_s=float(payload.get("max_lag_s", 0.0)),
            digest=str(payload.get("digest", "")),
            sections=sections,
        )


class ZoneMapRegistry:
    """Builds, stamps, persists and validates zone maps."""

    def __init__(
        self,
        store: JsonFileStore,
        stream: EventStream,
        kiln: KilnBody,
        *,
        document: str = ZONE_MAP_DOCUMENT,
        key: str = ZONE_MAP_KEY,
        history_limit: int = 20,
    ) -> None:
        self._store = store
        self._stream = stream
        self._kiln = kiln
        self._document = str(document)
        self._key = str(key)
        self._history_limit = max(1, int(history_limit))
        self._history: list[ZoneMap] = self._load()

    @property
    def key(self) -> str:
        return self._key

    @property
    def generation(self) -> int:
        return self._history[-1].generation if self._history else 0

    def refresh(
        self,
        *,
        at: float,
        author: str,
        max_lag_s: float,
        reason: str = "refresh",
    ) -> ZoneMap:
        if float(max_lag_s) <= 0.0:
            raise ValidationError("zone map lag budget must be positive")
        sections = self._kiln.section_ids()
        zone_map = ZoneMap(
            generation=self.generation + 1,
            geometry_revision=self._kiln.revision,
            captured_at=float(at),
            author=str(author),
            max_lag_s=float(max_lag_s),
            digest=digest_of({name: float(len(ids)) for name, ids in sections.items()}),
            sections=sections,
        )
        self._store.write(
            self._document,
            {"current": zone_map.as_dict(), "history": [item.as_dict() for item in self.history()]},
            written_at=at,
        )
        self._stream.put(
            self._key,
            zone_map.as_dict(),
            written_at=at,
            generation=zone_map.generation,
            reason=reason,
        )
        self._stream.commit(committed_at=at)
        self._history.append(zone_map)
        self._history = self._history[-self._history_limit :]
        return zone_map

    def latest(self) -> ZoneMap | None:
        return self._history[-1] if self._history else None

    def current(self) -> ZoneMap:
        zone_map = self.latest()
        if zone_map is None:
            raise NotFoundError("no zone map has been built yet")
        return zone_map

    def history(self) -> list[ZoneMap]:
        return list(self._history)

    def is_stale(self, *, now: float, geometry_revision: int | None = None) -> bool:
        zone_map = self.latest()
        if zone_map is None:
            return True
        if zone_map.expired(now):
            return True
        revision = self._kiln.revision if geometry_revision is None else int(geometry_revision)
        return zone_map.geometry_revision != revision

    def must_refresh(self, *, now: float) -> bool:
        return self.is_stale(now=now)

    def require_current(self, *, now: float, geometry_revision: int | None = None) -> ZoneMap:
        zone_map = self.latest()
        if zone_map is None:
            raise NotFoundError("no zone map has been built yet")
        if zone_map.expired(now):
            raise GenerationExpired(
                "zone map is older than its lag budget",
                age_s=zone_map.age_seconds(now),
                max_lag_s=zone_map.max_lag_s,
                generation=zone_map.generation,
            )
        revision = self._kiln.revision if geometry_revision is None else int(geometry_revision)
        if zone_map.geometry_revision != revision:
            raise GenerationMismatch(
                "kiln geometry changed; the zone map must be refreshed",
                mapped_revision=zone_map.geometry_revision,
                current_revision=revision,
                generation=zone_map.generation,
            )
        return zone_map

    def restore(self, records: Sequence[LedgerRecord]) -> ZoneMap | None:
        recovered = [
            ZoneMap.from_dict(record.payload)
            for record in records
            if record.key == self._key and not record.is_tombstone
        ]
        if not recovered:
            return None
        recovered.sort(key=lambda item: item.generation)
        merged = {item.generation: item for item in self._history}
        for item in recovered:
            merged.setdefault(item.generation, item)
        self._history = [merged[key] for key in sorted(merged)][-self._history_limit :]
        return self._history[-1]

    def snapshot(self, *, now: float) -> dict[str, Any]:
        zone_map = self.latest()
        return {
            "generation": self.generation,
            "stale": self.is_stale(now=now),
            "geometry_revision": self._kiln.revision,
            "current": None if zone_map is None else zone_map.as_dict(),
            "age_s": None if zone_map is None else zone_map.age_seconds(now),
            "history": [item.generation for item in self._history],
        }

    def _load(self) -> list[ZoneMap]:
        document = self._store.read_or_none(self._document)
        if document is None:
            return []
        history = document.data.get("history", [])
        current = document.data.get("current")
        entries: list[Mapping[str, Any]] = []
        if isinstance(history, list):
            entries.extend(item for item in history if isinstance(item, Mapping))
        if isinstance(current, Mapping):
            entries.append(current)
        recovered = [ZoneMap.from_dict(entry) for entry in entries]
        recovered.sort(key=lambda item: item.generation)
        merged = {item.generation: item for item in recovered}
        return [merged[key] for key in sorted(merged)][-self._history_limit :]
