"""Write semantics: append-only stream, commit watermark, rollback and tombstones."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.errors import LedgerCorruption, NotFoundError, WatermarkError
from kilnline.judge.query import QueryFilter, RecordQuery
from kilnline.ledger.records import KIND_PUT, LedgerRecord
from kilnline.ledger.replay import Projection, rebuild, replay
from kilnline.ledger.stream import WATERMARK_DOCUMENT, EventStream
from kilnline.store.json_store import JsonFileStore


def make_stream(tmp_path: Path, store: JsonFileStore | None = None) -> EventStream:
    files = store or JsonFileStore(tmp_path)
    return EventStream(tmp_path / "ledger.jsonl", files)


def test_appended_record_is_invisible_until_commit(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("kiln:zone", {"target_c": 1180.0}, written_at=1.0)
    assert stream.staged_count == 1
    assert stream.committed() == []
    assert stream.uncommitted()[0].sequence == 1
    assert stream.watermark == 0
    assert stream.resolve("kiln:zone") == (None, [])


def test_committed_records_become_visible(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("kiln:zone", {"target_c": 1180.0}, written_at=1.0)
    assert stream.commit(committed_at=2.0) == 1
    live, voided = stream.resolve("kiln:zone")
    assert live is not None and live.payload == {"target_c": 1180.0}
    assert voided == []
    assert stream.staged_count == 0


def test_partial_commit_publishes_only_the_prefix(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.put("b", {"n": 2}, written_at=2.0)
    stream.put("c", {"n": 3}, written_at=3.0)
    assert stream.commit_through(2, committed_at=4.0) == 2
    assert [record.key for record in stream.committed()] == ["a", "b"]
    assert [record.key for record in stream.uncommitted()] == ["c"]


def test_commit_cannot_move_the_watermark_backwards(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.put("b", {"n": 2}, written_at=2.0)
    stream.commit(committed_at=3.0)
    with pytest.raises(WatermarkError):
        stream.commit_through(1, committed_at=4.0)


def test_commit_beyond_the_last_record_is_rejected(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    with pytest.raises(WatermarkError):
        stream.commit_through(9, committed_at=2.0)


def test_rollback_drops_staged_records_and_keeps_the_committed_prefix(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    stream.put("b", {"n": 2}, written_at=3.0)
    stream.put("c", {"n": 3}, written_at=4.0)
    dropped = stream.rollback(removed_at=5.0)
    assert [record.key for record in dropped] == ["b", "c"]
    assert [record.key for record in stream.records()] == ["a"]
    assert stream.watermark == 1
    assert stream.rollback(removed_at=6.0) == []
    reopened = make_stream(tmp_path)
    assert [record.key for record in reopened.records()] == ["a"]


def test_restart_replays_committed_records_from_the_watermark(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    stream.put("b", {"n": 2}, written_at=3.0)
    stream.commit(committed_at=4.0)
    restarted = make_stream(tmp_path)
    assert restarted.watermark == 2
    assert [record.key for record in restarted.replay(after_watermark=1)] == ["b"]
    outcome = rebuild(restarted)
    assert outcome.applied == (1, 2)
    assert outcome.projection["values"] == {"a": {"n": 1}, "b": {"n": 2}}


def test_restart_does_not_see_uncommitted_records(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("kept", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    stream.put("lost", {"n": 2}, written_at=3.0)
    restarted = make_stream(tmp_path)
    assert [record.key for record in restarted.committed()] == ["kept"]
    assert restarted.latest_committed("lost") is None
    assert restarted.resolve("lost") == (None, [])


def test_replay_from_an_offset_returns_only_newer_records(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    for index in range(4):
        stream.put(f"k{index}", {"n": index}, written_at=float(index))
        stream.commit(committed_at=float(index) + 0.5)
    outcome = replay(stream, after_watermark=2)
    assert outcome.from_watermark == 2
    assert outcome.applied == (3, 4)
    assert outcome.to_watermark == 4


def test_tombstone_requires_a_committed_target(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    with pytest.raises(WatermarkError):
        stream.append_tombstone(1, written_at=2.0, reason="staged target")
    with pytest.raises(NotFoundError):
        stream.append_tombstone(99, written_at=2.0)


def test_tombstone_hides_its_target_only_while_it_is_the_live_value(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    stream.append_tombstone(1, written_at=3.0, reason="voided")
    stream.commit(committed_at=4.0)
    assert stream.resolve("a") == (None, [1])
    outcome = rebuild(stream)
    assert outcome.projection["values"] == {}
    assert outcome.voided == (1,)

    stream.put("a", {"n": 2}, written_at=5.0)
    stream.commit(committed_at=6.0)
    live, voided = stream.resolve("a")
    assert live is not None and live.payload == {"n": 2}
    # The voided sequence stays on the record; it is simply no longer the live value.
    assert voided == [1]
    outcome = rebuild(stream)
    assert outcome.projection["values"] == {"a": {"n": 2}}
    assert outcome.voided == (1,)


def test_tombstoning_an_older_record_keeps_the_newer_write(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    stream.put("a", {"n": 2}, written_at=3.0)
    stream.commit(committed_at=4.0)
    stream.append_tombstone(1, written_at=5.0, reason="void the first write")
    stream.commit(committed_at=6.0)
    live, voided = stream.resolve("a")
    assert live is not None and live.payload == {"n": 2}
    assert voided == []
    projection = Projection()
    for record in stream.committed():
        projection.apply(record)
    assert projection.get("a") == {"n": 2}


def test_record_checksum_mismatch_is_reported_as_corruption(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    tampered = LedgerRecord.from_dict({**stream.records()[0].as_dict(), "payload": {"n": 7}})
    assert tampered.recompute_matches() is False
    stream.path.write_text(tampered.to_line(), encoding="utf-8")
    with pytest.raises(LedgerCorruption):
        make_stream(tmp_path)


def test_truncated_final_line_is_dropped_on_recovery(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("kept", {"n": 1}, written_at=1.0)
    stream.commit(committed_at=2.0)
    with open(stream.path, "a", encoding="utf-8") as handle:
        handle.write('{"sequence":2,"key":"torn","kind":"put"')
    recovered = make_stream(tmp_path)
    assert recovered.truncated_tail is True
    assert [record.key for record in recovered.records()] == ["kept"]
    assert recovered.watermark == 1


def test_watermark_ahead_of_the_stored_records_is_corruption(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    stream = EventStream(tmp_path / "ledger.jsonl", store)
    stream.put("a", {"n": 1}, written_at=1.0)
    store.write(
        WATERMARK_DOCUMENT,
        {"watermark": 5, "last_sequence": 5, "updated_at": 9.0},
        written_at=9.0,
    )
    with pytest.raises(LedgerCorruption):
        make_stream(tmp_path)


def test_query_only_returns_committed_records(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0, generation=1)
    stream.commit(committed_at=2.0)
    stream.put("b", {"n": 2}, written_at=3.0, generation=2)
    query = RecordQuery(stream)
    assert [record.key for record in query.run(QueryFilter())] == ["a"]
    assert query.count(QueryFilter(key="a")) == 1
    assert query.count(QueryFilter(key="b")) == 0
    assert query.latest(QueryFilter()) is not None
    assert query.keys(QueryFilter()) == ["a"]


def test_stream_verifies_sequence_and_watermark_consistency(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("a", {"n": 1}, written_at=1.0, generation=3, reason="test")
    stream.commit(committed_at=2.0)
    report = stream.verify()
    assert report["records"] == 1
    assert report["committed_records"] == 1
    assert report["staged_records"] == 0
    assert report["watermark"] == 1
    assert report["truncated_tail"] is False
    assert stream.record_at(1).kind == KIND_PUT
