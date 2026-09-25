"""Process-wide settings.

Every tunable lives in a single frozen dataclass so that a test can build a
fully deterministic configuration and a deployment can override any field from
the environment with the ``KILNLINE_`` prefix.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, get_args, get_type_hints

from kilnline.errors import ConfigurationError, ValidationError

ENV_PREFIX = "KILNLINE_"


@dataclass(frozen=True)
class Settings:
    """Immutable configuration for one control service instance."""

    data_dir: Path = Path("var")
    zones: tuple[str, ...] = ("preheat", "firing", "soak", "cooling")
    sections_per_zone: int = 2
    zone_length_m: float = 4.5
    tick_interval_s: float = 1.0

    temp_band_c: float = 3.0
    over_temp_margin_c: float = 10.0
    ramp_rate_c_per_min: float = 4.0
    soak_hold_s: float = 120.0
    baseline_max_lag_s: float = 900.0
    probe_calibration_max_age_s: float = 3600.0
    confirmation_ttl_s: float = 1800.0
    latch_reset_hold_s: float = 30.0

    air_min_pressure_kpa: float = 2.5
    air_spin_up_s: float = 20.0
    gas_settle_s: float = 8.0
    flame_proof_s: float = 5.0

    dry_min_duration_s: float = 120.0
    dry_target_moisture_pct: float = 1.5
    dry_rate_pct_per_s: float = 0.05

    glaze_density_low: float = 1.55
    glaze_density_high: float = 1.75
    glaze_density_max_age_s: float = 600.0

    conveyor_gap_min_mm: float = 180.0
    conveyor_gap_max_mm: float = 260.0
    conveyor_speed_m_per_s: float = 0.2

    roller_speed_min_mpm: float = 1.0
    roller_speed_max_mpm: float = 24.0
    roller_speed_ramp_mpm_per_s: float = 0.4
    roller_calibration_max_age_s: float = 7200.0

    zone_map_max_lag_s: float = 1800.0
    ledger_retention_records: int = 5000
    state_history_limit: int = 200
    alarm_history_limit: int = 200
    monitor_interval_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.zones:
            raise ConfigurationError("at least one zone must be configured")
        if len(set(self.zones)) != len(self.zones):
            raise ConfigurationError("zone names must be unique", zones=list(self.zones))
        positive = (
            "sections_per_zone",
            "zone_length_m",
            "tick_interval_s",
            "ramp_rate_c_per_min",
            "baseline_max_lag_s",
            "probe_calibration_max_age_s",
            "confirmation_ttl_s",
            "air_min_pressure_kpa",
            "air_spin_up_s",
            "gas_settle_s",
            "flame_proof_s",
            "dry_rate_pct_per_s",
            "conveyor_speed_m_per_s",
            "roller_speed_ramp_mpm_per_s",
            "roller_calibration_max_age_s",
            "zone_map_max_lag_s",
            "ledger_retention_records",
            "state_history_limit",
            "alarm_history_limit",
            "monitor_interval_s",
        )
        for name in positive:
            if float(getattr(self, name)) <= 0.0:
                raise ConfigurationError(f"{name} must be positive", field=name)
        non_negative = (
            "temp_band_c",
            "over_temp_margin_c",
            "soak_hold_s",
            "latch_reset_hold_s",
            "dry_min_duration_s",
            "dry_target_moisture_pct",
            "glaze_density_max_age_s",
        )
        for name in non_negative:
            if float(getattr(self, name)) < 0.0:
                raise ConfigurationError(f"{name} must not be negative", field=name)
        if self.roller_speed_min_mpm > self.roller_speed_max_mpm:
            raise ConfigurationError(
                "roller speed range is inverted",
                low=self.roller_speed_min_mpm,
                high=self.roller_speed_max_mpm,
            )
        if self.conveyor_gap_min_mm >= self.conveyor_gap_max_mm:
            raise ConfigurationError(
                "conveyor gap range is inverted",
                low=self.conveyor_gap_min_mm,
                high=self.conveyor_gap_max_mm,
            )
        if self.glaze_density_low >= self.glaze_density_high:
            raise ConfigurationError(
                "slurry density range is inverted",
                low=self.glaze_density_low,
                high=self.glaze_density_high,
            )

    @property
    def section_count(self) -> int:
        return self.sections_per_zone * len(self.zones)

    @property
    def line_length_m(self) -> float:
        return self.zone_length_m * len(self.zones)

    def with_data_dir(self, data_dir: Path | str) -> "Settings":
        return dataclasses.replace(self, data_dir=Path(data_dir))

    def overrides(self, **values: Any) -> "Settings":
        """Return a copy with ``values`` applied, re-running validation."""

        unknown = sorted(set(values) - {field.name for field in dataclasses.fields(self)})
        if unknown:
            raise ConfigurationError("unknown settings field", fields=unknown)
        return dataclasses.replace(self, **values)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from defaults plus ``KILNLINE_`` environment entries."""

        env = os.environ if environ is None else environ
        hints = get_type_hints(cls)
        values: dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            key = f"{ENV_PREFIX}{field.name.upper()}"
            if key in env:
                values[field.name] = _coerce(env[key], hints[field.name], key)
        return cls(**values)


def _coerce(raw: str, target: Any, key: str) -> Any:
    text = str(raw).strip()
    if target is str:
        return text
    if target is bool:
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValidationError("environment flag must be boolean", key=key, value=raw)
    if target is int:
        try:
            return int(text)
        except ValueError as exc:
            raise ValidationError("environment value must be an integer", key=key, value=raw) from exc
    if target is float:
        try:
            return float(text)
        except ValueError as exc:
            raise ValidationError("environment value must be a number", key=key, value=raw) from exc
    if target is Path:
        return Path(text)
    if get_args(target):
        origin = target.__origin__ if hasattr(target, "__origin__") else None
        if origin is tuple:
            items = tuple(part.strip() for part in text.split(",") if part.strip())
            if not items:
                raise ValidationError("environment list must not be empty", key=key, value=raw)
            return items
    raise ConfigurationError("unsupported settings field type", key=key, target=str(target))
