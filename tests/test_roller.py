"""Roller start gate, drive ramp and speed calibration."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.errors import (
    DurabilityError,
    GenerationExpired,
    GenerationMismatch,
    InterlockActive,
    NotFoundError,
    StateConflict,
    ValidationError,
)
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.ledger.stream import EventStream
from kilnline.params.baseline import BaselineRegistry
from kilnline.roller.calibration import SpeedCalibrationRegistry
from kilnline.roller.drive import STATE_IDLE, STATE_RUNNING, RollerDrive
from kilnline.roller.gate import RollerStartGate
from kilnline.store.json_store import JsonFileStore

TEMP_KEY = "kiln.temp"


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.store = JsonFileStore(tmp_path)
        self.stream = EventStream(tmp_path / "ledger.jsonl", self.store)
        self.baselines = BaselineRegistry(self.store, self.stream)
        self.gates = PreGateRegistry()
        self.latches = LatchRegistry(default_hold_s=5.0)
        self.latches.declare("temp.over_temp")
        self.gate = RollerStartGate(self.gates, self.latches, blocking_latches=("temp.over_temp",))
        self.calibrations = SpeedCalibrationRegistry(self.baselines)
        self.drive = RollerDrive(self.latches, min_mpm=1.0, max_mpm=24.0, ramp_mpm_per_s=0.4)

    def persist_temp(self, *, at: float = 0.0, max_lag_s: float = 600.0) -> None:
        self.baselines.capture(
            TEMP_KEY,
            {"firing": 1180.0},
            captured_at=at,
            generation=1,
            author="op",
            max_lag_s=max_lag_s,
        )
        self.gate.mark_temp_persisted(at=at, detail="generation 1")

    def calibrate(self, *, at: float = 0.0, max_lag_s: float = 600.0) -> None:
        self.calibrations.capture(
            pulses_per_meter=120.0,
            at=at,
            generation=1,
            author="op",
            max_lag_s=max_lag_s,
        )
        self.gate.mark_calibrated(at=at, detail="generation 1")


def test_roller_start_requires_a_persisted_temperature_baseline(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.calibrate()
    with pytest.raises(DurabilityError) as failure:
        rig.gate.require(temp_baseline=None, calibration=rig.calibrations.latest(), now=1.0)
    assert failure.value.context["gate"] == rig.gate.temp_gate
    checks = rig.gate.preconditions(temp_baseline=None, calibration=rig.calibrations.latest(), now=1.0)
    assert [check.name for check in checks] == [rig.gate.temp_gate, rig.gate.calibration_gate, "temp.over_temp"]
    assert checks[0].satisfied is False


def test_expired_temperature_baseline_is_rejected(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.persist_temp(at=0.0, max_lag_s=50.0)
    rig.calibrate()
    rig.gate.require(
        temp_baseline=rig.baselines.latest(TEMP_KEY),
        calibration=rig.calibrations.latest(),
        now=40.0,
    )
    with pytest.raises(GenerationExpired) as failure:
        rig.gate.require(
            temp_baseline=rig.baselines.latest(TEMP_KEY),
            calibration=rig.calibrations.latest(),
            now=51.0,
        )
    assert failure.value.context["max_lag_s"] == 50.0


def test_roller_start_requires_a_speed_calibration(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.persist_temp()
    with pytest.raises(NotFoundError):
        rig.gate.require(temp_baseline=rig.baselines.latest(TEMP_KEY), calibration=None, now=1.0)
    rig.calibrations.capture(
        pulses_per_meter=120.0,
        at=0.0,
        generation=1,
        author="op",
        max_lag_s=30.0,
    )
    with pytest.raises(GenerationExpired):
        rig.gate.require(
            temp_baseline=rig.baselines.latest(TEMP_KEY),
            calibration=rig.calibrations.latest(),
            now=31.0,
        )


def test_roller_start_is_blocked_by_a_temperature_latch(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.persist_temp()
    rig.calibrate()
    rig.gate.require(
        temp_baseline=rig.baselines.latest(TEMP_KEY),
        calibration=rig.calibrations.latest(),
        now=1.0,
    )
    rig.latches.trip("temp.over_temp", "firing zone hot", at=1.0)
    with pytest.raises(InterlockActive) as failure:
        rig.gate.require(
            temp_baseline=rig.baselines.latest(TEMP_KEY),
            calibration=rig.calibrations.latest(),
            now=2.0,
        )
    assert failure.value.context["latch"] == "temp.over_temp"


def test_drive_ramps_towards_its_target_and_stops(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.drive.state == STATE_IDLE
    with pytest.raises(StateConflict):
        rig.drive.stop(at=0.0)
    state = rig.drive.start(at=0.0, target_mpm=12.0)
    assert state.state == STATE_RUNNING
    assert state.running is True
    rig.drive.advance(dt=10.0)
    assert rig.drive.speed_mpm == pytest.approx(4.0)
    rig.drive.advance(dt=20.0)
    assert rig.drive.speed_mpm == pytest.approx(12.0)
    assert rig.drive.at_target() is True
    with pytest.raises(StateConflict):
        rig.drive.start(at=30.0)
    rig.drive.stop(at=30.0, reason="batch done")
    rig.drive.advance(dt=40.0)
    assert rig.drive.state == STATE_IDLE
    assert rig.drive.snapshot().stop_reason == "batch done"


def test_drive_clamps_speed_and_reports_faults(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.drive.clamp_speed(0.0) == 1.0
    assert rig.drive.clamp_speed(99.0) == 24.0
    with pytest.raises(ValidationError):
        rig.drive.clamp_speed(float("nan"))
    rig.drive.start(at=0.0, target_mpm=12.0)
    faulted = rig.drive.fault("flame_lost", at=5.0)
    assert faulted.state == STATE_IDLE
    assert faulted.speed_mpm == 0.0
    assert rig.latches.is_tripped(rig.drive.latch_name) is True
    with pytest.raises(InterlockActive):
        rig.drive.start(at=6.0, target_mpm=12.0)


def test_speed_calibration_converts_pulses_and_speed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.calibrate()
    calibration = rig.calibrations.require_fresh(now=1.0)
    assert calibration.generation == 1
    assert calibration.pulses_per_s(12.0) == pytest.approx(24.0)
    assert calibration.speed_mpm(24.0) == pytest.approx(12.0)
    assert rig.calibrations.measure_speed_mpm(24.0, now=1.0) == pytest.approx(12.0)
    assert rig.calibrations.latest().as_dict()["pulses_per_meter"] == 120.0


def test_speed_calibration_must_match_the_current_generation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.calibrate()
    assert rig.calibrations.require_current(now=1.0, generation=1).generation == 1
    with pytest.raises(GenerationExpired):
        rig.calibrations.require_fresh(now=601.0)
    with pytest.raises(GenerationMismatch):
        rig.calibrations.require_current(now=1.0, generation=2)
    with pytest.raises(ValidationError):
        rig.calibrations.capture(
            pulses_per_meter=0.0,
            at=1.0,
            generation=1,
            author="op",
            max_lag_s=60.0,
        )
