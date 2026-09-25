"""Durable store, audit trail and snapshot behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kilnline.errors import NotFoundError, PersistenceError, ValidationError
from kilnline.store.audit import AuditLog
from kilnline.store.json_store import JsonFileStore
from kilnline.store.snapshot import SnapshotStore


def test_atomic_write_and_read_round_trip(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    document = store.write("temperature-baseline", {"zone": "main", "target_c": 1180.0}, written_at=1000.0)
    assert document.revision == 1
    assert store.exists("temperature-baseline")
    loaded = store.read("temperature-baseline")
    assert loaded.data == {"zone": "main", "target_c": 1180.0}
    assert loaded.written_at == 1000.0
    assert not list(tmp_path.glob("*.tmp"))


def test_revision_increments_on_rewrite(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    store.write("doc", {"value": 1}, written_at=1.0)
    second = store.write("doc", {"value": 2}, written_at=2.0)
    assert second.revision == 2
    assert store.read("doc").data == {"value": 2}


def test_checksum_mismatch_is_detected(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    store.write("doc", {"value": 1}, written_at=1.0)
    target = store.path_for("doc")
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["body"]["value"] = 99
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PersistenceError):
        store.read("doc")


def test_missing_document_raises_not_found(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    with pytest.raises(NotFoundError):
        store.read("absent")
    assert store.read_or_none("absent") is None


def test_document_name_with_separator_is_rejected(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    with pytest.raises(ValidationError):
        store.write("../escape", {}, written_at=1.0)
    with pytest.raises(ValidationError):
        store.write("", {}, written_at=1.0)


def test_delete_and_list_names(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    store.write("a", {}, written_at=1.0)
    store.write("b", {}, written_at=1.0)
    assert store.names() == ["a.json", "b.json"]
    store.delete("a")
    assert store.names() == ["b.json"]


def test_snapshot_store_keeps_bounded_history(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    snapshots = SnapshotStore(store, history_limit=3)
    with pytest.raises(NotFoundError):
        snapshots.require_latest()
    first = snapshots.capture({"state": "idle"}, captured_at=1.0, reason="init", watermark=0)
    second = snapshots.capture(
        {"state": "heating"},
        captured_at=2.0,
        reason="prepare",
        watermark=4,
        generation=1,
    )
    snapshots.capture({"state": "ready"}, captured_at=3.0, reason="ready", watermark=6)
    snapshots.capture({"state": "running"}, captured_at=4.0, reason="run", watermark=8)
    assert first.revision == 1
    assert second.watermark == 4
    assert second.generation == 1
    latest = snapshots.require_latest()
    assert latest.state == {"state": "running"}
    assert [item.revision for item in snapshots.history()] == [2, 3, 4]
    assert snapshots.state_since("prepare") == {"state": "heating"}


def test_audit_log_sequences_and_queries(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("sequence", "prepare", 10.0, zone="firing")
    log.record("speed", "rolled back", 20.0, target=12.0)
    log.record("sequence", "start", 30.0)
    assert log.sequence == 3
    assert [record.sequence for record in log.tail(2)] == [2, 3]
    assert log.count("sequence") == 2
    assert [record.message for record in log.since(20.0)] == ["rolled back", "start"]
    assert log.categories() == ["sequence", "speed"]
    assert log.messages("sequence") == ["prepare", "start"]


def test_audit_log_sequence_continues_after_reload(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("sequence", "one", 1.0)
    reloaded = AuditLog(path)
    reloaded.record("sequence", "two", 2.0)
    assert [record.sequence for record in reloaded.read_all()] == [1, 2]
    sink: list = []
    assert [record.sequence for record in reloaded.replay_into(sink, since_sequence=1)] == [2]
