"""Identifier construction and validation."""

from __future__ import annotations

import pytest

from kilnline.errors import ValidationError
from kilnline.ns.naming import (
    format_batch_code,
    format_car_id,
    format_work_order,
    next_batch_sequence,
    normalize_shift,
    normalize_tag,
    parse_batch_code,
    parse_work_order,
    sequence_of_any,
    validate_day,
)


def test_batch_code_round_trips() -> None:
    parsed = parse_batch_code("kb-20260101-d-0007")
    assert parsed.day == "20260101"
    assert parsed.shift == "D"
    assert parsed.sequence == 7
    assert parsed.text == "KB-20260101-D-0007"
    assert parsed.as_dict()["code"] == "KB-20260101-D-0007"
    assert str(parsed) == "KB-20260101-D-0007"
    assert format_batch_code("20260101", "n", 12) == "KB-20260101-N-0012"


def test_batch_code_rejects_malformed_input() -> None:
    for bad in ("KB-2026-01-01-D-1", "XX-20260101-D-0001", "KB-20260101-X-0001", "KB-20260101-D-0"):
        with pytest.raises(ValidationError):
            parse_batch_code(bad)
    with pytest.raises(ValidationError):
        format_batch_code("20261301", "D", 1)
    with pytest.raises(ValidationError):
        format_batch_code("20260101", "D", 0)


def test_day_and_shift_validation() -> None:
    assert validate_day("20260101") == "20260101"
    for bad in ("2026-01-01", "2026011", "abcdefgh"):
        with pytest.raises(ValidationError):
            validate_day(bad)
    assert normalize_shift(" d ") == "D"
    assert normalize_shift("s") == "S"
    with pytest.raises(ValidationError):
        normalize_shift("Z")


def test_next_batch_sequence_uses_the_highest_of_the_shift() -> None:
    codes = [
        "KB-20260101-D-0003",
        "KB-20260101-D-0009",
        "KB-20260101-N-0004",
        "KB-20260102-D-0011",
        "not-a-code",
    ]
    assert next_batch_sequence(codes, "20260101", "D") == 10
    assert next_batch_sequence(codes, "20260101", "N") == 5
    assert next_batch_sequence(codes, "20260103", "D") == 1
    assert next_batch_sequence([], "20260103", "D") == 1


def test_work_order_round_trips() -> None:
    assert format_work_order(123) == "WO-000123"
    assert parse_work_order("wo-000123") == 123
    with pytest.raises(ValidationError):
        format_work_order(0)
    with pytest.raises(ValidationError):
        parse_work_order("WO-123")
    with pytest.raises(ValidationError):
        parse_work_order("KB-000123")


def test_carrier_ids_and_tags() -> None:
    assert format_car_id("20260101", 14) == "CAR-20260101-014"
    with pytest.raises(ValidationError):
        format_car_id("20260101", -1)
    assert normalize_tag(" Firing  Zone ") == "firing_zone"
    assert normalize_tag("roller.speed") == "roller.speed"
    with pytest.raises(ValidationError):
        normalize_tag("bad/tag")


def test_sequence_of_any_reads_every_identifier_shape() -> None:
    assert sequence_of_any("KB-20260101-D-0007") == 7
    assert sequence_of_any("WO-000123") == 123
    assert sequence_of_any("CAR-20260101-014") == 14
    with pytest.raises(ValidationError):
        sequence_of_any("SOMETHING-ELSE")
