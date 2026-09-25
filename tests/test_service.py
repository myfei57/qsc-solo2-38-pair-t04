"""End-to-end contracts: durability, ordering, expiry, limits and recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.clock import ManualClock
from kilnline.config import Settings
from kilnline.console.service import ControlService
from kilnline.errors import (
    DurabilityError,
    DuplicateRecord,
    GenerationMismatch,
    InterlockActive,
    OrderingViolation,
    StateConflict,
)
from kilnline.ledger.replay import replay
from kilnline.params.confirmation import ConfirmationExpired
from kilnline.store.json_store import JsonFileStore

from conftest import BENCH_PROFILE, PREPARE_STEP_S

BATCH = "KB-20260101-D-0001"
CARS = ("CAR-20260101-001", "CAR-20260101-002")


def probe_id_for(service: ControlService, zone: str) -> str:
    return next(probe.probe_id for probe in service.probes.probes() if probe.zone == zone)


def test_prepare_opens_the_durability_gates_and_maps_the_zones(prepared: ControlService) -> None:
    assert prepared.ready() is True
    assert prepared.gates.satisfies("kiln.temp_persisted") is True
    assert prepared.gates.satisfies("kiln.grate_persisted") is True
    assert prepared.ignition.ready() is True
    assert prepared.zone_map.current().generation == 1
    assert prepared.baselines.latest("kiln.temp") is not None
    assert prepared.latest_snapshot() is not None
    assert prepared.audit.count("prepare") >= 1
    assert [zone.name for zone in prepared.kiln.zones()] == list(prepared.settings.zones)


def test_roller_start_is_refused_when_the_baseline_was_never_persisted(service: ControlService) -> None:
    service.calibrate_roller(pulses_per_meter=120.0, author="pytest")
    with pytest.raises(DurabilityError) as failure:
        service.start_rollers(target_mpm=12.0)
    assert failure.value.context["gate"] == "kiln.temp_persisted"
    assert service.roller.running is False


def test_glaze_requested_before_drying_is_an_ordering_violation(prepared: ControlService) -> None:
    prepared.calibrate_slurry(density_g_cm3=1.65, author="pytest")
    prepared.load_car(CARS[0])
    with pytest.raises(OrderingViolation) as failure:
        prepared.start_glazing(CARS[0], density_g_cm3=1.65)
    assert failure.value.context["stage"] == "dry"
    assert failure.value.context["car_id"] == CARS[0]


def test_feed_requested_before_the_grate_is_persisted_is_refused(service: ControlService) -> None:
    service.confirm_spacing(car_id=CARS[0], gap_mm=220.0)
    with pytest.raises(InterlockActive) as failure:
        service.feed_car(CARS[0])
    assert failure.value.context["gates"] == ["kiln.grate_persisted"]
    assert service.alarms.is_active("conv.feed_blocked") is True
    assert service.conveyor.entries() == []


def test_production_sequence_rejects_a_stage_before_its_prerequisites(prepared: ControlService) -> None:
    with pytest.raises(OrderingViolation) as failure:
        prepared.production.complete("feeding", at=prepared.now())
    assert failure.value.context["missing"] == ["glazing"]
    assert prepared.production.next_stage() == "drying"


def test_over_limit_reading_trips_the_latch_and_stops_the_rollers(prepared: ControlService) -> None:
    prepared.start_rollers(target_mpm=12.0)
    prepared.clock.advance(30.0)
    prepared.tick_once(30.0)
    assert prepared.roller.running is True
    probe = probe_id_for(prepared, "firing")
    prepared.probes.record(probe, 1400.0, at=prepared.now())
    report = prepared.tick_once(30.0)
    assert prepared.guard.over_temp_latch in report.latches_tripped
    assert prepared.guard.blocked() is True
    assert prepared.roller.running is False
    assert prepared.roller.snapshot().stop_reason == "temperature_interlock"
    assert prepared.alarms.is_active("temp.over_temp") is True
    assert prepared.alarms.is_active("roller.stopped") is True


def test_expired_baseline_trips_the_stale_latch_once_the_guard_is_armed(tmp_path: Path) -> None:
    clock = ManualClock()
    settings = (
        Settings.from_env({})
        .overrides(**{**BENCH_PROFILE, "baseline_max_lag_s": 60.0})
        .with_data_dir(tmp_path / "var")
    )
    service = ControlService(settings, clock, simulation=True)
    service.prepare(author="pytest", step=PREPARE_STEP_S)
    assert service.guard.blocked() is False
    clock.advance(120.0)
    service.tick_once(30.0)
    assert service.guard.stale_latch in service.guard.tripped()
    assert service.guard.blocked() is True
    assert service.alarms.is_active("temp.baseline_stale") is True


def test_run_batch_walks_the_whole_line_order(prepared: ControlService) -> None:
    result = prepared.run_batch(
        BATCH,
        cars=CARS,
        gap_mm=220.0,
        density_g_cm3=1.65,
        speed_mpm=12.0,
        work_order="WO-000001",
        step=PREPARE_STEP_S,
    )
    assert result["batch"]["status"] == "closed"
    assert result["batch"]["car_count"] == 2
    assert result["production"]["complete"] is True
    assert [entry["car_id"] for entry in result["entries"]] == list(CARS)
    assert result["roller"]["running"] is True
    assert prepared.batches.seen(BATCH) is True
    assert prepared.latest_snapshot().reason == f"batch:{BATCH}"
    assert prepared.audit.count("batch") >= 3


def test_run_batch_rejects_a_repeated_batch_code(prepared: ControlService) -> None:
    prepared.run_batch(
        BATCH,
        cars=CARS[:1],
        gap_mm=220.0,
        density_g_cm3=1.65,
        speed_mpm=12.0,
        step=PREPARE_STEP_S,
    )
    with pytest.raises(DuplicateRecord) as failure:
        prepared.batches.open(BATCH, at=prepared.now())
    assert failure.value.context["code"] == BATCH


def test_run_batch_requires_a_ready_line(service: ControlService) -> None:
    with pytest.raises(StateConflict) as failure:
        service.run_batch(
            BATCH,
            cars=CARS,
            gap_mm=220.0,
            density_g_cm3=1.65,
            speed_mpm=12.0,
            step=PREPARE_STEP_S,
        )
    assert failure.value.context["ready"] is False
    assert "kiln.grate_persisted" in failure.value.context["gates_closed"]


def test_restart_replays_the_ledger_and_restores_every_generation(
    prepared: ControlService,
    settings: Settings,
) -> None:
    prepared.publish_parameters({"peak_c": 1180.0}, author="lead")
    prepared.run_batch(
        BATCH,
        cars=CARS[:1],
        gap_mm=220.0,
        density_g_cm3=1.65,
        speed_mpm=12.0,
        step=PREPARE_STEP_S,
    )
    watermark = prepared.stream.watermark
    restarted = ControlService(settings, ManualClock(), simulation=True)
    assert restarted.recovery_report["to_watermark"] == watermark
    assert restarted.recovery_report["from_watermark"] == prepared.latest_snapshot().watermark
    assert restarted.params.generation == 1
    assert restarted.zone_map.generation == 1
    assert "kiln.temp" in restarted.recovery_report["restored_baselines"]
    assert restarted.batches.seen(BATCH) is True
    assert restarted.guard.blocked() is False


def test_uncommitted_records_stay_invisible_after_a_restart(
    prepared: ControlService,
    settings: Settings,
) -> None:
    prepared.stream.put("batch:KB-20260101-D-0009", {"staged": True}, written_at=prepared.now())
    assert prepared.stream.staged_count == 1
    restarted = ControlService(settings, ManualClock(), simulation=True)
    assert restarted.batches.seen("KB-20260101-D-0009") is False
    assert "batch:KB-20260101-D-0009" not in restarted.projection.values
    assert restarted.replay_ledger(after_watermark=0)["applied"]


def test_recovery_resumes_from_the_snapshot_watermark(prepared: ControlService) -> None:
    snapshot = prepared.latest_snapshot()
    assert snapshot is not None
    outcome = replay(prepared.stream, after_watermark=snapshot.watermark)
    assert outcome.from_watermark == snapshot.watermark
    assert all(sequence > snapshot.watermark for sequence in outcome.applied)


def test_commit_and_rollback_drive_the_watermark(prepared: ControlService) -> None:
    prepared.stream.put("probe:check", {"ok": True}, written_at=prepared.now())
    assert prepared.stream.staged_count == 1
    staged = prepared.staged_records()
    assert [record["key"] for record in staged] == ["probe:check"]
    committed = prepared.commit_ledger()
    assert committed["watermark"] == committed["before"] + 1
    assert prepared.stream.staged_count == 0
    prepared.stream.put("probe:second", {"ok": True}, written_at=prepared.now())
    rolled_back = prepared.rollback_ledger()
    assert rolled_back["dropped"] == [committed["watermark"] + 1]
    assert prepared.stream.staged_count == 0
    assert prepared.ledger_state()["integrity"]["committed_records"] == prepared.stream.watermark


def test_confirmation_slips_expire_on_the_service_timeline(prepared: ControlService) -> None:
    prepared.publish_parameters({"peak_c": 1180.0}, author="lead")
    slip = prepared.issue_confirmation(scope="batch_start", issued_by="lead")
    assert prepared.verify_confirmation(slip["token"], scope="batch_start")["scope"] == "batch_start"
    prepared.clock.advance(prepared.settings.confirmation_ttl_s + 10.0)
    with pytest.raises(ConfirmationExpired):
        prepared.verify_confirmation(slip["token"], scope="batch_start")


def test_section_extension_forces_a_zone_map_refresh(prepared: ControlService) -> None:
    prepared.extend_section(zone="firing", added_m=1.5, author="op")
    assert prepared.gates.satisfies("kiln.grate_persisted") is False
    with pytest.raises(GenerationMismatch):
        prepared.zone_map.require_current(now=prepared.now())
    assert prepared.alarms.is_active("kiln.geometry_changed") is True
    refreshed = prepared.refresh_zone_map(author="op")
    assert refreshed["generation"] == 2
    prepared.persist_grate(author="op")
    assert prepared.gates.satisfies("kiln.grate_persisted") is True


def test_alarm_registry_deduplicates_and_clears(prepared: ControlService) -> None:
    first = prepared.alarms.raise_alarm("manual.check", "look at the kiln", at=1.0, severity="warning")
    second = prepared.alarms.raise_alarm("manual.check", "still looking", at=2.0, severity="warning")
    assert second.count == 2
    assert second.raised_at == first.raised_at
    assert len(prepared.alarms.active()) == 1
    assert prepared.alarms.summary()["worst"] == "warning"
    prepared.alarms.acknowledge("manual.check", at=3.0)
    assert prepared.alarms.get("manual.check").acknowledged is True
    prepared.alarms.clear("manual.check", at=4.0)
    assert prepared.alarms.summary()["active"] == 0


def test_monitor_can_be_stepped_without_a_thread(prepared: ControlService) -> None:
    assert prepared.monitor.running() is False
    prepared.monitor.run_once()
    assert prepared.monitor.ticks == 1
    assert prepared.monitor.errors == 0
    snapshot = prepared.monitor.snapshot()
    assert snapshot["interval_s"] == prepared.settings.monitor_interval_s
    assert snapshot["last_error"] == ""


def test_health_reports_ok_and_the_watermark(prepared: ControlService) -> None:
    health = prepared.health()
    assert health["status"] == "ok"
    assert health["checks"] == {"store": True, "ledger": True, "audit": True}
    assert health["watermark"] == prepared.stream.watermark
    assert health["alarms"]["active"] == 0


def test_status_exposes_every_subsystem(prepared: ControlService) -> None:
    status = prepared.status()
    for key in (
        "zones",
        "curve",
        "ignition",
        "roller",
        "dryer",
        "glaze",
        "slurry",
        "conveyor",
        "batches",
        "production",
        "recovery",
        "gates",
        "latches",
        "probes",
        "baselines",
        "zone_map",
        "ledger",
        "rules",
        "monitor",
    ):
        assert key in status
    assert status["ready"] is True
    assert status["guard_armed"] is True
    assert prepared.gate_inventory()["open"]
    assert prepared.latch_states()["tripped"] == []


def test_zone_history_compares_current_state_with_an_earlier_moment(prepared: ControlService) -> None:
    keys = prepared.history_keys()
    assert f"zone:{prepared.settings.zones[0]}" in keys
    assert prepared.history.entry_count(f"zone:{prepared.settings.zones[0]}") > 0
    prepared.start_rollers(target_mpm=12.0)
    prepared.clock.advance(120.0)
    prepared.tick_once(30.0)
    as_of = prepared.now()
    prepared.clock.advance(600.0)
    prepared.tick_once(30.0)
    delta = prepared.compare_zone(prepared.settings.zones[0], as_of=as_of)
    assert "current" in delta
    assert "historical" in delta
    assert delta["key"] == f"zone:{prepared.settings.zones[0]}"


def test_data_directory_holds_every_durable_artefact(prepared: ControlService, settings: Settings) -> None:
    store = JsonFileStore(settings.data_dir)
    names = set(store.names())
    assert {"ledger-watermark.json", "state-snapshot.json", "baselines.json"}.issubset(names)
    assert (settings.data_dir / "ledger.jsonl").is_file()
    assert (settings.data_dir / "audit.jsonl").is_file()
    assert (settings.data_dir / "grate-placement.json").is_file()
