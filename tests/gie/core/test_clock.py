"""Independent tests for ``gie.core.clock``.

Covers requirement S00-ASG-19 ("Clock") and acceptance test S00-ACC-03 ("Replay clock").

The expected behaviour is taken from the docstrings of ``gie/core/clock.py`` and from
``reviews/session_00/rework_2_change_notice.md`` section 1, not from the implementation:

* a ``Clock`` has ``mode``, ``now_ns``, ``monotonic_ns`` and the read-only property
  ``regressions_observed``; a ``Scheduler`` has ``call_at``, ``call_after`` and ``cancel``;
* a ``ReplayClock`` starts at ``start_ns`` and moves only through ``advance_to`` /
  ``advance_by``; it never moves backwards (``ClockRegressionError``); it is the clock and
  the scheduler of a replay;
* timers fire in ``(due time, registration order)``; while a replay timer runs ``now_ns()``
  equals that timer's due time; a callback may schedule further timers but may not
  advance the clock;
* a ``SystemClock`` reads injected sources, never decreases **and never stands still**:
  while the wall source is behind the last reading, time continues from the last reading at
  the pace of the monotonic source, and each backward step is counted once in
  ``regressions_observed``;
* step time (``reviews/session_00/rework_2_change_notice.md`` section 1): in replay and in paper a
  component sees the instant of the input being processed; only the run loop moves time;
* a ``PaperClock`` returns the step instant from ``now_ns`` (it does not move between
  ``begin_step`` calls), reads the injected system clock in ``wall_ns`` and refuses a step before
  the current one (``ClockRegressionError``) or later than the wall clock (``FutureDataError``);
* a ``PollingScheduler`` runs on exactly a ``PaperClock``: timers fire only when ``run_due`` is
  called, in ``(due time, registration order)``, never early, each in a step that begins at its
  due time; a due time before the step instant is refused as in replay; ``call_after`` is relative
  to the step instant; the same schedule gives the same callbacks at the same instants as on a
  ``ReplayClock``;
* ``ClockView`` / ``SchedulerView`` are what components receive: reading time, scheduling and
  cancelling timers - nothing that moves time or fires timers.

SYNTHETIC FIXTURE: every instant, schedule and clock reading in this file is invented for
testing.  Nothing here is market data, broker data or a measurement.
"""

from __future__ import annotations

import functools
import inspect
import itertools
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from gie.core.clock import (
    Clock,
    ClockMode,
    ClockView,
    PaperClock,
    PollingScheduler,
    ReplayClock,
    Scheduler,
    SchedulerView,
    SystemClock,
)
from gie.core.errors import ClockRegressionError, ContractValidationError, FutureDataError, GieError
from gie.core.timeutil import MAX_TIME_NS, NS_PER_MS, NS_PER_SECOND

#: SYNTHETIC FIXTURE instant (2026-11-26 09:30:00 UTC), the same arbitrary instant as tests/gie/samples.py.
START = 1_795_685_400 * NS_PER_SECOND

#: Deterministic hypothesis runs: no random seed, no example database, no wall-clock deadline.
_PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=250)

#: Values that are not an ``int`` count of nanoseconds (``bool`` is explicitly not accepted).
_NOT_AN_INT = [True, False, 1.0, 1.5, float("nan"), "1", b"1", None, [1], Decimal(1)]

#: Integers outside the supported range of instants ``0 .. 2**63 - 1``.
_TOO_LARGE = [MAX_TIME_NS + 1, 2**64, 10**30]
_NEGATIVE = [-1, -NS_PER_SECOND, -(2**63)]


class _Boom(Exception):
    """SYNTHETIC FIXTURE failure raised on purpose by a timer callback."""


