"""Clocks (requirements S00-ASG-19 "Clock", S00-ACC-03 "replay clock").

All GIE code obtains time from a :class:`Clock` that is injected into it.  No
component may call ``time.time()``, ``datetime.now()`` or similar directly:
that is what makes replay deterministic and look-ahead detectable.

Two implementations exist:

* :class:`ReplayClock` - virtual time driven by the replay harness.  It only
  moves when told to, never backwards, and fires timers deterministically.
* :class:`SystemClock` - UTC wall clock for paper mode.  It never returns a
  smaller value than it returned before, even if the operating-system clock is
  stepped backwards.

``now_ns`` is the instant used for freshness and ordering decisions.
``monotonic_ns`` is only for measuring elapsed time.
"""

from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Final, Protocol, runtime_checkable

from gie.core.errors import ClockRegressionError, ContractValidationError
from gie.core.timeutil import MAX_TIME_NS


class ClockMode(str, Enum):
    """How a clock obtains time."""

    REPLAY = "REPLAY"
    SYSTEM = "SYSTEM"


@runtime_checkable
class Clock(Protocol):
    """Time source injected into every component."""

    @property
    def mode(self) -> ClockMode:
        """Whether time is virtual (replay) or taken from the operating system."""
        ...

    def now_ns(self) -> int:
        """Current instant: nanoseconds since the Unix epoch, UTC.  Never decreases."""
        ...

    def monotonic_ns(self) -> int:
        """Nanoseconds on a monotonic scale with an arbitrary origin.  Never decreases."""
        ...


TimerCallback = Callable[[int], None]


def _require_instant(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{what} must be an int of nanoseconds", value=repr(value)[:64])
    if not 0 <= value <= MAX_TIME_NS:
        raise ContractValidationError(f"{what} outside the supported range", value=value)


class ReplayClock:
    """Deterministic virtual clock for replay and tests.

    * Time changes only through :meth:`advance_to` / :meth:`advance_by`.
    * Moving backwards raises :class:`ClockRegressionError`; advancing to the
      current instant is a no-op.
    * Timers registered with :meth:`call_at` fire during an advance in
      ``(due time, registration order)`` order.  While a timer runs, ``now_ns``
      equals that timer's due time, so a callback can never observe time later
      than the instant it was scheduled for.
    * A callback may register further timers; those that are due within the
      current advance fire in the same advance.  A callback may not advance the
      clock itself.
    """

    def __init__(self, start_ns: int) -> None:
        _require_instant(start_ns, "start_ns")
        self._start_ns: Final[int] = start_ns
        self._now_ns: int = start_ns
        self._timers: list[tuple[int, int, TimerCallback]] = []
        self._cancelled: set[int] = set()
        self._next_handle: int = 0
        self._firing: bool = False

    @property
    def mode(self) -> ClockMode:
        return ClockMode.REPLAY

    @property
    def start_ns(self) -> int:
        return self._start_ns

    def now_ns(self) -> int:
        return self._now_ns

    def monotonic_ns(self) -> int:
        return self._now_ns - self._start_ns

    def call_at(self, due_ns: int, callback: TimerCallback) -> int:
        """Schedule ``callback(due_ns)``; returns a handle usable with :meth:`cancel`.

        Scheduling in the past is an error: it would mean an effect without a cause.
        """
        _require_instant(due_ns, "due_ns")
        if due_ns < self._now_ns:
            raise ClockRegressionError("timer scheduled in the past", due_ns=due_ns, now_ns=self._now_ns)
        handle = self._next_handle
        self._next_handle += 1
        heapq.heappush(self._timers, (due_ns, handle, callback))
        return handle

    def cancel(self, handle: int) -> None:
        """Cancel a pending timer.  Unknown or already fired handles are ignored."""
        self._cancelled.add(handle)

    def next_timer_ns(self) -> int | None:
        """Due time of the earliest pending timer, or ``None``."""
        self._drop_cancelled_head()
        return self._timers[0][0] if self._timers else None

    def pending_timers(self) -> int:
        return sum(1 for _, handle, _ in self._timers if handle not in self._cancelled)

    def _drop_cancelled_head(self) -> None:
        while self._timers and self._timers[0][1] in self._cancelled:
            _, handle, _ = heapq.heappop(self._timers)
            self._cancelled.discard(handle)

    def advance_to(self, target_ns: int) -> int:
        """Move to *target_ns*, firing every timer due on the way.  Returns the new time."""
        _require_instant(target_ns, "target_ns")
        if self._firing:
            raise ClockRegressionError("a timer callback may not advance the clock")
        if target_ns < self._now_ns:
            raise ClockRegressionError(
                "replay clock cannot move backwards", target_ns=target_ns, now_ns=self._now_ns
            )
        self._firing = True
        try:
            while True:
                self._drop_cancelled_head()
                if not self._timers or self._timers[0][0] > target_ns:
                    break
                due_ns, _, callback = heapq.heappop(self._timers)
                self._now_ns = due_ns
                callback(due_ns)
        finally:
            self._firing = False
        self._now_ns = target_ns
        return self._now_ns

    def advance_by(self, delta_ns: int) -> int:
        """Move forward by a non-negative duration."""
        if isinstance(delta_ns, bool) or not isinstance(delta_ns, int):
            raise ContractValidationError("delta_ns must be an int", value=repr(delta_ns)[:64])
        if delta_ns < 0:
            raise ClockRegressionError("replay clock cannot move backwards", delta_ns=delta_ns)
        return self.advance_to(self._now_ns + delta_ns)


class SystemClock:
    """UTC wall clock for paper mode.

    ``now_ns`` is clamped so that it never decreases: if the operating system
    steps the wall clock backwards the previous value is returned until real
    time catches up, and ``regressions_observed`` counts how often that
    happened so that health monitoring can fail closed on an unstable clock.
    """

    def __init__(
        self,
        wall_source: Callable[[], int] = time.time_ns,
        monotonic_source: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._wall_source = wall_source
        self._monotonic_source = monotonic_source
        self._lock = threading.Lock()
        self._last_wall_ns: int = 0
        self._last_monotonic_ns: int = 0
        self.regressions_observed: int = 0

    @property
    def mode(self) -> ClockMode:
        return ClockMode.SYSTEM

    def now_ns(self) -> int:
        with self._lock:
            reading = self._wall_source()
            if reading < self._last_wall_ns:
                self.regressions_observed += 1
                return self._last_wall_ns
            self._last_wall_ns = reading
            return reading

    def monotonic_ns(self) -> int:
        with self._lock:
            reading = self._monotonic_source()
            if reading < self._last_monotonic_ns:
                return self._last_monotonic_ns
            self._last_monotonic_ns = reading
            return reading
