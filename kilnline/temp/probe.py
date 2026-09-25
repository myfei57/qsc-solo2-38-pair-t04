"""Thermocouple registration, readings and calibration freshness.

A calibration is only allowed to be old for so long: readings from a probe
whose calibration has expired cannot be used to declare a zone "at
temperature", and the guard rejects them instead of averaging them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import GenerationExpired, NotFoundError, ValidationError


@dataclass(frozen=True)
class Probe:
    probe_id: str
    zone: str
    calibrated_at: float | None
    calibrated_by: str
    span_c: float

    @property
    def calibrated(self) -> bool:
        return self.calibrated_at is not None

    def calibration_age_s(self, now: float) -> float | None:
        if self.calibrated_at is None:
            return None
        return max(0.0, float(now) - self.calibrated_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "zone": self.zone,
            "calibrated_at": self.calibrated_at,
            "calibrated_by": self.calibrated_by,
            "span_c": self.span_c,
        }


@dataclass(frozen=True)
class Reading:
    probe_id: str
    zone: str
    value_c: float
    read_at: float

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.read_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "zone": self.zone,
            "value_c": self.value_c,
            "read_at": self.read_at,
        }


class ProbeBank:
    """Owns probes, their calibration stamps and their newest reading."""

    def __init__(self, *, calibration_max_age_s: float) -> None:
        if float(calibration_max_age_s) <= 0.0:
            raise ValidationError("calibration age budget must be positive")
        self._max_age_s = float(calibration_max_age_s)
        self._probes: dict[str, Probe] = {}
        self._readings: dict[str, Reading] = {}

    @property
    def calibration_max_age_s(self) -> float:
        return self._max_age_s

    def register(self, probe_id: str, zone: str) -> Probe:
        label = self._label(probe_id)
        probe = Probe(
            probe_id=label,
            zone=str(zone),
            calibrated_at=None,
            calibrated_by="",
            span_c=0.0,
        )
        self._probes[label] = probe
        return probe

    def calibrate(self, probe_id: str, *, at: float, by: str, span_c: float = 0.0) -> Probe:
        current = self.probe(probe_id)
        probe = Probe(
            probe_id=current.probe_id,
            zone=current.zone,
            calibrated_at=float(at),
            calibrated_by=str(by),
            span_c=float(span_c),
        )
        self._probes[current.probe_id] = probe
        return probe

    def record(self, probe_id: str, value_c: float, *, at: float) -> Reading:
        probe = self.probe(probe_id)
        value = float(value_c)
        if value != value:
            raise ValidationError("probe reading must be a number", probe=probe.probe_id)
        reading = Reading(probe_id=probe.probe_id, zone=probe.zone, value_c=value, read_at=float(at))
        self._readings[probe.probe_id] = reading
        return reading

    def probe(self, probe_id: str) -> Probe:
        label = self._label(probe_id)
        probe = self._probes.get(label)
        if probe is None:
            raise NotFoundError("unknown probe", probe=label)
        return probe

    def latest(self, probe_id: str) -> Reading:
        label = self._label(probe_id)
        reading = self._readings.get(label)
        if reading is None:
            raise NotFoundError("probe has no reading yet", probe=label)
        return reading

    def readings(self, *, zone: str | None = None) -> list[Reading]:
        values = list(self._readings.values())
        if zone is None:
            return sorted(values, key=lambda item: item.probe_id)
        return sorted((item for item in values if item.zone == zone), key=lambda item: item.probe_id)

    def zone_average(self, zone: str) -> float:
        values = [reading.value_c for reading in self.readings(zone=zone)]
        if not values:
            raise NotFoundError("no readings for that zone", zone=zone)
        return sum(values) / len(values)

    def zone_spread(self, zone: str) -> float:
        values = [reading.value_c for reading in self.readings(zone=zone)]
        if not values:
            raise NotFoundError("no readings for that zone", zone=zone)
        return max(values) - min(values)

    def calibration_age_s(self, probe_id: str, *, now: float) -> float | None:
        return self.probe(probe_id).calibration_age_s(now)

    def is_fresh(self, probe_id: str, *, now: float) -> bool:
        age = self.probe(probe_id).calibration_age_s(now)
        if age is None:
            return False
        return age <= self._max_age_s

    def require_fresh(self, probe_id: str, *, now: float) -> Probe:
        probe = self.probe(probe_id)
        age = probe.calibration_age_s(now)
        if age is None:
            raise GenerationExpired("probe has never been calibrated", probe=probe.probe_id)
        if age > self._max_age_s:
            raise GenerationExpired(
                "probe calibration has expired",
                probe=probe.probe_id,
                age_s=age,
                max_age_s=self._max_age_s,
            )
        return probe

    def stale(self, *, now: float) -> list[str]:
        return sorted(
            probe.probe_id
            for probe in self._probes.values()
            if not self.is_fresh(probe.probe_id, now=now)
        )

    def zone_is_usable(self, zone: str, *, now: float) -> bool:
        members = [probe for probe in self._probes.values() if probe.zone == zone]
        if not members:
            return False
        return all(self.is_fresh(probe.probe_id, now=now) for probe in members)

    def probes(self) -> list[Probe]:
        return sorted(self._probes.values(), key=lambda item: item.probe_id)

    def snapshot(self, *, now: float) -> dict[str, Any]:
        return {
            "max_age_s": self._max_age_s,
            "probes": [
                {
                    **probe.as_dict(),
                    "age_s": probe.calibration_age_s(now),
                    "fresh": self.is_fresh(probe.probe_id, now=now),
                    "reading": None
                    if probe.probe_id not in self._readings
                    else self._readings[probe.probe_id].as_dict(),
                }
                for probe in self.probes()
            ],
        }

    @staticmethod
    def _label(probe_id: str) -> str:
        label = str(probe_id).strip()
        if not label:
            raise ValidationError("probe id must not be empty")
        return label