class _Trace:
    """Records what timer callbacks of one clock observe: ``(label, due argument, now_ns())``."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.events: list[tuple[str, int, int]] = []

    def timer(self, label: str) -> Callable[[int], None]:
        def callback(due_ns: int) -> None:
            self.events.append((label, due_ns, self.clock.now_ns()))

        return callback

    @property
    def labels(self) -> list[str]:
        return [label for label, _, _ in self.events]


# =========================================================================== ReplayClock: time


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("start_ns", [0, 1, START, MAX_TIME_NS])
def test_replay_clock_starts_at_start_ns(start_ns: int) -> None:
    clock = ReplayClock(start_ns)
    assert clock.now_ns() == start_ns
    assert clock.start_ns == start_ns
    assert clock.monotonic_ns() == 0
    assert clock.pending_timers() == 0
    assert clock.next_timer_ns() is None


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_replay_clock_mode_is_replay() -> None:
    clock = ReplayClock(START)
    assert clock.mode is ClockMode.REPLAY
    assert clock.mode.value == "REPLAY"
    clock.advance_by(NS_PER_SECOND)
    assert clock.mode is ClockMode.REPLAY


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_replay_clock_satisfies_the_runtime_checkable_clock_protocol() -> None:
    assert isinstance(ReplayClock(START), Clock)
    # The check is meaningful: an arbitrary object is not a Clock.
    assert not isinstance(object(), Clock)
    assert not isinstance(START, Clock)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_clock_mode_vocabulary_is_replay_and_system() -> None:
    assert {member.name: member.value for member in ClockMode} == {"REPLAY": "REPLAY", "SYSTEM": "SYSTEM"}


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_replay_time_moves_only_when_told_to() -> None:
    clock = ReplayClock(START)
    handle = clock.call_at(START + 5, lambda due_ns: None)
    for _ in range(1000):
        assert clock.now_ns() == START
        assert clock.monotonic_ns() == 0
    clock.next_timer_ns()
    clock.pending_timers()
    clock.cancel(handle)
    assert clock.now_ns() == START
    assert clock.monotonic_ns() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_replay_clock_never_reads_the_operating_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("a replay clock must not read the operating-system clock")

    fired: list[int] = []
    with monkeypatch.context() as patch:
        for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"):
            patch.setattr(time, name, forbidden)
        clock = ReplayClock(START)
        clock.call_at(START + 3, fired.append)
        clock.advance_by(2)
        clock.advance_to(START + 10)
        readings = (clock.now_ns(), clock.monotonic_ns(), clock.next_timer_ns(), clock.pending_timers())
    assert readings == (START + 10, 10, None, 0)
    assert fired == [START + 3]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_advance_to_moves_time_forward_and_returns_the_new_time() -> None:
    clock = ReplayClock(START)
    assert clock.advance_to(START + 5) == START + 5
    assert clock.now_ns() == START + 5
    assert clock.advance_to(START + NS_PER_SECOND) == START + NS_PER_SECOND
    assert clock.now_ns() == START + NS_PER_SECOND
    assert clock.advance_to(MAX_TIME_NS) == MAX_TIME_NS
    assert clock.now_ns() == MAX_TIME_NS
    assert clock.start_ns == START


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_advance_by_moves_time_forward_by_the_duration_and_returns_the_new_time() -> None:
    clock = ReplayClock(START)
    assert clock.advance_by(1) == START + 1
    assert clock.advance_by(NS_PER_MS) == START + 1 + NS_PER_MS
    assert clock.now_ns() == START + 1 + NS_PER_MS
    assert clock.advance_by(MAX_TIME_NS - clock.now_ns()) == MAX_TIME_NS
    assert clock.now_ns() == MAX_TIME_NS


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_monotonic_ns_is_the_elapsed_virtual_time() -> None:
    clock = ReplayClock(START)
    assert clock.monotonic_ns() == 0
    clock.advance_by(7)
    assert clock.monotonic_ns() == 7
    clock.advance_to(START + 3 * NS_PER_SECOND)
    assert clock.monotonic_ns() == 3 * NS_PER_SECOND
    clock.advance_by(0)
    assert clock.monotonic_ns() == 3 * NS_PER_SECOND
    assert clock.monotonic_ns() == clock.now_ns() - clock.start_ns


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_two_replay_clocks_are_independent() -> None:
    first = ReplayClock(START)
    second = ReplayClock(START)
    fired: list[int] = []
    second.call_at(START + 1, fired.append)
    first.advance_by(NS_PER_SECOND)
    assert second.now_ns() == START
    assert second.pending_timers() == 1
    assert first.pending_timers() == 0
    assert fired == []


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_advancing_to_the_current_instant_is_a_no_op() -> None:
    clock = ReplayClock(START)
    fired: list[int] = []
    clock.advance_to(START + 7)
    clock.call_at(START + 8, fired.append)
    for _ in range(3):
        assert clock.advance_to(START + 7) == START + 7
        assert clock.advance_by(0) == START + 7
    assert clock.now_ns() == START + 7
    assert clock.monotonic_ns() == 7
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 8
    assert fired == []


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("back_by", [1, 2, NS_PER_MS, 10 * NS_PER_SECOND, 20 * NS_PER_SECOND + 1])
def test_moving_backwards_raises_and_leaves_time_unchanged(back_by: int) -> None:
    clock = ReplayClock(START)
    fired: list[int] = []
    clock.call_at(START + 50 * NS_PER_SECOND, fired.append)
    now = clock.advance_to(START + 20 * NS_PER_SECOND)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(now - back_by)
    assert clock.now_ns() == now
    assert clock.monotonic_ns() == 20 * NS_PER_SECOND
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 50 * NS_PER_SECOND
    assert fired == []
    # The refusal does not break the clock.
    assert clock.advance_by(1) == now + 1


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_fresh_clock_cannot_be_moved_before_its_start() -> None:
    clock = ReplayClock(START)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START - 1)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(0)
    assert clock.now_ns() == START
    assert clock.monotonic_ns() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delta_ns", [-1, -NS_PER_MS, -START, -(2**70)])
def test_advance_by_a_negative_duration_raises_and_leaves_time_unchanged(delta_ns: int) -> None:
    clock = ReplayClock(START)
    clock.advance_by(5)
    with pytest.raises(ClockRegressionError):
        clock.advance_by(delta_ns)
    assert clock.now_ns() == START + 5
    assert clock.monotonic_ns() == 5


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_clock_regression_error_is_a_gie_time_error_with_a_stable_code() -> None:
    clock = ReplayClock(START)
    with pytest.raises(GieError) as caught:
        clock.advance_to(START - 1)
    assert isinstance(caught.value, ClockRegressionError)
    assert caught.value.code == "GIE.TIME.CLOCK_REGRESSION"


# =========================================================================== ReplayClock: validation


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("start_ns", [*_NOT_AN_INT, *_NEGATIVE, *_TOO_LARGE])
def test_constructor_rejects_values_that_are_not_valid_instants(start_ns: Any) -> None:
    with pytest.raises(ContractValidationError):
        ReplayClock(start_ns)


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("target_ns", [*_NOT_AN_INT, *_TOO_LARGE])
def test_advance_to_rejects_values_that_are_not_valid_instants(target_ns: Any) -> None:
    clock = ReplayClock(START)
    fired: list[int] = []
    clock.call_at(START + 1, fired.append)
    with pytest.raises(ContractValidationError):
        clock.advance_to(target_ns)
    assert clock.now_ns() == START
    assert clock.pending_timers() == 1
    assert fired == []


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("target_ns", _NEGATIVE)
def test_advance_to_a_negative_instant_is_refused(target_ns: int) -> None:
    # A negative target is both an invalid instant and a move backwards; either refusal is correct.
    clock = ReplayClock(START)
    with pytest.raises((ContractValidationError, ClockRegressionError)):
        clock.advance_to(target_ns)
    assert clock.now_ns() == START
    zero = ReplayClock(0)
    with pytest.raises((ContractValidationError, ClockRegressionError)):
        zero.advance_to(target_ns)
    assert zero.now_ns() == 0


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delta_ns", _NOT_AN_INT)
def test_advance_by_rejects_values_that_are_not_int_durations(delta_ns: Any) -> None:
    clock = ReplayClock(START)
    with pytest.raises(ContractValidationError):
        clock.advance_by(delta_ns)
    assert clock.now_ns() == START


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delta_ns", [1, NS_PER_SECOND, MAX_TIME_NS, 2**64])
def test_advance_by_beyond_the_largest_instant_is_rejected_and_leaves_time_unchanged(delta_ns: int) -> None:
    clock = ReplayClock(MAX_TIME_NS - 5)
    clock.advance_by(5)
    with pytest.raises(ContractValidationError):
        clock.advance_by(delta_ns)
    assert clock.now_ns() == MAX_TIME_NS
    assert clock.monotonic_ns() == 5


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("due_ns", [*_NOT_AN_INT, *_TOO_LARGE])
def test_call_at_rejects_values_that_are_not_valid_instants(due_ns: Any) -> None:
    clock = ReplayClock(START)
    with pytest.raises(ContractValidationError):
        clock.call_at(due_ns, lambda due: None)
    assert clock.pending_timers() == 0
    assert clock.next_timer_ns() is None


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("due_ns", _NEGATIVE)
def test_call_at_a_negative_instant_is_refused(due_ns: int) -> None:
    # Negative is an invalid instant and (for a clock at 0 or later) also in the past.
    for clock in (ReplayClock(0), ReplayClock(START)):
        with pytest.raises((ContractValidationError, ClockRegressionError)):
            clock.call_at(due_ns, lambda due: None)
        assert clock.pending_timers() == 0


# =========================================================================== ReplayClock: timers


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_timers_fire_in_due_time_order_whatever_the_registration_order() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    for label, offset in (("c", 30), ("a", 10), ("d", 40), ("b", 20)):
        clock.call_at(START + offset, trace.timer(label))
    clock.advance_to(START + 100)
    assert trace.labels == ["a", "b", "c", "d"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_timers_with_the_same_due_time_fire_in_registration_order() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    labels = [f"t{index:02d}" for index in range(25)]
    for label in labels:
        clock.call_at(START + 10, trace.timer(label))
    clock.advance_to(START + 10)
    assert trace.labels == labels


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_due_time_takes_precedence_over_registration_order() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    clock.call_at(START + 20, trace.timer("late-1"))
    clock.call_at(START + 10, trace.timer("early-1"))
    clock.call_at(START + 20, trace.timer("late-2"))
    clock.call_at(START + 10, trace.timer("early-2"))
    clock.advance_by(20)
    assert trace.labels == ["early-1", "early-2", "late-1", "late-2"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_inside_a_callback_now_is_the_due_time_never_the_advance_target() -> None:
    clock = ReplayClock(START)
    observed: list[tuple[int, int, int]] = []

    def callback(due_ns: int) -> None:
        observed.append((due_ns, clock.now_ns(), clock.monotonic_ns()))

    for offset in (10, 25, 25, 999):
        clock.call_at(START + offset, callback)
    target = START + 5 * NS_PER_SECOND
    assert clock.advance_to(target) == target
    assert observed == [
        (START + 10, START + 10, 10),
        (START + 25, START + 25, 25),
        (START + 25, START + 25, 25),
        (START + 999, START + 999, 999),
    ]
    # After the advance the clock is at the target, not at the last timer.
    assert clock.now_ns() == target


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_timer_due_exactly_at_the_target_fires_and_a_later_one_does_not() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    clock.call_at(START + 99, trace.timer("before"))
    clock.call_at(START + 100, trace.timer("at-target"))
    clock.call_at(START + 101, trace.timer("after"))
    clock.advance_to(START + 100)
    assert trace.events == [("before", START + 99, START + 99), ("at-target", START + 100, START + 100)]
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 101
    clock.advance_by(1)
    assert trace.labels == ["before", "at-target", "after"]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_timer_fires_exactly_once() -> None:
    clock = ReplayClock(START)
    fired: list[int] = []
    clock.call_at(START + 10, fired.append)
    clock.advance_to(START + 10)
    clock.advance_to(START + 10)
    clock.advance_by(0)
    clock.advance_by(1000)
    assert fired == [START + 10]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_timer_due_at_the_current_instant_fires_on_the_next_advance_even_of_zero_length() -> None:
    # Scheduling for "now" is not scheduling in the past.  advance_to fires every timer that is due
    # at or before its target, and target == now is a legal advance, so a timer whose due time has
    # been reached is never left pending behind the clock.
    clock = ReplayClock(START)
    trace = _Trace(clock)
    clock.call_at(START, trace.timer("now"))
    assert clock.pending_timers() == 1
    assert trace.events == []  # registering alone fires nothing
    assert clock.advance_by(0) == START
    assert trace.events == [("now", START, START)]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_timers_fire_across_several_advances_each_in_the_advance_that_reaches_it() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    for offset in (5, 15, 25):
        clock.call_at(START + offset, trace.timer(f"t{offset}"))
    clock.advance_to(START + 4)
    assert trace.labels == []
    clock.advance_to(START + 5)
    assert trace.labels == ["t5"]
    clock.advance_to(START + 24)
    assert trace.labels == ["t5", "t15"]
    clock.advance_to(START + 26)
    assert trace.labels == ["t5", "t15", "t25"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_callback_can_schedule_timers_that_fire_in_the_same_advance_in_global_order() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)

    def first(due_ns: int) -> None:
        trace.timer("A")(due_ns)
        clock.call_at(START + 30, trace.timer("D"))  # same instant as B, registered later
        clock.call_at(START + 20, trace.timer("C"))  # between A and B
        clock.call_at(START + 50, trace.timer("E"))  # beyond the target of this advance

    clock.call_at(START + 10, first)
    clock.call_at(START + 30, trace.timer("B"))
    assert clock.advance_to(START + 40) == START + 40
    assert trace.events == [
        ("A", START + 10, START + 10),
        ("C", START + 20, START + 20),
        ("B", START + 30, START + 30),
        ("D", START + 30, START + 30),
    ]
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 50
    clock.advance_to(START + 50)
    assert trace.labels == ["A", "C", "B", "D", "E"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_timer_scheduled_by_a_callback_for_its_own_instant_fires_after_the_ones_already_registered() -> (
    None
):
    clock = ReplayClock(START)
    trace = _Trace(clock)

    def first(due_ns: int) -> None:
        trace.timer("first")(due_ns)
        clock.call_at(due_ns, trace.timer("child-of-first"))

    clock.call_at(START + 10, first)
    clock.call_at(START + 10, trace.timer("second"))
    clock.advance_to(START + 10)
    assert trace.events == [
        ("first", START + 10, START + 10),
        ("second", START + 10, START + 10),
        ("child-of-first", START + 10, START + 10),
    ]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_long_chain_of_timers_scheduled_from_callbacks_completes_in_one_advance() -> None:
    clock = ReplayClock(START)
    links = 5000
    fired: list[int] = []

    def link(due_ns: int) -> None:
        assert clock.now_ns() == due_ns
        fired.append(due_ns)
        if len(fired) < links:
            clock.call_at(due_ns + 1, link)

    clock.call_at(START + 1, link)
    assert clock.advance_to(START + links) == START + links
    assert fired == [START + step for step in range(1, links + 1)]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_callback_cannot_schedule_before_its_own_due_time() -> None:
    # Inside a callback "now" is the due time, so anything earlier is the past - even though it
    # lies after the instant at which the advance started.
    clock = ReplayClock(START)
    refusals: list[GieError] = []
    fired: list[int] = []

    def callback(due_ns: int) -> None:
        try:
            clock.call_at(due_ns - 1, fired.append)
        except GieError as exc:
            refusals.append(exc)

    clock.call_at(START + 10, callback)
    clock.advance_to(START + 20)
    assert len(refusals) == 1
    assert isinstance(refusals[0], ClockRegressionError)
    assert fired == []
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_scheduling_in_the_past_raises_and_registers_nothing() -> None:
    clock = ReplayClock(START)
    clock.advance_by(100)
    fired: list[int] = []
    for due_ns in (START + 99, START, START - 1, 0):
        with pytest.raises(ClockRegressionError):
            clock.call_at(due_ns, fired.append)
    assert clock.pending_timers() == 0
    assert clock.next_timer_ns() is None
    clock.advance_by(1000)
    assert fired == []
    assert clock.now_ns() == START + 1100


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_scheduling_at_the_current_instant_and_at_the_largest_instant_is_allowed() -> None:
    clock = ReplayClock(START)
    clock.advance_by(100)
    fired: list[int] = []
    clock.call_at(START + 100, fired.append)
    clock.call_at(MAX_TIME_NS, fired.append)
    assert clock.pending_timers() == 2
    assert clock.next_timer_ns() == START + 100
    clock.advance_to(MAX_TIME_NS)
    assert fired == [START + 100, MAX_TIME_NS]


# =========================================================================== ReplayClock: cancel


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_timer_cancelled_before_it_is_due_never_fires() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    clock.call_at(START + 10, trace.timer("keep-1"))
    doomed = clock.call_at(START + 20, trace.timer("doomed"))
    clock.call_at(START + 30, trace.timer("keep-2"))
    clock.cancel(doomed)
    assert clock.pending_timers() == 2
    clock.advance_to(START + 100)
    assert trace.labels == ["keep-1", "keep-2"]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_cancelling_one_of_several_timers_with_the_same_due_time_keeps_the_order_of_the_others() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    handles = [clock.call_at(START + 10, trace.timer(label)) for label in ("a", "b", "c", "d")]
    clock.cancel(handles[1])
    clock.cancel(handles[1])  # cancelling twice is harmless
    clock.advance_to(START + 10)
    assert trace.labels == ["a", "c", "d"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_callback_can_cancel_timers_that_are_due_later_in_the_same_advance() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    handles: dict[str, int] = {}

    def canceller(due_ns: int) -> None:
        trace.timer("canceller")(due_ns)
        clock.cancel(handles["same-instant"])
        clock.cancel(handles["later"])

    clock.call_at(START + 10, canceller)
    handles["same-instant"] = clock.call_at(START + 10, trace.timer("same-instant"))
    handles["later"] = clock.call_at(START + 20, trace.timer("later"))
    clock.call_at(START + 30, trace.timer("survivor"))
    clock.advance_to(START + 40)
    assert trace.labels == ["canceller", "survivor"]
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_cancelling_a_fired_handle_is_harmless_and_does_not_cancel_a_later_timer() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    fired_handle = clock.call_at(START + 10, trace.timer("first"))
    clock.advance_to(START + 10)
    later_handle = clock.call_at(START + 20, trace.timer("second"))
    clock.cancel(fired_handle)
    clock.cancel(fired_handle)
    assert later_handle != fired_handle
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 20
    clock.advance_to(START + 20)
    assert trace.labels == ["first", "second"]
    assert clock.now_ns() == START + 20


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_callback_cancelling_its_own_handle_is_harmless() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    own: list[int] = []

    def callback(due_ns: int) -> None:
        trace.timer("self-cancelling")(due_ns)
        clock.cancel(own[0])

    own.append(clock.call_at(START + 10, callback))
    clock.call_at(START + 20, trace.timer("next"))
    clock.advance_to(START + 30)
    assert trace.labels == ["self-cancelling", "next"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_cancelling_unknown_handles_does_not_disturb_pending_timers() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    known = [clock.call_at(START + 10 + index, trace.timer(f"t{index}")) for index in range(3)]
    for handle in range(-5, 200):
        if handle not in known:
            clock.cancel(handle)
    assert clock.pending_timers() == 3
    assert clock.next_timer_ns() == START + 10
    assert clock.now_ns() == START
    clock.advance_to(START + 100)
    assert trace.labels == ["t0", "t1", "t2"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_cancelling_a_handle_that_was_never_issued_does_not_cancel_a_timer_scheduled_later() -> None:
    # "Unknown or already fired handles are ignored": a cancel for a handle the clock has not
    # issued must have no effect at all - in particular it must not silently kill a timer that is
    # registered afterwards.  A lost timer in replay is a lost protective action.
    clock = ReplayClock(START)
    for never_issued in range(-5, 200):
        clock.cancel(never_issued)
    trace = _Trace(clock)
    for index in range(5):
        clock.call_at(START + 10 + index, trace.timer(f"t{index}"))
    assert clock.pending_timers() == 5
    assert clock.next_timer_ns() == START + 10
    clock.advance_to(START + 100)
    assert trace.labels == ["t0", "t1", "t2", "t3", "t4"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_timer_handles_are_never_reused() -> None:
    clock = ReplayClock(START)
    handles: list[int] = []
    for round_number in range(20):
        fired = clock.call_at(clock.now_ns() + 1, lambda due_ns: None)
        cancelled = clock.call_at(clock.now_ns() + 1, lambda due_ns: None)
        kept = clock.call_at(START + NS_PER_SECOND + round_number, lambda due_ns: None)
        handles += [fired, cancelled, kept]
        clock.cancel(cancelled)
        clock.advance_by(1)
    assert len(set(handles)) == len(handles) == 60
    assert clock.pending_timers() == 20


# =========================================================================== ReplayClock: introspection


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_pending_timers_and_next_timer_ns_follow_schedule_cancel_and_fire() -> None:
    clock = ReplayClock(START)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (0, None)
    late = clock.call_at(START + 30, lambda due_ns: None)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (1, START + 30)
    early = clock.call_at(START + 10, lambda due_ns: None)
    middle = clock.call_at(START + 20, lambda due_ns: None)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (3, START + 10)
    clock.cancel(early)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (2, START + 20)
    clock.cancel(early)  # second cancel does not count twice
    assert (clock.pending_timers(), clock.next_timer_ns()) == (2, START + 20)
    clock.advance_to(START + 20)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (1, START + 30)
    clock.cancel(middle)  # already fired
    assert (clock.pending_timers(), clock.next_timer_ns()) == (1, START + 30)
    clock.cancel(late)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (0, None)
    clock.advance_to(START + 100)
    assert (clock.pending_timers(), clock.next_timer_ns()) == (0, None)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_introspection_does_not_change_what_fires() -> None:
    plain = ReplayClock(START)
    inspected = ReplayClock(START)
    traces = (_Trace(plain), _Trace(inspected))
    for clock, trace in zip((plain, inspected), traces, strict=True):
        doomed = clock.call_at(START + 5, trace.timer("doomed"))
        clock.call_at(START + 7, trace.timer("a"))
        clock.call_at(START + 9, trace.timer("b"))
        clock.cancel(doomed)
    for _ in range(3):
        assert inspected.next_timer_ns() == START + 7
        assert inspected.pending_timers() == 2
    plain.advance_to(START + 10)
    inspected.advance_to(START + 10)
    assert traces[0].events == traces[1].events == [("a", START + 7, START + 7), ("b", START + 9, START + 9)]


# =========================================================================== ReplayClock: callbacks


def _advance_to_beyond(clock: ReplayClock) -> None:
    clock.advance_to(clock.now_ns() + NS_PER_SECOND)


def _advance_to_next_tick(clock: ReplayClock) -> None:
    clock.advance_to(clock.now_ns() + 1)


def _advance_by_one(clock: ReplayClock) -> None:
    clock.advance_by(1)


_ADVANCE_ATTEMPTS = [_advance_to_beyond, _advance_to_next_tick, _advance_by_one]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("attempt", _ADVANCE_ATTEMPTS)
def test_a_callback_may_not_advance_the_clock(attempt: Callable[[ReplayClock], None]) -> None:
    clock = ReplayClock(START)
    refusals: list[GieError] = []
    observed: list[int] = []
    later: list[int] = []

    def meddling(due_ns: int) -> None:
        try:
            attempt(clock)
        except GieError as exc:
            refusals.append(exc)
        observed.append(clock.now_ns())

    clock.call_at(START + 10, meddling)
    clock.call_at(START + 20, later.append)
    assert clock.advance_to(START + 50) == START + 50
    assert len(refusals) == 1, "the clock let a timer callback advance it"
    assert observed == [START + 10]  # time did not move inside the callback
    assert later == [START + 20]  # the rest of the advance is unaffected
    assert clock.now_ns() == START + 50


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("attempt", _ADVANCE_ATTEMPTS)
def test_an_uncaught_advance_attempt_in_a_callback_fails_the_advance_and_keeps_the_clock_usable(
    attempt: Callable[[ReplayClock], None],
) -> None:
    clock = ReplayClock(START)
    later: list[int] = []
    clock.call_at(START + 10, lambda due_ns: attempt(clock))
    clock.call_at(START + 20, later.append)
    with pytest.raises(GieError):
        clock.advance_to(START + 50)
    assert clock.now_ns() == START + 10
    assert later == []
    assert clock.pending_timers() == 1
    assert clock.advance_to(START + 50) == START + 50
    assert later == [START + 20]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_an_exception_in_a_callback_propagates_and_leaves_the_clock_consistent() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    failure = _Boom("synthetic fixture failure")

    def failing(due_ns: int) -> None:
        trace.timer("B")(due_ns)
        raise failure

    clock.call_at(START + 10, trace.timer("A"))
    clock.call_at(START + 20, failing)
    clock.call_at(START + 20, trace.timer("C"))
    clock.call_at(START + 30, trace.timer("D"))

    with pytest.raises(_Boom) as caught:
        clock.advance_to(START + 40)
    assert caught.value is failure

    # Time stands at the failing timer: not beyond it (the target was never reached) and not
    # before it (the callback already observed that instant and time never decreases).
    assert clock.now_ns() == START + 20
    assert clock.monotonic_ns() == 20
    assert trace.labels == ["A", "B"]
    # The timers that had not run yet are still pending.
    assert clock.pending_timers() == 2
    assert clock.next_timer_ns() == START + 20

    # A later advance works and delivers the remaining timers in order; the failed one is not repeated.
    assert clock.advance_to(START + 40) == START + 40
    assert trace.events == [
        ("A", START + 10, START + 10),
        ("B", START + 20, START + 20),
        ("C", START + 20, START + 20),
        ("D", START + 30, START + 30),
    ]
    assert clock.pending_timers() == 0
    assert clock.now_ns() == START + 40


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_clock_enforces_all_its_rules_after_a_failed_advance() -> None:
    clock = ReplayClock(START)
    fired: list[int] = []

    def failing(due_ns: int) -> None:
        raise _Boom("synthetic fixture failure")

    clock.call_at(START + 20, failing)
    remaining = clock.call_at(START + 30, fired.append)
    with pytest.raises(_Boom):
        clock.advance_to(START + 40)
    assert clock.now_ns() == START + 20

    # It is not stuck in "a callback is running" mode, and it still refuses to go backwards.
    assert clock.advance_by(0) == START + 20
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START + 19)
    with pytest.raises(ClockRegressionError):
        clock.call_at(START + 19, fired.append)
    assert clock.now_ns() == START + 20
    # Scheduling and cancelling still work.
    clock.call_at(START + 25, fired.append)
    clock.cancel(remaining)
    assert clock.pending_timers() == 1
    assert clock.advance_to(START + 40) == START + 40
    assert fired == [START + 25]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_every_failing_callback_stops_its_own_advance_only() -> None:
    clock = ReplayClock(START)
    attempts: list[int] = []

    def failing(due_ns: int) -> None:
        attempts.append(due_ns)
        raise _Boom("synthetic fixture failure")

    for offset in (10, 20, 30):
        clock.call_at(START + offset, failing)
    for expected_now in (START + 10, START + 20, START + 30):
        with pytest.raises(_Boom):
            clock.advance_to(START + 100)
        assert clock.now_ns() == expected_now
    assert attempts == [START + 10, START + 20, START + 30]
    assert clock.pending_timers() == 0
    assert clock.advance_to(START + 100) == START + 100


# =========================================================================== ReplayClock: determinism

# A script is a list of operations:
#   ("call_at", delay, child_delays)  register a timer at now + delay whose callback registers one
#                                     further timer per child delay (relative to its own due time)
#   ("cancel", index)                 cancel the index-th handle issued so far (modulo their number)
#   ("cancel_unknown", number)        cancel a handle that has not been issued so far (must be ignored)
#   ("advance", delta)                advance the clock by delta
_Script = Sequence[tuple[Any, ...]]

_OPERATIONS = st.one_of(
    st.tuples(st.just("call_at"), st.integers(0, 40), st.lists(st.integers(0, 25), max_size=3).map(tuple)),
    st.tuples(st.just("cancel"), st.integers(0, 60)),
    st.tuples(st.just("cancel_unknown"), st.integers(0, 60)),
    st.tuples(st.just("advance"), st.integers(0, 50)),
)
_SCRIPTS = st.lists(_OPERATIONS, max_size=40)

_EXAMPLE_SCRIPT: list[tuple[Any, ...]] = [
    ("call_at", 5, (0, 3, 30)),  # operation 0
    ("call_at", 5, ()),  # operation 1
    ("cancel_unknown", 0),  # a handle nobody holds yet - possibly the next one to be issued
    ("call_at", 8, (0,)),  # operation 3
    ("call_at", 2, ()),  # operation 4, the fourth handle
    ("cancel", 3),  # cancels the fourth handle
    ("advance", 10),
    ("cancel", 0),  # the first handle has fired by now
    ("advance", 0),
    ("advance", 50),
]


def _drive(start_ns: int, script: _Script) -> list[tuple[Any, ...]]:
    """Run *script* against a fresh ``ReplayClock`` and return everything that was observable."""
    clock = ReplayClock(start_ns)
    trace: list[tuple[Any, ...]] = []
    handles: list[int] = []

    def make_callback(label: tuple[int, ...], child_delays: tuple[int, ...]) -> Callable[[int], None]:
        def callback(due_ns: int) -> None:
            trace.append(("fired", label, due_ns, clock.now_ns(), clock.monotonic_ns()))
            for position, delay in enumerate(child_delays):
                handles.append(clock.call_at(due_ns + delay, make_callback((*label, position), ())))

        return callback

    for index, operation in enumerate(script):
        kind, amount = operation[0], operation[1]
        if kind == "call_at":
            handles.append(clock.call_at(clock.now_ns() + amount, make_callback((index,), operation[2])))
        elif kind == "cancel":
            if handles:
                clock.cancel(handles[amount % len(handles)])
        elif kind == "cancel_unknown":
            never_issued = amount
            while never_issued in handles:
                never_issued += 1
            clock.cancel(never_issued)
            if -1 - amount not in handles:
                clock.cancel(-1 - amount)
        else:
            trace.append(("advanced", clock.advance_by(amount)))
        trace.append(
            ("state", clock.now_ns(), clock.monotonic_ns(), clock.pending_timers(), clock.next_timer_ns())
        )
    return trace


@dataclass
class _ModelTimer:
    due_ns: int
    registration: int
    label: tuple[int, ...]
    child_delays: tuple[int, ...]
    pending: bool = True


def _reference(start_ns: int, script: _Script) -> list[tuple[Any, ...]]:
    """Naive model of the documented semantics, written without a heap.

    During an advance, repeatedly pick the pending timer with the smallest
    ``(due time, registration order)`` that is due at or before the target, move time to its due
    time and run it; when none is left, move time to the target.
    """
    now = start_ns
    timers: list[_ModelTimer] = []  # position == registration order == position of its handle
    trace: list[tuple[Any, ...]] = []
    for index, operation in enumerate(script):
        kind, amount = operation[0], operation[1]
        if kind == "call_at":
            timers.append(_ModelTimer(now + amount, len(timers), (index,), operation[2]))
        elif kind == "cancel":
            if timers:
                timers[amount % len(timers)].pending = False
        elif kind == "cancel_unknown":
            pass  # documented as ignored: no effect on any timer, now or later
        else:
            target = now + amount
            while True:
                due = [timer for timer in timers if timer.pending and timer.due_ns <= target]
                if not due:
                    break
                current = min(due, key=lambda timer: (timer.due_ns, timer.registration))
                current.pending = False
                now = current.due_ns
                trace.append(("fired", current.label, current.due_ns, now, now - start_ns))
                for position, delay in enumerate(current.child_delays):
                    timers.append(
                        _ModelTimer(current.due_ns + delay, len(timers), (*current.label, position), ())
                    )
            now = target
            trace.append(("advanced", now))
        waiting = [timer.due_ns for timer in timers if timer.pending]
        trace.append(("state", now, now - start_ns, len(waiting), min(waiting, default=None)))
    return trace


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_example_script_exercises_nested_scheduling_and_cancellation() -> None:
    # Guards the property tests below against being vacuous: this script fires timers, fires
    # timers scheduled by callbacks, cancels a pending timer, cancels a handle that was never
    # issued and cancels one that has already fired.  Timers are labelled by the position of the
    # operation that registered them; children append their own position.
    trace = _drive(START, _EXAMPLE_SCRIPT)
    fired = [entry[1] for entry in trace if entry[0] == "fired"]
    # Hand-derived: at +5 timers 0 and 1 (registered first) run before the child (0, 0) that timer 0
    # registered for the same instant; at +8 timer 3 runs before (0, 1) and its own child (3, 0),
    # which were registered in that order; (0, 2) is due at +35 and fires in the last advance.
    assert fired == [(0,), (1,), (0, 0), (3,), (0, 1), (3, 0), (0, 2)]
    assert (4,) not in fired  # cancelled before it was due
    assert trace[-1] == ("state", START + 60, 60, 0, None)
    assert trace == _reference(START, _EXAMPLE_SCRIPT)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(start_ns=st.integers(0, 10**18), script=_SCRIPTS)
@example(start_ns=START, script=_EXAMPLE_SCRIPT)
def test_two_clocks_driven_by_the_same_script_produce_the_identical_callback_trace(
    start_ns: int, script: _Script
) -> None:
    assert _drive(start_ns, script) == _drive(start_ns, script)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(start_ns=st.integers(0, 10**18), script=_SCRIPTS)
@example(start_ns=START, script=_EXAMPLE_SCRIPT)
def test_replay_clock_fires_timers_exactly_like_the_reference_model(start_ns: int, script: _Script) -> None:
    assert _drive(start_ns, script) == _reference(start_ns, script)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(start_ns=st.integers(0, 10**18), script=_SCRIPTS)
@example(start_ns=START, script=_EXAMPLE_SCRIPT)
def test_observed_replay_time_never_decreases_and_callbacks_see_their_due_time(
    start_ns: int, script: _Script
) -> None:
    previous = start_ns
    for entry in _drive(start_ns, script):
        if entry[0] == "fired":
            _, _, due_ns, now_ns, monotonic_ns = entry
            assert now_ns == due_ns
            assert monotonic_ns == due_ns - start_ns
        elif entry[0] == "state":
            now_ns = entry[1]
            assert entry[2] == now_ns - start_ns
        else:
            now_ns = entry[1]
        assert now_ns >= previous
        previous = now_ns


# =========================================================================== fake operating-system time


class _FakeTime:
    """SYNTHETIC FIXTURE operating-system time: a wall and a monotonic reading that only the test moves."""

    def __init__(self, wall_ns: int = START, monotonic_ns: int = 1_000) -> None:
        self.wall_ns = wall_ns
        self.monotonic_ns = monotonic_ns
        self.wall_calls = 0
        self.monotonic_calls = 0

    def read_wall(self) -> int:
        self.wall_calls += 1
        return self.wall_ns

    def read_monotonic(self) -> int:
        self.monotonic_calls += 1
        return self.monotonic_ns

    def pass_time(self, delta_ns: int) -> None:
        """Real time passes: both scales move forward together."""
        self.wall_ns += delta_ns
        self.monotonic_ns += delta_ns

    def step_wall(self, delta_ns: int) -> None:
        """The operating system steps the wall clock (negative: backwards); no time passes."""
        self.wall_ns += delta_ns

    def clock(self) -> SystemClock:
        return SystemClock(wall_source=self.read_wall, monotonic_source=self.read_monotonic)


def _replay_clock() -> Clock:
    return ReplayClock(START)


def _system_clock_on_fake_time() -> Clock:
    return _FakeTime().clock()


def _paper_clock_on_fake_time() -> Clock:
    return PaperClock(_FakeTime().clock())


# =========================================================================== protocols


class _ClockWithoutRegressions:
    """SYNTHETIC FIXTURE: everything a clock had before ``regressions_observed`` was added."""

    mode = ClockMode.REPLAY

    def now_ns(self) -> int:
        return START

    def monotonic_ns(self) -> int:
        return 0


class _CompleteClock(_ClockWithoutRegressions):
    """SYNTHETIC FIXTURE: the same with the health counter."""

    regressions_observed = 0


class _OnlyCallAt:
    """SYNTHETIC FIXTURE: a timer service without ``call_after`` and ``cancel``."""

    def call_at(self, due_ns: int, callback: Callable[[int], None]) -> int:
        return 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_clock_protocol_requires_regressions_observed() -> None:
    assert isinstance(_CompleteClock(), Clock)
    assert not isinstance(_ClockWithoutRegressions(), Clock)
    for member in ("mode", "now_ns", "monotonic_ns", "regressions_observed"):
        assert hasattr(Clock, member)
    assert isinstance(inspect.getattr_static(Clock, "regressions_observed"), property)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_replay_clock_never_observes_a_regression() -> None:
    clock = ReplayClock(START)
    assert clock.regressions_observed == 0
    assert type(clock.regressions_observed) is int
    clock.call_at(START + 5, lambda due_ns: None)
    clock.advance_by(NS_PER_SECOND)
    # A refused move backwards is a refused request, not a step of the time source.
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START)
    with pytest.raises(ClockRegressionError):
        clock.call_at(START, lambda due_ns: None)
    with pytest.raises(ClockRegressionError):
        clock.advance_by(-1)
    assert clock.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize(
    "make_clock",
    [_replay_clock, _system_clock_on_fake_time, _paper_clock_on_fake_time],
    ids=["replay", "system", "paper"],
)
def test_regressions_observed_is_a_read_only_property(make_clock: Callable[[], Clock]) -> None:
    clock = make_clock()
    assert isinstance(inspect.getattr_static(clock, "regressions_observed"), property)
    assert clock.regressions_observed == 0
    with pytest.raises(AttributeError):
        clock.regressions_observed = 7  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del clock.regressions_observed  # type: ignore[misc]
    assert clock.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
def test_replay_clock_and_polling_scheduler_satisfy_the_scheduler_protocol() -> None:
    replay = ReplayClock(START)
    system = _FakeTime().clock()
    paper_clock = PaperClock(system)
    paper = PollingScheduler(paper_clock)
    assert isinstance(replay, Scheduler)
    assert isinstance(replay, Clock)  # clock and scheduler of a replay run
    assert isinstance(paper, Scheduler)
    assert not isinstance(paper, Clock)  # a paper run takes its time from the PaperClock ...
    assert isinstance(paper_clock, Clock)
    assert not isinstance(paper_clock, Scheduler)  # ... which is no timer service
    assert not isinstance(system, Scheduler)  # nor is the system clock underneath it
    # The check is meaningful.
    assert not isinstance(object(), Scheduler)
    assert not isinstance(_OnlyCallAt(), Scheduler)
    for member in ("call_at", "call_after", "cancel"):
        assert callable(getattr(Scheduler, member))


# =========================================================================== ReplayClock: call_after


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delay_ns", [1, 7, NS_PER_MS, NS_PER_SECOND])
def test_call_after_schedules_relative_to_the_current_virtual_instant(delay_ns: int) -> None:
    clock = ReplayClock(START)
    clock.advance_by(123)
    trace = _Trace(clock)
    handle = clock.call_after(delay_ns, trace.timer("relative"))
    assert type(handle) is int
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 123 + delay_ns
    assert clock.now_ns() == START + 123  # scheduling does not move time
    clock.advance_by(delay_ns - 1)
    assert trace.events == []  # never early
    clock.advance_by(1)
    due_ns = START + 123 + delay_ns
    assert trace.events == [("relative", due_ns, due_ns)]  # callback(due_ns), fired exactly at its instant
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_call_after_with_no_delay_fires_on_the_next_advance_even_of_zero_length() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    clock.call_after(0, trace.timer("now"))
    assert trace.events == []  # registering alone fires nothing
    assert clock.next_timer_ns() == START
    assert clock.advance_by(0) == START
    assert trace.events == [("now", START, START)]


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
def test_a_callback_rescheduling_itself_with_call_after_fires_exactly_once_per_period() -> None:
    # "Once-per-second tactical work" is scheduled through the scheduler.  Inside a replay callback
    # "now" is the timer's due time, so call_after is relative to it and the period never drifts -
    # however far a single advance jumps.
    clock = ReplayClock(START)
    seen: list[tuple[int, int]] = []

    def every_second(due_ns: int) -> None:
        seen.append((due_ns, clock.now_ns()))
        clock.call_after(NS_PER_SECOND, every_second)

    clock.call_after(NS_PER_SECOND, every_second)
    clock.advance_by(10 * NS_PER_SECOND + 5)
    expected = [START + step * NS_PER_SECOND for step in range(1, 11)]
    assert seen == [(due_ns, due_ns) for due_ns in expected]
    assert clock.pending_timers() == 1
    assert clock.next_timer_ns() == START + 11 * NS_PER_SECOND


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_call_at_and_call_after_timers_share_one_queue_and_one_handle_space() -> None:
    clock = ReplayClock(START)
    trace = _Trace(clock)
    handles = [
        clock.call_after(20, trace.timer("after-20")),
        clock.call_at(START + 10, trace.timer("at-10")),
        clock.call_after(10, trace.timer("after-10")),  # same instant as at-10, registered later
        clock.call_at(START + 20, trace.timer("at-20")),  # same instant as after-20, registered later
        clock.call_after(15, trace.timer("cancelled")),
    ]
    assert len(set(handles)) == 5
    clock.cancel(handles[4])
    assert clock.pending_timers() == 4
    clock.advance_by(30)
    assert trace.labels == ["at-10", "after-10", "after-20", "at-20"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delay_ns", [-1, -NS_PER_MS, -START, -(2**70)])
def test_call_after_refuses_a_negative_delay_and_registers_nothing(delay_ns: int) -> None:
    # A negative delay is a due time in the past: "an effect without a cause".
    clock = ReplayClock(START)
    clock.advance_by(100)
    fired: list[int] = []
    with pytest.raises(ClockRegressionError) as caught:
        clock.call_after(delay_ns, fired.append)
    assert caught.value.code == "GIE.TIME.CLOCK_REGRESSION"
    assert clock.pending_timers() == 0
    assert clock.next_timer_ns() is None
    clock.advance_by(1000)
    assert fired == []


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delay_ns", _NOT_AN_INT)
def test_call_after_rejects_delays_that_are_not_int_durations(delay_ns: Any) -> None:
    clock = ReplayClock(START)
    with pytest.raises(ContractValidationError):
        clock.call_after(delay_ns, lambda due_ns: None)
    assert clock.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
@pytest.mark.parametrize("delay_ns", [6, NS_PER_SECOND, MAX_TIME_NS, 2**64])
def test_call_after_beyond_the_largest_instant_is_rejected(delay_ns: int) -> None:
    clock = ReplayClock(MAX_TIME_NS - 5)
    fired: list[int] = []
    with pytest.raises(ContractValidationError):
        clock.call_after(delay_ns, fired.append)
    assert clock.pending_timers() == 0
    clock.call_after(5, fired.append)  # the largest instant itself is a valid due time
    clock.advance_to(MAX_TIME_NS)
    assert fired == [MAX_TIME_NS]


# =========================================================================== SystemClock


class _Readings:
    """SYNTHETIC FIXTURE time source: returns the scripted readings one after another."""

    def __init__(self, readings: Sequence[int]) -> None:
        self._readings = list(readings)
        self.calls = 0

    def __call__(self) -> int:
        value = self._readings[self.calls]
        self.calls += 1
        return value


def _system_clock(wall: Sequence[int] = (), monotonic: Sequence[int] = ()) -> SystemClock:
    return SystemClock(wall_source=_Readings(wall), monotonic_source=_Readings(monotonic))


def _read_script(script: Sequence[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """Show a fresh ``SystemClock`` the ``(wall, monotonic)`` readings of *script* one after another.

    Returns the values of ``now_ns()`` and the value of ``regressions_observed`` after each reading.
    """
    fake = _FakeTime(wall_ns=0, monotonic_ns=0)
    clock = fake.clock()
    values: list[int] = []
    counts: list[int] = []
    for wall_ns, monotonic_ns in script:
        fake.wall_ns, fake.monotonic_ns = wall_ns, monotonic_ns
        values.append(clock.now_ns())
        counts.append(clock.regressions_observed)
    return values, counts


@pytest.mark.requirement("S00-ASG-19")
def test_system_clock_mode_is_system() -> None:
    clock = _system_clock()
    assert clock.mode is ClockMode.SYSTEM
    assert clock.mode.value == "SYSTEM"
    assert SystemClock().mode is ClockMode.SYSTEM


@pytest.mark.requirement("S00-ASG-19")
def test_system_clock_satisfies_the_runtime_checkable_clock_protocol() -> None:
    assert isinstance(_system_clock(), Clock)
    assert isinstance(SystemClock(), Clock)


@pytest.mark.requirement("S00-ASG-19")
def test_system_clock_reads_the_injected_sources() -> None:
    fake = _FakeTime(wall_ns=START, monotonic_ns=1_000)
    clock = fake.clock()
    assert (fake.wall_calls, fake.monotonic_calls) == (0, 0)  # constructing reads nothing
    assert clock.regressions_observed == 0
    assert (fake.wall_calls, fake.monotonic_calls) == (0, 0)  # neither does asking for the counter

    # While the wall source moves forward its reading is the time - whatever the monotonic scale shows.
    assert clock.now_ns() == START
    fake.wall_ns, fake.monotonic_ns = START + 5, 1_001
    assert clock.now_ns() == START + 5
    fake.wall_ns, fake.monotonic_ns = START + 5 + NS_PER_SECOND, 5_000
    assert clock.now_ns() == START + 5 + NS_PER_SECOND
    assert fake.wall_calls >= 3
    assert clock.monotonic_ns() == 5_000
    fake.monotonic_ns = 5_017
    assert clock.monotonic_ns() == 5_017
    assert clock.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19")
def test_system_clock_defaults_to_the_operating_system_utc_and_monotonic_clocks() -> None:
    parameters = inspect.signature(SystemClock).parameters
    assert parameters["wall_source"].default is time.time_ns
    assert parameters["monotonic_source"].default is time.monotonic_ns

    clock = SystemClock()
    first, second = clock.now_ns(), clock.now_ns()
    assert type(first) is int
    assert 0 < first <= second <= MAX_TIME_NS
    elapsed_first, elapsed_second = clock.monotonic_ns(), clock.monotonic_ns()
    assert type(elapsed_first) is int
    assert 0 <= elapsed_first <= elapsed_second


@pytest.mark.requirement("S00-ASG-19", "G08-06")
@pytest.mark.parametrize("step_back_ns", [10 * NS_PER_SECOND, 3_600 * NS_PER_SECOND, 86_400 * NS_PER_SECOND])
def test_after_a_backward_step_time_continues_at_the_pace_of_the_monotonic_source(step_back_ns: int) -> None:
    fake = _FakeTime()
    clock = fake.clock()
    assert clock.now_ns() == START
    fake.step_wall(-step_back_ns)  # the operating system steps the wall clock backwards
    assert clock.now_ns() == START  # no time has passed since the last reading
    fake.pass_time(5 * NS_PER_MS)
    # Neither the stepped reading (START - step + 5 ms) nor a frozen clock (START).
    assert clock.now_ns() == START + 5 * NS_PER_MS
    fake.pass_time(7)
    assert clock.now_ns() == START + 5 * NS_PER_MS + 7
    fake.pass_time(2 * NS_PER_SECOND)
    assert clock.now_ns() == START + 5 * NS_PER_MS + 7 + 2 * NS_PER_SECOND
    assert clock.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_system_clock_never_decreases_when_the_wall_source_steps_backwards() -> None:
    #          forward      step back    behind       behind       caught up    forward
    script = [(1_000, 0), (900, 10), (950, 60), (990, 100), (1_100, 200), (1_101, 201)]
    values, counts = _read_script(script)
    assert values == [1_000, 1_010, 1_060, 1_100, 1_100, 1_101]
    assert all(later >= earlier for earlier, later in itertools.pairwise(values))
    assert counts == [0, 1, 1, 1, 1, 1]  # one backward step, counted once


@pytest.mark.requirement("S00-ASG-19", "G08-06")
@pytest.mark.parametrize("readings_while_behind", [1, 2, 10, 500])
def test_a_backward_step_is_counted_once_however_long_the_wall_source_stays_behind(
    readings_while_behind: int,
) -> None:
    fake = _FakeTime()
    clock = fake.clock()
    clock.now_ns()
    fake.step_wall(-NS_PER_SECOND)
    previous = START
    for _ in range(readings_while_behind):
        fake.pass_time(3)
        value = clock.now_ns()
        assert value == previous + 3  # never stands still
        assert clock.regressions_observed == 1
        previous = value


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_regressions_observed_counts_each_backward_step() -> None:
    fake = _FakeTime()
    clock = fake.clock()
    assert clock.now_ns() == START
    expected_now = START
    for step_number in range(1, 5):
        fake.step_wall(-1_000)  # a backward step ...
        fake.pass_time(10)
        expected_now += 10
        assert clock.now_ns() == expected_now
        assert clock.regressions_observed == step_number
        fake.step_wall(+5_000)  # ... and a re-synchronisation beyond the last reading
        expected_now = fake.wall_ns
        assert clock.now_ns() == expected_now
        assert clock.regressions_observed == step_number  # catching up is not a regression


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_a_second_backward_step_while_the_wall_source_is_still_behind_is_counted_too() -> None:
    # "Each backward step is counted once" / "how often the underlying time source was seen to step
    # backwards".  The wall source steps back twice: 1000 -> 900 and, before it has caught up,
    # 950 -> 800.  Both are steps of the time source; health monitoring must see two events.
    #          forward      step 1       behind      step 2      behind
    script = [(1_000, 0), (900, 10), (950, 60), (800, 70), (810, 80)]
    values, counts = _read_script(script)
    assert values == [1_000, 1_010, 1_060, 1_070, 1_080]  # time itself keeps its pace
    assert counts == [0, 1, 1, 2, 2]


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_system_clock_resumes_the_wall_reading_as_soon_as_the_source_has_caught_up() -> None:
    fake = _FakeTime()
    clock = fake.clock()
    assert clock.now_ns() == START
    fake.step_wall(-400)
    fake.pass_time(100)
    assert clock.now_ns() == START + 100  # wall shows START - 300: behind
    fake.step_wall(+400)  # caught up exactly: wall shows START + 100
    assert clock.now_ns() == START + 100
    fake.pass_time(1)
    assert clock.now_ns() == START + 101
    # From here on the wall reading is the time again, also when it runs faster than the monotonic scale.
    fake.wall_ns += 500
    assert clock.now_ns() == START + 601
    assert clock.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_forward_and_repeated_readings_are_not_regressions() -> None:
    script = [(START, 0), (START, 0), (START, 5), (START + 1, 5), (START + 1, 9), (START + NS_PER_SECOND, 9)]
    values, counts = _read_script(script)
    assert values == [START, START, START, START + 1, START + 1, START + NS_PER_SECOND]
    assert counts == [0] * 6


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_expiry_keeps_failing_closed_during_a_clock_step() -> None:
    # A record valid for one second must be expired one second later although the wall clock was
    # stepped back by an hour in between: a clock that froze at its last reading (or followed the
    # stepped reading) would keep the record usable for another hour.
    fake = _FakeTime()
    clock = fake.clock()
    valid_until_ns = clock.now_ns() + NS_PER_SECOND
    fake.step_wall(-3_600 * NS_PER_SECOND)
    fake.pass_time(NS_PER_SECOND - 1)
    assert clock.now_ns() < valid_until_ns  # not yet
    fake.pass_time(1)
    assert clock.now_ns() >= valid_until_ns  # at the expiry instant it is already expired
    assert clock.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19")
def test_reading_the_monotonic_scale_does_not_change_the_pace_of_a_clock_that_is_behind() -> None:
    fake = _FakeTime()
    clock = fake.clock()
    assert clock.now_ns() == START
    fake.step_wall(-NS_PER_SECOND)
    fake.pass_time(10)
    clock.monotonic_ns()  # elapsed-time measurements in between ...
    fake.pass_time(10)
    clock.monotonic_ns()
    assert clock.now_ns() == START + 20  # ... do not swallow the time that passed
    assert clock.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_a_misbehaving_monotonic_source_cannot_make_time_decrease_while_the_wall_source_is_behind() -> None:
    #          forward       step back    monotonic goes back   and forward again
    script = [(1_000, 500), (900, 510), (901, 100), (902, 505), (903, 520)]
    values, _ = _read_script(script)
    assert values[:2] == [1_000, 1_010]
    assert all(later >= earlier for earlier, later in itertools.pairwise(values))


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
def test_system_clock_never_exceeds_the_largest_instant() -> None:
    fake = _FakeTime(wall_ns=MAX_TIME_NS - 3, monotonic_ns=0)
    clock = fake.clock()
    assert clock.now_ns() == MAX_TIME_NS - 3
    fake.step_wall(-1_000)
    fake.monotonic_ns += 2
    assert clock.now_ns() == MAX_TIME_NS - 1
    fake.monotonic_ns += 50
    assert clock.now_ns() == MAX_TIME_NS
    fake.monotonic_ns += 50
    assert clock.now_ns() == MAX_TIME_NS


@pytest.mark.requirement("S00-ASG-19")
def test_monotonic_ns_never_decreases_even_if_its_source_misbehaves() -> None:
    clock = _system_clock(monotonic=[500, 100, 300, 500, 501, 0, 600])
    assert [clock.monotonic_ns() for _ in range(7)] == [500, 500, 500, 500, 501, 501, 600]


@pytest.mark.requirement("S00-ASG-19", "G08-06")
@_PROPERTY
@given(script=st.lists(st.tuples(st.integers(0, 60), st.integers(0, 8)), min_size=1, max_size=60))
@example(script=[(50, 0), (40, 3), (45, 0), (30, 2), (60, 1), (59, 4)])
def test_system_clock_follows_the_documented_rule_for_any_readings(script: list[tuple[int, int]]) -> None:
    # script: (wall reading, monotonic time elapsed since the previous reading).
    fake = _FakeTime(wall_ns=0, monotonic_ns=0)
    clock = fake.clock()
    last: int | None = None
    behind = False
    times_fallen_behind = 0
    readings_behind = 0
    for wall_ns, elapsed_ns in script:
        fake.wall_ns = wall_ns
        fake.monotonic_ns += elapsed_ns
        value = clock.now_ns()
        if last is None or wall_ns >= last:
            behind = False
            assert value == wall_ns  # the wall reading is the time
        else:
            readings_behind += 1
            times_fallen_behind += 0 if behind else 1
            behind = True
            assert value == last + elapsed_ns  # continues from the last reading at the monotonic pace
        assert last is None or value >= last  # never decreases
        last = value
        # Every time the wall source falls behind it has stepped backwards at least once, and it
        # cannot have stepped more often than it was read while behind.
        assert times_fallen_behind <= clock.regressions_observed <= readings_behind


@pytest.mark.requirement("S00-ASG-19")
@_PROPERTY
@given(readings=st.lists(st.integers(0, MAX_TIME_NS), min_size=1, max_size=60))
def test_system_clock_monotonic_scale_is_the_running_maximum_of_its_readings(readings: list[int]) -> None:
    clock = _system_clock(monotonic=readings)
    assert [clock.monotonic_ns() for _ in readings] == list(itertools.accumulate(readings, max))


class _YieldingInt(int):
    """SYNTHETIC FIXTURE reading that gives other threads a chance to run whenever it is compared.

    A clock has to compare a new reading with the last value it handed out before it stores the
    reading.  Yielding inside that comparison puts a thread switch exactly between "check" and
    "store", which is where an unsynchronised clock goes wrong; a clock that serialises its
    readers is unaffected.  The value itself is an ordinary ``int``.
    """

    __hash__ = int.__hash__

    def __lt__(self, other: int) -> bool:
        result = int.__lt__(self, other)
        time.sleep(0)
        return result

    def __le__(self, other: int) -> bool:
        result = int.__le__(self, other)
        time.sleep(0)
        return result

    def __gt__(self, other: int) -> bool:
        result = int.__gt__(self, other)
        time.sleep(0)
        return result

    def __ge__(self, other: int) -> bool:
        result = int.__ge__(self, other)
        time.sleep(0)
        return result


class _SawtoothSource:
    """SYNTHETIC FIXTURE time source for the thread tests (thread-safe, deterministic by call index).

    The n-th reading is ``base + 10 * n - 25 * (7919 * n % 13)``: it rises on average but steps
    backwards by up to 300 ns again and again, like an unstable operating-system clock.  Readings
    are handed out as :class:`_YieldingInt` so that threads interleave at the critical point.
    """

    def __init__(self, base: int) -> None:
        self._base = base
        self._lock = threading.Lock()
        self.calls = 0

    def reading(self, index: int) -> int:
        return self._base + 10 * index - 25 * (7919 * index % 13)

    def __call__(self) -> int:
        with self._lock:
            index = self.calls
            self.calls += 1
        return _YieldingInt(self.reading(index))


class _SteadySource:
    """SYNTHETIC FIXTURE monotonic source for the thread tests: every reading is 1 ns after the previous."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self) -> int:
        with self._lock:
            self.calls += 1
            return self.calls


