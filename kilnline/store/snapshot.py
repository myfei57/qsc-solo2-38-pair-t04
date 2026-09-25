"""Whole-state snapshots used for restart recovery and shift hand-over."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kilnline.errors import NotFoundError, PersistenceError
from kilnline.store.json_store import JsonFileStore

SNAPSHOT_DOCUMENT = "state-snapshot"
HISTORY_DOCUMENT = "state-snapshot-history"


@dataclass(frozen=True)
class Snapshot:
    revision: int
    captured_at: float
    reason: str
    watermark: int
    generation: int
    state: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "captured_at": self.captured_at,
            "reason": self.reason,
            "watermark": self.watermark,
            "generation": self.generation,
            "state": self.state,
        }

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.captured_at)


class SnapshotStore:
    """Keeps the newest snapshot plus a bounded rolling history."""

    def __init__(self, store: JsonFileStore, *, history_limit: int = 20) -> None:
        self._store = store
        self._history_limit = max(1, int(history_limit))

    @property
    def path(self) -> Path:
        return self._store.path_for(SNAPSHOT_DOCUMENT)

    def capture(
        self,
        state: dict[str, Any],
        *,
        captured_at: float,
        reason: str,
        watermark: int = 0,
        generation: int = 0,
    ) -> Snapshot:
        previous = self.latest()
        revision = 1 if previous is None else previous.revision + 1
        snapshot = Snapshot(
            revision=revision,
            captured_at=float(captured_at),
            reason=str(reason),
            watermark=int(watermark),
            generation=int(generation),
            state=dict(state),
        )
        self._store.write(SNAPSHOT_DOCUMENT, snapshot.as_dict(), written_at=captured_at, revision=revision)
        history = self._load_history()
        history.append(snapshot.as_dict())
        self._store.write(
            HISTORY_DOCUMENT,
            {"entries": history[-self._history_limit :]},
            written_at=captured_at,
        )
        return snapshot

    def latest(self) -> Snapshot | None:
        document = self._store.read_or_none(SNAPSHOT_DOCUMENT)
        if document is None:
            return None
        return self._from_payload(document.data)

    def require_latest(self) -> Snapshot:
        snapshot = self.latest()
        if snapshot is None:
            raise NotFoundError("no state snapshot has been captured yet")
        return snapshot

    def history(self) -> list[Snapshot]:
        return [self._from_payload(entry) for entry in self._load_history()]

    def state_since(self, reason: str) -> dict[str, Any] | None:
        for snapshot in reversed(self.history()):
            if snapshot.reason == reason:
                return dict(snapshot.state)
        return None

    def _load_history(self) -> list[dict[str, Any]]:
        document = self._store.read_or_none(HISTORY_DOCUMENT)
        if document is None:
            return []
        try:
            entries = document.data.get("entries", [])
        except AttributeError:  # pragma: no cover - defensive
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _from_payload(payload: dict[str, Any]) -> Snapshot:
        state = payload.get("state", {})
        if not isinstance(state, dict):
            raise PersistenceError("snapshot state must be an object")
        return Snapshot(
            revision=int(payload.get("revision", 0)),
            captured_at=float(payload.get("captured_at", 0.0)),
            reason=str(payload.get("reason", "")),
            watermark=int(payload.get("watermark", 0)),
            generation=int(payload.get("generation", 0)),
            state=dict(state),
        )
