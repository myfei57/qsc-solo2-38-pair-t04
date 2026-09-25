"""Version semantics: generations, confirmation slips and expiring baselines."""

from __future__ import annotations

from pathlib import Path

import pytest

from kilnline.errors import (
    CommandRejected,
    GenerationExpired,
    GenerationMismatch,
    NotFoundError,
    ValidationError,
)
from kilnline.ledger.stream import EventStream
from kilnline.params.baseline import BaselineRegistry
from kilnline.params.confirmation import ConfirmationBook, ConfirmationExpired
from kilnline.params.generation import Generation, GenerationCounter, digest_of
from kilnline.params.registry import ParameterRegistry
from kilnline.store.json_store import JsonFileStore


def make_env(tmp_path: Path) -> tuple[JsonFileStore, EventStream]:
    store = JsonFileStore(tmp_path)
    stream = EventStream(tmp_path / "ledger.jsonl", store)
    return store, stream


def test_generation_counter_never_moves_backwards() -> None:
    counter = GenerationCounter()
    assert counter.current == 0
    assert counter.bump() == 1
    assert counter.bump() == 2
    assert counter.adopt(1) == 2
    assert counter.adopt(7) == 7
    assert counter.is_current(7)


def test_generation_compares_by_number() -> None:
    older = Generation(number=1, digest="a", issued_at=1.0, issuer="op")
    newer = Generation(number=2, digest="b", issued_at=2.0, issuer="op")
    assert older.precedes(newer)
    assert not newer.precedes(older)
    assert Generation.from_dict(newer.as_dict()) == newer


def test_digest_is_order_independent() -> None:
    assert digest_of({"a": 1.0, "b": 2.0}) == digest_of({"b": 2.0, "a": 1.0})
    assert digest_of({"a": 1.0}) != digest_of({"a": 1.5})


