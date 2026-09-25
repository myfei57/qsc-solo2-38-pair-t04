"""Atomic JSON document store.

A document is written to a sibling temporary file, flushed and then renamed
over the target.  The envelope carries a SHA-256 of the canonical body, so a
torn or hand-edited file is detected instead of silently trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kilnline.errors import NotFoundError, PersistenceError, ValidationError

SCHEMA_VERSION = 1


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def checksum_of(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("document name must be a non-empty string")
    candidate = name.strip()
    if candidate != Path(candidate).name:
        raise ValidationError("document name must not contain path separators", name=name)
    if not candidate.endswith(".json"):
        candidate = f"{candidate}.json"
    return candidate


@dataclass(frozen=True)
class JsonDocument:
    """A loaded document plus the metadata needed to reason about freshness."""

    name: str
    path: Path
    written_at: float
    revision: int
    checksum: str
    data: dict[str, Any]

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.written_at)


class JsonFileStore:
    """Stores named JSON documents under a single directory."""

    def __init__(self, root: Path, *, fsync: bool = True) -> None:
        self._root = Path(root)
        self._fsync = fsync
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, name: str) -> Path:
        return self._root / _safe_name(name)

    def exists(self, name: str) -> bool:
        return self.path_for(name).is_file()

    def names(self) -> list[str]:
        return sorted(path.name for path in self._root.glob("*.json"))

    def write(
        self,
        name: str,
        data: dict[str, Any],
        *,
        written_at: float,
        revision: int | None = None,
    ) -> JsonDocument:
        if not isinstance(data, dict):
            raise ValidationError("document payload must be a JSON object", name=name)
        target = self.path_for(name)
        previous_revision = 0
        if target.is_file():
            try:
                previous_revision = self.read(name).revision
            except PersistenceError:
                previous_revision = 0
        next_revision = previous_revision + 1 if revision is None else int(revision)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "name": target.name,
            "written_at": float(written_at),
            "revision": next_revision,
            "checksum": checksum_of(data),
            "body": data,
        }
        encoded = canonical_bytes(envelope)
        temp_path = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(temp_path, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            os.replace(temp_path, target)
            if self._fsync:
                self._sync_directory()
        except OSError as exc:
            raise PersistenceError("failed to write document", path=str(target)) from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:  # pragma: no cover - best effort cleanup
                    pass
        return JsonDocument(
            name=target.name,
            path=target,
            written_at=float(written_at),
            revision=next_revision,
            checksum=envelope["checksum"],
            data=dict(data),
        )

    def read(self, name: str) -> JsonDocument:
        target = self.path_for(name)
        if not target.is_file():
            raise NotFoundError("document is not present", name=target.name, root=str(self._root))
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise PersistenceError("failed to read document", path=str(target)) from exc
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PersistenceError("document is not valid JSON", path=str(target)) from exc
        if not isinstance(envelope, dict) or "body" not in envelope:
            raise PersistenceError("document envelope is malformed", path=str(target))
        body = envelope["body"]
        expected = envelope.get("checksum")
        actual = checksum_of(body)
        if expected != actual:
            raise PersistenceError(
                "document checksum mismatch", path=str(target), expected=expected, actual=actual
            )
        if not isinstance(body, dict):
            raise PersistenceError("document body must be an object", path=str(target))
        return JsonDocument(
            name=target.name,
            path=target,
            written_at=float(envelope.get("written_at", 0.0)),
            revision=int(envelope.get("revision", 0)),
            checksum=str(expected),
            data=body,
        )

    def read_or_none(self, name: str) -> JsonDocument | None:
        if not self.exists(name):
            return None
        return self.read(name)

    def delete(self, name: str) -> None:
        target = self.path_for(name)
        if target.is_file():
            target.unlink()
            if self._fsync:
                self._sync_directory()

    def _sync_directory(self) -> None:
        if os.name == "nt":  # directory fsync does not exist on Windows
            return
        try:
            descriptor = os.open(self._root, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
