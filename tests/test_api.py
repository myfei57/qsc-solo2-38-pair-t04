"""JSON console: routing, status mapping, schema validation and the socket adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from kilnline.console.api import ConsoleServer, Router, create_server
from kilnline.console.schemas import as_body, as_int_string, boolean, integer, number, text, text_list
from kilnline.console.service import ControlService
from kilnline.errors import ValidationError

BATCH = "KB-20260101-D-0007"
CARS = ("CAR-20260101-001", "CAR-20260101-002")


@pytest.fixture()
def router(prepared: ControlService) -> Router:
    return Router(prepared)


@pytest.fixture()
def server(prepared: ControlService):
    instance = create_server(prepared, host="127.0.0.1", port=0).start()
    yield instance
    instance.stop()


def call(server: ConsoleServer, method: str, path: str, payload: object = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        server.url() + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as failure:
        return failure.code, json.loads(failure.read().decode("utf-8"))


def test_health_route_reports_ok(router: Router) -> None:
    response = router.handle("GET", "/healthz")
    assert response.ok is True
    assert response.body["status"] == "ok"
    assert response.body["checks"]["ledger"] is True
    assert response.body["version"] == "1.0.0"


def test_status_route_lists_the_subsystems(router: Router) -> None:
    response = router.handle("GET", "/api/v1/status")
    assert response.status == 200
    body = response.body
    assert body["ready"] is True
    assert body["production"]["next"] == "drying"
    assert "ignition" in body and "conveyor" in body and "zone_map" in body


def test_unknown_route_and_wrong_method(router: Router) -> None:
    missing = router.handle("GET", "/api/v1/nope")
    assert missing.status == 404
    assert missing.body["code"] == "not_found"
    wrong = router.handle("POST", "/healthz")
    assert wrong.status == 405
    assert wrong.body["allowed"] == ["/healthz"]


def test_missing_and_out_of_range_fields_return_400(router: Router) -> None:
    missing = router.handle("POST", "/api/v1/roller/start", body={})
    assert missing.status == 400
    assert missing.body["error"]["code"] == "validation_error"
    assert missing.body["error"]["context"]["field"] == "target_mpm"
    negative = router.handle("POST", "/api/v1/roller/start", body={"target_mpm": -3.0})
    assert negative.status == 400
    assert negative.body["error"]["context"]["minimum"] == 0.0


def test_durability_and_interlock_refusals_keep_their_status(
    service: ControlService,
) -> None:
    router = Router(service)
    unsaved = router.handle("POST", "/api/v1/roller/start", body={"target_mpm": 12.0})
    assert unsaved.status == 409
    assert unsaved.body["error"]["code"] == "durability_error"
    service.confirm_spacing(car_id=CARS[0], gap_mm=220.0)
    blocked = router.handle("POST", "/api/v1/conv/feed", body={"car_id": CARS[0]})
    assert blocked.status == 423
    assert blocked.body["error"]["code"] == "interlock_active"


def test_state_conflict_and_not_found_status_mapping(router: Router) -> None:
    idle = router.handle("POST", "/api/v1/roller/stop", body={})
    assert idle.status == 409
    assert idle.body["error"]["code"] == "state_conflict"
    unknown = router.handle("POST", "/api/v1/latch/trip", body={"name": "nope", "reason": "x"})
    assert unknown.status == 404
    assert unknown.body["error"]["code"] == "not_found"


def test_routes_table_lists_gets_and_posts(router: Router) -> None:
    routes = router.routes
    assert "/healthz" in routes["GET"]
    assert "/api/v1/status" in routes["GET"]
    assert "/api/v1/prepare" in routes["POST"]
    assert "/api/v1/batch/run" in routes["POST"]
    assert len(routes["GET"]) >= 20
    assert len(routes["POST"]) >= 25


def test_records_route_applies_the_query_filter(prepared: ControlService, router: Router) -> None:
    prepared.publish_parameters({"peak_c": 1180.0}, author="lead")
    every = router.handle("GET", "/api/v1/records")
    assert every.status == 200
    assert every.body["count"] >= 1
    filtered = router.handle("GET", "/api/v1/records", query={"kind": "put", "limit": "1"})
    assert filtered.body["count"] == 1
    assert filtered.body["filter"]["kinds"] == ["put"]
    bad = router.handle("GET", "/api/v1/records", query={"after": "soon"})
    assert bad.status == 400
    assert bad.body["error"]["context"]["parameter"] == "after"


def test_ledger_routes_commit_and_tombstone(prepared: ControlService, router: Router) -> None:
    prepared.open_batch(BATCH, car_count=1)
    sequence = prepared.stream.committed()[-1].sequence
    staged = prepared.stream.put("probe:extra", {"ok": True}, written_at=prepared.now())
    assert staged.sequence == sequence + 1
    assert router.handle("GET", "/api/v1/ledger/staged").body["staged"][0]["key"] == "probe:extra"
    committed = router.handle("POST", "/api/v1/ledger/commit", body={})
    assert committed.status == 200
    assert committed.body["watermark"] == staged.sequence
    tombstone = router.handle(
        "POST",
        "/api/v1/ledger/tombstone",
        body={"sequence": sequence, "reason": "wrong batch"},
    )
    assert tombstone.status == 200
    assert tombstone.body["tombstone"]["target"] == sequence
    assert prepared.batches.seen(BATCH) is False
    replay = router.handle("GET", "/api/v1/ledger/replay", query={"after": "0"})
    assert replay.body["voided"] == [sequence]


def test_prepare_route_drives_the_commissioning_cycle(service: ControlService) -> None:
    router = Router(service)
    response = router.handle(
        "POST",
        "/api/v1/prepare",
        body={"peak_c": 620.0, "step": 30.0, "author": "console"},
    )
    assert response.status == 200
    assert response.body["ready"] is True
    gates = router.handle("GET", "/api/v1/gates").body
    assert "kiln.temp_persisted" in gates["open"]
    assert "kiln.grate_persisted" in gates["open"]
    assert "conv.spacing_confirmed" in gates["closed"]
    calibrated = router.handle(
        "POST",
        "/api/v1/roller/calibrate",
        body={"pulses_per_meter": 120.0, "author": "console"},
    )
    assert calibrated.status == 200
    started = router.handle("POST", "/api/v1/roller/start", body={"target_mpm": 12.0})
    assert started.status == 200
    assert started.body["running"] is True
    ticked = router.handle("POST", "/api/v1/tick", body={"dt": 5.0})
    assert ticked.status == 200
    assert ticked.body["dt"] == 5.0


def test_batch_run_route_completes_the_line_cycle(prepared: ControlService, router: Router) -> None:
    response = router.handle(
        "POST",
        "/api/v1/batch/run",
        body={
            "code": BATCH,
            "cars": list(CARS),
            "gap_mm": 220.0,
            "density_g_cm3": 1.65,
            "speed_mpm": 12.0,
            "step": 30.0,
        },
    )
    assert response.status == 200
    assert response.body["batch"]["status"] == "closed"
    assert response.body["production"]["complete"] is True
    repeated = router.handle(
        "POST",
        "/api/v1/batch/run",
        body={
            "code": BATCH,
            "cars": list(CARS),
            "gap_mm": 220.0,
            "density_g_cm3": 1.65,
            "speed_mpm": 12.0,
            "step": 30.0,
        },
    )
    assert repeated.status == 409
    assert repeated.body["error"]["code"] == "duplicate_record"


def test_batch_run_route_requires_a_ready_line(service: ControlService) -> None:
    router = Router(service)
    response = router.handle(
        "POST",
        "/api/v1/batch/run",
        body={"code": BATCH, "cars": list(CARS), "gap_mm": 220.0, "density_g_cm3": 1.65, "speed_mpm": 12.0},
    )
    assert response.status == 409
    assert response.body["error"]["context"]["ready"] is False


def test_http_server_serves_the_health_endpoint(server: ConsoleServer) -> None:
    status, body = call(server, "GET", "/healthz")
    assert status == 200
    assert body["status"] == "ok"
    assert server.port > 0
    assert server.url().startswith("http://127.0.0.1:")


def test_http_server_round_trips_a_json_body(server: ConsoleServer) -> None:
    status, body = call(server, "POST", "/api/v1/roller/start", {"target_mpm": 12.0})
    assert status == 200
    assert body["target_mpm"] == 12.0
    status, body = call(server, "GET", "/api/v1/status")
    assert status == 200
    assert body["roller"]["running"] is True
    status, body = call(server, "GET", "/api/v1/nope")
    assert status == 404
    assert body["code"] == "not_found"


def test_http_server_rejects_a_non_json_body(server: ConsoleServer) -> None:
    request = urllib.request.Request(
        server.url() + "/api/v1/roller/start",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as failure:
        urllib.request.urlopen(request, timeout=5)
    assert failure.value.code == 400
    payload = json.loads(failure.value.read().decode("utf-8"))
    assert payload["error"]["message"] == "body is not JSON"


def test_schema_helpers_validate_and_default() -> None:
    body = as_body({"name": "op", "count": "3", "flag": "yes"})
    assert text(body, "name") == "op"
    assert integer(body, "count") == 3
    assert boolean(body, "flag") is True
    assert number(body, "missing", required=False) is None
    assert integer(body, "missing", required=False, default=2) == 2
    assert as_int_string({"limit": "9"}, "limit", default=1) == 9
    assert text_list(body, "cars", required=False, default=["a", "b"]) == ["a", "b"]
    assert text_list({"cars": "a, b"}, "cars") == ["a", "b"]
    with pytest.raises(ValidationError):
        as_body("nope")
    with pytest.raises(ValidationError):
        text(body, "name", minimum_length=5)
    with pytest.raises(ValidationError):
        number(body, "count", maximum=1.0)
    with pytest.raises(ValidationError):
        boolean(body, "name")
    with pytest.raises(ValidationError):
        as_int_string({"limit": "many"}, "limit", default=1)


def test_operator_routes_cover_guard_speed_alarms_and_lookups(
    prepared: ControlService,
    router: Router,
) -> None:
    armed = router.handle("POST", "/api/v1/guard", body={"armed": False})
    assert armed.status == 200
    assert armed.body == {"armed": False, "guard_armed": False, "blocked": False}
    assert router.handle("POST", "/api/v1/guard", body={"armed": True}).body["guard_armed"] is True

    prepared.start_rollers(target_mpm=12.0)
    speed = router.handle("POST", "/api/v1/roller/setpoint", body={"target_mpm": 18.0})
    assert speed.status == 200
    assert speed.body["target_mpm"] == 18.0

    prepared.calibrate_slurry(density_g_cm3=1.65, author="console")
    slurry = router.handle("GET", "/api/v1/slurry")
    assert slurry.body["reference_density"] == 1.65
    assert slurry.body["slurry"]["band"]["high"] == 1.75

    window = router.handle("GET", "/api/v1/curve/window", query={"elapsed_s": "600"})
    assert window.status == 200
    assert window.body["target_c"] > 0.0
    bad = router.handle("GET", "/api/v1/curve/window", query={"elapsed_s": "soon"})
    assert bad.status == 400

    prepared.open_batch(BATCH, car_count=1)
    lookup = router.handle("GET", "/api/v1/batch", query={"code": BATCH})
    assert lookup.body["batch"]["code"] == BATCH
    assert router.handle("GET", "/api/v1/batch", query={"code": "KB-20260102-D-0001"}).status == 404

    prepared.publish_parameters({"peak_c": 1180.0}, author="lead")
    parameter_records = router.handle("GET", "/api/v1/params/records")
    assert parameter_records.body["count"] == 1
    assert parameter_records.body["records"][0]["key"] == prepared.params.key

    recorder = router.handle("GET", "/api/v1/recorder")
    assert recorder.body["ledger"]["watermark"]["watermark"] == prepared.stream.watermark
    assert recorder.body["audit"]
    assert recorder.body["snapshot"]["reason"] is not None

    prepared.alarms.raise_alarm("manual.check", "look at the kiln", at=prepared.now())
    cleared = router.handle("POST", "/api/v1/alarms/clear", body={})
    assert cleared.status == 200
    assert [item["code"] for item in cleared.body["cleared"]] == ["manual.check"]
    assert cleared.body["summary"]["active"] == 0
