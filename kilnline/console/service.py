"""Control service: one process that owns the whole line.

The service is the only place that knows the line order.  Each subsystem keeps
its own rules and refuses an out-of-order request on its own; the service adds
the cross-subsystem marks -- the production sequencer, the recovery sequencer,
the durable snapshot -- that turn individual refusals into one operator
workflow.

Timeline note: record stamps and expiry checks use the wall clock from the
injected clock, while latch holds use its monotonic side.  Tests inject a
:class:`~kilnline.clock.ManualClock`, so both advance together and every
scenario stays reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from kilnline import __version__
from kilnline.burner.air import CombustionAirTrain
from kilnline.burner.gas import GasTrain
from kilnline.burner.ignition import IgnitionSequence
from kilnline.clock import Clock, SystemClock
from kilnline.config import Settings
from kilnline.console.alarms import AlarmRegistry
from kilnline.console.monitor import MonitorThread
from kilnline.conv.line import ConveyorLine, EntryRecord
from kilnline.conv.spacing import EntryWindow
from kilnline.dryer.chamber import DryerBank
from kilnline.errors import InterlockActive, NotFoundError, StateConflict, ValidationError
from kilnline.glaze.slurry import DENSITY_VALUE, SlurryStation
from kilnline.glaze.station import GlazeStation
from kilnline.interlock.gate import PreGateRegistry
from kilnline.interlock.latch import LatchRegistry
from kilnline.interlock.sequencer import Stage, StageSequencer
from kilnline.judge.batch import BatchRecord, BatchRegistry
from kilnline.judge.history import StateHistory
from kilnline.judge.query import QueryFilter, RecordQuery
from kilnline.judge.threshold import RuleBook
from kilnline.kiln.mapping import ZoneMapRegistry
from kilnline.kiln.zones import GATE_GRATE_PERSISTED, KilnBody
from kilnline.ledger.replay import Projection, replay
from kilnline.ledger.stream import EventStream
from kilnline.params.baseline import BaselineRegistry
from kilnline.params.confirmation import ConfirmationBook
from kilnline.params.registry import ParameterRegistry
from kilnline.roller.calibration import SpeedCalibrationRegistry
from kilnline.roller.drive import RollerDrive
from kilnline.roller.gate import GATE_TEMP_PERSISTED, RollerStartGate
from kilnline.store.audit import AuditLog
from kilnline.store.json_store import JsonFileStore
from kilnline.store.snapshot import Snapshot, SnapshotStore
from kilnline.temp.controller import TemperatureController
from kilnline.temp.curve import FiringCurve
from kilnline.temp.guard import TemperatureGuard
from kilnline.temp.probe import ProbeBank

SERVICE_NAME = "kilnline"
TEMP_BASELINE_KEY = "kiln.temp"
AMBIENT_C = 20.0

ALARM_OVER_TEMP = "temp.over_temp"
ALARM_BASELINE_STALE = "temp.baseline_stale"
ALARM_ROLLER_STOPPED = "roller.stopped"
ALARM_FEED_BLOCKED = "conv.feed_blocked"
ALARM_FLAME_LOST = "burner.flame_lost"
ALARM_GEOMETRY_CHANGED = "kiln.geometry_changed"

PRODUCTION_STAGES: tuple[Stage, ...] = (
    Stage("drying", description="carriers dried to the moisture target"),
    Stage("glazing", ("drying",), description="glaze applied after drying"),
    Stage("feeding", ("glazing",), description="carriers admitted to the kiln"),
    Stage("rolling", ("feeding",), description="roller table turning"),
    Stage("firing", ("rolling",), description="zone temperatures following the curve"),
)

RECOVERY_STAGES: tuple[Stage, ...] = (
    Stage("alarm_reset", description="operator reset the alarm"),
    Stage("latch_release", ("alarm_reset",), description="latch released with the cause cleared"),
    Stage("burner_recovery", ("latch_release",), description="burner train ready again"),
)

RULE_AIR_PRESSURE = "burner.air_pressure"
RULE_ZONE_TEMPERATURE = "kiln.zone_temperature"
RULE_ENTRY_GAP = "conv.entry_gap"
RULE_SLURRY_DENSITY = "glaze.slurry_density"
RULE_ROLLER_SPEED = "roller.speed"


@dataclass(frozen=True)
class TickReport:
    """Summary of one control step."""

    at: float
    dt: float
    zone_temperatures: dict[str, float]
    heater_power: dict[str, float]
    in_band: bool
    latches_tripped: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "dt": self.dt,
            "zone_temperatures": dict(self.zone_temperatures),
            "heater_power": dict(self.heater_power),
            "in_band": self.in_band,
            "latches_tripped": list(self.latches_tripped),
        }


class ControlService:
    """Wires every subsystem into one line and exposes the operator commands."""

    def __init__(
        self,
        settings: Settings,
        clock: Clock | None = None,
        *,
        simulation: bool = False,
        start_monitor: bool = False,
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self.simulation = bool(simulation)
        self.started_at = self._now()

        data_dir: Path = settings.data_dir
        self.store = JsonFileStore(data_dir)
        self.audit = AuditLog(data_dir / "audit.jsonl")
        self.stream = EventStream(data_dir / "ledger.jsonl", self.store)
        self.snapshots = SnapshotStore(self.store, history_limit=20)

        self.gates = PreGateRegistry()
        self.latches = LatchRegistry(default_hold_s=settings.latch_reset_hold_s)
        self.rules = RuleBook()
        self.history = StateHistory(limit=settings.state_history_limit)
        self.alarms = AlarmRegistry(history_limit=settings.alarm_history_limit)
        self.records = RecordQuery(self.stream)

        self.params = ParameterRegistry(self.store, self.stream)
        self.confirmations = ConfirmationBook(self.store, ttl_s=settings.confirmation_ttl_s)
        self.baselines = BaselineRegistry(self.store, self.stream)
        self.speed_calibration = SpeedCalibrationRegistry(self.baselines)

        self.kiln = KilnBody(
            self.gates,
            self.store,
            zones=settings.zones,
            sections_per_zone=settings.sections_per_zone,
            zone_length_m=settings.zone_length_m,
            tolerance_c=settings.temp_band_c,
        )
        self.zone_map = ZoneMapRegistry(self.store, self.stream, self.kiln)

        self.probes = ProbeBank(calibration_max_age_s=settings.probe_calibration_max_age_s)
        for index, zone in enumerate(settings.zones):
            self.probes.register(f"tc-{zone}-{index + 1}", zone)
        self.curve = FiringCurve.firing_profile(
            start_c=AMBIENT_C,
            peak_c=1180.0,
            ramp_rate_c_per_min=settings.ramp_rate_c_per_min,
            soak_s=settings.soak_hold_s,
        )
        self.controllers: dict[str, TemperatureController] = {
            zone: TemperatureController(
                band_c=settings.temp_band_c,
                ramp_rate_c_per_min=settings.ramp_rate_c_per_min,
            )
            for zone in settings.zones
        }
        self.guard = TemperatureGuard(
            self.latches,
            over_temp_margin_c=settings.over_temp_margin_c,
            hold_s=settings.latch_reset_hold_s,
            band_c=settings.temp_band_c,
        )
        self.air = CombustionAirTrain(
            self.gates,
            self.latches,
            min_pressure_kpa=settings.air_min_pressure_kpa,
            spin_up_s=settings.air_spin_up_s,
        )
        self.gas = GasTrain(self.gates, self.latches, settle_s=settings.gas_settle_s)
        self.ignition = IgnitionSequence(
            self.air,
            self.gas,
            self.latches,
            flame_proof_s=settings.flame_proof_s,
        )
        self.roller = RollerDrive(
            self.latches,
            min_mpm=settings.roller_speed_min_mpm,
            max_mpm=settings.roller_speed_max_mpm,
            ramp_mpm_per_s=settings.roller_speed_ramp_mpm_per_s,
        )
        self.start_gate = RollerStartGate(
            self.gates,
            self.latches,
            blocking_latches=(self.guard.over_temp_latch, self.guard.stale_latch),
        )
        self.dryer = DryerBank(
            self.gates,
            target_moisture_pct=settings.dry_target_moisture_pct,
            rate_pct_per_s=settings.dry_rate_pct_per_s,
            min_duration_s=settings.dry_min_duration_s,
        )
        self.slurry = SlurryStation(
            self.baselines,
            low=settings.glaze_density_low,
            high=settings.glaze_density_high,
            max_age_s=settings.glaze_density_max_age_s,
        )
        self.glaze = GlazeStation(self.gates, self.dryer, self.slurry)
        self.conveyor = ConveyorLine(
            self.gates,
            self.latches,
            EntryWindow(min_mm=settings.conveyor_gap_min_mm, max_mm=settings.conveyor_gap_max_mm),
            speed_m_per_s=settings.conveyor_speed_m_per_s,
        )
        self.batches = BatchRegistry(self.stream)
        self.production = StageSequencer("production", PRODUCTION_STAGES)
        self.recovery = StageSequencer("recovery", RECOVERY_STAGES)
        self._declare_rules()

        self._zone_temperature: dict[str, float] = dict.fromkeys(settings.zones, AMBIENT_C)
        self._heat_gain_c_per_s = settings.ramp_rate_c_per_min / 60.0 * 2.0
        self._guard_armed = False
        self.projection = Projection()
        self.recovery_report: dict[str, Any] = {}
        self.monitor = MonitorThread(self.tick_once, self.clock, interval_s=settings.monitor_interval_s)
        self._recover()
        if start_monitor:
            self.monitor.start()

    def _declare_rules(self) -> None:
        self.rules.declare(
            RULE_AIR_PRESSURE,
            low=self.settings.air_min_pressure_kpa,
            high=self.settings.air_min_pressure_kpa * 4.0,
            unit="kPa",
            description="combustion air duct pressure",
        )
        self.rules.declare(
            RULE_ZONE_TEMPERATURE,
            low=AMBIENT_C,
            high=self.curve.peak_c + self.settings.over_temp_margin_c,
            unit="C",
            description="zone temperature inside the ramp and the over-temperature margin",
        )
        self.rules.declare(
            RULE_ENTRY_GAP,
            low=self.settings.conveyor_gap_min_mm,
            high=self.settings.conveyor_gap_max_mm,
            unit="mm",
            description="gap between carriers at the kiln mouth",
        )
        self.rules.declare(
            RULE_SLURRY_DENSITY,
            low=self.settings.glaze_density_low,
            high=self.settings.glaze_density_high,
            unit="g/cm3",
            description="glaze slurry density",
        )
        self.rules.declare(
            RULE_ROLLER_SPEED,
            low=self.settings.roller_speed_min_mpm,
            high=self.settings.roller_speed_max_mpm,
            unit="m/min",
            description="roller table speed",
        )

    def _recover(self) -> None:
        """Verify the stream, replay from the snapshot watermark and log it."""

        integrity = self.stream.verify()
        snapshot = self.snapshots.latest()
        if snapshot is None:
            from_watermark = 0
        else:
            self.projection = Projection.from_dict(snapshot.state.get("projection", {}))
            from_watermark = snapshot.watermark
        outcome = replay(self.stream, after_watermark=from_watermark, projection=self.projection)
        self.projection = Projection.from_dict(outcome.projection)
        committed = self.stream.committed()
        restored_baselines = self.baselines.restore(committed)
        restored_parameters = self.params.restore(committed)
        restored_map = self.zone_map.restore(committed)
        self._guard_armed = self.baselines.latest(TEMP_BASELINE_KEY) is not None
        self.recovery_report = {
            "integrity": integrity,
            "from_watermark": outcome.from_watermark,
            "to_watermark": outcome.to_watermark,
            "applied": list(outcome.applied),
            "voided": list(outcome.voided),
            "restored_baselines": restored_baselines,
            "restored_parameter_generation": None if restored_parameters is None else restored_parameters.generation,
            "restored_zone_map_generation": None if restored_map is None else restored_map.generation,
            "snapshot_revision": None if snapshot is None else snapshot.revision,
            "truncated_tail": bool(integrity["truncated_tail"]),
        }
        self.audit.record(
            "recovery",
            "control service started",
            self.started_at,
            from_watermark=outcome.from_watermark,
            to_watermark=outcome.to_watermark,
            applied=len(outcome.applied),
        )

    def _now(self) -> float:
        return float(self.clock.now())

    def now(self) -> float:
        """Wall-clock stamp used for records and expiry checks."""

        return self._now()

    def _mono(self) -> float:
        return float(self.clock.monotonic())

    def _step(self, step: float | None) -> float:
        if step is None:
            return float(self.settings.tick_interval_s)
        value = float(step)
        if value <= 0.0:
            raise ValidationError("control step must be positive", step=step)
        return value

    def health(self) -> dict[str, Any]:
        """Cheap liveness probe that also reports the durable artefacts."""

        integrity = self.stream.verify()
        checks = {
            "store": self.store.root.is_dir(),
            "ledger": integrity["watermark"] == self.stream.watermark
            and integrity["last_sequence"] == self.stream.last_sequence,
            "audit": self.audit.path.parent.is_dir(),
        }
        return {
            "status": "ok" if all(checks.values()) else "degraded",
            "service": SERVICE_NAME,
            "version": __version__,
            "checks": checks,
            "watermark": self.stream.watermark,
            "alarms": self.alarms.summary(),
        }

    def zone_temperatures(self) -> dict[str, float]:
        return {zone: self._zone_temperature[zone] for zone in self.settings.zones}

    def zone_readings(self) -> dict[str, float | None]:
        readings: dict[str, float | None] = {}
        for zone in self.settings.zones:
            try:
                readings[zone] = self.probes.zone_average(zone)
            except NotFoundError:
                readings[zone] = None
        return readings

    def zones_in_band(self) -> bool:
        for zone in self.settings.zones:
            controller = self.controllers[zone]
            if not controller.at_setpoint:
                return False
            if not controller.in_band(self._measured(zone)):
                return False
        return True

    def ready(self) -> bool:
        return (
            self.ignition.ready()
            and not self.guard.blocked()
            and self.gates.satisfies(GATE_TEMP_PERSISTED)
            and self.gates.satisfies(GATE_GRATE_PERSISTED)
            and self.zones_in_band()
        )

    def status(self) -> dict[str, Any]:
        baselines = {key: self.baselines.latest(key) for key in self.baselines.keys()}
        return {
            "service": SERVICE_NAME,
            "version": __version__,
            "at": self._now(),
            "simulation": self.simulation,
            "ready": self.ready(),
            "guard_armed": self._guard_armed,
            "watermark": self.stream.watermark,
            "staged_records": self.stream.staged_count,
            "parameter_generation": self.params.generation,
            "zones": [zone.as_dict() for zone in self.kiln.zones()],
            "zone_temperatures": self.zone_temperatures(),
            "zone_readings": self.zone_readings(),
            "controllers": {zone: controller.snapshot() for zone, controller in self.controllers.items()},
            "curve": self.curve.as_dict(),
            "ignition": self.ignition.snapshot(),
            "roller": self.roller.snapshot().as_dict(),
            "dryer": self.dryer.snapshot(),
            "glaze": self.glaze.snapshot(),
            "slurry": self.slurry.snapshot(now=self._now()),
            "conveyor": self.conveyor.snapshot(),
            "batches": self.batches.snapshot(),
            "production": self.production.progress(),
            "recovery": self.recovery.progress(),
            "gates": self.gates.snapshot(),
            "latches": self.latches.snapshot(),
            "alarms": self.alarms.summary(),
            "probes": self.probes.snapshot(now=self._now()),
            "baselines": {
                key: (None if baseline is None else baseline.as_dict())
                for key, baseline in baselines.items()
            },
            "zone_map": self.zone_map.snapshot(now=self._now()),
            "ledger": self.stream.watermark_state().as_dict(),
            "rules": self.rules.snapshot(),
            "monitor": self.monitor.snapshot(),
        }

    def ledger_state(self) -> dict[str, Any]:
        return {
            "watermark": self.stream.watermark_state().as_dict(),
            "integrity": self.stream.verify(),
            "committed": [
                {
                    "sequence": record.sequence,
                    "key": record.key,
                    "kind": record.kind,
                    "generation": record.generation,
                    "written_at": record.written_at,
                }
                for record in self.stream.committed()
            ],
            "staged": [record.sequence for record in self.stream.uncommitted()],
        }

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.audit.tail(limit)]

    def latest_snapshot(self) -> Snapshot | None:
        return self.snapshots.latest()

    def capture_snapshot(self, *, reason: str) -> Snapshot:
        return self.snapshots.capture(
            {
                "projection": self.projection.as_dict(),
                "zones": self.zone_temperatures(),
                "production": self.production.progress(),
                "gates": self.gates.open_gates(),
                "latches": self.latches.tripped_names(),
            },
            captured_at=self._now(),
            reason=reason,
            watermark=self.stream.watermark,
            generation=self.params.generation,
        )

    # ------------------------------------------------------------------ control

    def tick_once(self, dt: float | None = None) -> TickReport:
        """Advance every subsystem by one control step."""

        step = self._step(dt)
        now = self._now()
        mono = self._mono()
        self.air.advance(dt=step, at=now)
        self.gas.advance(dt=step, at=now)
        self.conveyor.advance(dt=step)
        self.roller.advance(dt=step)
        self.dryer.advance(dt=step)

        temperatures: dict[str, float] = {}
        power: dict[str, float] = {}
        tripped: list[str] = []
        for zone in self.kiln.zones():
            controller = self.controllers[zone.name]
            measured = self._measured(zone.name)
            output = controller.update(measured, dt=step)
            if self.simulation:
                self._apply_thermal(zone.name, step, output.heater_power)
            temperatures[zone.name] = measured
            power[zone.name] = output.heater_power
            self.history.record(
                f"zone:{zone.name}",
                measured,
                at=now,
                generation=self.params.generation,
            )
            if not self._guard_armed:
                continue
            report = self.guard.evaluate(
                zone=zone.name,
                value_c=measured,
                target_c=controller.ramp_target_c,
                baseline_fresh=self._baseline_fresh(),
                probe_fresh=self._probes_fresh(zone.name),
                now=mono,
            )
            for name in report["tripped"]:
                if name in tripped:
                    continue
                tripped.append(name)
                self._raise_interlock_alarm(name, zone=zone.name, at=now)
        if tripped and self.roller.running:
            self.roller.stop(at=now, reason="temperature_interlock")
            self.alarms.raise_alarm(
                ALARM_ROLLER_STOPPED,
                "roller table stopped by a temperature interlock",
                at=now,
                severity="major",
                source="roller",
            )
        return TickReport(
            at=now,
            dt=step,
            zone_temperatures=temperatures,
            heater_power=power,
            in_band=self.zones_in_band(),
            latches_tripped=tuple(tripped),
        )

    def run_until(
        self,
        predicate,
        *,
        label: str,
        limit_s: float,
        step: float | None = None,
    ) -> float:
        """Advance the clock and tick until ``predicate`` holds.

        ``step`` is how far the clock moves per outer iteration; the control
        core still runs on the configured tick interval, because a proportional
        controller on an integrating bench model is only stable while the loop
        period stays well below its own time constant.
        """

        quantum = self._step(step)
        inner = float(self.settings.tick_interval_s)
        elapsed = 0.0
        limit = float(limit_s)
        while elapsed < limit:
            block = min(quantum, limit - elapsed)
            consumed = 0.0
            while block - consumed > 1e-9:
                sub = min(inner, block - consumed)
                self.clock.sleep(sub)
                self.tick_once(sub)
                consumed += sub
            elapsed += block
            if predicate():
                return elapsed
        raise StateConflict(
            "condition was not reached in time",
            condition=str(label),
            limit_s=limit,
            next_stage=self.production.next_stage(),
        )

    def prepare(
        self,
        *,
        peak_c: float | None = None,
        author: str = "operator",
        limit_s: float = 7200.0,
        step: float | None = None,
    ) -> dict[str, Any]:
        """Commission the line: air, flame, heat, durable baseline, zone map."""

        if self.guard.blocked():
            raise InterlockActive(
                "a temperature interlock is active; the line cannot be prepared",
                latches=self.guard.tripped(),
            )
        quantum = max(self._step(step), 5.0)
        self.calibrate_probes(by=author)
        self.air.start(at=self._now())
        self.run_until(
            lambda: self.air.established,
            label="air_established",
            limit_s=max(60.0, self.settings.air_spin_up_s * 3.0),
            step=quantum,
        )
        self.ignition.ignite(at=self._now())
        self.run_until(
            lambda: self.gas.settled,
            label="gas_settled",
            limit_s=max(60.0, self.settings.gas_settle_s * 3.0),
            step=quantum,
        )
        target = self.curve.peak_c if peak_c is None else float(peak_c)
        for zone in self.settings.zones:
            self.kiln.set_target(zone, target, at=self._now())
            self.controllers[zone].set_target(target)
        self.run_until(self.zones_in_band, label="zones_in_band", limit_s=limit_s, step=quantum)
        baseline = self.persist_temperature_baseline(author=author)
        zone_map = self.refresh_zone_map(author=author)
        self.ignition.mark_zone_map_refreshed(at=self._now())
        grate = self.persist_grate(author=author)
        self._guard_armed = True
        self.capture_snapshot(reason="prepare")
        self.audit.record(
            "prepare",
            "line prepared",
            self._now(),
            target_c=target,
            baseline_generation=baseline["generation"],
            zone_map_generation=zone_map["generation"],
            grate_revision=grate["revision"],
        )
        return {
            "target_c": target,
            "baseline": baseline,
            "zone_map": zone_map,
            "grate": grate,
            "ignition": self.ignition.progress(),
            "ready": self.ready(),
        }

    def calibrate_probes(self, *, by: str, span_c: float = 0.0) -> list[dict[str, Any]]:
        at = self._now()
        calibrated = [
            self.probes.calibrate(probe.probe_id, at=at, by=by, span_c=span_c).as_dict()
            for probe in self.probes.probes()
        ]
        self.audit.record("probe", "probe bank calibrated", at, probes=len(calibrated), by=by)
        return calibrated

    def persist_temperature_baseline(self, *, author: str) -> dict[str, Any]:
        """Write the zone temperatures to disk, then open the roller gate."""

        at = self._now()
        values = {zone: round(self._measured(zone), 6) for zone in self.settings.zones}
        baseline = self.baselines.capture(
            TEMP_BASELINE_KEY,
            values,
            captured_at=at,
            generation=self.params.generation,
            author=author,
            max_lag_s=self.settings.baseline_max_lag_s,
            reason="temperature_baseline",
        )
        self.start_gate.mark_temp_persisted(at=at, detail=f"generation {baseline.generation}")
        self.audit.record(
            "baseline",
            "temperature baseline persisted",
            at,
            generation=baseline.generation,
            values=values,
        )
        return baseline.as_dict()

    def refresh_zone_map(self, *, author: str) -> dict[str, Any]:
        at = self._now()
        zone_map = self.zone_map.refresh(
            at=at,
            author=author,
            max_lag_s=self.settings.zone_map_max_lag_s,
        )
        self.audit.record(
            "zone_map",
            "zone map refreshed",
            at,
            generation=zone_map.generation,
            geometry_revision=zone_map.geometry_revision,
        )
        return zone_map.as_dict()

    def persist_grate(self, *, author: str) -> dict[str, Any]:
        at = self._now()
        record = self.kiln.persist_grate(at=at, author=author)
        self.audit.record("grate", "grate placement persisted", at, revision=record.revision)
        return record.as_dict()

    def extend_section(
        self,
        *,
        zone: str,
        added_m: float,
        author: str,
        sections: int = 1,
    ) -> dict[str, Any]:
        at = self._now()
        extension = self.kiln.extend_section(
            zone=zone,
            at=at,
            added_m=added_m,
            author=author,
            sections=sections,
        )
        self.alarms.raise_alarm(
            ALARM_GEOMETRY_CHANGED,
            f"zone {extension.zone} extended; the zone map and grate placement must be refreshed",
            at=at,
            severity="warning",
            source="kiln",
        )
        self.audit.record(
            "kiln",
            "section extended",
            at,
            zone=extension.zone,
            added_m=extension.added_m,
            revision=extension.revision,
        )
        return extension.as_dict()

    def ignite(self) -> dict[str, Any]:
        result = self.ignition.ignite(at=self._now())
        self.audit.record("burner", "burner lit", self._now(), steps=result["steps"])
        return result

    def extinguish(self, *, reason: str = "requested") -> dict[str, Any]:
        result = self.ignition.extinguish(at=self._now(), reason=reason)
        self.audit.record("burner", "burner extinguished", self._now(), reason=reason)
        return result

    def burner_flame_lost(self, *, reason: str = "flame_proof_lost") -> dict[str, Any]:
        at = self._now()
        result = self.ignition.flame_lost(at=at, reason=reason)
        self.alarms.raise_alarm(ALARM_FLAME_LOST, reason, at=at, severity="critical", source="burner")
        if self.roller.running:
            self.roller.stop(at=at, reason="flame_lost")
        return result

    def recover_burner(self, *, alarm_reset: bool, clear_alarms: bool = True) -> dict[str, Any]:
        """Alarm reset, then latch release, then the burner train is ready again."""

        now = self._now()
        if self.recovery.is_complete("alarm_reset"):
            self.recovery.reset(at=now, reason="recovery_restarted")
        if alarm_reset:
            self.recovery.complete("alarm_reset", at=now)
        if clear_alarms:
            for code in self.alarms.active_codes():
                self.alarms.clear(code, at=now)
        result = self.ignition.recover(at=now, now=self._mono(), alarm_reset=alarm_reset)
        if result["released"]:
            self.recovery.complete("latch_release", at=now)
            self.recovery.complete("burner_recovery", at=now)
        self.audit.record(
            "recovery",
            "burner recovery evaluated",
            now,
            alarm_reset=alarm_reset,
            released=result["released"],
        )
        return result

    def request_latch_reset(self) -> dict[str, Any]:
        at = self._mono()
        requested = [state.as_dict() for state in self.guard.request_reset(at=at)]
        self.audit.record(
            "latch",
            "latch reset requested",
            self._now(),
            latches=[item["name"] for item in requested],
        )
        return {"requested": requested, "latches": self.latches.snapshot()}

    def trip_latch(self, name: str, reason: str) -> dict[str, Any]:
        state = self.latches.trip(name, reason, at=self._mono())
        self.audit.record("latch", "latch tripped", self._now(), latch=state.name, reason=reason)
        return state.as_dict()

    def calibrate_roller(
        self,
        *,
        pulses_per_meter: float,
        author: str,
        max_lag_s: float | None = None,
    ) -> dict[str, Any]:
        calibration = self.speed_calibration.capture(
            pulses_per_meter=pulses_per_meter,
            at=self._now(),
            generation=self.params.generation,
            author=author,
            max_lag_s=self.settings.roller_calibration_max_age_s if max_lag_s is None else max_lag_s,
        )
        self.start_gate.mark_calibrated(at=self._now(), detail=f"generation {calibration.generation}")
        self.audit.record(
            "calibration",
            "roller speed calibrated",
            self._now(),
            generation=calibration.generation,
            pulses_per_meter=calibration.pulses_per_meter,
        )
        return calibration.as_dict()

    def start_rollers(self, *, target_mpm: float, checked: bool = True) -> dict[str, Any]:
        """Start the roller table; the temperature baseline must be on disk."""

        now = self._now()
        if checked:
            self.start_gate.require(
                temp_baseline=self.baselines.latest(TEMP_BASELINE_KEY),
                calibration=self.speed_calibration.latest(),
                now=now,
            )
        state = self.roller.start(at=now, target_mpm=target_mpm)
        self.audit.record("roller", "roller table started", now, target_mpm=state.target_mpm)
        return state.as_dict()

    def stop_rollers(self, *, reason: str = "requested") -> dict[str, Any]:
        state = self.roller.stop(at=self._now(), reason=reason)
        self.audit.record("roller", "roller table stopping", self._now(), reason=reason)
        return state.as_dict()

    def set_roller_speed(self, *, target_mpm: float) -> dict[str, Any]:
        """Move the speed setpoint of a running roller table."""

        state = self.roller.set_speed(target_mpm=target_mpm, at=self._now())
        self.audit.record("roller", "roller speed set", self._now(), target_mpm=state.target_mpm)
        return state.as_dict()

    def roller_speed_from_pulses(self, pulses_per_s: float) -> dict[str, Any]:
        speed = self.speed_calibration.measure_speed_mpm(pulses_per_s, now=self._now())
        verdict = self.rules.evaluate(RULE_ROLLER_SPEED, speed)
        return {"speed_mpm": speed, "verdict": verdict.as_dict()}

    def publish_parameters(self, values: Mapping[str, float], *, author: str) -> dict[str, Any]:
        parameter_set = self.params.publish(values, published_at=self._now(), author=author)
        self.audit.record(
            "parameters",
            "parameter generation published",
            self._now(),
            generation=parameter_set.generation,
            author=author,
        )
        return parameter_set.as_dict()

    def issue_confirmation(self, *, scope: str, issued_by: str) -> dict[str, Any]:
        confirmation = self.confirmations.issue(
            scope=scope,
            parameter_set=self.params.current(),
            issued_at=self._now(),
            issued_by=issued_by,
        )
        self.audit.record(
            "confirmation",
            "confirmation slip issued",
            self._now(),
            token=confirmation.token,
            scope=confirmation.scope,
        )
        return confirmation.as_dict()

    def verify_confirmation(self, token: str, *, scope: str | None = None) -> dict[str, Any]:
        confirmation = self.confirmations.verify(
            token,
            now=self._now(),
            scope=scope,
            parameter_set=self.params.current(),
        )
        self.audit.record(
            "confirmation",
            "confirmation slip accepted",
            self._now(),
            token=confirmation.token,
            scope=confirmation.scope,
        )
        return confirmation.as_dict()

    # ------------------------------------------------------- carriers and batches

    def load_car(self, car_id: str, *, initial_moisture_pct: float = 6.0) -> dict[str, Any]:
        run = self.dryer.load(car_id, at=self._now(), initial_moisture_pct=initial_moisture_pct)
        self.audit.record("dryer", "carrier loaded", self._now(), car_id=run.car_id)
        return run.as_dict()

    def complete_drying(self, car_id: str) -> dict[str, Any]:
        run = self.dryer.complete(car_id, at=self._now())
        self.audit.record(
            "dryer",
            "carrier dried",
            self._now(),
            car_id=run.car_id,
            moisture_pct=run.moisture_pct,
        )
        return run.as_dict()

    def dry_car(
        self,
        car_id: str,
        *,
        initial_moisture_pct: float = 6.0,
        limit_s: float = 3600.0,
        step: float | None = None,
    ) -> dict[str, Any]:
        self.load_car(car_id, initial_moisture_pct=initial_moisture_pct)
        quantum = max(self._step(step), 5.0)
        target = self.dryer.target_moisture_pct
        self.run_until(
            lambda: self.dryer.run(car_id).moisture_pct <= target,
            label="drying_moisture",
            limit_s=limit_s,
            step=quantum,
        )
        return self.complete_drying(car_id)

    def calibrate_slurry(
        self,
        *,
        density_g_cm3: float,
        author: str,
        max_lag_s: float | None = None,
    ) -> dict[str, Any]:
        baseline = self.slurry.calibrate(
            density_g_cm3=density_g_cm3,
            at=self._now(),
            generation=self.params.generation,
            author=author,
            max_lag_s=max_lag_s,
        )
        self.audit.record(
            "glaze",
            "slurry calibrated",
            self._now(),
            generation=baseline.generation,
            density_g_cm3=density_g_cm3,
        )
        return baseline.as_dict()

    def start_glazing(self, car_id: str, *, density_g_cm3: float) -> dict[str, Any]:
        run = self.glaze.start(
            car_id,
            at=self._now(),
            now=self._now(),
            density_g_cm3=density_g_cm3,
            parameter_generation=self.params.generation,
        )
        self.audit.record(
            "glaze",
            "carrier coating started",
            self._now(),
            car_id=run.car_id,
            density_g_cm3=run.density_g_cm3,
        )
        return run.as_dict()

    def slurry_reference_density(self) -> float:
        baseline = self.slurry.reference(now=self._now(), generation=self.params.generation)
        return baseline.value(DENSITY_VALUE)

    def confirm_spacing(self, *, car_id: str, gap_mm: float) -> dict[str, Any]:
        verdict = self.conveyor.confirm_spacing(car_id=car_id, gap_mm=gap_mm, at=self._now())
        self.audit.record(
            "conv",
            "entry spacing confirmed",
            self._now(),
            car_id=verdict.car_id,
            gap_mm=verdict.gap_mm,
        )
        return verdict.as_dict()

    def feed_car(self, car_id: str) -> EntryRecord:
        try:
            record = self.conveyor.feed(car_id=car_id, at=self._now())
        except InterlockActive as exc:
            self.alarms.raise_alarm(
                ALARM_FEED_BLOCKED,
                str(exc.message),
                at=self._now(),
                severity="major",
                source="conv",
            )
            raise
        self.audit.record(
            "conv",
            "carrier admitted",
            self._now(),
            car_id=record.car_id,
            gap_mm=record.gap_mm,
        )
        return record

    def open_batch(self, code: str, *, work_order: str = "", car_count: int = 0) -> dict[str, Any]:
        record = self.batches.open(
            code,
            at=self._now(),
            work_order=work_order,
            car_count=car_count,
            generation=self.params.generation,
        )
        self.audit.record("batch", "batch opened", self._now(), code=record.code)
        return record.as_dict()

    def close_batch(self, code: str, *, car_count: int | None = None) -> dict[str, Any]:
        record = self.batches.close(code, at=self._now(), car_count=car_count)
        self.audit.record("batch", "batch closed", self._now(), code=record.code)
        return record.as_dict()

    def void_batch(self, code: str, *, reason: str = "voided") -> dict[str, Any]:
        sequence = self.batches.void(code, at=self._now(), reason=reason)
        self.audit.record("batch", "batch record voided", self._now(), code=code, tombstone=sequence)
        return {"tombstone": sequence, "batches": self.batches.snapshot()}

    def batch_record(self, code: str) -> BatchRecord:
        return self.batches.record(code)

    def run_batch(
        self,
        code: str,
        *,
        cars: Sequence[str],
        gap_mm: float,
        density_g_cm3: float,
        speed_mpm: float,
        work_order: str = "",
        step: float | None = None,
        limit_s: float = 7200.0,
    ) -> dict[str, Any]:
        """Drive one batch through drying, glazing, feeding, rolling and firing."""

        car_ids = [str(car).strip() for car in cars if str(car).strip()]
        if not car_ids:
            raise ValidationError("a batch needs at least one carrier")
        if not self.ready():
            raise StateConflict(
                "line is not ready for production",
                ready=False,
                gates_closed=self.gates.closed(),
                latches=self.latches.tripped_names(),
            )
        quantum = max(self._step(step), 5.0)
        opened = self.open_batch(code, work_order=work_order, car_count=len(car_ids))
        self.calibrate_slurry(density_g_cm3=density_g_cm3, author="batch_setup")
        for car in car_ids:
            self.load_car(car)
        target_moisture = self.dryer.target_moisture_pct
        self.run_until(
            lambda: all(
                self.dryer.run(car).moisture_pct <= target_moisture for car in car_ids
            ),
            label="batch_drying",
            limit_s=limit_s,
            step=quantum,
        )
        for car in car_ids:
            self.complete_drying(car)
        self.production.complete("drying", at=self._now())
        for car in car_ids:
            self.start_glazing(car, density_g_cm3=density_g_cm3)
        self.production.complete("glazing", at=self._now())
        for car in car_ids:
            self.confirm_spacing(car_id=car, gap_mm=gap_mm)
            self.feed_car(car)
        self.production.complete("feeding", at=self._now())
        self.start_rollers(target_mpm=speed_mpm)
        self.production.complete("rolling", at=self._now())
        self.run_until(self.zones_in_band, label="firing_curve", limit_s=limit_s, step=quantum)
        self.production.complete("firing", at=self._now())
        closed = self.close_batch(code, car_count=len(car_ids))
        self.capture_snapshot(reason=f"batch:{closed['code']}")
        self.audit.record(
            "batch",
            "batch completed",
            self._now(),
            code=closed["code"],
            cars=car_ids,
        )
        return {
            "batch": closed,
            "production": self.production.progress(),
            "entries": [entry.as_dict() for entry in self.conveyor.entries()],
            "roller": self.roller.snapshot().as_dict(),
        }

    # ------------------------------------------------------------------- ledger

    def commit_ledger(self) -> dict[str, Any]:
        before = self.stream.watermark
        watermark = self.stream.commit(committed_at=self._now())
        self.projection = Projection.from_dict(replay(self.stream).projection)
        self.audit.record("ledger", "records committed", self._now(), watermark=watermark)
        return {"before": before, "watermark": watermark, "state": self.stream.watermark_state().as_dict()}

    def rollback_ledger(self) -> dict[str, Any]:
        dropped = self.stream.rollback(removed_at=self._now())
        self.audit.record(
            "ledger",
            "staged records dropped",
            self._now(),
            dropped=[record.sequence for record in dropped],
        )
        return {
            "dropped": [record.sequence for record in dropped],
            "watermark": self.stream.watermark,
            "state": self.stream.watermark_state().as_dict(),
        }

    def tombstone_record(self, sequence: int, *, reason: str) -> dict[str, Any]:
        record = self.stream.append_tombstone(int(sequence), written_at=self._now(), reason=reason)
        watermark = self.stream.commit(committed_at=self._now())
        self.projection = Projection.from_dict(replay(self.stream).projection)
        self.audit.record(
            "ledger",
            "record voided",
            self._now(),
            sequence=int(sequence),
            tombstone=record.sequence,
        )
        return {
            "tombstone": record.as_dict(),
            "watermark": watermark,
            "state": self.stream.watermark_state().as_dict(),
        }

    def replay_ledger(self, *, after_watermark: int = 0) -> dict[str, Any]:
        return replay(self.stream, after_watermark=int(after_watermark)).as_dict()

    def staged_records(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.stream.uncommitted()]

    def query_records(self, filter: QueryFilter | None = None) -> dict[str, Any]:
        return self.records.result(filter).as_dict()

    def query_parameters(self, filter: QueryFilter | None = None) -> dict[str, Any]:
        criteria = QueryFilter(key=self.params.key) if filter is None else filter
        return self.records.result(criteria).as_dict()

    # --------------------------------------------------------- introspection

    def gate_inventory(self) -> dict[str, Any]:
        return {
            "gates": self.gates.snapshot(),
            "closed": self.gates.closed(),
            "open": self.gates.open_gates(),
        }

    def latch_states(self) -> dict[str, Any]:
        return {"latches": self.latches.snapshot(), "tripped": self.latches.tripped_names()}

    def recorder(self) -> dict[str, Any]:
        snapshot = self.latest_snapshot()
        return {
            "ledger": self.ledger_state(),
            "audit": self.audit_tail(limit=20),
            "snapshot": None if snapshot is None else snapshot.as_dict(),
        }

    def curve_window(self, *, elapsed_s: float) -> dict[str, float]:
        low, high = self.curve.window_at(elapsed_s, band_c=self.settings.temp_band_c)
        return {
            "elapsed_s": float(elapsed_s),
            "target_c": self.curve.target_at(elapsed_s),
            "low_c": low,
            "high_c": high,
        }

    def curve_trace(self, *, samples: int = 8) -> list[dict[str, float]]:
        count = int(samples)
        if count <= 0:
            raise ValidationError("curve trace needs at least one sample", samples=samples)
        span = max(1, count - 1)
        return [
            {
                "elapsed_s": self.curve.duration_s * index / span,
                "target_c": self.curve.target_at(self.curve.duration_s * index / span),
            }
            for index in range(count)
        ]

    def zone_for_section(self, section: int) -> str:
        return self.kiln.zone_for_section(section)

    def compare_zone(self, zone: str, *, as_of: float) -> dict[str, Any]:
        return self.history.compare(f"zone:{zone}", as_of=as_of).as_dict()

    def history_keys(self) -> list[str]:
        return self.history.keys()

    def arm_guard(self) -> bool:
        self._guard_armed = True
        return self._guard_armed

    def disarm_guard(self) -> bool:
        self._guard_armed = False
        return self._guard_armed

    def set_guard_armed(self, armed: bool) -> dict[str, Any]:
        """Arm or disarm the temperature guard, for commissioning and tests."""

        if armed:
            self.arm_guard()
        else:
            self.disarm_guard()
        self.audit.record(
            "guard",
            "temperature guard armed" if armed else "temperature guard disarmed",
            self._now(),
        )
        return {"armed": bool(armed), "guard_armed": self._guard_armed, "blocked": self.guard.blocked()}

    def start_monitor(self) -> dict[str, Any]:
        self.monitor.start()
        return self.monitor.snapshot()

    def stop_monitor(self) -> dict[str, Any]:
        self.monitor.stop()
        return self.monitor.snapshot()

    # ---------------------------------------------------------------- internals

    def _raise_interlock_alarm(self, latch: str, *, zone: str, at: float) -> None:
        if latch == self.guard.over_temp_latch:
            self.alarms.raise_alarm(
                ALARM_OVER_TEMP,
                f"{zone} left its over-temperature margin",
                at=at,
                severity="critical",
                source=zone,
            )
            return
        self.alarms.raise_alarm(
            ALARM_BASELINE_STALE,
            "baseline or probe calibration expired",
            at=at,
            severity="major",
            source=zone,
        )

    def _measured(self, zone: str) -> float:
        try:
            return self.probes.zone_average(zone)
        except NotFoundError:
            return self._zone_temperature[zone]

    def _apply_thermal(self, zone: str, dt: float, heater_power: float) -> None:
        """Bench thermal model: an integrator whose rate scales with heater power."""

        current = self._zone_temperature[zone]
        self._zone_temperature[zone] = max(AMBIENT_C, current + float(heater_power) * self._heat_gain_c_per_s * dt)
        updated = self._zone_temperature[zone]
        for probe in self.probes.probes():
            if probe.zone != zone:
                continue
            offset = 0.4 * (int(probe.probe_id.rsplit("-", 1)[-1]) - 1)
            self.probes.record(probe.probe_id, updated + offset, at=self._now())

    def _baseline_fresh(self) -> bool:
        baseline = self.baselines.latest(TEMP_BASELINE_KEY)
        return baseline is not None and not baseline.expired(self._now())

    def _probes_fresh(self, zone: str) -> bool:
        members = [probe for probe in self.probes.probes() if probe.zone == zone]
        if not members:
            return False
        return all(self.probes.is_fresh(probe.probe_id, now=self._now()) for probe in members)