def _hammer(read: Callable[[], int], threads: int = 8, reads: int = 300) -> list[list[int]]:
    """Call *read* from several threads at once; return what each thread saw, in its own order."""
    barrier = threading.Barrier(threads)
    seen: list[list[int]] = [[] for _ in range(threads)]
    errors: list[Exception] = []

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=30)
            mine = seen[slot]
            for _ in range(reads):
                mine.append(read())
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(slot,), daemon=True) for slot in range(threads)]
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # force frequent thread switches
    try:
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(timeout=60)
    finally:
        sys.setswitchinterval(previous_interval)
    assert not any(thread.is_alive() for thread in workers), "a reader thread did not finish"
    assert errors == []
    assert [len(values) for values in seen] == [reads] * threads
    return seen


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_system_clock_now_ns_is_non_decreasing_for_every_thread() -> None:
    source = _SawtoothSource(START)
    clock = SystemClock(wall_source=source, monotonic_source=_SteadySource())
    seen = _hammer(clock.now_ns)
    for slot, values in enumerate(seen):
        decreases = [(a, b) for a, b in itertools.pairwise(values) if b < a]
        assert decreases == [], f"thread {slot} saw time go backwards"
    # Time never ran behind the unstable source: the newest value is at least its highest reading.
    produced = [source.reading(index) for index in range(source.calls)]
    assert source.calls >= 8 * 300
    assert max(max(values) for values in seen) >= max(produced)
    assert min(min(values) for values in seen) >= min(produced)
    # The unstable source was noticed, and no more often than it was read.
    assert 0 < clock.regressions_observed < source.calls