def test_publish_increments_generation_and_digests_values(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    first = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    second = registry.publish({"roller_speed_mpm": 14.0}, published_at=20.0, author="op")
    assert (first.generation, second.generation) == (1, 2)
    assert second.digest == digest_of({"roller_speed_mpm": 14.0})
    assert registry.generation == 2
    assert registry.current() == second
    assert registry.get(1) == first
    with pytest.raises(NotFoundError):
        registry.get(9)


def test_publish_is_durable_and_lands_in_the_stream(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    registry.publish({"peak_c": 1180.0}, published_at=10.0, author="op")
    assert store.exists("parameter-set")
    live, _ = stream.resolve(registry.key)
    assert live is not None
    assert live.generation == 1
    assert live.payload["values"] == {"peak_c": 1180.0}
    with pytest.raises(ValidationError):
        registry.publish({}, published_at=11.0, author="op")


def test_history_keeps_every_published_generation(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    for index in range(3):
        registry.publish({"n": float(index)}, published_at=float(index), author="op")
    assert [item.generation for item in registry.history()] == [1, 2, 3]
    assert registry.published_count == 3
    assert registry.age_seconds(now=5.0) == 3.0
    assert registry.is_current(3) is True
    assert registry.is_current(2) is False


def test_registry_restores_generation_from_ledger_records(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    registry.publish({"peak_c": 1180.0}, published_at=10.0, author="op")
    registry.publish({"peak_c": 1200.0}, published_at=20.0, author="op")
    store.delete("parameter-set")
    recovered = ParameterRegistry(store, stream)
    assert recovered.generation == 0
    restored = recovered.restore(stream.committed())
    assert restored is not None and restored.generation == 2
    assert recovered.current().values == {"peak_c": 1200.0}


def test_confirmation_is_bound_to_the_generation_and_digest(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    published = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    book = ConfirmationBook(store, ttl_s=60.0)
    slip = book.issue(scope="batch_start", parameter_set=published, issued_at=11.0, issued_by="lead")
    accepted = book.verify(slip.token, now=12.0, scope="batch_start", parameter_set=published)
    assert accepted.token == slip.token
    assert accepted.parameter_generation == 1
    assert accepted.digest == published.digest


def test_confirmation_past_its_ttl_is_rejected(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    published = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    book = ConfirmationBook(store, ttl_s=30.0)
    slip = book.issue(scope="batch_start", parameter_set=published, issued_at=10.0, issued_by="lead")
    assert slip.expires_at == 40.0
    with pytest.raises(ConfirmationExpired):
        book.verify(slip.token, now=41.0, parameter_set=published)
    assert book.active(now=41.0) == []
    assert [item.token for item in book.expired(now=41.0)] == [slip.token]


def test_confirmation_against_a_superseded_generation_is_rejected(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    first = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    book = ConfirmationBook(store, ttl_s=600.0)
    slip = book.issue(scope="batch_start", parameter_set=first, issued_at=11.0, issued_by="lead")
    second = registry.publish({"roller_speed_mpm": 15.0}, published_at=12.0, author="op")
    with pytest.raises(GenerationMismatch):
        book.verify(slip.token, now=13.0, parameter_set=second)
    # The slip is still valid while nothing supersedes it.
    assert book.verify(slip.token, now=13.0, parameter_set=first).token == slip.token


def test_revoked_confirmation_is_rejected(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    published = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    book = ConfirmationBook(store, ttl_s=600.0)
    slip = book.issue(scope="batch_start", parameter_set=published, issued_at=11.0, issued_by="lead")
    book.revoke(slip.token, at=12.0)
    with pytest.raises(CommandRejected):
        book.verify(slip.token, now=13.0, parameter_set=published)
    assert book.active(now=13.0) == []


def test_confirmation_for_another_scope_is_rejected(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    registry = ParameterRegistry(store, stream)
    published = registry.publish({"roller_speed_mpm": 12.0}, published_at=10.0, author="op")
    book = ConfirmationBook(store, ttl_s=600.0)
    slip = book.issue(scope="batch_start", parameter_set=published, issued_at=11.0, issued_by="lead")
    with pytest.raises(ValidationError):
        book.verify(slip.token, now=12.0, scope="kiln_shutdown", parameter_set=published)
    with pytest.raises(ValidationError):
        book.issue(scope="  ", parameter_set=published, issued_at=12.0, issued_by="lead")


def test_baseline_expires_after_its_lag_budget(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    baselines = BaselineRegistry(store, stream)
    baseline = baselines.capture(
        "kiln.temp",
        {"preheat": 900.0, "firing": 1180.0},
        captured_at=100.0,
        generation=1,
        author="op",
        max_lag_s=50.0,
    )
    assert baseline.age_seconds(now=120.0) == 20.0
    assert baseline.expired(now=120.0) is False
    assert baselines.require_fresh("kiln.temp", now=140.0).generation == 1
    with pytest.raises(GenerationExpired):
        baselines.require_fresh("kiln.temp", now=151.0)
    assert baselines.expired_keys(now=151.0) == ["kiln.temp"]
    with pytest.raises(NotFoundError):
        baselines.require_fresh("glaze.density", now=100.0)


def test_baseline_require_current_rejects_a_superseded_generation(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    baselines = BaselineRegistry(store, stream)
    baselines.capture(
        "roller.speed",
        {"pulses_per_meter": 120.0},
        captured_at=100.0,
        generation=1,
        author="op",
        max_lag_s=600.0,
    )
    assert baselines.latest("roller.speed") is not None
    with pytest.raises(GenerationMismatch):
        baselines.require_current("roller.speed", now=110.0, generation=2)
    assert baselines.require_current("roller.speed", now=110.0, generation=1).generation == 1


def test_baselines_are_restored_from_a_ledger_replay(tmp_path: Path) -> None:
    store, stream = make_env(tmp_path)
    baselines = BaselineRegistry(store, stream)
    baselines.capture(
        "kiln.temp",
        {"firing": 1180.0},
        captured_at=100.0,
        generation=1,
        author="op",
        max_lag_s=600.0,
    )
    store.delete("baselines")
    recovered = BaselineRegistry(store, stream)
    assert recovered.keys() == []
    restored = recovered.restore(stream.committed())
    assert restored == ["kiln.temp"]
    assert recovered.require_fresh("kiln.temp", now=110.0).value("firing") == 1180.0


def test_service_publishes_and_confirms_parameter_generations(service) -> None:
    published = service.publish_parameters({"peak_c": 1180.0}, author="lead")
    assert published["generation"] == 1
    slip = service.issue_confirmation(scope="batch_start", issued_by="lead")
    accepted = service.verify_confirmation(slip["token"], scope="batch_start")
    assert accepted["parameter_generation"] == 1
    service.publish_parameters({"peak_c": 1200.0}, author="lead")
    with pytest.raises(GenerationMismatch):
        service.verify_confirmation(slip["token"], scope="batch_start")
