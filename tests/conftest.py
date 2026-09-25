"""Shared fixtures for the control service test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.clock import ManualClock
from kilnline.config import Settings
from kilnline.console.service import ControlService

# A bench profile: the same logic, but the ramp and dwell periods are short so a
# whole heat-up fits inside a test without touching a real clock.
BENCH_PROFILE = {
    "ramp_rate_c_per_min": 60.0,
    "air_spin_up_s": 4.0,
    "gas_settle_s": 2.0,
    "dry_min_duration_s": 10.0,
    "dry_rate_pct_per_s": 0.5,
    "latch_reset_hold_s": 5.0,
    "baseline_max_lag_s": 7200.0,
    "probe_calibration_max_age_s": 7200.0,
    "glaze_density_max_age_s": 3600.0,
    "roller_calibration_max_age_s": 7200.0,
    "zone_map_max_lag_s": 7200.0,
    "confirmation_ttl_s": 1800.0,
}

PREPARE_STEP_S = 30.0


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env({})
    return base.overrides(**BENCH_PROFILE).with_data_dir(tmp_path / "var")


@pytest.fixture()
def service(settings: Settings, clock: ManualClock) -> ControlService:
    return ControlService(settings, clock, simulation=True)


def advance(service: ControlService, clock: ManualClock, seconds: float, *, step: float = 1.0) -> int:
    """Advance the manual clock and tick the service once per step."""

    remaining = float(seconds)
    ticks = 0
    while remaining > 1e-9:
        quantum = min(step, remaining)
        clock.advance(quantum)
        service.tick_once(quantum)
        remaining -= quantum
        ticks += 1
    return ticks


def prepare_line(service: ControlService, *, step: float = PREPARE_STEP_S) -> ControlService:
    """Bring the line to ready and calibrate the roller, as the operator would."""

    service.prepare(author="pytest", step=step)
    service.calibrate_roller(pulses_per_meter=120.0, author="pytest")
    return service


@pytest.fixture()
def advance_clock():
    """Expose the tick helper without importing conftest from a test module."""

    return advance


@pytest.fixture()
def prepared(service: ControlService) -> ControlService:
    return prepare_line(service)