@pytest.mark.requirement("S00-ASG-19")
def test_system_clock_monotonic_ns_is_non_decreasing_for_every_thread() -> None:
    source = _SawtoothSource(1_000_000)
    clock = SystemClock(wall_source=_Readings([]), monotonic_source=source)
    seen = _hammer(clock.monotonic_ns)
    for slot, values in enumerate(seen):
        decreases = [(a, b) for a, b in itertools.pairwise(values) if b < a]
        assert decreases == [], f"thread {slot} saw the monotonic scale go backwards"
    produced = {source.reading(index) for index in range(source.calls)}
    assert {value for values in seen for value in values} <= produced


# =========================================================================== PaperClock


def _paper_clock(wall_ns: int = START) -> tuple[_FakeTime, PaperClock]:
    """SYNTHETIC FIXTURE paper clock: a ``PaperClock`` on a ``SystemClock`` that reads fake sources."""
    fake = _FakeTime(wall_ns=wall_ns)
    return fake, PaperClock(fake.clock())


@pytest.mark.requirement("S00-ASG-19")
def test_a_paper_clock_is_a_clock_in_system_mode_that_starts_at_the_system_reading() -> None:
    _, clock = _paper_clock()
    assert isinstance(clock, Clock)
    assert not isinstance(clock, Scheduler)  # time and timers are separate objects in paper
    assert clock.mode is ClockMode.SYSTEM
    assert clock.now_ns() == START
    assert clock.wall_ns() == START
    assert clock.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_paper_step_time_does_not_move_between_steps_while_the_wall_clock_does() -> None:
    fake, clock = _paper_clock()
    monotonic_before = clock.monotonic_ns()
    fake.pass_time(5 * NS_PER_SECOND)  # processing takes real time ...
    reads_before = fake.wall_calls
    assert [clock.now_ns() for _ in range(3)] == [START] * 3  # ... step time stands still
    assert fake.wall_calls == reads_before  # reading step time does not read the operating system
    assert clock.wall_ns() == START + 5 * NS_PER_SECOND  # the run loop sees real time
    assert clock.monotonic_ns() == monotonic_before + 5 * NS_PER_SECOND  # elapsed time is measurable
    assert clock.now_ns() == START


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("step_offset", [0, 1, 7, NS_PER_SECOND])
def test_begin_step_moves_step_time_to_any_instant_from_the_current_one_up_to_the_wall_clock(
    step_offset: int,
) -> None:
    fake, clock = _paper_clock()
    fake.pass_time(NS_PER_SECOND)
    assert clock.begin_step(START + step_offset) == START + step_offset
    assert clock.now_ns() == START + step_offset
    # Several inputs share an instant: beginning a step at the current instant is allowed.
    assert clock.begin_step(START + step_offset) == START + step_offset
    # The wall clock itself is the latest instant a step may begin at.
    assert clock.begin_step(START + NS_PER_SECOND) == START + NS_PER_SECOND
    assert clock.now_ns() == START + NS_PER_SECOND


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("back_by", [1, 2, NS_PER_MS, 10 * NS_PER_SECOND, START])
def test_begin_step_refuses_an_instant_before_the_current_step_and_leaves_time_unchanged(
    back_by: int,
) -> None:
    fake, clock = _paper_clock()
    fake.pass_time(20 * NS_PER_SECOND)
    clock.begin_step(START + 10 * NS_PER_SECOND)
    current = clock.now_ns()
    with pytest.raises(ClockRegressionError) as caught:
        clock.begin_step(current - back_by)
    assert caught.value.code == "GIE.TIME.CLOCK_REGRESSION"
    assert clock.now_ns() == current
    assert clock.regressions_observed == 0  # a refused request is no step of the time source
    assert clock.begin_step(current + 1) == current + 1  # still usable


