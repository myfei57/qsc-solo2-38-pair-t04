"""Settings: defaults, environment overrides and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.config import Settings
from kilnline.errors import ConfigurationError, ValidationError


def test_settings_defaults_are_internally_consistent() -> None:
    settings = Settings()
    assert settings.section_count == settings.sections_per_zone * len(settings.zones)
    assert settings.line_length_m == settings.zone_length_m * len(settings.zones)
    assert settings.roller_speed_min_mpm < settings.roller_speed_max_mpm
    assert settings.conveyor_gap_min_mm < settings.conveyor_gap_max_mm


def test_settings_from_env_overrides_typed_fields() -> None:
    settings = Settings.from_env(
        {
            "KILNLINE_DATA_DIR": "run/state",
            "KILNLINE_TEMP_BAND_C": "4.5",
            "KILNLINE_SECTIONS_PER_ZONE": "3",
            "KILNLINE_ZONES": "preheat, firing ,cooling",
            "KILNLINE_ROLLER_SPEED_MAX_MPM": "30",
        }
    )
    assert settings.data_dir == Path("run/state")
    assert settings.temp_band_c == 4.5
    assert settings.sections_per_zone == 3
    assert settings.zones == ("preheat", "firing", "cooling")
    assert settings.roller_speed_max_mpm == 30.0


def test_settings_from_env_rejects_unparsable_values() -> None:
    with pytest.raises(ValidationError):
        Settings.from_env({"KILNLINE_TEMP_BAND_C": "wide"})
    with pytest.raises(ValidationError):
        Settings.from_env({"KILNLINE_SECTIONS_PER_ZONE": "two"})
    with pytest.raises(ValidationError):
        Settings.from_env({"KILNLINE_ZONES": "  "})


def test_settings_reject_inverted_and_non_positive_ranges() -> None:
    with pytest.raises(ConfigurationError):
        Settings(roller_speed_min_mpm=20.0, roller_speed_max_mpm=10.0)
    with pytest.raises(ConfigurationError):
        Settings(conveyor_gap_min_mm=300.0, conveyor_gap_max_mm=200.0)
    with pytest.raises(ConfigurationError):
        Settings(glaze_density_low=1.8, glaze_density_high=1.5)
    with pytest.raises(ConfigurationError):
        Settings(ramp_rate_c_per_min=0.0)
    with pytest.raises(ConfigurationError):
        Settings(temp_band_c=-1.0)
    with pytest.raises(ConfigurationError):
        Settings(zones=())
    with pytest.raises(ConfigurationError):
        Settings(zones=("firing", "firing"))


def test_settings_overrides_and_data_dir_helpers() -> None:
    settings = Settings().overrides(ramp_rate_c_per_min=45.0)
    assert settings.ramp_rate_c_per_min == 45.0
    assert settings.with_data_dir("var/test").data_dir == Path("var/test")
    with pytest.raises(ConfigurationError):
        Settings().overrides(unknown_field=1)
