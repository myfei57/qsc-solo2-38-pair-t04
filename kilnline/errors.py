"""Domain error hierarchy.

Every failure carries a stable machine readable ``code`` plus an HTTP status so
that the transport layer never has to match on message text.
"""

from __future__ import annotations


class KilnError(Exception):
    """Base class for every error raised by the control service."""

    code = "kiln_error"
    http_status = 500

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.context:
            payload["context"] = dict(self.context)
        return payload


class ConfigurationError(KilnError):
    code = "configuration_error"
    http_status = 500


class ValidationError(KilnError):
    code = "validation_error"
    http_status = 400


class NotFoundError(KilnError):
    code = "not_found"
    http_status = 404


class StateConflict(KilnError):
    """The requested transition is illegal for the current state."""

    code = "state_conflict"
    http_status = 409


class OrderingViolation(StateConflict):
    """A process stage was requested before its prerequisites completed."""

    code = "ordering_violation"
    http_status = 409


class InterlockActive(KilnError):
    """A latch or a closed precondition gate forbids the requested action."""

    code = "interlock_active"
    http_status = 423


class DurabilityError(KilnError):
    """A value that must survive a restart was not durably written first."""

    code = "durability_error"
    http_status = 409


class PersistenceError(KilnError):
    code = "persistence_error"
    http_status = 500


class LedgerCorruption(PersistenceError):
    """The record stream cannot be trusted, so recovery must refuse to start."""

    code = "ledger_corruption"
    http_status = 500


class WatermarkError(StateConflict):
    """A commit watermark operation is not possible for the current stream."""

    code = "watermark_error"
    http_status = 409


class GenerationExpired(StateConflict):
    """A confirmation, snapshot or baseline is past its allowed age."""

    code = "generation_expired"
    http_status = 409


class GenerationMismatch(StateConflict):
    """A confirmation refers to a generation that is no longer current."""

    code = "generation_mismatch"
    http_status = 409


class DuplicateRecord(StateConflict):
    """A unique key such as a batch code was used twice."""

    code = "duplicate_record"
    http_status = 409


class ThresholdExceeded(StateConflict):
    """A measured value left its allowed band."""

    code = "threshold_exceeded"
    http_status = 409


class CommandRejected(StateConflict):
    """A command lost a priority conflict against another command."""

    code = "command_rejected"
    http_status = 409
