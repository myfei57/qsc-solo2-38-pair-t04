"""Temperature measurement, curve tracking, control and overload protection."""

from __future__ import annotations

import pytest

from kilnline.errors import GenerationExpired, NotFoundError, ValidationError
from kilnline.interlock.latch import LatchRegistry
from kilnline.temp.controller import TemperatureController
from kilnline.temp.curve import (
    VERDICT_ABOVE,
    VERDICT_BELOW,
    VERDICT_WITHIN,
    CurvePoint,
    FiringCurve,
    compare_window,
    deviation,
    within_band,
)
from kilnline.temp.guard import TemperatureGuard
from kilnline.temp.probe import ProbeBank


def make_curve() -> FiringCurve:
    return FiringCurve(
        (CurvePoint(0.0, 20.0), CurvePoint(600.0, 620.0), CurvePoint(1200.0, 620.0)),
        ramp_rate_c_per_min=60.0,
    )


def test_curve_interpolates_between_points() -> None:
    curve = make_curve()
    assert curve.target_at(0.0) == 20.0
    assert curve.target_at(300.0) == 320.0
    assert curve.target_at(900.0) == 620.0
    assert curve.target_at(99_999.0) == 620.0
    assert curve.duration_s == 1200.0
    assert curve.peak_c == 620.0
    assert curve.slope_at(300.0) == pytest.approx(1.0)
    assert curve.slope_at(1500.0) == 0.0


def test_curve_definition_is_validated() -> None:
    with pytest.raises(ValidationError):
        FiringCurve((CurvePoint(0.0, 20.0),), ramp_rate_c_per_min=10.0)
    with pytest.raises(ValidationError):
        FiringCurve(
            (CurvePoint(10.0, 20.0), CurvePoint(10.0, 30.0)),
            ramp_rate_c_per_min=10.0,
        )
    with pytest.raises(ValidationError):
        FiringCurve((CurvePoint(0.0, 20.0), CurvePoint(10.0, 30.0)), ramp_rate_c_per_min=0.0)


def test_curve_limits_step_to_the_ramp_rate() -> None:
    curve = make_curve()
    assert curve.limited_step(100.0, 200.0, 1.0) == 101.0
    assert curve.limited_step(100.0, 200.0, 200.0) == 200.0
    assert curve.limited_step(200.0, 100.0, 1.0) == 199.0
    low, high = curve.window_at(300.0, band_c=5.0)
    assert (low, high) == (315.0, 325.0)


def test_window_comparison_reports_direction_and_magnitude() -> None:
    below = compare_window(1.0, low=2.0, high=4.0)
    above = compare_window(9.0, low=2.0, high=4.0)
    inside = compare_window(3.0, low=2.0, high=4.0)
    assert below.verdict == VERDICT_BELOW and below.deviation == 1.0
    assert above.verdict == VERDICT_ABOVE and above.deviation == 5.0
    assert inside.verdict == VERDICT_WITHIN and inside.within is True
    assert deviation(1.0, target=3.0, band=1.0) == 1.0
    assert deviation(9.0, target=3.0, band=1.0) == -5.0
    assert deviation(3.5, target=3.0, band=1.0) == 0.0
    assert within_band(3.5, target=3.0, band=1.0) is True
    with pytest.raises(ValidationError):
        compare_window(1.0, low=4.0, high=2.0)


def test_firing_profile_builds_heat_soak_and_cool() -> None:
    profile = FiringCurve.firing_profile(
        start_c=20.0,
        peak_c=620.0,
        ramp_rate_c_per_min=60.0,
        soak_s=300.0,
    )
    assert len(profile.points) == 4
    assert profile.peak_c == 620.0
    assert profile.target_at(profile.duration_s) == 20.0
    assert profile.duration_s == pytest.approx(600.0 + 300.0 + 600.0)


def test_probe_bank_averages_and_spreads_zone_readings() -> None:
    bank = ProbeBank(calibration_max_age_s=600.0)
    bank.register("tc-1", "firing")
    bank.register("tc-2", "firing")
    bank.calibrate("tc-1", at=0.0, by="op")
    bank.calibrate("tc-2", at=0.0, by="op")
    bank.record("tc-1", 1180.0, at=100.0)
    bank.record("tc-2", 1184.0, at=100.0)
    assert bank.zone_average("firing") == 1182.0
    assert bank.zone_spread("firing") == 4.0
    assert bank.latest("tc-1").value_c == 1180.0
    assert bank.latest("tc-1").age_seconds(110.0) == 10.0
    assert [reading.probe_id for reading in bank.readings(zone="firing")] == ["tc-1", "tc-2"]


def test_probe_bank_reports_missing_objects() -> None:
    bank = ProbeBank(calibration_max_age_s=600.0)
    with pytest.raises(NotFoundError):
        bank.probe("tc-1")
    bank.register("tc-1", "firing")
    with pytest.raises(NotFoundError):
        bank.latest("tc-1")
    with pytest.raises(NotFoundError):
        bank.zone_average("firing")
    with pytest.raises(NotFoundError):
        bank.zone_spread("firing")
    with pytest.raises(ValidationError):
        bank.register("   ", "firing")


