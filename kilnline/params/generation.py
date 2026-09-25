"""Generation stamps for parameter and configuration sets.

Any artefact that was confirmed, snapshotted or baselined against a parameter
set records the generation it saw.  Comparing generations is then a single
integer comparison instead of a timestamp race.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


def digest_of(values: Mapping[str, Any]) -> str:
    """Stable digest of a value mapping, independent of insertion order."""

    payload = json.dumps(
        {str(key): values[key] for key in sorted(values)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Generation:
    """A monotonic parameter generation with the digest of its values."""

    number: int
    digest: str
    issued_at: float
    issuer: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": int(self.number),
            "digest": self.digest,
            "issued_at": float(self.issued_at),
            "issuer": self.issuer,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Generation":
        return cls(
            number=int(payload.get("number", 0)),
            digest=str(payload.get("digest", "")),
            issued_at=float(payload.get("issued_at", 0.0)),
            issuer=str(payload.get("issuer", "")),
        )

    def precedes(self, other: "Generation") -> bool:
        return self.number < other.number


class GenerationCounter:
    """Monotonic counter that refuses to move backwards."""

    def __init__(self, current: int = 0) -> None:
        self._current = int(current)

    @property
    def current(self) -> int:
        return self._current

    def bump(self) -> int:
        self._current += 1
        return self._current

    def adopt(self, number: int) -> int:
        """Move forward to ``number`` if it is newer; never move backwards."""

        candidate = int(number)
        if candidate > self._current:
            self._current = candidate
        return self._current

    def is_current(self, number: int) -> bool:
        return int(number) == self._current
