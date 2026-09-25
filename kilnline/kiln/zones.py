"""Kiln geometry, grate placement and section extension.

Two facts live here.  The first is geometry: how many sections each zone owns,
which changes when a section is extended.  The second is the grate placement
record, which is *persisted to disk* before the entry conveyor is allowed to
feed.  Extending a section invalidates the placement, because the physical
grate no longer lines up with the geometry the record was written against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from kilnline.errors import NotFoundError, ValidationError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.store.json_store import JsonFileStore

GATE_GRATE_PERSISTED = "kiln.grate_persisted"
GRATE_DOCUMENT = "grate-placement"


@dataclass(frozen=True)
class Zone:
    """One controlled zone of the kiln."""

    name: str
    index: int
    target_c: float
    tolerance_c: float
    length_m: float
    sections: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "target_c": self.target_c,
            "tolerance_c": self.tolerance_c,
            "length_m": self.length_m,
            "sections": self.sections,
        }


@dataclass(frozen=True)
class SectionExtension:
    """A recorded change of kiln geometry."""

    zone: str
    at: float
    added_m: float
    revision: int
    author: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "at": self.at,
            "added_m": self.added_m,
            "revision": self.revision,
            "author": self.author,
        }


@dataclass(frozen=True)
class GrateRecord:
    """Durable proof that the grate placement matches a geometry revision."""

    revision: int
    placed_at: float
    author: str
    sections: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "placed_at": self.placed_at,
            "author": self.author,
            "sections": self.sections,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GrateRecord":
        return cls(
            revision=int(payload.get("revision", 0)),
            placed_at=float(payload.get("placed_at", 0.0)),
            author=str(payload.get("author", "")),
            sections=int(payload.get("sections", 0)),
        )


class KilnBody:
    """Geometry plus the persisted grate placement."""

    def __init__(
        self,
        gates: PreGateRegistry,
        store: JsonFileStore,
        *,
        zones: Sequence[str],
        sections_per_zone: int,
        zone_length_m: float,
        tolerance_c: float = 3.0,
        gate_name: str = GATE_GRATE_PERSISTED,
        document: str = GRATE_DOCUMENT,
    ) -> None:
        names = [str(name) for name in zones]
        if not names:
            raise ValidationError("kiln needs at least one zone")
        if int(sections_per_zone) <= 0:
            raise ValidationError("each zone needs at least one section")
        if float(zone_length_m) <= 0.0:
            raise ValidationError("zone length must be positive")
        self._gates = gates
        self._store = store
        self._gate_name = str(gate_name)
        self._document = str(document)
        if self._gate_name not in gates.names():
            gates.declare(self._gate_name, description="grate placement matches the geometry")
        self._order = names
        self._sections_per_zone = int(sections_per_zone)
        self._zone_length_m = float(zone_length_m)
        self._tolerance_c = float(tolerance_c)
        self._targets: dict[str, float] = {name: 0.0 for name in names}
        self._extra_m: dict[str, float] = {name: 0.0 for name in names}
        self._extra_sections: dict[str, int] = {name: 0 for name in names}
        self._revision = 1
        self._extensions: list[SectionExtension] = []
        self._grate: GrateRecord | None = self._load_grate()

    @property
    def gate_name(self) -> str:
        return self._gate_name

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def tolerance_c(self) -> float:
        return self._tolerance_c

    def zones(self) -> list[Zone]:
        return [self.zone(name) for name in self._order]

    def zone(self, name: str) -> Zone:
        label = self._label(name)
        index = self._order.index(label)
        return Zone(
            name=label,
            index=index,
            target_c=self._targets[label],
            tolerance_c=self._tolerance_c,
            length_m=self._zone_length_m + self._extra_m[label],
            sections=self._sections_per_zone + self._extra_sections[label],
        )

    def set_target(self, name: str, target_c: float, *, at: float) -> Zone:
        label = self._label(name)
        value = float(target_c)
        if value < 0.0:
            raise ValidationError("zone target must not be negative", zone=label, target_c=value)
        self._targets[label] = value
        return self.zone(label)

    def target_of(self, name: str) -> float:
        return self._targets[self._label(name)]

    def section_count(self) -> int:
        return sum(zone.sections for zone in self.zones())

    def section_ids(self) -> dict[str, list[int]]:
        mapping: dict[str, list[int]] = {}
        cursor = 0
        for zone in self.zones():
            mapping[zone.name] = list(range(cursor, cursor + zone.sections))
            cursor += zone.sections
        return mapping

    def zone_for_section(self, section: int) -> str:
        wanted = int(section)
        for name, sections in self.section_ids().items():
            if wanted in sections:
                return name
        raise NotFoundError("section is outside the kiln", section=wanted, sections=self.section_count())

    def zone_for_position(self, position_m: float) -> str:
        position = max(0.0, float(position_m))
        cursor = 0.0
        for zone in self.zones():
            cursor += zone.length_m
            if position < cursor:
                return zone.name
        return self._order[-1]

    def extend_section(
        self,
        *,
        zone: str,
        at: float,
        added_m: float,
        author: str,
        sections: int = 1,
    ) -> SectionExtension:
        label = self._label(zone)
        added = float(added_m)
        if added <= 0.0:
            raise ValidationError("a section extension must add length", added_m=added_m)
        if int(sections) <= 0:
            raise ValidationError("a section extension must add at least one section")
        self._extra_m[label] += added
        self._extra_sections[label] += int(sections)
        self._revision += 1
        extension = SectionExtension(
            zone=label,
            at=float(at),
            added_m=added,
            revision=self._revision,
            author=str(author),
        )
        self._extensions.append(extension)
        self._gates.unsatisfy(
            self._gate_name,
            at=at,
            detail=f"geometry revision {self._revision} needs a new grate placement",
        )
        return extension

    def persist_grate(self, *, at: float, author: str) -> GrateRecord:
        """Write the placement to disk and only then open the feeding gate."""

        record = GrateRecord(
            revision=self._revision,
            placed_at=float(at),
            author=str(author),
            sections=self.section_count(),
        )
        self._store.write(self._document, record.as_dict(), written_at=at)
        self._grate = record
        self._gates.satisfy(
            self._gate_name,
            at=at,
            detail=f"grate placement for revision {record.revision}",
        )
        return record

    def grate(self) -> GrateRecord | None:
        return self._grate

    def grate_matches_geometry(self) -> bool:
        return self._grate is not None and self._grate.revision == self._revision

    def extensions(self) -> list[SectionExtension]:
        return list(self._extensions)

    def snapshot(self, *, now: float) -> dict[str, Any]:
        grate = self._grate
        return {
            "revision": self._revision,
            "section_count": self.section_count(),
            "zones": [zone.as_dict() for zone in self.zones()],
            "extensions": [extension.as_dict() for extension in self._extensions],
            "grate": None if grate is None else grate.as_dict(),
            "grate_matches_geometry": self.grate_matches_geometry(),
            "grate_age_s": None if grate is None else max(0.0, float(now) - grate.placed_at),
        }

    def _load_grate(self) -> GrateRecord | None:
        document = self._store.read_or_none(self._document)
        return None if document is None else GrateRecord.from_dict(document.data)

    def _label(self, name: str) -> str:
        label = str(name).strip()
        if label not in self._order:
            raise NotFoundError("unknown kiln zone", zone=label, zones=list(self._order))
        return label
