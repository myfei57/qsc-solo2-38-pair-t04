"""Confirmation slips that expire and bind to one parameter generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kilnline.errors import (
    CommandRejected,
    GenerationExpired,
    GenerationMismatch,
    NotFoundError,
    ValidationError,
)
from kilnline.params.generation import digest_of
from kilnline.params.registry import ParameterSet
from kilnline.store.json_store import JsonFileStore

CONFIRMATION_DOCUMENT = "confirmations"
DEFAULT_TTL_S = 1800.0


class ConfirmationExpired(GenerationExpired):
    """Raised when a confirmation slip is used past its expiry."""

    code = "confirmation_expired"


@dataclass(frozen=True)
class Confirmation:
    """A short-lived, scope-limited approval of one parameter generation."""

    token: str
    scope: str
    parameter_generation: int
    digest: str
    issued_at: float
    expires_at: float
    issued_by: str
    revoked_at: float | None = None

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.issued_at)

    def expired(self, now: float) -> bool:
        return float(now) > self.expires_at

    def revoked(self) -> bool:
        return self.revoked_at is not None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self.token,
            "scope": self.scope,
            "parameter_generation": int(self.parameter_generation),
            "digest": self.digest,
            "issued_at": float(self.issued_at),
            "expires_at": float(self.expires_at),
            "issued_by": self.issued_by,
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = float(self.revoked_at)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Confirmation":
        revoked = payload.get("revoked_at")
        return cls(
            token=str(payload.get("token", "")),
            scope=str(payload.get("scope", "")),
            parameter_generation=int(payload.get("parameter_generation", 0)),
            digest=str(payload.get("digest", "")),
            issued_at=float(payload.get("issued_at", 0.0)),
            expires_at=float(payload.get("expires_at", 0.0)),
            issued_by=str(payload.get("issued_by", "")),
            revoked_at=None if revoked is None else float(revoked),
        )


class ConfirmationBook:
    """Issues, verifies and revokes confirmation slips."""

    def __init__(
        self,
        store: JsonFileStore,
        *,
        document: str = CONFIRMATION_DOCUMENT,
        ttl_s: float = DEFAULT_TTL_S,
        history_limit: int = 100,
    ) -> None:
        if float(ttl_s) <= 0.0:
            raise ValidationError("confirmation ttl must be positive", ttl_s=ttl_s)
        self._store = store
        self._document = document
        self._ttl_s = float(ttl_s)
        self._history_limit = max(1, int(history_limit))
        self._entries: list[Confirmation] = self._load()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    def issue(
        self,
        *,
        scope: str,
        parameter_set: ParameterSet,
        issued_at: float,
        issued_by: str,
        ttl_s: float | None = None,
    ) -> Confirmation:
        label = str(scope).strip()
        if not label:
            raise ValidationError("confirmation scope must not be empty")
        lifetime = self._ttl_s if ttl_s is None else float(ttl_s)
        if lifetime <= 0.0:
            raise ValidationError("confirmation ttl must be positive", ttl_s=lifetime)
        token = self._token_for(
            scope=label,
            parameter_set=parameter_set,
            issued_at=float(issued_at),
            issued_by=str(issued_by),
        )
        confirmation = Confirmation(
            token=token,
            scope=label,
            parameter_generation=parameter_set.generation,
            digest=parameter_set.digest,
            issued_at=float(issued_at),
            expires_at=float(issued_at) + lifetime,
            issued_by=str(issued_by),
        )
        self._entries = [entry for entry in self._entries if entry.token != token]
        self._entries.append(confirmation)
        self._persist(issued_at)
        return confirmation

    def verify(
        self,
        token: str,
        *,
        now: float,
        scope: str | None = None,
        parameter_set: ParameterSet | None = None,
    ) -> Confirmation:
        confirmation = self.get(token)
        if confirmation.revoked():
            raise CommandRejected(
                "confirmation slip was revoked",
                token=confirmation.token,
                revoked_at=confirmation.revoked_at,
            )
        if confirmation.expired(now):
            raise ConfirmationExpired(
                "confirmation slip expired",
                token=confirmation.token,
                expires_at=confirmation.expires_at,
                now=float(now),
                age_s=confirmation.age_seconds(now),
            )
        if scope is not None and str(scope) != confirmation.scope:
            raise ValidationError(
                "confirmation slip was issued for a different scope",
                token=confirmation.token,
                expected=confirmation.scope,
                actual=str(scope),
            )
        if parameter_set is not None:
            if parameter_set.generation != confirmation.parameter_generation:
                raise GenerationMismatch(
                    "confirmation slip refers to a superseded parameter generation",
                    token=confirmation.token,
                    confirmed_generation=confirmation.parameter_generation,
                    current_generation=parameter_set.generation,
                )
            if parameter_set.digest != confirmation.digest:
                raise GenerationMismatch(
                    "confirmation slip does not match the published parameter digest",
                    token=confirmation.token,
                    confirmed_digest=confirmation.digest,
                    current_digest=parameter_set.digest,
                )
        return confirmation

    def revoke(self, token: str, *, at: float) -> Confirmation:
        confirmation = self.get(token)
        revoked = Confirmation(
            token=confirmation.token,
            scope=confirmation.scope,
            parameter_generation=confirmation.parameter_generation,
            digest=confirmation.digest,
            issued_at=confirmation.issued_at,
            expires_at=confirmation.expires_at,
            issued_by=confirmation.issued_by,
            revoked_at=float(at),
        )
        self._entries = [revoked if entry.token == revoked.token else entry for entry in self._entries]
        self._persist(at)
        return revoked

    def get(self, token: str) -> Confirmation:
        wanted = str(token).strip()
        for entry in reversed(self._entries):
            if entry.token == wanted:
                return entry
        raise NotFoundError("unknown confirmation slip", token=token)

    def active(self, *, now: float) -> list[Confirmation]:
        return [entry for entry in self._entries if not entry.expired(now) and not entry.revoked()]

    def expired(self, *, now: float) -> list[Confirmation]:
        return [entry for entry in self._entries if entry.expired(now) and not entry.revoked()]

    def entries(self) -> Sequence[Confirmation]:
        return tuple(self._entries)

    def _token_for(
        self,
        *,
        scope: str,
        parameter_set: ParameterSet,
        issued_at: float,
        issued_by: str,
    ) -> str:
        material = {
            "scope": scope,
            "generation": parameter_set.generation,
            "digest": parameter_set.digest,
            "issued_at": issued_at,
            "issued_by": issued_by,
            "issued": len(self._entries),
        }
        return f"CF-{digest_of(material)[:16].upper()}"

    def _persist(self, at: float) -> None:
        self._entries = self._entries[-self._history_limit :]
        self._store.write(
            self._document,
            {"entries": [entry.as_dict() for entry in self._entries]},
            written_at=at,
        )

    def _load(self) -> list[Confirmation]:
        document = self._store.read_or_none(self._document)
        if document is None:
            return []
        raw = document.data.get("entries", [])
        if not isinstance(raw, list):
            return []
        return [Confirmation.from_dict(entry) for entry in raw if isinstance(entry, Mapping)]
