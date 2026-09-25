"""Append-only record stream with a durable commit watermark.

Records are appended as *staged* entries: they exist on disk in insertion
order but are invisible to every reader until :meth:`EventStream.commit`
advances the watermark past them.  A restart replays only records at or below
the persisted watermark, so an interrupted writer can never publish a partial
transition.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from kilnline.errors import LedgerCorruption, NotFoundError, ValidationError, WatermarkError
from kilnline.ledger.records import (
    KIND_PUT,
    KIND_TOMBSTONE,
    KNOWN_KINDS,
    LedgerRecord,
)
from kilnline.store.json_store import JsonFileStore

WATERMARK_DOCUMENT = "ledger-watermark"


@dataclass(frozen=True)
class WatermarkState:
    """Persisted position of the commit watermark."""

    watermark: int
    last_sequence: int
    updated_at: float
    committed_records: int
    staged_records: int

    def as_dict(self) -> dict[str, int | float]:
        return {
            "watermark": self.watermark,
            "last_sequence": self.last_sequence,
            "updated_at": self.updated_at,
            "committed_records": self.committed_records,
            "staged_records": self.staged_records,
        }


class EventStream:
    """Durable append-only stream with explicit commit points."""

    def __init__(
        self,
        path: Path,
        store: JsonFileStore,
        *,
        fsync: bool = True,
        watermark_document: str = WATERMARK_DOCUMENT,
    ) -> None:
        self._path = Path(path)
        self._store = store
        self._fsync = fsync
        self._watermark_document = watermark_document
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records, self._truncated_tail = _read_records(self._path)
        self._watermark = self._load_watermark()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def watermark(self) -> int:
        return self._watermark

    @property
    def last_sequence(self) -> int:
        return self._records[-1].sequence if self._records else 0

    @property
    def truncated_tail(self) -> bool:
        """True when recovery dropped a partially written final line."""

        return self._truncated_tail

    @property
    def staged_count(self) -> int:
        return sum(1 for record in self._records if record.sequence > self._watermark)

    def records(self) -> list[LedgerRecord]:
        return list(self._records)

    def committed(self) -> list[LedgerRecord]:
        """Every record visible to a reader right now."""

        return [record for record in self._records if record.sequence <= self._watermark]

    def uncommitted(self) -> list[LedgerRecord]:
        """Records written but not yet published by a commit."""

        return [record for record in self._records if record.sequence > self._watermark]

    def append(
        self,
        key: str,
        kind: str,
        payload: dict[str, object] | None = None,
        *,
        written_at: float,
        generation: int = 0,
        reason: str = "",
        target: int | None = None,
    ) -> LedgerRecord:
        if kind not in KNOWN_KINDS:
            raise ValidationError("unknown ledger record kind", kind=kind)
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("ledger record key must be a non-empty string")
        record = LedgerRecord(
            sequence=self.last_sequence + 1,
            key=key.strip(),
            kind=kind,
            payload=dict(payload or {}),
            generation=int(generation),
            written_at=float(written_at),
            reason=str(reason),
            target=None if target is None else int(target),
        ).with_checksum()
        self._append_line(record)
        self._records.append(record)
        return record

    def put(
        self,
        key: str,
        payload: dict[str, object],
        *,
        written_at: float,
        generation: int = 0,
        reason: str = "",
    ) -> LedgerRecord:
        return self.append(
            key,
            KIND_PUT,
            payload,
            written_at=written_at,
            generation=generation,
            reason=reason,
        )

    def append_tombstone(
        self,
        target: int,
        *,
        written_at: float,
        reason: str = "",
        generation: int = 0,
    ) -> LedgerRecord:
        """Stage a compensating record that voids a committed record."""

        victim = self.record_at(int(target))
        if victim is None:
            raise NotFoundError("no ledger record with that sequence", sequence=int(target))
        if victim.sequence > self._watermark:
            raise WatermarkError(
                "only a committed record can be tombstoned",
                sequence=victim.sequence,
                watermark=self._watermark,
            )
        return self.append(
            victim.key,
            KIND_TOMBSTONE,
            {"voided_payload": dict(victim.payload)},
            written_at=written_at,
            generation=max(int(generation), victim.generation),
            reason=reason or "voided",
            target=victim.sequence,
        )

    def commit(self, *, committed_at: float | None = None) -> int:
        """Publish every staged record; returns the new watermark."""

        staged = self.uncommitted()
        if not staged:
            return self._watermark
        return self.commit_through(staged[-1].sequence, committed_at=committed_at)

    def commit_through(self, sequence: int, *, committed_at: float | None = None) -> int:
        """Publish staged records up to and including ``sequence``."""

        target = int(sequence)
        if target <= self._watermark:
            raise WatermarkError(
                "watermark must move forward",
                watermark=self._watermark,
                requested=target,
            )
        if target > self.last_sequence:
            raise WatermarkError(
                "cannot commit past the last staged record",
                last_sequence=self.last_sequence,
                requested=target,
            )
        if not any(record.sequence == target for record in self._records):
            raise NotFoundError("no ledger record with that sequence", sequence=target)
        self._watermark = target
        self._persist_watermark(committed_at)
        return self._watermark

    def rollback(self, *, removed_at: float | None = None) -> list[LedgerRecord]:
        """Drop every staged record, leaving the committed prefix untouched."""

        dropped = self.uncommitted()
        if not dropped:
            return []
        self._records = [record for record in self._records if record.sequence <= self._watermark]
        self._rewrite()
        if removed_at is not None:
            self._persist_watermark(removed_at)
        return dropped

    def record_at(self, sequence: int) -> LedgerRecord | None:
        for record in self._records:
            if record.sequence == int(sequence):
                return record
        return None

    def read_committed(
        self,
        *,
        after: int = 0,
        key: str | None = None,
        kinds: tuple[str, ...] | None = None,
        min_generation: int | None = None,
        max_generation: int | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[LedgerRecord]:
        """Committed records after a watermark, optionally narrowed by filter."""

        selected: list[LedgerRecord] = []
        for record in self.committed():
            if record.sequence <= int(after):
                continue
            if key is not None and record.key != key:
                continue
            if kinds is not None and record.kind not in kinds:
                continue
            if min_generation is not None and record.generation < int(min_generation):
                continue
            if max_generation is not None and record.generation > int(max_generation):
                continue
            if since is not None and record.written_at < float(since):
                continue
            if until is not None and record.written_at > float(until):
                continue
            selected.append(record)
        return selected

    def replay(self, *, after_watermark: int = 0) -> list[LedgerRecord]:
        """Records a restart must re-apply, oldest first."""

        return self.read_committed(after=int(after_watermark))

    def latest_committed(self, key: str) -> LedgerRecord | None:
        found: LedgerRecord | None = None
        for record in self.committed():
            if record.key == key:
                found = record
        return found

    def resolve(self, key: str) -> tuple[LedgerRecord | None, list[int]]:
        """Return the live record for ``key`` plus the sequences voided by it."""

        live: LedgerRecord | None = None
        voided: list[int] = []
        for record in self.committed():
            if record.key != key:
                continue
            if record.is_tombstone:
                if live is not None and record.voided_sequence == live.sequence:
                    voided.append(live.sequence)
                    live = None
                continue
            live = record
        return live, voided

    def watermark_state(self) -> WatermarkState:
        document = self._store.read_or_none(self._watermark_document)
        updated_at = 0.0 if document is None else float(document.data.get("updated_at", 0.0))
        return WatermarkState(
            watermark=self._watermark,
            last_sequence=self.last_sequence,
            updated_at=updated_at,
            committed_records=len(self.committed()),
            staged_records=self.staged_count,
        )

    def verify(self) -> dict[str, object]:
        """Integrity report; raises :class:`LedgerCorruption` when unusable."""

        for record in self._records:
            if not record.recompute_matches():
                raise LedgerCorruption("ledger record checksum mismatch", sequence=record.sequence)
        gaps = [
            record.sequence
            for previous, record in zip(self._records, self._records[1:])
            if record.sequence != previous.sequence + 1
        ]
        if gaps:
            raise LedgerCorruption("ledger sequence gap", sequences=gaps)
        if self._watermark > self.last_sequence:
            raise LedgerCorruption(
                "commit watermark is ahead of the stored records",
                watermark=self._watermark,
                last_sequence=self.last_sequence,
            )
        return {
            "records": len(self._records),
            "committed_records": len(self.committed()),
            "staged_records": self.staged_count,
            "watermark": self._watermark,
            "last_sequence": self.last_sequence,
            "truncated_tail": self._truncated_tail,
        }

    def _append_line(self, record: LedgerRecord) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(record.to_line())
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
        except OSError as exc:  # pragma: no cover - disk failure path
            raise LedgerCorruption("failed to append ledger record", path=str(self._path)) from exc

    def _rewrite(self) -> None:
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                for record in self._records:
                    handle.write(record.to_line())
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        except OSError as exc:  # pragma: no cover - disk failure path
            raise LedgerCorruption("failed to rewrite ledger stream", path=str(self._path)) from exc

    def _load_watermark(self) -> int:
        document = self._store.read_or_none(self._watermark_document)
        if document is None:
            return 0
        raw = document.data.get("watermark", 0)
        try:
            watermark = int(raw)
        except (TypeError, ValueError) as exc:
            raise LedgerCorruption("commit watermark is not an integer", value=raw) from exc
        if watermark < 0:
            raise LedgerCorruption("commit watermark is negative", watermark=watermark)
        if watermark > self.last_sequence:
            raise LedgerCorruption(
                "commit watermark is ahead of the stored records",
                watermark=watermark,
                last_sequence=self.last_sequence,
            )
        return watermark

    def _persist_watermark(self, updated_at: float | None) -> None:
        moment = 0.0 if updated_at is None else float(updated_at)
        self._store.write(
            self._watermark_document,
            {
                "watermark": self._watermark,
                "last_sequence": self.last_sequence,
                "updated_at": moment,
            },
            written_at=moment,
        )


def _read_records(path: Path) -> tuple[list[LedgerRecord], bool]:
    """Read the stream, dropping at most one partially flushed trailing line."""

    if not path.is_file():
        return [], False
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - disk failure path
        raise LedgerCorruption("failed to read ledger stream", path=str(path)) from exc
    if not raw:
        return [], False
    truncated = False
    chunks = raw.split(b"\n")
    if chunks[-1] != b"":
        chunks.pop()
        truncated = True
    records: list[LedgerRecord] = []
    for index, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerCorruption("ledger line is not valid UTF-8", line=index + 1) from exc
        try:
            payload = _loads(text)
        except ValueError as exc:
            raise LedgerCorruption("ledger line is not valid JSON", line=index + 1) from exc
        record = LedgerRecord.from_dict(payload)
        if not record.recompute_matches():
            raise LedgerCorruption("ledger record checksum mismatch", sequence=record.sequence)
        records.append(record)
    return records, truncated


def _loads(text: str) -> dict[str, object]:
    return json.loads(text)
