"""Temperature measurement, curve tracking, control and overload protection."""

from kilnline.temp.controller import ControlOutput, TemperatureController
from kilnline.temp.curve import CurvePoint, FiringCurve, WindowVerdict, compare_window, within_band
from kilnline.temp.guard import TemperatureGuard
from kilnline.temp.probe import Probe, ProbeBank, Reading

__all__ = [
    "ControlOutput",
    "CurvePoint",
    "FiringCurve",
    "Probe",
    "ProbeBank",
    "Reading",
    "TemperatureController",
    "TemperatureGuard",
    "WindowVerdict",
    "compare_window",
    "within_band",
]