@pytest.mark.requirement("S00-ASG-19", "G08-06")
@pytest.mark.parametrize("ahead_by", [1, 2, NS_PER_MS, NS_PER_SECOND, 2**40])
def test_begin_step_refuses_an_instant_the_system_clock_has_not_reached(ahead_by: int) -> None:
    # A step later than the wall clock would be an input from the future (look-ahead).
    fake, clock = _paper_clock()
    fake.pass_time(100)
    with pytest.raises(FutureDataError) as caught:
        clock.begin_step(START + 100 + ahead_by)
    assert caught.value.code == "GIE.TIME.FUTURE"
    assert isinstance(caught.value, GieError)
    assert clock.now_ns() == START
    fake.pass_time(ahead_by)  # once real time has reached the instant, the step may begin
    assert clock.begin_step(START + 100 + ahead_by) == START + 100 + ahead_by


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize("time_ns", [*_NOT_AN_INT, *_NEGATIVE, *_TOO_LARGE])
def test_begin_step_rejects_values_that_are_not_valid_instants(time_ns: Any) -> None:
    fake, clock = _paper_clock()
    fake.pass_time(NS_PER_SECOND)
    with pytest.raises(ContractValidationError):
        clock.begin_step(time_ns)
    assert clock.now_ns() == START


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    "not_a_system_clock",
    [ReplayClock(START), _CompleteClock(), object(), 0, START],
    ids=["replay", "replay-mode-clock", "object", "zero", "int"],
)
def test_a_paper_clock_runs_only_on_a_system_clock(not_a_system_clock: Any) -> None:
    with pytest.raises(ContractValidationError):
        PaperClock(not_a_system_clock)
    assert PaperClock(_FakeTime().clock()).now_ns() == START  # a system clock is accepted


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_a_paper_clock_reports_the_regressions_of_its_system_clock_and_keeps_stepping() -> None:
    fake, clock = _paper_clock()
    fake.pass_time(10)
    clock.begin_step(START + 10)
    fake.step_wall(-3_600 * NS_PER_SECOND)  # the operating system steps the wall clock back an hour
    fake.pass_time(5)
    assert clock.wall_ns() == START + 15  # the system clock keeps running at the monotonic pace
    assert clock.regressions_observed == 1
    assert clock.begin_step(START + 15) == START + 15  # step time is unaffected by the step back
    with pytest.raises(FutureDataError):
        clock.begin_step(START + 16)
    assert clock.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19")
def test_a_paper_clock_never_reads_the_operating_system_clock_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("the paper clock must read the injected system clock only")

    with monkeypatch.context() as patch:
        for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"):
            patch.setattr(time, name, forbidden)
        fake, clock = _paper_clock()
        fake.pass_time(50)
        assert clock.begin_step(START + 50) == START + 50
        assert (clock.now_ns(), clock.wall_ns(), clock.regressions_observed) == (START + 50, START + 50, 0)


# =========================================================================== PollingScheduler


def _paper() -> tuple[_FakeTime, PaperClock, PollingScheduler]:
    """SYNTHETIC FIXTURE paper run: a ``PaperClock`` on fake sources and its ``PollingScheduler``."""
    fake, clock = _paper_clock()
    return fake, clock, PollingScheduler(clock)


class _PaperClockSubclass(PaperClock):
    """SYNTHETIC FIXTURE: a subclass of the paper clock - not exactly a ``PaperClock``."""

    __slots__ = ()


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    "make_clock",
    [
        lambda: _FakeTime().clock(),
        lambda: ReplayClock(START),
        lambda: ClockView(PaperClock(_FakeTime().clock())),
        lambda: _PaperClockSubclass(_FakeTime().clock()),
        lambda: None,
        object,
    ],
    ids=["system", "replay", "view-of-paper", "paper-subclass", "none", "object"],
)
def test_a_polling_scheduler_requires_exactly_a_paper_clock(make_clock: Callable[[], Any]) -> None:
    with pytest.raises(ContractValidationError):
        PollingScheduler(make_clock())
    assert isinstance(PollingScheduler(PaperClock(_FakeTime().clock())), Scheduler)


@pytest.mark.requirement("S00-ASG-19", "G03-03")
def test_polling_timers_fire_only_when_the_loop_polls() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    for label, delay in (("a", 10), ("b", 20), ("c", 30)):
        scheduler.call_after(delay, trace.timer(label))
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (3, START + 10)
    fake.pass_time(NS_PER_SECOND)  # every timer is long overdue ...
    for _ in range(3):
        clock.now_ns()  # ... and reading the clock is not polling
        clock.wall_ns()
    assert trace.events == []
    assert scheduler.pending_timers() == 3
    assert scheduler.run_due() == 3  # returns how many fired
    assert trace.labels == ["a", "b", "c"]
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (0, None)
    assert scheduler.run_due() == 0  # every timer fires exactly once
    assert trace.labels == ["a", "b", "c"]


@pytest.mark.requirement("S00-ASG-19")
def test_polling_timers_run_on_the_polling_thread_and_start_no_threads() -> None:
    fake, _, scheduler = _paper()
    threads_before = threading.active_count()
    callers: list[int] = []
    for delay in range(100):
        scheduler.call_after(delay, lambda due_ns: callers.append(threading.get_ident()))
    assert threading.active_count() == threads_before
    fake.pass_time(1_000)
    assert scheduler.run_due() == 100
    assert set(callers) == {threading.get_ident()}
    assert threading.active_count() == threads_before


