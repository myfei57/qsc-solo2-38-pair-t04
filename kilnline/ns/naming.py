"""Batch, work-order and carrier identifiers.

Codes are structured rather than opaque: the day, the shift and a per-shift
sequence are all recoverable, which is what lets the batch registry reject a
repeat without keeping a second index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from kilnline.errors import ValidationError

BATCH_PREFIX = "KB"
WORK_ORDER_PREFIX = "WO"
CAR_PREFIX = "CAR"

SHIFTS: tuple[str, ...] = ("D", "N", "S")
_DAY_PATTERN = re.compile(r"^\d{8}$")
_BATCH_PATTERN = re.compile(r"^(?P<prefix>[A-Z]{2})-(?P<day>\d{8})-(?P<shift>[DNS])-(?P<seq>\d{4,})$")
_WORK_ORDER_PATTERN = re.compile(r"^(?P<prefix>[A-Z]{2})-(?P<seq>\d{6,})$")
_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def validate_day(day: str) -> str:
    text = str(day).strip()
    if not _DAY_PATTERN.match(text):
        raise ValidationError("day must use the YYYYMMDD form", day=day)
    month = int(text[4:6])
    day_of_month = int(text[6:8])
    if not 1 <= month <= 12 or not 1 <= day_of_month <= 31:
        raise ValidationError("day is not a calendar date", day=day)
    return text


def normalize_shift(shift: str) -> str:
    text = str(shift).strip().upper()
    if text not in SHIFTS:
        raise ValidationError("shift must be one of D, N or S", shift=shift, allowed=list(SHIFTS))
    return text


def normalize_tag(text: str) -> str:
    candidate = re.sub(r"\s+", "_", str(text).strip().lower())
    if not _TAG_PATTERN.match(candidate):
        raise ValidationError("tag must be lowercase alphanumeric with _ . or -", tag=text)
    return candidate


def format_batch_code(day: str, shift: str, sequence: int) -> str:
    number = int(sequence)
    if number <= 0:
        raise ValidationError("batch sequence must be positive", sequence=sequence)
    return f"{BATCH_PREFIX}-{validate_day(day)}-{normalize_shift(shift)}-{number:04d}"


@dataclass(frozen=True)
class BatchCode:
    """Parsed form of a batch code."""

    day: str
    shift: str
    sequence: int

    @property
    def text(self) -> str:
        return format_batch_code(self.day, self.shift, self.sequence)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.text, "day": self.day, "shift": self.shift, "sequence": self.sequence}

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.text


def parse_batch_code(code: str) -> BatchCode:
    text = str(code).strip().upper()
    match = _BATCH_PATTERN.match(text)
    if match is None or match.group("prefix") != BATCH_PREFIX:
        raise ValidationError("batch code is malformed", code=code, expected="KB-YYYYMMDD-S-NNNN")
    return BatchCode(
        day=validate_day(match.group("day")),
        shift=normalize_shift(match.group("shift")),
        sequence=int(match.group("seq")),
    )


def next_batch_sequence(codes: Iterable[str], day: str, shift: str) -> int:
    """Highest sequence used on ``day``/``shift`` plus one, starting at 1."""

    wanted_day = validate_day(day)
    wanted_shift = normalize_shift(shift)
    highest = 0
    for code in codes:
        try:
            parsed = parse_batch_code(code)
        except ValidationError:
            continue
        if parsed.day == wanted_day and parsed.shift == wanted_shift:
            highest = max(highest, parsed.sequence)
    return highest + 1


def format_work_order(sequence: int) -> str:
    number = int(sequence)
    if number <= 0:
        raise ValidationError("work order sequence must be positive", sequence=sequence)
    return f"{WORK_ORDER_PREFIX}-{number:06d}"


def parse_work_order(code: str) -> int:
    text = str(code).strip().upper()
    match = _WORK_ORDER_PATTERN.match(text)
    if match is None or match.group("prefix") != WORK_ORDER_PREFIX:
        raise ValidationError("work order code is malformed", code=code, expected="WO-NNNNNN")
    return int(match.group("seq"))


def format_car_id(day: str, sequence: int) -> str:
    number = int(sequence)
    if number <= 0:
        raise ValidationError("carrier sequence must be positive", sequence=sequence)
    return f"{CAR_PREFIX}-{validate_day(day)}-{number:03d}"


def sequence_of_any(code: str) -> int:
    """Sequence carried by either a batch, a work order or a carrier id."""

    text = str(code).strip().upper()
    if text.startswith(f"{BATCH_PREFIX}-"):
        return parse_batch_code(text).sequence
    if text.startswith(f"{WORK_ORDER_PREFIX}-"):
        return parse_work_order(text)
    if text.startswith(f"{CAR_PREFIX}-"):
        return int(text.rsplit("-", 1)[-1])
    raise ValidationError("unrecognised identifier", code=code)
