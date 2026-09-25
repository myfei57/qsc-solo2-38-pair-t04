"""Background control tick.

The monitor only drives :meth:`ControlService.tick_once`; it owns no process
state of its own, so a test can either start the thread or call ``tick_once``
directly and get identical behaviour.  Timing comes from the injected clock.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from kilnline.clock import Clock
from kilnline.errors import ValidationError


class MonitorThread:
    """Runs the control tick on a fixed cadence."""

    def __init__(
        self,
        tick: Callable[[float], None],
        clock: Clock,
        *,
        interval_s: float,
    ) -> None:
        if float(interval_s) <= 0.0:
            raise ValidationError("monitor interval must be positive", interval_s=interval_s)
        self._tick = tick
        self._clock = clock
        self._interval_s = float(interval_s)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ticks = 0
        self._errors = 0
        self._last_error = ""

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def ticks(self) -> int:
        return self._ticks

    @property
    def errors(self) -> int:
        return self._errors

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running():
            raise ValidationError("monitor is already running")
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="kilnline-monitor", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=float(timeout_s))
        self._thread = None

    def run_once(self) -> None:
        """Execute one iteration; used by tests instead of a real thread."""

        self._step()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._step()
            self._clock.sleep(self._interval_s)

    def _step(self) -> None:
        try:
            self._tick(self._interval_s)
            self._ticks += 1
        except Exception as exc:  # the monitor must never kill the service
            self._errors += 1
            self._last_error = str(exc)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running(),
            "interval_s": self._interval_s,
            "ticks": self._ticks,
            "errors": self._errors,
            "last_error": self._last_error,
        }