@pytest.mark.requirement("S00-ASG-19")
def test_polling_timers_fire_in_due_time_then_registration_order() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    scheduler.call_at(START + 20, trace.timer("late-1"))
    scheduler.call_at(START + 10, trace.timer("early-1"))
    scheduler.call_after(20, trace.timer("late-2"))
    scheduler.call_after(10, trace.timer("early-2"))
    scheduler.call_at(START + 5, trace.timer("first"))
    fake.pass_time(100)
    assert scheduler.run_due() == 5
    assert trace.labels == ["first", "early-1", "early-2", "late-1", "late-2"]


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_a_polling_timer_never_fires_early() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    scheduler.call_after(10, trace.timer("t"))
    assert scheduler.run_due() == 0
    fake.pass_time(9)
    assert scheduler.run_due() == 0  # one nanosecond before its instant
    assert trace.events == []
    assert scheduler.pending_timers() == 1
    fake.pass_time(1)
    assert scheduler.run_due() == 1  # the first poll at or after its instant
    assert trace.events == [("t", START + 10, START + 10)]


@pytest.mark.requirement("S00-ASG-19")
def test_run_due_begins_a_step_at_each_due_time_however_late_the_loop_polls() -> None:
    # Step time: while a timer runs, ``now_ns`` is its due time - as in replay - even though the
    # wall clock is already later.  The lateness is measurable (wall_ns) but never seen as now_ns.
    fake, clock, scheduler = _paper()
    seen: list[tuple[str, int, int, int]] = []

    def timer(label: str) -> Callable[[int], None]:
        return lambda due_ns: seen.append((label, due_ns, clock.now_ns(), clock.wall_ns()))

    scheduler.call_after(10, timer("a"))
    scheduler.call_after(40, timer("b"))
    fake.pass_time(75)  # the loop polls late
    assert scheduler.run_due() == 2
    assert seen == [("a", START + 10, START + 10, START + 75), ("b", START + 40, START + 40, START + 75)]
    # Only the run loop moves time: after the poll step time is the last timer's instant.
    assert clock.now_ns() == START + 40
    clock.begin_step(START + 75)
    assert clock.now_ns() == START + 75


@pytest.mark.requirement("S00-ASG-19")
def test_run_due_with_a_limit_fires_only_timers_due_at_or_before_the_limit() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    for delay in (10, 20, 30):
        scheduler.call_after(delay, trace.timer(f"t{delay}"))
    fake.pass_time(100)  # all three are due by now
    # The next input was received at START + 20: timers due up to and including that instant fire
    # before it is delivered, later ones after it.
    assert scheduler.run_due(limit_ns=START + 20) == 2
    assert trace.labels == ["t10", "t20"]
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (1, START + 30)
    assert scheduler.run_due(limit_ns=START + 29) == 0
    assert scheduler.run_due(START + 30) == 1
    assert trace.labels == ["t10", "t20", "t30"]


@pytest.mark.requirement("S00-ASG-19", "G08-06")
@pytest.mark.parametrize("limit_offset", [21, 49, 50, 51, 10**6, None])
def test_a_limit_beyond_the_current_time_never_makes_a_timer_fire_early(limit_offset: int | None) -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    scheduler.call_after(10, trace.timer("due"))
    scheduler.call_after(50, trace.timer("not-yet"))
    fake.pass_time(20)
    limit_ns = MAX_TIME_NS if limit_offset is None else START + limit_offset
    assert scheduler.run_due(limit_ns=limit_ns) == 1  # the limit is capped at the wall clock
    assert trace.labels == ["due"]
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (1, START + 50)
    fake.pass_time(29)
    assert scheduler.run_due(limit_ns=limit_ns) == 0  # one nanosecond before its instant
    fake.pass_time(1)
    assert scheduler.run_due(limit_ns=limit_ns) == (1 if limit_ns >= START + 50 else 0)
    assert scheduler.run_due() == (0 if limit_ns >= START + 50 else 1)
    assert trace.events[-1] == ("not-yet", START + 50, START + 50)


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize("limit_ns", [True, False, 1.5, float("nan"), "1", [1], -1, MAX_TIME_NS + 1, 2**64])
def test_run_due_rejects_a_limit_that_is_not_a_valid_instant(limit_ns: Any) -> None:
    fake, _, scheduler = _paper()
    fired: list[int] = []
    scheduler.call_after(1, fired.append)
    fake.pass_time(10)
    with pytest.raises(ContractValidationError):
        scheduler.run_due(limit_ns=limit_ns)
    assert fired == []
    assert scheduler.pending_timers() == 1
    assert scheduler.run_due() == 1  # the refusal does not break the scheduler


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("before_by", [1, 50, START + 100])
def test_a_polling_due_time_before_the_step_instant_is_refused_like_in_replay(before_by: int) -> None:
    # "call_at refuses a due time in the past, exactly like ReplayClock" - the past is the step instant.
    fake, clock, scheduler = _paper()
    fake.pass_time(100)
    clock.begin_step(START + 100)
    fired: list[int] = []
    with pytest.raises(ClockRegressionError) as caught:
        scheduler.call_at(START + 100 - before_by, fired.append)
    assert caught.value.code == "GIE.TIME.CLOCK_REGRESSION"
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (0, None)
    # The same refusal in replay, for the same request at the same instant.
    replay = ReplayClock(START)
    replay.advance_to(START + 100)
    with pytest.raises(ClockRegressionError):
        replay.call_at(START + 100 - before_by, fired.append)
    # The step instant itself is a valid due time and fires at the next poll.
    scheduler.call_at(START + 100, fired.append)
    assert scheduler.run_due() == 1
    assert fired == [START + 100]


@pytest.mark.requirement("S00-ASG-19")
def test_a_polling_due_time_after_the_step_instant_but_before_the_wall_clock_fires_at_the_next_poll() -> None:
    # Real time has moved on while the step is processed; what counts for "the past" is step time.
    fake, clock, scheduler = _paper()
    fake.pass_time(100)
    trace = _Trace(clock)
    scheduler.call_at(START + 90, trace.timer("90"))
    scheduler.call_at(START + 50, trace.timer("50"))
    scheduler.call_at(START + 100, trace.timer("100"))
    scheduler.call_at(START, trace.timer("step-instant"))
    scheduler.call_at(START + 101, trace.timer("future"))
    assert trace.events == []  # scheduling alone fires nothing
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (5, START)
    assert scheduler.run_due() == 4
    assert trace.events == [
        ("step-instant", START, START),
        ("50", START + 50, START + 50),
        ("90", START + 90, START + 90),
        ("100", START + 100, START + 100),
    ]
    assert scheduler.pending_timers() == 1


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize("due_ns", [*_NOT_AN_INT, *_NEGATIVE, *_TOO_LARGE])
def test_polling_call_at_rejects_values_that_are_not_valid_instants(due_ns: Any) -> None:
    _, _, scheduler = _paper()
    with pytest.raises(ContractValidationError):
        scheduler.call_at(due_ns, lambda due: None)
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (0, None)


@pytest.mark.requirement("S00-ASG-19", "G03-03")
def test_polling_call_after_is_relative_to_the_step_instant_not_to_the_wall_clock() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    fake.pass_time(123)  # real time moved on, step time did not
    handle = scheduler.call_after(NS_PER_SECOND, trace.timer("from-step"))
    assert type(handle) is int
    assert scheduler.next_timer_ns() == START + NS_PER_SECOND
    clock.begin_step(START + 123)
    scheduler.call_after(NS_PER_SECOND, trace.timer("from-next-step"))
    assert scheduler.pending_timers() == 2
    fake.pass_time(NS_PER_SECOND - 124)  # wall: START + NS_PER_SECOND - 1
    assert scheduler.run_due() == 0
    fake.pass_time(1)
    assert scheduler.run_due() == 1
    fake.pass_time(122)
    assert scheduler.run_due() == 0
    fake.pass_time(1)
    assert scheduler.run_due() == 1
    assert trace.events == [
        ("from-step", START + NS_PER_SECOND, START + NS_PER_SECOND),
        ("from-next-step", START + 123 + NS_PER_SECOND, START + 123 + NS_PER_SECOND),
    ]


@pytest.mark.requirement("S00-ASG-19")
def test_polling_call_after_with_no_delay_fires_at_the_next_poll_at_the_step_instant() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    fake.pass_time(30)
    scheduler.call_after(0, trace.timer("now"))
    assert trace.events == []
    assert scheduler.run_due() == 1
    assert trace.events == [("now", START, START)]


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize("delay_ns", _NOT_AN_INT)
def test_polling_call_after_rejects_delays_that_are_not_int_durations(delay_ns: Any) -> None:
    _, _, scheduler = _paper()
    with pytest.raises(ContractValidationError):
        scheduler.call_after(delay_ns, lambda due_ns: None)
    assert scheduler.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
def test_polling_callbacks_may_schedule_further_timers_and_those_already_due_fire_in_the_same_poll() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    refusals: list[GieError] = []

    def first(due_ns: int) -> None:
        trace.timer("A")(due_ns)
        scheduler.call_at(START + 30, trace.timer("D"))  # same instant as B, registered later
        scheduler.call_at(START + 20, trace.timer("C"))  # between A and B
        try:
            scheduler.call_at(START + 5, trace.timer("P"))  # before A's own due time: the past
        except GieError as exc:
            refusals.append(exc)
        scheduler.call_after(0, trace.timer("N"))  # A's own instant: fires in this poll, after A
        scheduler.call_after(1, trace.timer("L"))  # START + 11
        scheduler.call_after(31, trace.timer("X"))  # START + 41: not due in this poll

    scheduler.call_at(START + 10, first)
    scheduler.call_at(START + 30, trace.timer("B"))
    fake.pass_time(40)
    assert scheduler.run_due() == 6
    assert trace.labels == ["A", "N", "L", "C", "B", "D"]
    assert [type(refusal) for refusal in refusals] == [ClockRegressionError]
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (1, START + 41)
    fake.pass_time(1)
    assert scheduler.run_due() == 1
    assert trace.events[-1] == ("X", START + 41, START + 41)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("schedule", ["call_at", "call_after"])
def test_a_timer_a_callback_registers_for_its_own_instant_fires_in_the_same_poll(schedule: str) -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)

    def parent(due_ns: int) -> None:
        trace.timer("parent")(due_ns)
        if schedule == "call_at":
            scheduler.call_at(due_ns, trace.timer("child"))
        else:
            scheduler.call_after(0, trace.timer("child"))

    scheduler.call_after(10, parent)
    scheduler.call_after(10, trace.timer("sibling"))  # registered before the child
    fake.pass_time(10)
    assert scheduler.run_due(limit_ns=START + 10) == 3
    assert trace.events == [
        ("parent", START + 10, START + 10),
        ("sibling", START + 10, START + 10),
        ("child", START + 10, START + 10),
    ]
    assert scheduler.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
def test_a_limit_also_holds_for_timers_scheduled_during_the_poll_and_while_time_moves_on() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)

    def slow(due_ns: int) -> None:
        trace.timer("slow")(due_ns)
        fake.pass_time(1_000)  # the wall clock keeps moving while callbacks run
        scheduler.call_at(START + 15, trace.timer("child-before-limit"))
        scheduler.call_at(START + 25, trace.timer("child-after-limit"))

    scheduler.call_at(START + 10, slow)
    scheduler.call_at(START + 30, trace.timer("after-limit"))
    fake.pass_time(50)
    assert scheduler.run_due(limit_ns=START + 20) == 2
    assert trace.labels == ["slow", "child-before-limit"]
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (2, START + 25)
    assert scheduler.run_due() == 2
    assert trace.labels == ["slow", "child-before-limit", "child-after-limit", "after-limit"]
    assert [now_ns for _, _, now_ns in trace.events] == [START + 10, START + 15, START + 25, START + 30]


@pytest.mark.requirement("S00-ASG-19")
def test_a_polling_callback_may_not_start_a_nested_poll() -> None:
    fake, clock, scheduler = _paper()
    refusals: list[GieError] = []
    later: list[int] = []

    def meddling(due_ns: int) -> None:
        for attempt in (scheduler.run_due, lambda: scheduler.run_due(limit_ns=clock.now_ns())):
            try:
                attempt()
            except GieError as exc:
                refusals.append(exc)

    scheduler.call_after(10, meddling)
    scheduler.call_after(20, later.append)
    fake.pass_time(50)
    assert scheduler.run_due() == 2
    assert [type(refusal) for refusal in refusals] == [ClockRegressionError, ClockRegressionError]
    assert later == [START + 20]  # the nested attempts fired nothing; the outer poll went on
    assert scheduler.pending_timers() == 0


@pytest.mark.requirement("S00-ASG-19")
def test_an_uncaught_nested_poll_fails_the_poll_and_keeps_the_scheduler_usable() -> None:
    fake, _, scheduler = _paper()
    later: list[int] = []
    scheduler.call_after(10, lambda due_ns: scheduler.run_due())
    scheduler.call_after(20, later.append)
    fake.pass_time(50)
    with pytest.raises(ClockRegressionError):
        scheduler.run_due()
    assert later == []
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (1, START + 20)
    assert scheduler.run_due() == 1  # not stuck in "a callback is running" mode
    assert later == [START + 20]


@pytest.mark.requirement("S00-ASG-19")
def test_an_exception_in_a_polling_callback_propagates_and_leaves_the_other_timers_pending() -> None:
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    failure = _Boom("synthetic fixture failure")

    def failing(due_ns: int) -> None:
        trace.timer("B")(due_ns)
        raise failure

    scheduler.call_after(10, trace.timer("A"))
    scheduler.call_after(20, failing)
    scheduler.call_after(20, trace.timer("C"))
    scheduler.call_after(30, trace.timer("D"))
    fake.pass_time(40)
    with pytest.raises(_Boom) as caught:
        scheduler.run_due()
    assert caught.value is failure
    assert trace.labels == ["A", "B"]
    assert clock.now_ns() == START + 20  # step time stays at the failed timer's instant, as in replay
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (2, START + 20)
    # A later poll delivers the remaining timers in order; the failed one is not repeated.
    assert scheduler.run_due() == 2
    assert trace.labels == ["A", "B", "C", "D"]


@pytest.mark.requirement("S00-ASG-19")
def test_a_poll_refused_because_the_loop_already_stepped_past_a_timer_keeps_that_timer_pending() -> None:
    # The run loop must poll before it begins a later step.  If it does not, run_due cannot begin a
    # step at the overdue timer's instant (step time never moves back) and refuses - fail closed.
    # A refusal changes nothing: the timer (a feed-silence check, an expiry) must not vanish silently.
    fake, clock, scheduler = _paper()
    fired: list[int] = []
    scheduler.call_after(10, fired.append)
    fake.pass_time(100)
    clock.begin_step(START + 50)  # loop error: stepped past the timer without polling
    with pytest.raises(ClockRegressionError):
        scheduler.run_due()
    assert fired == []
    assert clock.now_ns() == START + 50
    assert (scheduler.pending_timers(), scheduler.next_timer_ns()) == (1, START + 10)


