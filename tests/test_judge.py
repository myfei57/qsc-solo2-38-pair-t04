"""Judgement semantics: thresholds, batch uniqueness, history and queries."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.errors import (
    DuplicateRecord,
    NotFoundError,
    StateConflict,
    ThresholdExceeded,
    ValidationError,
)
from kilnline.judge.batch import BatchRegistry
from kilnline.judge.history import StateHistory
from kilnline.judge.query import QueryFilter, RecordQuery
from kilnline.judge.threshold import RuleBook, ThresholdRule
from kilnline.ledger.stream import EventStream
from kilnline.store.json_store import JsonFileStore

CODE_A = "KB-20260101-D-0001"
CODE_B = "KB-20260101-D-0002"


def make_stream(tmp_path: Path) -> EventStream:
    return EventStream(tmp_path / "ledger.jsonl", JsonFileStore(tmp_path))


def test_threshold_rule_classifies_below_within_and_above() -> None:
    rule = ThresholdRule(name="conv.entry_gap", low=180.0, high=260.0, unit="mm")
    assert rule.evaluate(100.0).verdict == "below"
    assert rule.evaluate(220.0).within is True
    assert rule.evaluate(400.0).deviation == 140.0
    assert rule.evaluate(400.0).as_dict()["rule"] == "conv.entry_gap"
    with pytest.raises(ValidationError):
        ThresholdRule(name="inverted", low=5.0, high=1.0)
    with pytest.raises(ValidationError):
        ThresholdRule(name="  ", low=1.0, high=5.0)


def test_rule_book_require_raises_with_the_band_in_context() -> None:
    book = RuleBook()
    book.declare("glaze.slurry_density", low=1.55, high=1.75, unit="g/cm3")
    assert book.names() == ["glaze.slurry_density"]
    assert book.require("glaze.slurry_density", 1.6).within is True
    with pytest.raises(ThresholdExceeded) as failure:
        book.require("glaze.slurry_density", 1.9)
    assert failure.value.context["low"] == 1.55
    assert failure.value.context["high"] == 1.75
    assert failure.value.context["unit"] == "g/cm3"
    with pytest.raises(NotFoundError):
        book.rule("nothing")


def test_rule_book_collects_violations_for_a_measurement_set() -> None:
    book = RuleBook()
    book.declare("roller.speed", low=1.0, high=24.0, unit="m/min")
    book.declare("burner.air_pressure", low=2.5, high=8.0, unit="kPa")
    verdicts = book.evaluate_all({"roller.speed": 12.0, "burner.air_pressure": 1.0})
    assert [item.rule for item in verdicts] == ["burner.air_pressure", "roller.speed"]
    violations = book.violations({"roller.speed": 30.0, "burner.air_pressure": 4.0})
    assert [item.rule for item in violations] == ["roller.speed"]
    assert book.snapshot()["roller.speed"]["high"] == 24.0
    assert len(book.snapshot()) == 2


def test_batch_codes_are_unique_per_stream(tmp_path: Path) -> None:
    batches = BatchRegistry(make_stream(tmp_path))
    opened = batches.open(CODE_A, at=10.0, work_order="WO-000001", car_count=3)
    assert opened.status == "open"
    assert batches.seen(CODE_A) is True
    assert batches.seen(CODE_B) is False
    with pytest.raises(DuplicateRecord) as failure:
        batches.open(CODE_A, at=12.0)
    assert failure.value.context["existing_status"] == "open"
    assert batches.record(CODE_A).work_order == "WO-000001"
    with pytest.raises(NotFoundError):
        batches.record(CODE_B)


def test_batch_close_is_recorded_once(tmp_path: Path) -> None:
    batches = BatchRegistry(make_stream(tmp_path))
    with pytest.raises(NotFoundError):
        batches.close(CODE_A, at=1.0)
    batches.open(CODE_A, at=10.0, car_count=3)
    closed = batches.close(CODE_A, at=20.0, car_count=4)
    assert closed.status == "closed"
    assert closed.closed_at == 20.0
    assert closed.car_count == 4
    with pytest.raises(StateConflict):
        batches.close(CODE_A, at=30.0)
    with pytest.raises(DuplicateRecord):
        batches.open(CODE_A, at=40.0)
    snapshot = batches.snapshot()
    assert snapshot["count"] == 1
    assert snapshot["open"] == []
    assert snapshot["closed"][0]["code"] == CODE_A


def test_batch_rejects_malformed_codes(tmp_path: Path) -> None:
    batches = BatchRegistry(make_stream(tmp_path))
    with pytest.raises(ValidationError):
        batches.open("KB-2026-1-D-1", at=1.0)
    with pytest.raises(ValidationError):
        batches.open("XX-20260101-D-0001", at=1.0)


def test_batch_code_may_be_reused_only_after_a_tombstone(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    batches = BatchRegistry(stream)
    batches.open(CODE_A, at=10.0, car_count=1)
    batches.close(CODE_A, at=20.0)
    batches.void(CODE_A, at=30.0, reason="rejected batch")
    assert batches.seen(CODE_A) is False
    assert batches.codes() == []
    reopened = batches.open(CODE_A, at=40.0, car_count=2)
    assert reopened.status == "open"
    assert batches.codes() == [CODE_A]


def test_batch_records_survive_a_stream_reopen(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    BatchRegistry(stream).open(CODE_A, at=10.0, car_count=1)
    reopened = BatchRegistry(make_stream(tmp_path))
    assert reopened.seen(CODE_A) is True
    assert reopened.open_batches()[0].code == CODE_A


def test_history_compare_reports_the_delta_between_now_and_a_moment() -> None:
    history = StateHistory(limit=10)
    history.record("zone:firing", 900.0, at=100.0, generation=1)
    history.record("zone:firing", 1180.0, at=200.0, generation=2)
    assert history.current("zone:firing").value == 1180.0
    assert history.historical("zone:firing", as_of=150.0).value == 900.0
    delta = history.compare("zone:firing", as_of=150.0)
    assert delta.current == 1180.0
    assert delta.historical == 900.0
    assert delta.delta == 280.0
    assert delta.changed is True
    assert delta.generation_historical == 1
    assert history.changed_since("zone:firing", as_of=150.0) is True
    assert history.entry_count("zone:firing") == 2


def test_history_reports_no_change_within_its_tolerance() -> None:
    history = StateHistory(limit=5, tolerance=1.0)
    history.record("glaze.density", 1.6500, at=1.0)
    history.record("glaze.density", 1.6505, at=2.0)
    assert history.changed_since("glaze.density", as_of=1.5) is False
    assert history.compare("glaze.density", as_of=1.5).changed is False


def test_history_bounds_and_missing_keys() -> None:
    history = StateHistory(limit=2)
    with pytest.raises(NotFoundError):
        history.current("nothing")
    for index in range(4):
        history.record("roller.speed", float(index), at=float(index))
    assert [entry.value for entry in history.history("roller.speed")] == [2.0, 3.0]
    with pytest.raises(NotFoundError):
        history.historical("roller.speed", as_of=-1.0)
    with pytest.raises(ValidationError):
        history.record("  ", 1.0, at=1.0)
    with pytest.raises(ValidationError):
        StateHistory(limit=0)
    assert history.keys() == ["roller.speed"]
    assert history.snapshot()["keys"]["roller.speed"]["entries"] == 2


def test_query_filter_narrows_by_key_kind_generation_and_time(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    stream.put("batch:a", {"n": 1}, written_at=10.0, generation=1)
    stream.commit(committed_at=11.0)
    stream.put("batch:b", {"n": 2}, written_at=20.0, generation=1)
    stream.commit(committed_at=21.0)
    stream.put("params:active", {"n": 3}, written_at=30.0, generation=2)
    stream.commit(committed_at=31.0)
    query = RecordQuery(stream)
    assert query.count(QueryFilter()) == 3
    assert query.count(QueryFilter(key="batch:a")) == 1
    assert query.count(QueryFilter(kinds=("put",))) == 3
    assert query.count(QueryFilter(min_generation=2)) == 1
    assert query.count(QueryFilter(since=15.0, until=25.0)) == 1
    assert query.keys(QueryFilter(after=1)) == ["batch:b", "params:active"]
    assert query.group_by_key(QueryFilter(kinds=("put",)))["batch:a"] == [1]
    assert query.latest(QueryFilter()).key == "params:active"


def test_query_filter_limit_keeps_the_newest_records(tmp_path: Path) -> None:
    stream = make_stream(tmp_path)
    for index in range(5):
        stream.put(f"k{index}", {"n": index}, written_at=float(index))
        stream.commit(committed_at=float(index) + 0.5)
    query = RecordQuery(stream)
    records = query.run(QueryFilter(limit=2))
    assert [record.key for record in records] == ["k3", "k4"]
    assert query.result(QueryFilter(limit=1)).count == 1


def test_query_filter_rejects_invalid_arguments() -> None:
    with pytest.raises(ValidationError):
        QueryFilter(after=-1)
    with pytest.raises(ValidationError):
        QueryFilter(limit=0)
    with pytest.raises(ValidationError):
        QueryFilter(kinds=("upsert",))
    with pytest.raises(ValidationError):
        QueryFilter(since=10.0, until=5.0)
    with pytest.raises(ValidationError):
        QueryFilter.from_mapping({"after": "soon"})
    with pytest.raises(ValidationError):
        QueryFilter.from_mapping({"limit": "many"})


def test_query_filter_parses_query_string_values() -> None:
    parsed = QueryFilter.from_mapping(
        {"key": "batch:a", "kind": "put,tombstone", "after": "3", "limit": "5", "since": "1.5"}
    )
    assert parsed.key == "batch:a"
    assert parsed.kinds == ("put", "tombstone")
    assert parsed.after == 3
    assert parsed.limit == 5
    assert parsed.since == 1.5
    assert parsed.as_dict()["kinds"] == ["put", "tombstone"]
    assert QueryFilter.from_mapping({}).after == 0
