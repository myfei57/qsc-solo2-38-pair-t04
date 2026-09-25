"""Queries over the committed record stream.

The filter is the same object whether it was built in Python or parsed from an
HTTP query string, and it only ever walks committed records: a staged write is
invisible to a query by construction, not by a flag the caller could forget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from kilnline.errors import ValidationError
from kilnline.ledger.records import KNOWN_KINDS, LedgerRecord
from kilnline.ledger.stream import EventStream


def _optional_int(mapping: Mapping[str, Any], name: str) -> int | None:
    if name not in mapping or mapping[name] in (None, ""):
        return None
    try:
        return int(mapping[name])
    except (TypeError, ValueError) as exc:
        raise ValidationError("query parameter must be an integer", parameter=name, value=mapping[name]) from exc


def _optional_float(mapping: Mapping[str, Any], name: str) -> float | None:
    if name not in mapping or mapping[name] in (None, ""):
        return None
    try:
        return float(mapping[name])
    except (TypeError, ValueError) as exc:
        raise ValidationError("query parameter must be a number", parameter=name, value=mapping[name]) from exc


@dataclass(frozen=True)
class QueryFilter:
    """Narrowing applied to committed records."""

    key: str | None = None
    kinds: tuple[str, ...] | None = None
    after: int = 0
    min_generation: int | None = None
    max_generation: int | None = None
    since: float | None = None
    until: float | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        if int(self.after) < 0:
            raise ValidationError("query watermark must not be negative", after=self.after)
        if self.limit is not None and int(self.limit) <= 0:
            raise ValidationError("query limit must be positive", limit=self.limit)
        if self.kinds is not None:
            for kind in self.kinds:
                if kind not in KNOWN_KINDS:
                    raise ValidationError("unknown record kind in filter", kind=kind, allowed=list(KNOWN_KINDS))
        if self.since is not None and self.until is not None and float(self.since) > float(self.until):
            raise ValidationError(
                "query time window is inverted",
                since=self.since,
                until=self.until,
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kinds": None if self.kinds is None else list(self.kinds),
            "after": int(self.after),
            "min_generation": self.min_generation,
            "max_generation": self.max_generation,
            "since": self.since,
            "until": self.until,
            "limit": self.limit,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "QueryFilter":
        kinds: tuple[str, ...] | None = None
        raw_kinds = mapping.get("kind", mapping.get("kinds"))
        if raw_kinds:
            if isinstance(raw_kinds, str):
                parts = tuple(part.strip() for part in raw_kinds.split(",") if part.strip())
            else:
                parts = tuple(str(part).strip() for part in raw_kinds)
            kinds = parts or None
        key = mapping.get("key")
        return cls(
            key=None if key in (None, "") else str(key),
            kinds=kinds,
            after=_optional_int(mapping, "after") or 0,
            min_generation=_optional_int(mapping, "min_generation"),
            max_generation=_optional_int(mapping, "max_generation"),
            since=_optional_float(mapping, "since"),
            until=_optional_float(mapping, "until"),
            limit=_optional_int(mapping, "limit"),
        )


@dataclass(frozen=True)
class QueryResult:
    """Records plus the filter that produced them."""

    filter: QueryFilter
    records: tuple[LedgerRecord, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.records)

    def as_dict(self) -> dict[str, Any]:
        return {
            "filter": self.filter.as_dict(),
            "count": self.count,
            "sequences": [record.sequence for record in self.records],
            "records": [record.as_dict() for record in self.records],
        }


class RecordQuery:
    """Applies :class:`QueryFilter` to a stream's committed records."""

    def __init__(self, stream: EventStream) -> None:
        self._stream = stream

    def run(self, filter: QueryFilter | None = None) -> list[LedgerRecord]:
        criteria = QueryFilter() if filter is None else filter
        records = self._stream.read_committed(
            after=criteria.after,
            key=criteria.key,
            kinds=criteria.kinds,
            min_generation=criteria.min_generation,
            max_generation=criteria.max_generation,
            since=criteria.since,
            until=criteria.until,
        )
        if criteria.limit is not None:
            records = records[-int(criteria.limit) :]
        return records

    def result(self, filter: QueryFilter | None = None) -> QueryResult:
        criteria = QueryFilter() if filter is None else filter
        return QueryResult(filter=criteria, records=tuple(self.run(criteria)))

    def count(self, filter: QueryFilter | None = None) -> int:
        return len(self.run(filter))

    def keys(self, filter: QueryFilter | None = None) -> list[str]:
        seen: list[str] = []
        for record in self.run(filter):
            if record.key not in seen:
                seen.append(record.key)
        return seen

    def latest(self, filter: QueryFilter | None = None) -> LedgerRecord | None:
        records = self.run(filter)
        return records[-1] if records else None

    def group_by_key(self, filter: QueryFilter | None = None) -> dict[str, Sequence[int]]:
        grouped: dict[str, list[int]] = {}
        for record in self.run(filter):
            grouped.setdefault(record.key, []).append(record.sequence)
        return {key: grouped[key] for key in sorted(grouped)}
