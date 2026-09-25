"""Batch uniqueness backed by the record stream.

A batch code may be opened once.  Uniqueness is not kept in a side table: the
live record for ``batch:<code>`` *is* the answer, so a restart, a replay or a
tombstone all fall out of the same rule -- a voided batch may be opened again,
a committed one may not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from kilnline.errors import DuplicateRecord, NotFoundError, StateConflict
from kilnline.ledger.stream import EventStream
from kilnline.ns.naming import parse_batch_code

BATCH_KEY_PREFIX = "batch:"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


@dataclass(frozen=True)
class BatchRecord:
    """One batch of carriers travelling through the kiln."""

    code: str
    status: str
    work_order: str
    car_count: int
    opened_at: float
    closed_at: float | None
    generation: int

    @property
    def open(self) -> bool:
        return self.status == STATUS_OPEN

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "status": self.status,
            "work_order": self.work_order,
            "car_count": int(self.car_count),
            "opened_at": float(self.opened_at),
            "generation": int(self.generation),
        }
        if self.closed_at is not None:
            payload["closed_at"] = float(self.closed_at)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BatchRecord":
        closed = payload.get("closed_at")
        return cls(
            code=str(payload.get("code", "")),
            status=str(payload.get("status", STATUS_OPEN)),
            work_order=str(payload.get("work_order", "")),
            car_count=int(payload.get("car_count", 0)),
            opened_at=float(payload.get("opened_at", 0.0)),
            closed_at=None if closed is None else float(closed),
            generation=int(payload.get("generation", 0)),
        )


class BatchRegistry:
    """Opens and closes batches, rejecting repeated codes."""

    def __init__(self, stream: EventStream, *, key_prefix: str = BATCH_KEY_PREFIX) -> None:
        self._stream = stream
        self._key_prefix = str(key_prefix)

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    def ledger_key(self, code: str) -> str:
        return f"{self._key_prefix}{parse_batch_code(code).text}"

    def open(
        self,
        code: str,
        *,
        at: float,
        work_order: str = "",
        car_count: int = 0,
        generation: int = 0,
    ) -> BatchRecord:
        parsed = parse_batch_code(code)
        existing = self.live(parsed.text)
        if existing is not None:
            raise DuplicateRecord(
                "batch code has already been used",
                code=parsed.text,
                existing_status=existing.status,
                opened_at=existing.opened_at,
            )
        record = BatchRecord(
            code=parsed.text,
            status=STATUS_OPEN,
            work_order=str(work_order),
            car_count=int(car_count),
            opened_at=float(at),
            closed_at=None,
            generation=int(generation),
        )
        self._stream.put(
            self.ledger_key(parsed.text),
            record.as_dict(),
            written_at=at,
            generation=int(generation),
            reason="batch_open",
        )
        self._stream.commit(committed_at=at)
        return record

    def close(self, code: str, *, at: float, car_count: int | None = None) -> BatchRecord:
        parsed = parse_batch_code(code)
        current = self.live(parsed.text)
        if current is None:
            raise NotFoundError("batch has never been opened", code=parsed.text)
        if not current.open:
            raise StateConflict("batch is already closed", code=parsed.text, closed_at=current.closed_at)
        record = BatchRecord(
            code=current.code,
            status=STATUS_CLOSED,
            work_order=current.work_order,
            car_count=current.car_count if car_count is None else int(car_count),
            opened_at=current.opened_at,
            closed_at=float(at),
            generation=current.generation,
        )
        self._stream.put(
            self.ledger_key(parsed.text),
            record.as_dict(),
            written_at=at,
            generation=current.generation,
            reason="batch_close",
        )
        self._stream.commit(committed_at=at)
        return record

    def void(self, code: str, *, at: float, reason: str = "voided") -> int:
        """Tombstone the live batch record so the code may be reused."""

        parsed = parse_batch_code(code)
        key = self.ledger_key(parsed.text)
        live, _ = self._stream.resolve(key)
        if live is None:
            raise NotFoundError("batch has never been opened", code=parsed.text)
        tombstone = self._stream.append_tombstone(live.sequence, written_at=at, reason=reason)
        self._stream.commit(committed_at=at)
        return tombstone.sequence

    def live(self, code: str) -> BatchRecord | None:
        parsed = parse_batch_code(code)
        record, _ = self._stream.resolve(self.ledger_key(parsed.text))
        return None if record is None else BatchRecord.from_dict(record.payload)

    def record(self, code: str) -> BatchRecord:
        found = self.live(code)
        if found is None:
            raise NotFoundError("batch has never been opened", code=str(code))
        return found

    def seen(self, code: str) -> bool:
        return self.live(code) is not None

    def codes(self) -> list[str]:
        """Every batch code that still has a live record."""

        candidates: list[str] = []
        for record in self._stream.committed():
            if not record.key.startswith(self._key_prefix) or record.is_tombstone:
                continue
            code = record.key[len(self._key_prefix) :]
            if code not in candidates:
                candidates.append(code)
        return sorted(code for code in candidates if self.live(code) is not None)

    def open_batches(self) -> list[BatchRecord]:
        records = [self.live(code) for code in self.codes()]
        return sorted(
            (record for record in records if record is not None and record.open),
            key=lambda item: item.code,
        )

    def snapshot(self) -> dict[str, Any]:
        batches = [self.live(code) for code in self.codes()]
        present = [record for record in batches if record is not None]
        return {
            "count": len(present),
            "open": [record.as_dict() for record in present if record.open],
            "closed": [record.as_dict() for record in present if not record.open],
        }
