"""Command line entry point: ``python -m kilnline``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from kilnline import __version__
from kilnline.clock import ManualClock, SystemClock
from kilnline.config import Settings
from kilnline.console.api import create_server
from kilnline.console.service import ControlService
from kilnline.errors import KilnError
from kilnline.logging_setup import configure_logging

BENCH_OVERRIDES: dict[str, Any] = {
    "ramp_rate_c_per_min": 60.0,
    "air_spin_up_s": 4.0,
    "gas_settle_s": 2.0,
    "dry_min_duration_s": 10.0,
    "dry_rate_pct_per_s": 0.5,
    "baseline_max_lag_s": 7200.0,
    "latch_reset_hold_s": 5.0,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kilnline", description="roller line control service")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the JSON console")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--data-dir", default=None)
    serve.add_argument("--log-level", default="INFO")
    serve.add_argument("--no-simulation", action="store_true", help="take readings from the field feed")
    serve.add_argument("--no-monitor", action="store_true", help="do not start the background tick")

    selftest = sub.add_parser("selftest", help="run the scripted line cycle on the bench rig")
    selftest.add_argument("--data-dir", default=None)
    selftest.add_argument("--step", type=float, default=10.0)

    status = sub.add_parser("status", help="print the persisted snapshot and health")
    status.add_argument("--data-dir", default=None)

    replay = sub.add_parser("replay", help="replay the committed record stream")
    replay.add_argument("--data-dir", default=None)
    replay.add_argument("--after", type=int, default=0)

    routes = sub.add_parser("routes", help="list the JSON console routes")
    routes.add_argument("--data-dir", default=None)
    return parser


def _settings(data_dir: str | None) -> Settings:
    settings = Settings.from_env()
    if data_dir:
        settings = settings.with_data_dir(data_dir)
    return settings


def _bench_settings(data_dir: str | None) -> Settings:
    return _settings(data_dir).overrides(**BENCH_OVERRIDES)


def _run_serve(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    service = ControlService(
        _settings(args.data_dir),
        simulation=not args.no_simulation,
        start_monitor=not args.no_monitor,
    )
    server = create_server(service, host=args.host, port=args.port)
    print(json.dumps({"event": "listening", "url": server.url()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        server.stop()
    return 0


def _run_selftest(args: argparse.Namespace) -> int:
    clock = ManualClock()
    service = ControlService(_bench_settings(args.data_dir), clock, simulation=True)
    step = float(args.step)
    try:
        prepared = service.prepare(step=step)
        service.calibrate_roller(pulses_per_meter=120.0, author="selftest")
        batch = service.run_batch(
            "KB-20260101-D-0001",
            cars=["CAR-20260101-001", "CAR-20260101-002"],
            gap_mm=220.0,
            density_g_cm3=1.65,
            speed_mpm=12.0,
            work_order="WO-000001",
            step=step,
        )
    except KilnError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False, indent=2))
        return 1
    payload = {
        "ok": True,
        "prepared": prepared["ready"],
        "batch": batch["batch"],
        "entries": len(batch["entries"]),
        "roller": batch["roller"],
        "watermark": service.stream.watermark,
        "health": service.health(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_status(args: argparse.Namespace) -> int:
    service = ControlService(_settings(args.data_dir), SystemClock(), simulation=False)
    snapshot = service.latest_snapshot()
    payload = {
        "health": service.health(),
        "recovery": service.recovery_report,
        "latest_snapshot": None if snapshot is None else snapshot.as_dict(),
        "audit_tail": service.audit_tail(limit=10),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    service = ControlService(_settings(args.data_dir), SystemClock(), simulation=False)
    payload = {
        "replay": service.replay_ledger(after_watermark=args.after),
        "ledger": service.ledger_state(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_routes(args: argparse.Namespace) -> int:
    from kilnline.console.api import Router

    service = ControlService(_settings(args.data_dir), SystemClock(), simulation=False)
    payload = Router(service).routes
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "selftest":
        return _run_selftest(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "replay":
        return _run_replay(args)
    if args.command == "routes":
        return _run_routes(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
