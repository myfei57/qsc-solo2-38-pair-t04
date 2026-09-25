"""Record shape for the append-only stream.

Every record is self describing and carries a checksum over its own content, so
a stream file can be validated line by line without external metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from kilnline.errors import ValidationError

LEDGER_SCHEMA = 1
KIND_PUT = "put"
KIND_TOMBSTONE = "tombstone"
KNOWN_KINDS = (KIND_PUT, KIND_TOMBSTONE)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def record_checksum(
    *,
    sequence: int,
    key: str,
    kind: str,
    payload: dict[str, Any],
    generation: int,
    written_at: float,
    reason: str,
    target: int | None,
) -> str:
    """Checksum over the semantic content of a record, excluding the digest."""

    body = {
        "sequence": int(sequence),
        "key": str(key),
        "kind": str(kind),
        "payload": payload,
        "generation": int(generation),
        "written_at": float(written_at),
        "reason": str(reason),
        "target": None if target is None else int(target),
    }
    return hashlib.sha256(_canonical(body)).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One entry of the append-only stream."""

    sequence: int
    key: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    written_at: float = 0.0
    reason: str = ""
    target: int | None = None
    checksum: str = ""

    @property
    def is_tombstone(self) -> bool:
        return self.kind == KIND_TOMBSTONE

    @property
    def voided_sequence(self) -> int | None:
        """Sequence this record voids, only meaningful for tombstones."""

        return None if self.target is None else int(self.target)

    def with_checksum(self) -> "LedgerRecord":
        digest = record_checksum(
            sequence=self.sequence,
            key=self.key,
            kind=self.kind,
            payload=self.payload,
            generation=self.generation,
            written_at=self.written_at,
            reason=self.reason,
            target=self.target,
        )
        return LedgerRecord(
            sequence=self.sequence,
            key=self.key,
            kind=self.kind,
            payload=dict(self.payload),
            generation=self.generation,
            written_at=self.written_at,
            reason=self.reason,
            target=self.target,
            checksum=digest,
        )

    def recompute_matches(self) -> bool:
        return self.checksum == record_checksum(
            sequence=self.sequence,
            key=self.key,
            kind=self.kind,
            payload=self.payload,
            generation=self.generation,
            written_at=self.written_at,
            reason=self.reason,
            target=self.target,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": int(self.sequence),
            "key": self.key,
            "kind": self.kind,
            "payload": dict(self.payload),
            "generation": int(self.generation),
            "written_at": float(self.written_at),
            "reason": self.reason,
            "checksum": self.checksum,
        }
        if self.target is not None:
            payload["target"] = int(self.target)
        return payload

    def to_line(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, default=str) + "\n"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LedgerRecord":
        if not isinstance(payload, dict):
            raise ValidationError("ledger record must be an object")
        kind = str(payload.get("kind", ""))
        if kind not in KNOWN_KINDS:
            raise ValidationError("unknown ledger record kind", kind=kind)
        body = payload.get("payload", {})
        if not isinstance(body, dict):
            raise ValidationError("ledger record payload must be an object")
        target = payload.get("target")
        return cls(
            sequence=int(payload.get("sequence", 0)),
            key=str(payload.get("key", "")),
            kind=kind,
            payload=dict(body),
            generation=int(payload.get("generation", 0)),
            written_at=float(payload.get("written_at", 0.0)),
            reason=str(payload.get("reason", "")),
            target=None if target is None else int(target),
            checksum=str(payload.get("checksum", "")),
        )
