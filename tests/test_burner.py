"""Ignition, shut-down and recovery ordering for the burner train."""

from __future__ import annotations

import pytest

from kilnline.burner.air import CombustionAirTrain
from kilnline.burner.gas import GasTrain
from kilnline.burner.ignition import IgnitionSequence
from kilnline.errors import InterlockActive, OrderingViolation, StateConflict, ValidationError
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry


class Rig:
    def __init__(self) -> None:
        self.gates = PreGateRegistry()
        self.latches = LatchRegistry(default_hold_s=5.0)
        self.air = CombustionAirTrain(
            self.gates,
            self.latches,
            min_pressure_kpa=2.5,
            spin_up_s=10.0,
            max_pressure_kpa=8.0,
        )
        self.gas = GasTrain(self.gates, self.latches, settle_s=5.0)
        self.ignition = IgnitionSequence(self.air, self.gas, self.latches, flame_proof_s=5.0)

    def establish_air(self) -> None:
        self.air.start(at=0.0)
        self.air.advance(dt=10.0, at=10.0)


def test_air_pressure_ramps_up_and_satisfies_its_gate() -> None:
    rig = Rig()
    assert rig.air.established is False
    assert rig.gates.satisfies(rig.air.gate_name) is False
    rig.air.start(at=0.0)
    rig.air.advance(dt=2.0, at=2.0)
    assert rig.air.pressure_kpa == pytest.approx(1.6)
    assert rig.air.established is False
    rig.air.advance(dt=8.0, at=10.0)
    assert rig.air.established is True
    assert rig.gates.satisfies(rig.air.gate_name) is True
    rig.air.stop(at=11.0)
    assert rig.gates.satisfies(rig.air.gate_name) is False
    with pytest.raises(InterlockActive):
        rig.air.require_established()


def test_air_train_refuses_to_start_twice() -> None:
    rig = Rig()
    rig.air.start(at=0.0)
    with pytest.raises(StateConflict):
        rig.air.start(at=1.0)
    with pytest.raises(ValidationError):
        rig.air.advance(dt=0.0, at=2.0)


def test_gas_cannot_open_before_the_air_gate_is_satisfied() -> None:
    rig = Rig()
    with pytest.raises(InterlockActive) as failure:
        rig.gas.open(at=0.0)
    assert failure.value.context["gate"] == rig.air.gate_name
    assert rig.gas.is_open is False
    rig.establish_air()
    rig.gas.open(at=10.0)
    assert rig.gas.is_open is True
    assert rig.gas.opened_at == 10.0


def test_gas_flow_settles_after_the_settle_period() -> None:
    rig = Rig()
    rig.establish_air()
    rig.gas.open(at=10.0)
    rig.gas.advance(dt=2.5, at=12.5)
    assert rig.gas.settled is False
    rig.gas.advance(dt=2.5, at=15.0)
    assert rig.gas.flow_nm3h == pytest.approx(120.0)
    assert rig.gas.settled is True
    assert rig.gates.satisfies(rig.gas.gate_name) is True
    rig.gas.close(at=16.0)
    assert rig.gates.satisfies(rig.gas.gate_name) is False
    with pytest.raises(StateConflict):
        rig.gas.close(at=17.0)


def test_ignition_requires_established_combustion_air() -> None:
    rig = Rig()
    with pytest.raises(OrderingViolation) as failure:
        rig.ignition.ignite(at=0.0)
    assert failure.value.context["stage"] == "air_established"
    assert failure.value.context["missing"] == ["air_established"]
    rig.air.start(at=0.0)
    with pytest.raises(OrderingViolation):
        rig.ignition.ignite(at=1.0)


def test_ignition_walks_every_stage_in_order() -> None:
    rig = Rig()
    rig.establish_air()
    result = rig.ignition.ignite(at=10.0)
    assert result["steps"] == ["air_established", "gas_open", "igniter_spark", "flame_confirmed"]
    assert result["next"] == "zone_map_refreshed"
    assert rig.ignition.lit is True
    assert rig.ignition.ready() is False
    with pytest.raises(StateConflict):
        rig.ignition.ignite(at=11.0)
    refreshed = rig.ignition.mark_zone_map_refreshed(at=12.0)
    assert refreshed["complete"] is True
    assert rig.ignition.ready() is True
    assert rig.ignition.progress()["complete"] is True


def test_zone_map_stage_cannot_complete_before_the_flame_is_proved() -> None:
    rig = Rig()
    rig.establish_air()
    with pytest.raises(OrderingViolation) as failure:
        rig.ignition.mark_zone_map_refreshed(at=10.0)
    assert failure.value.context["missing"] == ["flame_confirmed"]


def test_extinguish_closes_the_gas_before_stopping_the_air() -> None:
    rig = Rig()
    rig.establish_air()
    rig.ignition.ignite(at=10.0)
    result = rig.ignition.extinguish(at=12.0)
    assert result["steps"] == ["gas_closed", "air_stopped"]
    assert rig.gas.is_open is False
    assert rig.air.running is False
    assert rig.ignition.lit is False
    with pytest.raises(StateConflict):
        rig.ignition.extinguish(at=13.0)


def test_flame_loss_trips_the_latch_and_cuts_the_fuel() -> None:
    rig = Rig()
    rig.establish_air()
    rig.ignition.ignite(at=10.0)
    report = rig.ignition.flame_lost(at=20.0, reason="flame_proof_lost")
    assert rig.latches.is_tripped(rig.ignition.latch_name) is True
    assert rig.gas.is_open is False
    assert rig.air.running is True
    assert report["latch"]["reason"] == "flame_proof_lost"
    with pytest.raises(InterlockActive):
        rig.gas.open(at=21.0)


def test_recovery_refuses_to_run_without_an_alarm_reset() -> None:
    rig = Rig()
    rig.establish_air()
    rig.ignition.ignite(at=10.0)
    rig.ignition.flame_lost(at=20.0)
    with pytest.raises(OrderingViolation) as failure:
        rig.ignition.recover(at=21.0, now=21.0, alarm_reset=False)
    assert failure.value.context["stage"] == "alarm_reset"


def test_recovery_releases_the_latch_once_the_hold_has_run_out() -> None:
    rig = Rig()
    rig.establish_air()
    rig.ignition.ignite(at=10.0)
    rig.ignition.flame_lost(at=20.0)
    early = rig.ignition.recover(at=21.0, now=22.0, alarm_reset=True)
    assert early["released"] is False
    assert early["conditions_ok"] is True
    assert early["hold_remaining_s"] == pytest.approx(4.0)
    late = rig.ignition.recover(at=21.0, now=27.0, alarm_reset=True)
    assert late["released"] is True
    assert rig.latches.is_tripped(rig.ignition.latch_name) is False
    healthy = rig.ignition.recover(at=30.0, now=31.0, alarm_reset=True)
    assert healthy["released"] is False
    assert healthy["reason"] == "not_tripped"
