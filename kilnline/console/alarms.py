"""Alarm registry.

Alarms are operator facing and deduplicated: raising the same code again while
it is still active refreshes the message instead of creating a second entry,
so the overview page always shows one row per live problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kilnline.errors import NotFoundError, ValidationError

SEVERITY_ORDER = ("info", "warning", "major", "critical")


@dataclass(frozen=True)
class Alarm:
    code: str
    severity: str
    message: str
    source: str
    raised_at: float
    updated_at: float
    cleared_at: float | None
    acknowledged_at: float | None
    count: int

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "raised_at": self.raised_at,
            "updated_at": self.updated_at,
            "cleared_at": self.cleared_at,
            "acknowledged_at": self.acknowledged_at,
            "count": self.count,
            "active": self.active,
        }


class AlarmRegistry:
    """Keeps the live alarm set plus a bounded history."""

    def __init__(self, *, history_limit: int = 200) -> None:
        if int(history_limit) <= 0:
            raise ValidationError("alarm history limit must be positive")
        self._limit = int(history_limit)
        self._entries: dict[str, Alarm] = {}
        self._history: list[Alarm] = []

    def raise_alarm(
        self,
        code: str,
        message: str,
        *,
        at: float,
        severity: str = "major",
        source: str = "",
    ) -> Alarm:
        label = self._label(code)
        level = str(severity).strip().lower()
        if level not in SEVERITY_ORDER:
            raise ValidationError("unknown alarm severity", severity=severity, allowed=list(SEVERITY_ORDER))
        current = self._entries.get(label)
        active_before = current is not None and current.active
        alarm = Alarm(
            code=label,
            severity=level,
            message=str(message),
            source=str(source),
            raised_at=current.raised_at if active_before else float(at),
            updated_at=float(at),
            cleared_at=None,
            acknowledged_at=current.acknowledged_at if active_before else None,
            count=current.count + 1 if active_before else 1,
        )
        self._entries[label] = alarm
        self._push_history(alarm)
        return alarm

    def acknowledge(self, code: str, *, at: float) -> Alarm:
        current = self.get(code)
        acknowledged = Alarm(
            code=current.code,
            severity=current.severity,
            message=current.message,
            source=current.source,
            raised_at=current.raised_at,
            updated_at=float(at),
            cleared_at=current.cleared_at,
            acknowledged_at=float(at),
            count=current.count,
        )
        self._entries[current.code] = acknowledged
        self._push_history(acknowledged)
        return acknowledged

    def clear(self, code: str, *, at: float) -> Alarm:
        current = self.get(code)
        if not current.active:
            raise ValidationError("alarm is already cleared", code=current.code)
        cleared = Alarm(
            code=current.code,
            severity=current.severity,
            message=current.message,
            source=current.source,
            raised_at=current.raised_at,
            updated_at=float(at),
            cleared_at=float(at),
            acknowledged_at=current.acknowledged_at,
            count=current.count,
        )
        self._entries[current.code] = cleared
        self._push_history(cleared)
        return cleared

    def clear_all(self, *, at: float) -> list[Alarm]:
        return [self.clear(code, at=at) for code in self.active_codes()]

    def get(self, code: str) -> Alarm:
        label = self._label(code)
        alarm = self._entries.get(label)
        if alarm is None:
            raise NotFoundError("unknown alarm", code=label)
        return alarm

    def active(self) -> list[Alarm]:
        return [self._entries[code] for code in self.active_codes()]

    def active_codes(self) -> list[str]:
        return sorted(code for code, alarm in self._entries.items() if alarm.active)

    def history(self) -> list[Alarm]:
        return list(self._history)

    def is_active(self, code: str) -> bool:
        alarm = self._entries.get(str(code).strip())
        return bool(alarm is not None and alarm.active)

    def summary(self) -> dict[str, Any]:
        active = self.active()
        by_severity: dict[str, int] = {}
        for alarm in active:
            by_severity[alarm.severity] = by_severity.get(alarm.severity, 0) + 1
        return {
            "active": len(active),
            "by_severity": by_severity,
            "worst": max(
                (alarm.severity for alarm in active),
                key=SEVERITY_ORDER.index,
                default="none",
            ),
            "codes": [alarm.code for alarm in active],
        }

    def _push_history(self, alarm: Alarm) -> None:
        self._history.append(alarm)
        if len(self._history) > self._limit:
            del self._history[: len(self._history) - self._limit]

    @staticmethod
    def _label(code: str) -> str:
        label = str(code).strip()
        if not label:
            raise ValidationError("alarm code must not be empty")
        return label