def test_probe_calibration_expiry_is_reported() -> None:
    bank = ProbeBank(calibration_max_age_s=600.0)
    bank.register("tc-1", "firing")
    with pytest.raises(GenerationExpired):
        bank.require_fresh("tc-1", now=0.0)
    bank.calibrate("tc-1", at=100.0, by="op")
    assert bank.is_fresh("tc-1", now=700.0) is True
    assert bank.is_fresh("tc-1", now=701.0) is False
    assert bank.stale(now=701.0) == ["tc-1"]
    assert bank.zone_is_usable("firing", now=701.0) is False
    with pytest.raises(GenerationExpired):
        bank.require_fresh("tc-1", now=701.0)
    assert bank.calibration_age_s("tc-1", now=700.0) == 600.0
    assert bank.snapshot(now=700.0)["probes"][0]["fresh"] is True


def test_controller_ramp_limits_the_setpoint() -> None:
    controller = TemperatureController(band_c=3.0, ramp_rate_c_per_min=60.0)
    controller.set_target(620.0)
    first = controller.update(20.0, dt=10.0)
    assert first.ramp_target_c == 30.0
    assert controller.at_setpoint is False
    for _ in range(600):
        controller.update(20.0, dt=1.0)
    assert controller.ramp_target_c == 620.0
    assert controller.at_setpoint is True
    assert controller.snapshot()["target_c"] == 620.0


def test_controller_band_state_and_power_limits() -> None:
    controller = TemperatureController(band_c=3.0, ramp_rate_c_per_min=60.0)
    controller.update(20.0, dt=1.0)
    controller.set_target(100.0)
    hot = controller.update(500.0, dt=1.0)
    assert hot.band_state == VERDICT_ABOVE
    assert hot.heater_power == 0.0
    assert hot.at_target is False
    assert controller.in_band(500.0) is False
    assert TemperatureController.classify(10.0, band=3.0) == VERDICT_BELOW
    assert TemperatureController.classify(0.0, band=3.0) == VERDICT_WITHIN
    with pytest.raises(ValidationError):
        controller.update(100.0, dt=0.0)


def test_controller_integral_stays_within_its_limit() -> None:
    controller = TemperatureController(band_c=3.0, ramp_rate_c_per_min=60.0, integral_limit=5.0)
    controller.set_target(25.0)
    for _ in range(200):
        output = controller.update(20.0, dt=1.0)
    assert abs(output.integral) <= 5.0
    assert 0.0 <= output.heater_power <= 1.0
    assert controller.steps == 200
    controller.reset()
    assert controller.integral == 0.0
    assert controller.ramp_target_c == 25.0


def test_guard_trips_the_over_temperature_latch() -> None:
    latches = LatchRegistry(default_hold_s=30.0)
    guard = TemperatureGuard(latches, over_temp_margin_c=10.0, hold_s=30.0, band_c=3.0)
    report = guard.evaluate(
        zone="firing",
        value_c=640.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=True,
        now=0.0,
    )
    assert report["over_limit_c"] == 10.0
    assert report["tripped"] == [guard.over_temp_latch]
    assert guard.blocked() is True
    assert guard.over_limit_c(619.0, target_c=620.0) == 0.0


def test_guard_trips_when_the_baseline_or_probe_is_stale() -> None:
    latches = LatchRegistry(default_hold_s=30.0)
    guard = TemperatureGuard(latches, over_temp_margin_c=10.0, hold_s=30.0, band_c=3.0)
    report = guard.evaluate(
        zone="firing",
        value_c=620.0,
        target_c=620.0,
        baseline_fresh=False,
        probe_fresh=True,
        now=1.0,
    )
    assert report["tripped"] == [guard.stale_latch]
    assert latches.state(guard.stale_latch).reason == "baseline_stale"
    guard.evaluate(
        zone="firing",
        value_c=620.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=False,
        now=2.0,
    )
    assert latches.state(guard.stale_latch).reason == "probe_calibration_stale"
    assert guard.tripped() == [guard.stale_latch]


def test_guard_releases_only_after_reset_hold_and_clear() -> None:
    latches = LatchRegistry(default_hold_s=30.0)
    guard = TemperatureGuard(latches, over_temp_margin_c=10.0, hold_s=30.0, band_c=3.0)
    guard.evaluate(
        zone="firing",
        value_c=700.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=True,
        now=0.0,
    )
    assert guard.blocked() is True
    guard.evaluate(
        zone="firing",
        value_c=619.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=True,
        now=10.0,
    )
    assert guard.blocked() is True
    requested = guard.request_reset(at=20.0)
    assert [state.name for state in requested] == [guard.over_temp_latch]
    guard.evaluate(
        zone="firing",
        value_c=619.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=True,
        now=30.0,
    )
    assert guard.blocked() is True
    report = guard.evaluate(
        zone="firing",
        value_c=619.0,
        target_c=620.0,
        baseline_fresh=True,
        probe_fresh=True,
        now=60.0,
    )
    assert report["released"] == [guard.over_temp_latch]
    assert guard.blocked() is False
    assert guard.snapshot()["margin_c"] == 10.0