@pytest.mark.requirement("S00-ASG-19", "G08-06")
def test_polling_timers_keep_firing_on_time_when_the_wall_clock_is_stepped_backwards() -> None:
    # Feed-silence checks and expiries are timers.  After a backward step of the wall clock they
    # must still fire when their delay has really elapsed - not an hour later.
    fake, clock, scheduler = _paper()
    trace = _Trace(clock)
    scheduler.call_after(NS_PER_SECOND, trace.timer("silence-check"))
    fake.step_wall(-3_600 * NS_PER_SECOND)
    fake.pass_time(NS_PER_SECOND - 1)
    assert scheduler.run_due() == 0
    fake.pass_time(1)
    assert scheduler.run_due() == 1
    assert trace.events == [("silence-check", START + NS_PER_SECOND, START + NS_PER_SECOND)]
    assert clock.regressions_observed == 1
    # A timer scheduled during the step is relative to the step instant (the silence check's).
    scheduler.call_after(500, trace.timer("next"))
    fake.pass_time(499)
    assert scheduler.run_due() == 0
    fake.pass_time(1)
    assert scheduler.run_due() == 1
    assert trace.events[-1] == ("next", START + NS_PER_SECOND + 500, START + NS_PER_SECOND + 500)


@pytest.mark.requirement("S00-ASG-19")
def test_a_polling_scheduler_takes_its_time_only_from_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("the scheduler must not read the operating-system clock itself")

    fired: list[int] = []
    with monkeypatch.context() as patch:
        for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"):
            patch.setattr(time, name, forbidden)
        fake, _, scheduler = _paper()
        scheduler.call_after(10, fired.append)
        scheduler.call_at(START + 20, fired.append)
        assert scheduler.run_due() == 0
        fake.pass_time(10)
        assert scheduler.run_due() == 1
        fake.pass_time(10)
        assert scheduler.run_due(limit_ns=START + 20) == 1
    assert fired == [START + 10, START + 20]


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.acceptance("S00-ACC-03")
def test_call_after_beyond_the_largest_instant_behaves_the_same_in_replay_and_paper() -> None:
    # "call_after: relative; same behaviour in replay and paper" and "invalid input is never
    # clamped".  A delay that leads beyond the largest instant is refused in replay; paper must
    # refuse it too instead of silently scheduling the timer for another instant.
    replay = ReplayClock(START)
    with pytest.raises(ContractValidationError):
        replay.call_after(MAX_TIME_NS, lambda due_ns: None)
    assert replay.pending_timers() == 0

    _, _, scheduler = _paper()
    with pytest.raises(ContractValidationError):
        scheduler.call_after(MAX_TIME_NS, lambda due_ns: None)
    assert scheduler.pending_timers() == 0


# =========================================================================== ClockView / SchedulerView

#: What a component must never be able to do with what it is handed.
_RUN_LOOP_ONLY = ("advance_to", "advance_by", "begin_step", "run_due", "wall_ns")


def _public(value: object) -> set[str]:
    return {name for name in dir(value) if not name.startswith("_")}


def _clocks_for_views() -> list[tuple[str, Clock]]:
    replay = ReplayClock(START)
    replay.advance_by(7)
    fake, paper = _paper_clock()
    fake.pass_time(9)
    paper.begin_step(START + 9)
    return [("replay", replay), ("paper", paper), ("system", fake.clock())]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("index", [0, 1, 2], ids=["replay", "paper", "system"])
def test_a_clock_view_reads_the_clock_and_offers_nothing_else(index: int) -> None:
    _, clock = _clocks_for_views()[index]
    view = ClockView(clock)
    assert isinstance(view, Clock)
    assert not isinstance(view, Scheduler)  # not even of a replay clock, which is both
    assert _public(view) == {"mode", "now_ns", "monotonic_ns", "regressions_observed"}
    for name in (*_RUN_LOOP_ONLY, "call_at", "call_after", "cancel"):
        assert not hasattr(view, name), name
    assert view.mode is clock.mode
    assert view.now_ns() == clock.now_ns()
    assert view.monotonic_ns() == clock.monotonic_ns()
    assert view.regressions_observed == clock.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19")
def test_a_clock_view_follows_the_clock_as_the_run_loop_moves_it() -> None:
    replay = ReplayClock(START)
    fake, paper = _paper_clock()
    replay_view, paper_view = ClockView(replay), ClockView(paper)
    replay.advance_to(START + 40)
    fake.pass_time(60)
    paper.begin_step(START + 40)
    assert replay_view.now_ns() == paper_view.now_ns() == START + 40
    fake.step_wall(-NS_PER_SECOND)
    paper.wall_ns()
    assert paper_view.regressions_observed == 1


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("make_scheduler", ["replay", "paper"])
def test_a_scheduler_view_schedules_and_cancels_and_offers_nothing_else(make_scheduler: str) -> None:
    if make_scheduler == "replay":
        replay = ReplayClock(START)
        scheduler: Any = replay
        clock: Clock = replay

        def run_loop(target_ns: int) -> None:
            replay.advance_to(target_ns)
    else:
        fake, paper, polling = _paper()
        scheduler, clock = polling, paper

        def run_loop(target_ns: int) -> None:
            fake.pass_time(target_ns - fake.wall_ns)
            polling.run_due(limit_ns=target_ns)
            paper.begin_step(target_ns)

    view = SchedulerView(scheduler)
    assert isinstance(view, Scheduler)
    assert not isinstance(view, Clock)
    assert _public(view) == {"call_at", "call_after", "cancel"}
    for name in (*_RUN_LOOP_ONLY, "now_ns", "next_timer_ns", "pending_timers"):
        assert not hasattr(view, name), name
    trace = _Trace(clock)
    view.call_at(START + 10, trace.timer("at"))
    view.call_after(20, trace.timer("after"))
    doomed = view.call_after(15, trace.timer("doomed"))
    view.cancel(doomed)
    assert scheduler.pending_timers() == 2  # the view schedules on the scheduler itself
    with pytest.raises(ClockRegressionError):
        view.call_after(-1, trace.timer("negative"))
    run_loop(START + 30)
    assert trace.events == [("at", START + 10, START + 10), ("after", START + 20, START + 20)]
    with pytest.raises(ClockRegressionError):
        view.call_at(START + 29, trace.timer("past"))  # the same refusal through the view


@pytest.mark.requirement("S00-ASG-19")
def test_nothing_can_be_set_on_a_view() -> None:
    replay = ReplayClock(START)
    clock_view, scheduler_view = ClockView(replay), SchedulerView(replay)
    for view, names in (
        (clock_view, ("now_ns", "monotonic_ns", "mode", "regressions_observed", "advance_to", "begin_step")),
        (scheduler_view, ("call_at", "call_after", "cancel", "run_due", "advance_to")),
    ):
        for name in names:
            with pytest.raises(AttributeError):
                setattr(view, name, lambda *args: None)
        assert not hasattr(view, "__dict__")
    assert clock_view.now_ns() == START
    assert _public(clock_view) == {"mode", "now_ns", "monotonic_ns", "regressions_observed"}
    assert _public(scheduler_view) == {"call_at", "call_after", "cancel"}


@pytest.mark.requirement("S00-ASG-19")
def test_a_view_cannot_be_redirected_to_another_clock_or_scheduler() -> None:
    # "Read-only view of the run clock": a component that holds the view must not be able to make
    # it (and every other component sharing it) read a clock or use a scheduler of its own choosing.
    run_clock = ReplayClock(START)
    other = ReplayClock(START + NS_PER_SECOND)
    clock_view, scheduler_view = ClockView(run_clock), SchedulerView(run_clock)
    with pytest.raises((AttributeError, GieError)):
        clock_view._clock = other  # type: ignore[misc]
    with pytest.raises((AttributeError, GieError)):
        scheduler_view._scheduler = other  # type: ignore[misc]
    assert clock_view.now_ns() == START


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    "not_a_clock",
    [None, object(), START, _ClockWithoutRegressions(), _OnlyCallAt()],
    ids=["none", "object", "int", "no-regressions", "scheduler-only"],
)
def test_a_clock_view_wraps_exactly_one_clock(not_a_clock: Any) -> None:
    with pytest.raises(ContractValidationError):
        ClockView(not_a_clock)
    _, _, polling = _paper()
    for wrong in (polling, SchedulerView(polling)):
        with pytest.raises(ContractValidationError):
            ClockView(wrong)
    replay = ReplayClock(START)
    with pytest.raises(TypeError):
        ClockView()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ClockView(replay, replay)  # type: ignore[call-arg]
    # A view of a view reads the same clock and is still only a view.
    nested = ClockView(ClockView(replay))
    replay.advance_by(3)
    assert nested.now_ns() == START + 3
    assert _public(nested) == {"mode", "now_ns", "monotonic_ns", "regressions_observed"}


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    "not_a_scheduler",
    [None, object(), START, _OnlyCallAt()],
    ids=["none", "object", "int", "call-at-only"],
)
def test_a_scheduler_view_wraps_exactly_one_scheduler(not_a_scheduler: Any) -> None:
    with pytest.raises(ContractValidationError):
        SchedulerView(not_a_scheduler)
    fake, paper = _paper_clock()
    for wrong in (fake.clock(), paper, ClockView(paper)):
        with pytest.raises(ContractValidationError):
            SchedulerView(wrong)
    replay = ReplayClock(START)
    with pytest.raises(TypeError):
        SchedulerView()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SchedulerView(replay, replay)  # type: ignore[call-arg]
    nested = SchedulerView(SchedulerView(replay))
    fired: list[int] = []
    nested.call_after(5, fired.append)
    replay.advance_by(5)
    assert fired == [START + 5]
    assert _public(nested) == {"call_at", "call_after", "cancel"}


# =========================================================================== both schedulers: shared rules


class _ReplayRun:
    """SYNTHETIC FIXTURE replay driver: virtual time; timers fire while the clock is advanced."""

    def __init__(self) -> None:
        self.clock: Clock = ReplayClock(START)
        self._replay: ReplayClock = self.clock  # type: ignore[assignment]
        self.scheduler: Scheduler = self._replay

    def advance(self, delta_ns: int) -> None:
        self._replay.advance_to(self._replay.now_ns() + delta_ns)

    def state(self) -> tuple[int, int | None]:
        return self._replay.pending_timers(), self._replay.next_timer_ns()


class _PaperRun:
    """SYNTHETIC FIXTURE paper loop: real time passes, the loop polls, then begins the next step."""

    def __init__(self) -> None:
        self.fake = _FakeTime()
        self.paper = PaperClock(self.fake.clock())
        self.clock: Clock = self.paper
        self.polling = PollingScheduler(self.paper)
        self.scheduler: Scheduler = self.polling

    def advance(self, delta_ns: int) -> None:
        target_ns = self.paper.now_ns() + delta_ns
        self.fake.pass_time(delta_ns)
        self.polling.run_due()
        self.paper.begin_step(target_ns)

    def state(self) -> tuple[int, int | None]:
        return self.polling.pending_timers(), self.polling.next_timer_ns()


_Run = _ReplayRun | _PaperRun
_BOTH_RUNS = pytest.mark.parametrize("make_run", [_ReplayRun, _PaperRun], ids=["replay", "paper"])


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
def test_cancel_a_pending_timer_and_it_never_fires(make_run: Callable[[], _Run]) -> None:
    run = make_run()
    trace = _Trace(run.clock)
    run.scheduler.call_after(10, trace.timer("keep-1"))
    doomed = run.scheduler.call_after(20, trace.timer("doomed"))
    same_instant = [run.scheduler.call_after(30, trace.timer(label)) for label in ("a", "b", "c")]
    assert run.state() == (5, START + 10)
    run.scheduler.cancel(doomed)
    run.scheduler.cancel(doomed)  # cancelling twice is harmless and does not count twice
    run.scheduler.cancel(same_instant[1])
    assert run.state() == (3, START + 10)
    run.advance(100)
    assert trace.labels == ["keep-1", "a", "c"]  # the order of the others is kept
    assert run.state() == (0, None)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
