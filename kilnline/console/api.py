"""JSON console transport.

Routing is a pure function: ``Router.handle(method, path, query, body)`` never
touches a socket, so the whole API can be exercised without opening a port.
:class:`ConsoleServer` is the thin adapter that binds the router to
``http.server`` and exposes the health endpoint the container probes.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from kilnline.console.schemas import (
    as_body,
    as_float_string,
    as_int_string,
    boolean,
    integer,
    number,
    optional_text,
    text,
    text_list,
)
from kilnline.console.service import ControlService
from kilnline.errors import KilnError, NotFoundError
from kilnline.judge.query import QueryFilter

Handler = Callable[[Mapping[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Response:
    """One HTTP response, produced without any transport involvement."""

    status: int
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def encode(self) -> bytes:
        return json.dumps(self.body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


class Router:
    """Maps ``(method, path)`` onto a service call."""

    def __init__(self, service: ControlService) -> None:
        self.service = service
        self._gets: dict[str, Handler] = {
            "/healthz": self._health,
            "/api/v1/status": self._status,
            "/api/v1/audit": self._audit,
            "/api/v1/alarms": self._alarms,
            "/api/v1/snapshot": self._snapshot,
            "/api/v1/ledger": self._ledger,
            "/api/v1/ledger/staged": self._staged,
            "/api/v1/ledger/replay": self._replay,
            "/api/v1/params": self._params,
            "/api/v1/confirmations": self._confirmations,
            "/api/v1/baselines": self._baselines,
            "/api/v1/zone-map": self._zone_map,
            "/api/v1/gates": self._gates,
            "/api/v1/latches": self._latches,
            "/api/v1/batches": self._batches,
            "/api/v1/history": self._history,
            "/api/v1/rules": self._rules,
            "/api/v1/records": self._records,
            "/api/v1/curve": self._curve,
            "/api/v1/curve/window": self._curve_window,
            "/api/v1/conveyor": self._conveyor,
            "/api/v1/dryer": self._dryer,
            "/api/v1/slurry": self._slurry,
            "/api/v1/recorder": self._recorder,
            "/api/v1/batch": self._batch_lookup,
            "/api/v1/params/records": self._param_records,
            "/api/v1/probes": self._probes,
            "/api/v1/monitor": self._monitor,
        }
        self._posts: dict[str, Handler] = {
            "/api/v1/prepare": self._prepare,
            "/api/v1/tick": self._tick,
            "/api/v1/burner/ignite": self._ignite,
            "/api/v1/burner/extinguish": self._extinguish,
            "/api/v1/burner/recover": self._recover_burner,
            "/api/v1/burner/flame-lost": self._flame_lost,
            "/api/v1/probe/calibrate": self._calibrate_probes,
            "/api/v1/roller/calibrate": self._calibrate_roller,
            "/api/v1/roller/start": self._start_rollers,
            "/api/v1/roller/stop": self._stop_rollers,
            "/api/v1/roller/speed": self._roller_speed,
            "/api/v1/roller/setpoint": self._set_roller_speed,
            "/api/v1/slurry/calibrate": self._calibrate_slurry,
            "/api/v1/kiln/grate": self._persist_grate,
            "/api/v1/kiln/extend": self._extend_section,
            "/api/v1/zone-map/refresh": self._refresh_zone_map,
            "/api/v1/temperature/baseline": self._persist_baseline,
            "/api/v1/latch/reset": self._reset_latch,
            "/api/v1/latch/trip": self._trip_latch,
            "/api/v1/params/publish": self._publish_parameters,
            "/api/v1/params/confirmation": self._issue_confirmation,
            "/api/v1/params/confirmation/verify": self._verify_confirmation,
            "/api/v1/ledger/commit": self._commit_ledger,
            "/api/v1/ledger/rollback": self._rollback_ledger,
            "/api/v1/ledger/tombstone": self._tombstone,
            "/api/v1/dryer/load": self._load_car,
            "/api/v1/dryer/complete": self._complete_drying,
            "/api/v1/dryer/dry": self._dry_car,
            "/api/v1/glaze/start": self._start_glazing,
            "/api/v1/conv/spacing": self._confirm_spacing,
            "/api/v1/conv/feed": self._feed_car,
            "/api/v1/batch/open": self._open_batch,
            "/api/v1/batch/close": self._close_batch,
            "/api/v1/batch/void": self._void_batch,
            "/api/v1/batch/run": self._run_batch,
            "/api/v1/monitor/start": self._start_monitor,
            "/api/v1/monitor/stop": self._stop_monitor,
            "/api/v1/guard": self._set_guard,
            "/api/v1/alarms/clear": self._clear_alarms,
        }

    @property
    def routes(self) -> dict[str, list[str]]:
        return {"GET": sorted(self._gets), "POST": sorted(self._posts)}

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> Response:
        verb = str(method).upper()
        route = str(path).rstrip("/") or "/"
        tables = {"GET": self._gets, "POST": self._posts}
        handler = tables.get(verb, {}).get(route)
        if handler is None:
            elsewhere = sorted(
                other
                for other_verb, table in tables.items()
                if other_verb != verb
                for other in table
                if other == route
            )
            return Response(
                405 if elsewhere else 404,
                {
                    "code": "method_not_allowed" if elsewhere else "not_found",
                    "message": "no route for this request",
                    "method": verb,
                    "path": route,
                    "allowed": elsewhere,
                },
            )
        try:
            payload = handler(dict(query or {}), as_body(body))
        except KilnError as exc:
            return Response(exc.http_status, {"error": exc.as_dict()})
        return Response(200, payload)

    # ---------------------------------------------------------------- GET routes

    def _health(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.health()

    def _status(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.status()

    def _audit(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        limit = as_int_string(query, "limit", default=50)
        records = self.service.audit_tail(limit=limit)
        return {"records": records, "count": len(records)}

    def _alarms(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": self.service.alarms.summary(),
            "active": [alarm.as_dict() for alarm in self.service.alarms.active()],
            "history": [alarm.as_dict() for alarm in self.service.alarms.history()],
        }

    def _snapshot(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        latest = self.service.latest_snapshot()
        return {
            "latest": None if latest is None else latest.as_dict(),
            "history": [item.as_dict() for item in self.service.snapshots.history()],
        }

    def _ledger(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.ledger_state()

    def _staged(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {"staged": self.service.staged_records()}

    def _replay(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        after = as_int_string(query, "after", default=0)
        return self.service.replay_ledger(after_watermark=after)

    def _params(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {
            "generation": self.service.params.generation,
            "history": [item.as_dict() for item in self.service.params.history()],
        }

    def _confirmations(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        book = self.service.confirmations
        return {
            "ttl_s": book.ttl_s,
            "entries": [entry.as_dict() for entry in book.entries()],
        }

    def _baselines(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        now = self.service.now()
        return {
            "baselines": {
                key: baseline.as_dict()
                for key in self.service.baselines.keys()
                if (baseline := self.service.baselines.latest(key)) is not None
            },
            "expired": self.service.baselines.expired_keys(now=now),
            "ages_s": self.service.baselines.ages(now=now),
        }

    def _zone_map(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        now = self.service.now()
        return {
            "zone_map": self.service.zone_map.snapshot(now=now),
            "history": [item.as_dict() for item in self.service.zone_map.history()],
        }

    def _gates(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.gate_inventory()

    def _latches(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.latch_states()

    def _batches(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.batches.snapshot()

    def _history(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        key = query.get("key")
        if key:
            return {"key": str(key), "entries": [item.as_dict() for item in self.service.history.history(str(key))]}
        return {"keys": self.service.history_keys()}

    def _rules(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {"rules": self.service.rules.snapshot()}

    def _records(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.query_records(QueryFilter.from_mapping(query))

    def _curve(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        samples = as_int_string(query, "samples", default=8)
        return {
            "curve": self.service.curve.as_dict(),
            "trace": self.service.curve_trace(samples=samples),
        }

    def _curve_window(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.curve_window(elapsed_s=as_float_string(query, "elapsed_s", default=0.0))

    def _conveyor(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.conveyor.snapshot()

    def _dryer(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.dryer.snapshot()

    def _slurry(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        reference = self.service.baselines.latest(self.service.slurry.key)
        return {
            "reference_density": None if reference is None else self.service.slurry_reference_density(),
            "slurry": self.service.slurry.snapshot(now=self.service.now()),
        }

    def _recorder(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.recorder()

    def _batch_lookup(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {"batch": self.service.batch_record(text(query, "code")).as_dict()}

    def _param_records(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.query_parameters()

    def _probes(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.probes.snapshot(now=self.service.now())

    def _monitor(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.monitor.snapshot()

    # --------------------------------------------------------------- POST routes

    def _prepare(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.prepare(
            peak_c=number(body, "peak_c", required=False, default=None),
            author=optional_text(body, "author", default="operator"),
            step=number(body, "step", required=False, default=None),
        )

    def _tick(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        report = self.service.tick_once(number(body, "dt", required=False, default=None))
        return report.as_dict()

    def _ignite(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.ignite()

    def _extinguish(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.extinguish(reason=optional_text(body, "reason", default="requested"))

    def _recover_burner(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.recover_burner(
            alarm_reset=boolean(body, "alarm_reset", default=False),
            clear_alarms=boolean(body, "clear_alarms", default=True),
        )

    def _flame_lost(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.burner_flame_lost(reason=optional_text(body, "reason", default="flame_proof_lost"))

    def _calibrate_probes(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {
            "probes": self.service.calibrate_probes(
                by=text(body, "by"),
                span_c=number(body, "span_c", required=False, default=0.0),
            )
        }

    def _calibrate_roller(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.calibrate_roller(
            pulses_per_meter=number(body, "pulses_per_meter", minimum=0.0001),
            author=text(body, "author"),
            max_lag_s=number(body, "max_lag_s", required=False, default=None),
        )

    def _start_rollers(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.start_rollers(
            target_mpm=number(body, "target_mpm", minimum=0.0),
            checked=boolean(body, "checked", default=True),
        )

    def _stop_rollers(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.stop_rollers(reason=optional_text(body, "reason", default="requested"))

    def _roller_speed(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.roller_speed_from_pulses(number(body, "pulses_per_s", minimum=0.0))

    def _set_roller_speed(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.set_roller_speed(target_mpm=number(body, "target_mpm", minimum=0.0))

    def _calibrate_slurry(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.calibrate_slurry(
            density_g_cm3=number(body, "density_g_cm3", minimum=0.0),
            author=text(body, "author"),
            max_lag_s=number(body, "max_lag_s", required=False, default=None),
        )

    def _persist_grate(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.persist_grate(author=optional_text(body, "author", default="operator"))

    def _extend_section(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.extend_section(
            zone=text(body, "zone"),
            added_m=number(body, "added_m", minimum=0.0001),
            author=optional_text(body, "author", default="operator"),
            sections=integer(body, "sections", required=False, default=1, minimum=1),
        )

    def _refresh_zone_map(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.refresh_zone_map(author=optional_text(body, "author", default="operator"))

    def _persist_baseline(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.persist_temperature_baseline(author=optional_text(body, "author", default="operator"))

    def _reset_latch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.request_latch_reset()

    def _trip_latch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.trip_latch(text(body, "name"), text(body, "reason"))

    def _publish_parameters(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        values = body.get("values", {})
        if not isinstance(values, Mapping) or not values:
            raise NotFoundError("parameter values must be a non-empty object", field="values")
        numeric = {str(key): float(value) for key, value in values.items()}
        return self.service.publish_parameters(numeric, author=text(body, "author"))

    def _issue_confirmation(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.issue_confirmation(
            scope=text(body, "scope"),
            issued_by=text(body, "issued_by"),
        )

    def _verify_confirmation(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        scope = optional_text(body, "scope", default="")
        return self.service.verify_confirmation(
            text(body, "token"),
            scope=scope or None,
        )

    def _commit_ledger(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.commit_ledger()

    def _rollback_ledger(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.rollback_ledger()

    def _tombstone(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.tombstone_record(
            integer(body, "sequence", minimum=1),
            reason=optional_text(body, "reason", default="voided"),
        )

    def _load_car(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.load_car(
            text(body, "car_id"),
            initial_moisture_pct=number(body, "initial_moisture_pct", required=False, default=6.0),
        )

    def _complete_drying(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.complete_drying(text(body, "car_id"))

    def _dry_car(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.dry_car(
            text(body, "car_id"),
            initial_moisture_pct=number(body, "initial_moisture_pct", required=False, default=6.0),
            step=number(body, "step", required=False, default=None),
        )

    def _start_glazing(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.start_glazing(
            text(body, "car_id"),
            density_g_cm3=number(body, "density_g_cm3", minimum=0.0),
        )

    def _confirm_spacing(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.confirm_spacing(
            car_id=text(body, "car_id"),
            gap_mm=number(body, "gap_mm", minimum=0.0),
        )

    def _feed_car(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.feed_car(text(body, "car_id")).as_dict()

    def _open_batch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.open_batch(
            text(body, "code"),
            work_order=optional_text(body, "work_order"),
            car_count=integer(body, "car_count", required=False, default=0, minimum=0),
        )

    def _close_batch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.close_batch(
            text(body, "code"),
            car_count=integer(body, "car_count", required=False, default=None, minimum=0),
        )

    def _void_batch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.void_batch(
            text(body, "code"),
            reason=optional_text(body, "reason", default="voided"),
        )

    def _run_batch(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.run_batch(
            text(body, "code"),
            cars=text_list(body, "cars"),
            gap_mm=number(body, "gap_mm", minimum=0.0),
            density_g_cm3=number(body, "density_g_cm3", minimum=0.0),
            speed_mpm=number(body, "speed_mpm", minimum=0.0),
            work_order=optional_text(body, "work_order"),
            step=number(body, "step", required=False, default=None),
        )

    def _start_monitor(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.start_monitor()

    def _stop_monitor(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.stop_monitor()

    def _set_guard(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return self.service.set_guard_armed(boolean(body, "armed"))

    def _clear_alarms(self, query: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        cleared = self.service.alarms.clear_all(at=self.service.now())
        self.service.audit.record("alarm", "alarms cleared", self.service.now(), count=len(cleared))
        return {"cleared": [alarm.as_dict() for alarm in cleared], "summary": self.service.alarms.summary()}


class ConsoleServer:
    """Binds a :class:`Router` to ``http.server``."""

    def __init__(self, service: ControlService, *, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._service = service
        self._host = str(host)
        self._port = int(port)
        self.router = Router(service)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._port
        return int(self._httpd.server_address[1])

    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def serve_forever(self) -> None:
        """Create the socket if needed and block until :meth:`stop`."""

        self.start(background=False)
        assert self._httpd is not None
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()
            self._httpd = None

    def start(self, *, background: bool = True) -> "ConsoleServer":
        if self._httpd is None:
            router = self.router
            handler = _handler_class(router)
            self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
            self._httpd.daemon_threads = True
        if background and self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever, name="kilnline-console", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _handler_class(router: Router) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "kilnline"

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - logging hook
            return

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = {key: value for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write(Response(400, {"error": {"code": "validation_error", "message": "body is not JSON"}}))
                return
            self._write(router.handle(method, parsed.path, query=query, body=body))

        def _write(self, response: Response) -> None:
            payload = response.encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


def create_server(service: ControlService, *, host: str = "127.0.0.1", port: int = 8080) -> ConsoleServer:
    return ConsoleServer(service, host=host, port=port)
