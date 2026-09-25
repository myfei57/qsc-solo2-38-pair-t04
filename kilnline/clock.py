"""Time sources.

The control core never reads the wall clock directly.  Tests and replay runs
drive a :class:`ManualClock`, so ramps, holds and expiry checks stay
deterministic.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Minimal clock interface used by every stateful component."""

    def now(self) -> float:
        """Wall clock seconds since the epoch, used for stamped records."""

    def monotonic(self) -> float:
        """Monotonic seconds, used for durations and hold timers."""

    def sleep(self, seconds: float) -> None:
        """Block the caller for ``seconds``."""


class SystemClock:
    """Production clock backed by :mod:`time`."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock:
    """Deterministic clock used by tests and by recorded scenario replay."""

    def __init__(self, epoch: float = 1_800_000_000.0, monotonic_start: float = 0.0) -> None:
        self._epoch = float(epoch)
        self._monotonic = float(monotonic_start)

    def now(self) -> float:
        return self._epoch

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a manual clock backwards")
        self._epoch += float(seconds)
        self._monotonic += float(seconds)


def age_seconds(now: float, moment: float) -> float:
    """Return a non-negative age for wall-clock stamps."""

    return max(0.0, float(now) - float(moment))
