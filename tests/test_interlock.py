"""State machine and interlock primitives: latches, gates and stage order."""

from __future__ import annotations

import pytest

from kilnline.errors import (
    InterlockActive,
    NotFoundError,
    OrderingViolation,
    StateConflict,
    ValidationError,
)
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.interlock.sequencer import Stage, StageSequencer


def test_latch_trip_blocks_the_dependent_action() -> None:
    latches = LatchRegistry(default_hold_s=5.0)
    latches.declare("temp.over_temp", description="zone too hot")
    latches.require_clear("temp.over_temp")
    state = latches.trip("temp.over_temp", "firing zone hot", at=100.0)
    assert state.tripped is True
    assert state.trips == 1
    assert latches.tripped_names() == ["temp.over_temp"]
    with pytest.raises(InterlockActive) as failure:
        latches.require_clear("temp.over_temp")
    assert failure.value.context["latch"] == "temp.over_temp"
    assert failure.value.context["reason"] == "firing zone hot"
    assert latches.any_tripped("temp.over_temp")


def test_latch_trip_is_idempotent_while_it_stays_set() -> None:
    latches = LatchRegistry()
    latches.declare("temp.over_temp")
    first = latches.trip("temp.over_temp", "hot", at=100.0)
    second = latches.trip("temp.over_temp", "still hot", at=130.0)
    assert first.tripped_at == 100.0
    assert second.tripped_at == 100.0
    assert second.reason == "still hot"
    assert second.trips == 1
    assert latches.clear("temp.over_temp", at=140.0).tripped is False


def test_latch_reset_needs_a_tripped_latch() -> None:
    latches = LatchRegistry()
    latches.declare("temp.over_temp")
    with pytest.raises(StateConflict):
        latches.request_reset("temp.over_temp", at=10.0)
    with pytest.raises(NotFoundError):
        latches.state("temp.missing")
    with pytest.raises(ValidationError):
        latches.declare("  ")
    latches.trip("temp.over_temp", "hot", at=10.0)
    with pytest.raises(StateConflict):
        latches.declare("temp.over_temp")


def test_latch_release_waits_for_the_hold_time() -> None:
    latches = LatchRegistry(default_hold_s=30.0)
    latches.declare("temp.over_temp")
    latches.trip("temp.over_temp", "hot", at=0.0)
    latches.request_reset("temp.over_temp", at=10.0)
    assert latches.evaluate("temp.over_temp", now=20.0, conditions_ok=True).tripped is True
    state = latches.state("temp.over_temp")
    assert state.hold_remaining(20.0) == 20.0
    released = latches.evaluate("temp.over_temp", now=40.0, conditions_ok=True)
    assert released.tripped is False
    assert released.released_at == 40.0
    assert latches.tripped_names() == []


def test_latch_release_waits_for_the_cause_to_clear() -> None:
    latches = LatchRegistry(default_hold_s=5.0)
    latches.declare("temp.over_temp")
    latches.trip("temp.over_temp", "hot", at=0.0)
    latches.request_reset("temp.over_temp", at=1.0)
    assert latches.evaluate("temp.over_temp", now=60.0, conditions_ok=False).tripped is True
    assert latches.evaluate("temp.over_temp", now=61.0, conditions_ok=True).tripped is False


def test_latch_without_a_reset_request_never_releases() -> None:
    latches = LatchRegistry(default_hold_s=0.0)
    latches.declare("temp.over_temp")
    latches.trip("temp.over_temp", "hot", at=0.0)
    assert latches.evaluate("temp.over_temp", now=999.0, conditions_ok=True).tripped is True


def test_gate_require_reports_the_closed_gate() -> None:
    gates = PreGateRegistry()
    gates.declare("kiln.grate_persisted", description="grate placement on disk")
    assert gates.closed() == ["kiln.grate_persisted"]
    with pytest.raises(InterlockActive) as failure:
        gates.require("kiln.grate_persisted")
    assert failure.value.context["gate"] == "kiln.grate_persisted"
    gates.satisfy("kiln.grate_persisted", at=5.0, detail="revision 1")
    assert gates.satisfies("kiln.grate_persisted") is True
    assert gates.open_gates() == ["kiln.grate_persisted"]
    gates.unsatisfy("kiln.grate_persisted", at=9.0, detail="geometry changed")
    assert gates.satisfies("kiln.grate_persisted") is False
    assert gates.get("kiln.grate_persisted").detail == "geometry changed"
    with pytest.raises(NotFoundError):
        gates.require("kiln.unknown")


def test_gate_require_all_stops_at_the_first_missing_gate() -> None:
    gates = PreGateRegistry()
    gates.declare_many(["kiln.temp_persisted", "kiln.grate_persisted"], prefix="")
    gates.satisfy("kiln.temp_persisted", at=1.0)
    with pytest.raises(InterlockActive) as failure:
        gates.require_all(["kiln.temp_persisted", "kiln.grate_persisted"])
    assert failure.value.context["gate"] == "kiln.grate_persisted"
    gates.satisfy("kiln.grate_persisted", at=2.0)
    assert len(gates.require_all(["kiln.temp_persisted", "kiln.grate_persisted"])) == 2


def test_sequencer_rejects_a_stage_before_its_prerequisites() -> None:
    sequencer = StageSequencer(
        "production",
        (
            Stage("drying"),
            Stage("glazing", ("drying",)),
            Stage("feeding", ("glazing",)),
        ),
    )
    with pytest.raises(OrderingViolation) as failure:
        sequencer.complete("feeding", at=1.0)
    assert failure.value.context["missing"] == ["glazing"]
    assert failure.value.context["stage"] == "feeding"
    assert sequencer.next_stage() == "drying"
    sequencer.complete("drying", at=2.0)
    assert sequencer.next_stage() == "glazing"
    assert sequencer.complete("glazing", at=3.0).completed is True
    assert sequencer.completed() == ["drying", "glazing"]
    assert sequencer.pending() == ["feeding"]


def test_sequencer_rejects_duplicate_and_unknown_stages() -> None:
    sequencer = StageSequencer("production", (Stage("drying"), Stage("glazing", ("drying",))))
    sequencer.complete("drying", at=1.0)
    with pytest.raises(StateConflict):
        sequencer.complete("drying", at=2.0)
    with pytest.raises(NotFoundError):
        sequencer.complete("nothing", at=2.0)
    with pytest.raises(NotFoundError):
        sequencer.stage("nothing")


def test_sequencer_definition_is_validated() -> None:
    with pytest.raises(ValidationError):
        StageSequencer("empty", ())
    with pytest.raises(ValidationError):
        StageSequencer("duplicate", (Stage("a"), Stage("a")))
    with pytest.raises(ValidationError):
        StageSequencer("forward", (Stage("a", ("b",)), Stage("b")))
    with pytest.raises(ValidationError):
        StageSequencer("alien", (Stage("a", ("ghost",)),))


def test_sequencer_progress_and_reset() -> None:
    sequencer = StageSequencer("production", (Stage("drying"), Stage("glazing", ("drying",))))
    with pytest.raises(StateConflict):
        sequencer.reset(at=1.0)
    sequencer.complete_all(at=5.0)
    assert sequencer.progress()["complete"] is True
    sequencer.reset(at=9.0, reason="restart")
    assert sequencer.completed() == []
    assert sequencer.resets == 1
    assert sequencer.last_reset_reason == "restart"
