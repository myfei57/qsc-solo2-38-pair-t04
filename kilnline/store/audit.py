"""Append only audit trail.

The audit file is descriptive: it records what the operator and the control
core did, in order, and it survives a crash.  Transactional semantics live in
:mod:`kilnline.ledger`; this module never gates a state change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from kilnline.errors import PersistenceError


@dataclass(frozen=True)
class AuditRecord:
    sequence: int
    timestamp: float
    category: str
    message: str
    fields: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "category": self.category,
            "message": self.message,
        }
        if self.fields:
            payload["fields"] = dict(self.fields)
        return payload


class AuditLog:
    """Newline delimited JSON trail that can be queried and replayed."""

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = self._last_sequence()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def sequence(self) -> int:
        return self._sequence

    def record(self, category: str, message: str, timestamp: float, **fields: Any) -> AuditRecord:
        self._sequence += 1
        entry = AuditRecord(
            sequence=self._sequence,
            timestamp=float(timestamp),
            category=str(category),
            message=str(message),
            fields=dict(fields),
        )
        line = json.dumps(entry.as_dict(), ensure_ascii=False, sort_keys=True, default=str)
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise PersistenceError("failed to append audit record", path=str(self._path)) from exc
        return entry

    def read_all(self) -> list[AuditRecord]:
        if not self._path.is_file():
            return []
        records: list[AuditRecord] = []
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for line in handle:
                    entry = _decode(line)
                    if entry is not None:
                        records.append(entry)
        except OSError as exc:
            raise PersistenceError("failed to read audit trail", path=str(self._path)) from exc
        return records

    def tail(self, limit: int = 50) -> list[AuditRecord]:
        if limit <= 0:
            return []
        return self.read_all()[-limit:]

    def since(self, timestamp: float) -> list[AuditRecord]:
        return [entry for entry in self.read_all() if entry.timestamp >= float(timestamp)]

    def find(self, category: str) -> Iterator[AuditRecord]:
        return (entry for entry in self.read_all() if entry.category == category)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for entry in self.read_all():
            if entry.category not in seen:
                seen.append(entry.category)
        return seen

    def count(self, category: str | None = None) -> int:
        if category is None:
            return len(self.read_all())
        return sum(1 for _ in self.find(category))

    def messages(self, category: str) -> list[str]:
        return [entry.message for entry in self.find(category)]

    def replay_into(self, sink: list[AuditRecord], *, since_sequence: int = 0) -> list[AuditRecord]:
        """Append every record after ``since_sequence`` and return the new tail."""

        appended: list[AuditRecord] = []
        for entry in self.read_all():
            if entry.sequence > since_sequence:
                sink.append(entry)
                appended.append(entry)
        return appended

    def _last_sequence(self) -> int:
        if not self._path.is_file():
            return 0
        last = 0
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for line in handle:
                    entry = _decode(line)
                    if entry is not None:
                        last = entry.sequence
        except OSError:  # pragma: no cover - defensive branch
            return 0
        return last

    def entries(self) -> Iterable[AuditRecord]:
        return self.read_all()


def _decode(line: str) -> AuditRecord | None:
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    fields = payload.get("fields", {})
    return AuditRecord(
        sequence=int(payload.get("sequence", 0)),
        timestamp=float(payload.get("timestamp", 0.0)),
        category=str(payload.get("category", "")),
        message=str(payload.get("message", "")),
        fields=dict(fields) if isinstance(fields, dict) else {},
    )
