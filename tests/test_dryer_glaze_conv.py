"""Drying, glazing and kiln entry: the wet-process half of the line order."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.conv.line import ConveyorLine
from kilnline.conv.spacing import GAP_OK, GAP_TOO_CLOSE, GAP_TOO_FAR, EntryWindow
from kilnline.dryer.chamber import STATE_DRYING, DryerBank
from kilnline.errors import (
    DuplicateRecord,
    GenerationExpired,
    GenerationMismatch,
    InterlockActive,
    NotFoundError,
    OrderingViolation,
    StateConflict,
    ThresholdExceeded,
    ValidationError,
)
from kilnline.glaze.slurry import SlurryStation
from kilnline.glaze.station import GlazeStation
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.kiln.zones import GATE_GRATE_PERSISTED
from kilnline.ledger.stream import EventStream
from kilnline.params.baseline import BaselineRegistry
from kilnline.store.json_store import JsonFileStore

CAR = "CAR-20260101-001"


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.store = JsonFileStore(tmp_path)
        self.stream = EventStream(tmp_path / "ledger.jsonl", self.store)
        self.baselines = BaselineRegistry(self.store, self.stream)
        self.gates = PreGateRegistry()
        self.latches = LatchRegistry(default_hold_s=5.0)
        self.gates.declare(GATE_GRATE_PERSISTED, description="grate placement on disk")
        self.dryer = DryerBank(
            self.gates,
            target_moisture_pct=1.5,
            rate_pct_per_s=0.5,
            min_duration_s=10.0,
        )
        self.slurry = SlurryStation(
            self.baselines,
            low=1.55,
            high=1.75,
            max_age_s=300.0,
        )
        self.glaze = GlazeStation(self.gates, self.dryer, self.slurry)
        self.conveyor = ConveyorLine(
            self.gates,
            self.latches,
            EntryWindow(min_mm=180.0, max_mm=260.0),
            speed_m_per_s=0.2,
        )

    def dry(self, car_id: str = CAR, *, at: float = 0.0) -> None:
        self.dryer.load(car_id, at=at)
        self.dryer.advance(dt=20.0)
        self.dryer.complete(car_id, at=at + 20.0)

    def calibrate_slurry(self, *, density: float = 1.65, at: float = 0.0, max_lag_s: float = 300.0) -> None:
        self.slurry.calibrate(
            density_g_cm3=density,
            at=at,
            generation=1,
            author="op",
            max_lag_s=max_lag_s,
        )


def test_dryer_requires_the_dwell_before_completion(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dryer.load(CAR, at=0.0)
    assert rig.dryer.run(CAR).state == STATE_DRYING
    rig.dryer.advance(dt=5.0)
    with pytest.raises(OrderingViolation) as failure:
        rig.dryer.complete(CAR, at=5.0)
    assert failure.value.context["required_s"] == 10.0
    assert rig.dryer.active()[0].car_id == CAR


def test_dryer_rejects_completion_above_the_moisture_target(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dryer.load(CAR, at=0.0, initial_moisture_pct=12.0)
    rig.dryer.advance(dt=12.0)
    with pytest.raises(ThresholdExceeded) as failure:
        rig.dryer.complete(CAR, at=12.0)
    assert failure.value.context["moisture_pct"] > failure.value.context["target_moisture_pct"]
    rig.dryer.advance(dt=10.0)
    assert rig.dryer.complete(CAR, at=22.0).dry is True


def test_dryer_rejects_duplicate_carriers_and_unknown_ones(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dryer.load(CAR, at=0.0)
    with pytest.raises(DuplicateRecord):
        rig.dryer.load(CAR, at=1.0)
    with pytest.raises(NotFoundError):
        rig.dryer.run("CAR-unknown")
    with pytest.raises(NotFoundError):
        rig.dryer.complete("CAR-unknown", at=1.0)
    with pytest.raises(ValidationError):
        rig.dryer.load("  ", at=1.0)
    with pytest.raises(ValidationError):
        rig.dryer.load("CAR-dry", at=1.0, initial_moisture_pct=1.0)


def test_dryer_unload_requires_a_dry_carrier(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dryer.load(CAR, at=0.0)
    with pytest.raises(StateConflict):
        rig.dryer.unload(CAR, at=1.0)
    rig.dryer.advance(dt=20.0)
    rig.dryer.complete(CAR, at=20.0)
    assert rig.dryer.require_dry(CAR).dry is True
    assert rig.dryer.completed()[0].car_id == CAR
    assert rig.gates.satisfies(rig.dryer.gate_name) is True
    rig.dryer.unload(CAR, at=30.0)
    assert rig.gates.satisfies(rig.dryer.gate_name) is False


def test_glaze_rejects_a_carrier_that_is_not_dry(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.calibrate_slurry()
    rig.dryer.load(CAR, at=0.0)
    with pytest.raises(OrderingViolation) as failure:
        rig.glaze.start(
            CAR,
            at=5.0,
            now=5.0,
            density_g_cm3=1.65,
            parameter_generation=1,
        )
    assert failure.value.context["stage"] == "dry"


def test_glaze_requires_a_fresh_calibration(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dry()
    with pytest.raises(NotFoundError):
        rig.glaze.start(CAR, at=21.0, now=21.0, density_g_cm3=1.65, parameter_generation=1)
    rig.calibrate_slurry(at=0.0, max_lag_s=30.0)
    with pytest.raises(GenerationExpired):
        rig.glaze.start(CAR, at=21.0, now=31.0, density_g_cm3=1.65, parameter_generation=1)


def test_glaze_rejects_an_out_of_band_density(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dry()
    rig.calibrate_slurry()
    with pytest.raises(ThresholdExceeded) as failure:
        rig.glaze.start(CAR, at=21.0, now=21.0, density_g_cm3=1.92, parameter_generation=1)
    assert failure.value.context["verdict"] == "above"
    assert rig.slurry.require_in_band(1.60).within is True
    assert rig.slurry.in_band(1.40) is False
    assert rig.slurry.classify(1.40).deviation == pytest.approx(0.15)


def test_glaze_rejects_a_superseded_calibration_generation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dry()
    rig.calibrate_slurry()
    with pytest.raises(GenerationMismatch):
        rig.glaze.start(CAR, at=21.0, now=21.0, density_g_cm3=1.65, parameter_generation=2)


def test_glaze_accepts_a_dry_carrier_with_a_fresh_calibration(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dry()
    rig.calibrate_slurry()
    run = rig.glaze.start(CAR, at=21.0, now=21.0, density_g_cm3=1.65, parameter_generation=1)
    assert run.reference_generation == 1
    assert run.density_g_cm3 == 1.65
    assert rig.gates.satisfies(rig.glaze.gate_name) is True
    assert rig.slurry.latest().in_band is True
    with pytest.raises(StateConflict):
        rig.glaze.start(CAR, at=22.0, now=22.0, density_g_cm3=1.65, parameter_generation=1)
    finished = rig.glaze.finish(CAR, at=25.0)
    assert finished.state == "finished"
    assert rig.glaze.coated() == []


def test_spacing_must_sit_inside_the_entry_window(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.conveyor.window.width_mm == 80.0
    with pytest.raises(ThresholdExceeded) as failure:
        rig.conveyor.confirm_spacing(car_id=CAR, gap_mm=120.0, at=0.0)
    assert failure.value.context["verdict"] == GAP_TOO_CLOSE
    verdict = rig.conveyor.confirm_spacing(car_id=CAR, gap_mm=220.0, at=1.0)
    assert verdict.verdict == GAP_OK
    assert rig.conveyor.pending() == CAR
    assert rig.gates.satisfies(rig.conveyor.spacing_gate) is True


def test_gap_plan_reports_both_failure_directions(tmp_path: Path) -> None:
    window = EntryWindow(min_mm=180.0, max_mm=260.0)
    plan = window.plan([100.0, 220.0, 400.0], prefix="CAR-")
    assert [item.verdict for item in plan] == [GAP_TOO_CLOSE, GAP_OK, GAP_TOO_FAR]
    assert [item.car_id for item in plan] == ["CAR-1", "CAR-2", "CAR-3"]
    assert plan[0].deviation == 80.0
    assert window.evaluate(220.0).ok is True
    with pytest.raises(ValidationError):
        EntryWindow(min_mm=260.0, max_mm=180.0)


def test_feed_requires_the_grate_placement_on_disk(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.conveyor.confirm_spacing(car_id=CAR, gap_mm=220.0, at=0.0)
    with pytest.raises(InterlockActive) as failure:
        rig.conveyor.feed(car_id=CAR, at=1.0)
    assert failure.value.context["gates"] == [GATE_GRATE_PERSISTED]
    rig.gates.satisfy(GATE_GRATE_PERSISTED, at=1.0, detail="revision 1")
    record = rig.conveyor.feed(car_id=CAR, at=2.0)
    assert record.car_id == CAR
    assert record.gap_mm == 220.0
    assert rig.conveyor.entries()[0].fed_at == 2.0
    assert rig.gates.satisfies(rig.conveyor.spacing_gate) is False


def test_feed_requires_the_spacing_to_be_confirmed_first(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.gates.satisfy(GATE_GRATE_PERSISTED, at=0.0)
    with pytest.raises(StateConflict):
        rig.conveyor.feed(car_id=CAR, at=1.0)
    rig.conveyor.confirm_spacing(car_id="CAR-20260101-002", gap_mm=220.0, at=2.0)
    with pytest.raises(StateConflict):
        rig.conveyor.feed(car_id=CAR, at=3.0)
    rig.conveyor.advance(dt=10.0)
    assert rig.conveyor.position_m == pytest.approx(2.0)
    rig.conveyor.fault("jam", at=4.0)
    assert rig.latches.is_tripped(rig.conveyor.latch_name) is True
