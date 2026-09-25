"""Kiln geometry, grate placement and zone map freshness."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.errors import (
    GenerationExpired,
    GenerationMismatch,
    NotFoundError,
    ValidationError,
)
from kilnline.interlock.gate import PreGateRegistry
from kilnline.kiln.mapping import ZONE_MAP_DOCUMENT, ZoneMapRegistry
from kilnline.kiln.zones import GATE_GRATE_PERSISTED, GRATE_DOCUMENT, KilnBody
from kilnline.ledger.stream import EventStream
from kilnline.store.json_store import JsonFileStore

ZONES = ("preheat", "firing", "cooling")


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.store = JsonFileStore(tmp_path)
        self.stream = EventStream(tmp_path / "ledger.jsonl", self.store)
        self.gates = PreGateRegistry()
        self.kiln = KilnBody(
            self.gates,
            self.store,
            zones=ZONES,
            sections_per_zone=2,
            zone_length_m=4.0,
            tolerance_c=3.0,
        )
        self.zone_map = ZoneMapRegistry(self.store, self.stream, self.kiln)


def test_kiln_geometry_reports_sections_and_positions(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert [zone.name for zone in rig.kiln.zones()] == list(ZONES)
    assert rig.kiln.section_count() == 6
    assert rig.kiln.section_ids() == {
        "preheat": [0, 1],
        "firing": [2, 3],
        "cooling": [4, 5],
    }
    assert rig.kiln.zone_for_section(3) == "firing"
    assert rig.kiln.zone_for_position(2.0) == "preheat"
    assert rig.kiln.zone_for_position(6.0) == "firing"
    assert rig.kiln.zone_for_position(100.0) == "cooling"
    with pytest.raises(NotFoundError):
        rig.kiln.zone_for_section(99)
    with pytest.raises(NotFoundError):
        rig.kiln.zone("soak")


def test_zone_targets_are_validated(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    zone = rig.kiln.set_target("firing", 1180.0, at=0.0)
    assert zone.target_c == 1180.0
    assert rig.kiln.target_of("firing") == 1180.0
    with pytest.raises(ValidationError):
        rig.kiln.set_target("firing", -5.0, at=1.0)
    with pytest.raises(NotFoundError):
        rig.kiln.set_target("soak", 900.0, at=1.0)


def test_grate_persist_writes_a_document_and_opens_the_gate(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.gates.satisfies(GATE_GRATE_PERSISTED) is False
    record = rig.kiln.persist_grate(at=5.0, author="op")
    assert record.revision == 1
    assert record.sections == 6
    assert rig.store.exists(GRATE_DOCUMENT) is True
    assert rig.gates.satisfies(GATE_GRATE_PERSISTED) is True
    assert rig.kiln.grate_matches_geometry() is True
    snapshot = rig.kiln.snapshot(now=15.0)
    assert snapshot["grate_age_s"] == 10.0


def test_grate_placement_survives_a_reopen(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kiln.persist_grate(at=5.0, author="op")
    reopened = KilnBody(
        PreGateRegistry(),
        JsonFileStore(tmp_path),
        zones=ZONES,
        sections_per_zone=2,
        zone_length_m=4.0,
    )
    grate = reopened.grate()
    assert grate is not None
    assert grate.author == "op"
    assert grate.revision == 1


def test_section_extension_bumps_the_revision_and_clears_the_grate_gate(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kiln.persist_grate(at=5.0, author="op")
    extension = rig.kiln.extend_section(zone="firing", at=6.0, added_m=2.5, author="op")
    assert extension.revision == 2
    assert rig.kiln.revision == 2
    assert rig.kiln.zone("firing").length_m == 6.5
    assert rig.kiln.zone("firing").sections == 3
    assert rig.kiln.section_count() == 7
    assert rig.gates.satisfies(GATE_GRATE_PERSISTED) is False
    assert rig.kiln.grate_matches_geometry() is False
    assert [item.revision for item in rig.kiln.extensions()] == [2]
    with pytest.raises(ValidationError):
        rig.kiln.extend_section(zone="firing", at=7.0, added_m=0.0, author="op")


def test_zone_map_refresh_captures_the_section_mapping(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(NotFoundError):
        rig.zone_map.current()
    assert rig.zone_map.latest() is None
    assert rig.zone_map.is_stale(now=0.0) is True
    zone_map = rig.zone_map.refresh(at=10.0, author="op", max_lag_s=300.0)
    assert zone_map.generation == 1
    assert zone_map.geometry_revision == 1
    assert zone_map.section_count() == 6
    assert zone_map.zone_of_section(4) == "cooling"
    assert zone_map.zone_of_section(99) is None
    assert rig.zone_map.must_refresh(now=20.0) is False
    assert rig.zone_map.snapshot(now=20.0)["age_s"] == 10.0
    assert rig.store.exists(ZONE_MAP_DOCUMENT) is True


def test_zone_map_expiry_is_rejected(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.zone_map.refresh(at=0.0, author="op", max_lag_s=60.0)
    assert rig.zone_map.require_current(now=59.0).generation == 1
    with pytest.raises(GenerationExpired) as failure:
        rig.zone_map.require_current(now=61.0)
    assert failure.value.context["max_lag_s"] == 60.0
    assert rig.zone_map.is_stale(now=61.0) is True


def test_zone_map_must_be_refreshed_after_a_geometry_change(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.zone_map.refresh(at=10.0, author="op", max_lag_s=300.0)
    rig.kiln.extend_section(zone="cooling", at=20.0, added_m=1.0, author="op")
    with pytest.raises(GenerationMismatch) as failure:
        rig.zone_map.require_current(now=21.0)
    assert failure.value.context["current_revision"] == 2
    assert rig.zone_map.must_refresh(now=21.0) is True
    refreshed = rig.zone_map.refresh(at=22.0, author="op", max_lag_s=300.0)
    assert refreshed.generation == 2
    assert rig.zone_map.require_current(now=23.0).geometry_revision == 2
    assert [item.generation for item in rig.zone_map.history()] == [1, 2]


def test_zone_map_restores_from_ledger_records(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.zone_map.refresh(at=10.0, author="op", max_lag_s=300.0)
    rig.store.delete(ZONE_MAP_DOCUMENT)
    recovered = ZoneMapRegistry(JsonFileStore(tmp_path), rig.stream, rig.kiln)
    assert recovered.latest() is None
    restored = recovered.restore(rig.stream.committed())
    assert restored is not None and restored.generation == 1
    assert recovered.snapshot(now=11.0)["current"]["sections"]["preheat"] == [0, 1]
