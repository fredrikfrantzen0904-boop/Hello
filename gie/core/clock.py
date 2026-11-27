"""Clocks and timers (requirements S00-ASG-19 "Clock", S00-ACC-03 "replay clock").

All GIE code obtains time from a :class:`Clock` and schedules future work through a
:class:`Scheduler`; both are injected.  No component may call ``time.time()``,
``datetime.now()``, ``threading.Timer`` or similar directly: that is what makes
replay deterministic, look-ahead detectable, and paper and replay equivalent.

Implementations:

* :class:`ReplayClock` - virtual time driven by the replay harness.  It only
  moves when told to, never backwards, and fires timers deterministically.  It
  is both the :class:`Clock` and the :class:`Scheduler` of a replay run.
* :class:`SystemClock` - UTC wall clock for paper mode.  It never returns a
  smaller value than it returned before, even if the operating-system clock is
  stepped backwards.
* :class:`PollingScheduler` - the :class:`Scheduler` of a paper run.  The event
  loop calls :meth:`PollingScheduler.run_due` between inputs; there are no timer
  threads, so callbacks run on the same thread and in the same order as in replay.

``now_ns`` is the instant used for freshness and ordering decisions.
``monotonic_ns`` is only for measuring elapsed time.

Timer rule shared by replay and paper: timers fire in ``(due time, registration
order)``; a timer due at instant *t* fires before any input that became
available at *t* is delivered; a timer never fires early.  In replay it fires
exactly at its due instant; in paper it fires at the first poll at or after it.
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


@runtime_checkable
class Scheduler(Protocol):
    """Timer service injected together with the clock.

    Everything that must happen when no market data arrives - feed-silence
    checks, closing a candle in a quiet market, expiring records, once-per-second
    tactical work - is scheduled here, never with threads or sleeps.
    """

    def call_at(self, due_ns: int, callback: TimerCallback) -> int:
        """Schedule ``callback(due_ns)``; returns a handle usable with :meth:`cancel`."""
        ...

    def cancel(self, handle: int) -> None:
        """Cancel a pending timer; unknown, fired or cancelled handles are ignored."""
        ...


def _require_instant(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{what} must be an int of nanoseconds", value=repr(value)[:64])
    if not 0 <= value <= MAX_TIME_NS:
        raise ContractValidationError(f"{what} outside the supported range", value=value)


class _TimerQueue:
    """Pending timers ordered by ``(due time, registration order)``."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, TimerCallback]] = []
        self._pending: set[int] = set()
        self._cancelled: set[int] = set()
        self._next_handle: int = 0

    def add(self, due_ns: int, callback: TimerCallback) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self._pending.add(handle)
        heapq.heappush(self._heap, (due_ns, handle, callback))
        return handle

    def cancel(self, handle: int) -> None:
        # Only a handle that is actually pending is remembered as cancelled.  A handle that
        # was never issued must not cancel a timer that receives that number later.
        if handle in self._pending:
            self._pending.discard(handle)
            self._cancelled.add(handle)

    def _drop_cancelled_head(self) -> None:
        while self._heap and self._heap[0][1] in self._cancelled:
            _, handle, _ = heapq.heappop(self._heap)
            self._cancelled.discard(handle)

    def next_due_ns(self) -> int | None:
        self._drop_cancelled_head()
        return self._heap[0][0] if self._heap else None

    def pop_due(self, limit_ns: int) -> tuple[int, TimerCallback] | None:
        """Remove and return the earliest timer due at or before *limit_ns*, if any."""
        self._drop_cancelled_head()
        if not self._heap or self._heap[0][0] > limit_ns:
            return None
        due_ns, handle, callback = heapq.heappop(self._heap)
        self._pending.discard(handle)
        return due_ns, callback

    def pending(self) -> int:
        return len(self._pending)


class ReplayClock:
    """Deterministic virtual clock and scheduler for replay and tests.

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
    * If a callback raises, the exception propagates, the clock stays at that
      timer's due time and all other timers stay pending.
    """

    def __init__(self, start_ns: int) -> None:
        _require_instant(start_ns, "start_ns")
        self._start_ns: Final[int] = start_ns
        self._now_ns: int = start_ns
        self._timers: Final[_TimerQueue] = _TimerQueue()
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
        return self._timers.add(due_ns, callback)

    def cancel(self, handle: int) -> None:
        """Cancel a pending timer.

        A handle that was never issued, has already fired or was already cancelled
        is ignored and has no effect on any other timer, now or later.
        """
        self._timers.cancel(handle)

    def next_timer_ns(self) -> int | None:
        """Due time of the earliest pending timer, or ``None``."""
        return self._timers.next_due_ns()

    def pending_timers(self) -> int:
        """Number of timers that are scheduled and neither fired nor cancelled."""
        return self._timers.pending()

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
                due = self._timers.pop_due(target_ns)
                if due is None:
                    break
                due_ns, callback = due
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

    Resolution is that of the operating system.  On Windows with Python older
    than 3.13 ``time.time_ns`` advances in steps of about 15.6 ms; paper runs
    that need finer receive times must use Python 3.13+ there, and the measured
    resolution belongs in the feed benchmark of session 01.
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


class PollingScheduler:
    """Scheduler of a paper run: timers fire when the event loop polls.

    The loop that delivers inputs calls :meth:`run_due` before it delivers each
    input (and at least once per idle period).  Timers therefore run on the loop
    thread, in ``(due time, registration order)``, and never early; a timer can
    fire late by at most the poll interval.  The callback receives its *due*
    time; code that needs the actual time reads the clock.

    The rules are the same as for :class:`ReplayClock`: scheduling in the past
    is an error, a callback may schedule further timers (those already due fire
    in the same poll) and may not start a nested poll.  The class is not
    thread-safe by design: all calls come from the loop thread.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock: Final[Clock] = clock
        self._timers: Final[_TimerQueue] = _TimerQueue()
        self._firing: bool = False

    def call_at(self, due_ns: int, callback: TimerCallback) -> int:
        _require_instant(due_ns, "due_ns")
        now_ns = self._clock.now_ns()
        if due_ns < now_ns:
            raise ClockRegressionError("timer scheduled in the past", due_ns=due_ns, now_ns=now_ns)
        return self._timers.add(due_ns, callback)

    def cancel(self, handle: int) -> None:
        self._timers.cancel(handle)

    def next_timer_ns(self) -> int | None:
        return self._timers.next_due_ns()

    def pending_timers(self) -> int:
        return self._timers.pending()

    def run_due(self) -> int:
        """Fire every timer that is due now; returns how many fired."""
        if self._firing:
            raise ClockRegressionError("a timer callback may not start a nested poll")
        fired = 0
        self._firing = True
        try:
            while True:
                due = self._timers.pop_due(self._clock.now_ns())
                if due is None:
                    break
                due_ns, callback = due
                fired += 1
                callback(due_ns)
        finally:
            self._firing = False
        return fired