def test_cancel_ignores_fired_and_never_issued_handles(make_run: Callable[[], _Run]) -> None:
    run = make_run()
    trace = _Trace(run.clock)
    for never_issued in range(-5, 50):  # nothing has been issued yet
        run.scheduler.cancel(never_issued)
    fired_handle = run.scheduler.call_after(10, trace.timer("first"))
    run.advance(10)
    later_handles = [run.scheduler.call_after(10 + index, trace.timer(f"t{index}")) for index in range(5)]
    run.scheduler.cancel(fired_handle)
    run.scheduler.cancel(fired_handle)
    for unknown in range(-5, 200):
        if unknown not in later_handles:
            run.scheduler.cancel(unknown)
    # Neither the early cancels nor the fired handle killed a timer that was registered afterwards.
    assert fired_handle not in later_handles
    assert run.state() == (5, START + 20)
    run.advance(100)
    assert trace.labels == ["first", "t0", "t1", "t2", "t3", "t4"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
def test_a_callback_can_cancel_timers_due_later_and_its_own_handle(make_run: Callable[[], _Run]) -> None:
    run = make_run()
    trace = _Trace(run.clock)
    handles: dict[str, int] = {}

    def canceller(due_ns: int) -> None:
        trace.timer("canceller")(due_ns)
        for name in ("canceller", "same-instant", "later"):
            run.scheduler.cancel(handles[name])

    handles["canceller"] = run.scheduler.call_after(10, canceller)
    handles["same-instant"] = run.scheduler.call_after(10, trace.timer("same-instant"))
    handles["later"] = run.scheduler.call_after(20, trace.timer("later"))
    run.scheduler.call_after(30, trace.timer("survivor"))
    run.advance(40)
    assert trace.labels == ["canceller", "survivor"]
    assert run.state() == (0, None)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
def test_timer_handles_are_unique_for_both_schedulers(make_run: Callable[[], _Run]) -> None:
    run = make_run()
    handles: list[int] = []
    for round_number in range(20):
        fired = run.scheduler.call_after(1, lambda due_ns: None)
        cancelled = run.scheduler.call_after(1, lambda due_ns: None)
        kept = run.scheduler.call_at(START + NS_PER_SECOND + round_number, lambda due_ns: None)
        handles += [fired, cancelled, kept]
        run.scheduler.cancel(cancelled)
        run.advance(1)
    assert len(set(handles)) == len(handles) == 60
    assert run.state() == (20, START + NS_PER_SECOND)


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
@pytest.mark.parametrize("delay_ns", [-1, -NS_PER_MS, -START, -(2**70)])
def test_a_negative_delay_is_refused_alike_by_both_schedulers(
    make_run: Callable[[], _Run], delay_ns: int
) -> None:
    run = make_run()
    run.advance(100)
    fired: list[int] = []
    with pytest.raises(ClockRegressionError):
        run.scheduler.call_after(delay_ns, fired.append)
    assert run.state() == (0, None)
    run.advance(1_000)
    assert fired == []


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
@_BOTH_RUNS
@pytest.mark.parametrize("before_by", [1, 100])
def test_a_due_time_before_the_current_instant_is_refused_alike_by_both_schedulers(
    make_run: Callable[[], _Run], before_by: int
) -> None:
    run = make_run()
    run.advance(100)
    fired: list[int] = []
    with pytest.raises(ClockRegressionError):
        run.scheduler.call_at(START + 100 - before_by, fired.append)
    assert run.state() == (0, None)
    run.scheduler.call_at(START + 100, fired.append)  # the current instant is allowed
    run.advance(0)
    assert fired == [START + 100]


# =========================================================================== replay / paper equivalence

_SCHEDULE_OPERATIONS = st.one_of(
    _OPERATIONS,
    st.tuples(st.just("call_after"), st.integers(0, 40), st.lists(st.integers(0, 25), max_size=3).map(tuple)),
)
_SCHEDULE_SCRIPTS = st.lists(_SCHEDULE_OPERATIONS, max_size=40)


def _drive_schedule(run: _Run, script: _Script) -> list[tuple[Any, ...]]:
    """Run a schedule script (the operations of ``_drive`` plus ``call_after``) on *run*.

    Timers scheduled from a callback use the absolute instant ``due + delay`` (never before the
    step instant, which is the due time).  Returns everything that must be the same in replay and
    paper: which timer fired with which due time at which step instant, in which order, and the
    pending timers after every operation.
    """
    trace: list[tuple[Any, ...]] = []
    handles: list[int] = []

    def make_callback(label: tuple[int, ...], child_delays: tuple[int, ...]) -> Callable[[int], None]:
        def callback(due_ns: int) -> None:
            assert run.clock.now_ns() == due_ns, "step time inside a timer is its due time"
            trace.append(("fired", label, due_ns - START, run.clock.now_ns() - START))
            for position, delay in enumerate(child_delays):
                handles.append(run.scheduler.call_at(due_ns + delay, make_callback((*label, position), ())))

        return callback

    for index, operation in enumerate(script):
        kind, amount = operation[0], operation[1]
        if kind == "call_at":
            due_ns = run.clock.now_ns() + amount
            handles.append(run.scheduler.call_at(due_ns, make_callback((index,), operation[2])))
        elif kind == "call_after":
            handles.append(run.scheduler.call_after(amount, make_callback((index,), operation[2])))
        elif kind == "cancel":
            if handles:
                run.scheduler.cancel(handles[amount % len(handles)])
        elif kind == "cancel_unknown":
            never_issued = amount
            while never_issued in handles:
                never_issued += 1
            run.scheduler.cancel(never_issued)
        else:
            run.advance(amount)
            trace.append(("advanced", run.clock.now_ns() - START))
        trace.append(("state", *run.state()))
    return trace


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_example_schedule_gives_the_hand_derived_callback_order_in_replay_and_in_paper() -> None:
    # Guards the equivalence property below against being vacuous (see the hand derivation at
    # test_the_example_script_exercises_nested_scheduling_and_cancellation).
    expected_fired = [
        ((0,), 5, 5),
        ((1,), 5, 5),
        ((0, 0), 5, 5),
        ((3,), 8, 8),
        ((0, 1), 8, 8),
        ((3, 0), 8, 8),
        ((0, 2), 35, 35),
    ]
    for make_run in (_ReplayRun, _PaperRun):
        trace = _drive_schedule(make_run(), _EXAMPLE_SCRIPT)
        assert [entry[1:] for entry in trace if entry[0] == "fired"] == expected_fired
        assert trace[-2:] == [("advanced", 60), ("state", 0, None)]


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(script=_SCHEDULE_SCRIPTS)
@example(script=_EXAMPLE_SCRIPT)
@example(
    script=[("call_after", 3, (0, 2)), ("call_at", 3, ()), ("advance", 0), ("advance", 3), ("advance", 2)]
)
def test_the_same_schedule_fires_the_same_callbacks_in_the_same_order_in_replay_and_in_paper(
    script: _Script,
) -> None:
    replay = _drive_schedule(_ReplayRun(), script)
    paper = _drive_schedule(_PaperRun(), script)
    assert replay == paper
    # ... and both are the documented (due time, registration order) semantics of the reference model.
    reference_script = [
        ("call_at", *operation[1:]) if operation[0] == "call_after" else operation for operation in script
    ]
    reference = [
        ("fired", entry[1], entry[2] - START, entry[3] - START) if entry[0] == "fired" else entry
        for entry in _reference(START, reference_script)
    ]
    reference = [("advanced", entry[1] - START) if entry[0] == "advanced" else entry for entry in reference]
    reference = [("state", entry[3], entry[4]) if entry[0] == "state" else entry for entry in reference]
    assert replay == reference


# One input of a run loop: (time since the previous input, delays of the timers its handler schedules).
_INPUTS = st.lists(
    st.tuples(st.integers(0, 30), st.lists(st.integers(0, 40), max_size=3).map(tuple)), max_size=25
)
_INITIAL_TIMERS = st.lists(st.integers(0, 120), max_size=8)


def _run_loop(
    run: _Run, initial_timers: Sequence[int], inputs: _Script, lag_ns: int
) -> tuple[list[tuple[Any, ...]], list[tuple[int, int, int]]]:
    """One documented run loop: fire the timers due up to the next input, then deliver the input.

    Replay advances the virtual clock to the input's availability instant.  Paper gets to the
    input *lag_ns* after it was received, calls ``run_due(limit_ns=<receive time>)`` first and then
    begins the input's step.  Returns the log of fired timers and delivered inputs (with the step
    instant each saw), and for every timer ``(label, due offset, length of the log when it was
    registered)``.
    """
    log: list[tuple[Any, ...]] = []
    registrations: list[tuple[int, int, int]] = []

    def schedule(due_ns: int) -> None:
        label = len(registrations)
        registrations.append((label, due_ns - START, len(log)))

        def callback(fired_due_ns: int) -> None:
            assert fired_due_ns == due_ns
            assert run.clock.now_ns() == due_ns, "step time inside a timer is its due time"
            log.append(("timer", label, due_ns - START))

        run.scheduler.call_at(due_ns, callback)

    def fire_timers_up_to(available_ns: int) -> None:
        if isinstance(run, _ReplayRun):
            run.advance(available_ns - run.clock.now_ns())
        else:
            run.fake.pass_time(max(available_ns + lag_ns - run.fake.wall_ns, 0))
            run.polling.run_due(limit_ns=available_ns)
            run.paper.begin_step(available_ns)

    for delay in initial_timers:
        schedule(START + delay)
    available_ns = START
    for gap, follow_ups in inputs:
        available_ns += gap
        fire_timers_up_to(available_ns)
        assert run.clock.now_ns() == available_ns, "step time of an input is its availability instant"
        log.append(("input", available_ns - START))
        for delay in follow_ups:
            schedule(available_ns + delay)
    return log, registrations


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(initial_timers=_INITIAL_TIMERS, inputs=_INPUTS, lag_ns=st.sampled_from([0, 1, 13, 10_000]))
@example(initial_timers=[10, 10, 25], inputs=[(10, (0, 5)), (0, ()), (5, (10,)), (10, ())], lag_ns=7)
def test_timers_and_inputs_interleave_identically_in_a_replay_loop_and_a_late_paper_loop(
    initial_timers: list[int], inputs: _Script, lag_ns: int
) -> None:
    replay, registrations = _run_loop(_ReplayRun(), initial_timers, inputs, 0)
    paper, paper_registrations = _run_loop(_PaperRun(), initial_timers, inputs, lag_ns)
    assert replay == paper
    assert registrations == paper_registrations
    # The timer rule itself, checked on the log.
    fired_at = {entry[1]: index for index, entry in enumerate(replay) if entry[0] == "timer"}
    for index, entry in enumerate(replay):
        if entry[0] == "timer":
            # Never early: every input delivered before the timer became available before its instant
            # or - for a timer an input scheduled for its own instant - at that instant.
            assert all(item[1] <= entry[2] for item in replay[:index] if item[0] == "input")
        else:
            # A timer that existed when the input was delivered and was due at or before the
            # input's availability instant has fired before the input.
            for label, due_offset, registered_at in registrations:
                if registered_at <= index and due_offset <= entry[1]:
                    assert fired_at[label] < index
            # ... and no timer that is due later has fired yet.
            assert all(item[2] <= entry[1] for item in replay[:index] if item[0] == "timer")


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_example_run_loop_gives_the_hand_derived_interleaving() -> None:
    # Timers 0 and 1 are due at +10, timer 2 at +25.  Input at +10 schedules timers 3 (+10) and
    # 4 (+15); a second input at +10 follows; the input at +15 schedules timer 5 (+25); input at +25.
    expected = [
        ("timer", 0, 10),
        ("timer", 1, 10),
        ("input", 10),  # timers due at +10 fired before the input available at +10
        ("timer", 3, 10),  # scheduled by that input for its own instant: before the next input at +10
        ("input", 10),
        ("timer", 4, 15),
        ("input", 15),
        ("timer", 2, 25),
        ("timer", 5, 25),  # same instant as timer 2, registered later
        ("input", 25),
    ]
    initial_timers = [10, 10, 25]
    inputs: _Script = [(10, (0, 5)), (0, ()), (5, (10,)), (10, ())]
    assert _run_loop(_ReplayRun(), initial_timers, inputs, 0)[0] == expected
    for lag_ns in (0, 7, 10_000):
        assert _run_loop(_PaperRun(), initial_timers, inputs, lag_ns)[0] == expected


# =========================================================================== component conformance


class _Component:
    """SYNTHETIC FIXTURE component: what a subsystem does with the views it is handed.

    On every input it reads step time and schedules a follow-up timer; the follow-up reads step
    time again and re-arms a heartbeat.  It records every instant it saw.  It holds only the views.
    """

    def __init__(self, clock: ClockView, scheduler: SchedulerView, follow_up_ns: int) -> None:
        self._clock = clock
        self._scheduler = scheduler
        self._follow_up_ns = follow_up_ns
        self._heartbeats = 0
        self.seen: list[tuple[Any, ...]] = []

    def on_input(self, label: int) -> None:
        for name in _RUN_LOOP_ONLY:
            assert not hasattr(self._clock, name)
            assert not hasattr(self._scheduler, name)
        now_ns = self._clock.now_ns()
        self.seen.append(("input", label, now_ns - START))
        self._scheduler.call_after(self._follow_up_ns, functools.partial(self._follow_up, label, now_ns))

    def _follow_up(self, label: int, scheduled_at_ns: int, due_ns: int) -> None:
        now_ns = self._clock.now_ns()
        self.seen.append(("follow-up", label, scheduled_at_ns - START, due_ns - START, now_ns - START))
        if self._heartbeats < 3:
            self._heartbeats += 1
            self._scheduler.call_after(7, self._heartbeat)

    def _heartbeat(self, due_ns: int) -> None:
        self.seen.append(("heartbeat", due_ns - START, self._clock.now_ns() - START))


def _run_component_in_replay(gaps: Sequence[int], follow_up_ns: int) -> list[tuple[Any, ...]]:
    clock = ReplayClock(START)
    component = _Component(ClockView(clock), SchedulerView(clock), follow_up_ns)
    available_ns = START
    for label, gap in enumerate(gaps):
        available_ns += gap
        clock.advance_to(available_ns)
        component.on_input(label)
    clock.advance_to(available_ns + 1_000)
    return component.seen


def _run_component_in_paper(gaps: Sequence[int], follow_up_ns: int, lag_ns: int) -> list[tuple[Any, ...]]:
    fake, clock = _paper_clock()
    scheduler = PollingScheduler(clock)
    component = _Component(ClockView(clock), SchedulerView(scheduler), follow_up_ns)

    def step_to(available_ns: int) -> None:
        fake.pass_time(max(available_ns + lag_ns - fake.wall_ns, 0))  # the input arrives, late
        scheduler.run_due(limit_ns=available_ns)
        clock.begin_step(available_ns)

    available_ns = START
    for label, gap in enumerate(gaps):
        available_ns += gap
        step_to(available_ns)
        component.on_input(label)
    step_to(available_ns + 1_000)
    return component.seen


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
def test_the_example_component_observes_the_hand_derived_instants_in_replay_and_in_paper() -> None:
    # Inputs at +0, +5 and +20; follow-ups 10 later; the first three follow-ups re-arm a heartbeat 7 later.
    expected = [
        ("input", 0, 0),
        ("input", 1, 5),
        ("follow-up", 0, 0, 10, 10),
        ("follow-up", 1, 5, 15, 15),
        ("heartbeat", 17, 17),
        ("input", 2, 20),
        ("heartbeat", 22, 22),
        ("follow-up", 2, 20, 30, 30),
        ("heartbeat", 37, 37),
    ]
    assert _run_component_in_replay([0, 5, 15], 10) == expected
    for lag_ns in (0, 3, 10_000):
        assert _run_component_in_paper([0, 5, 15], 10, lag_ns) == expected


@pytest.mark.requirement("S00-ASG-19", "G03-03")
@pytest.mark.acceptance("S00-ACC-03")
@_PROPERTY
@given(
    gaps=st.lists(st.integers(0, 40), max_size=20),
    follow_up_ns=st.integers(0, 30),
    lag_ns=st.sampled_from([0, 1, 9, 10_000]),
)
def test_the_same_component_observes_identical_instants_under_replay_and_under_paper(
    gaps: list[int], follow_up_ns: int, lag_ns: int
) -> None:
    replay = _run_component_in_replay(gaps, follow_up_ns)
    paper = _run_component_in_paper(gaps, follow_up_ns, lag_ns)
    assert replay == paper
    assert len([entry for entry in replay if entry[0] == "input"]) == len(gaps)
    for entry in replay:
        if entry[0] == "follow-up":
            assert entry[3] == entry[4] == entry[2] + follow_up_ns  # fired exactly at its instant
        elif entry[0] == "heartbeat":
            assert entry[1] == entry[2]
