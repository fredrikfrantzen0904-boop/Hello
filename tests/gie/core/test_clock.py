"""Independent tests for ``gie.core.clock``.

Covers requirement S00-ASG-19 ("Clock") and acceptance test S00-ACC-03 ("Replay clock").

The expected behaviour is taken from the docstrings of ``gie/core/clock.py`` and from
``specifications/SHARED_CONTRACTS.md`` section 3.2 (revision 2), not from the implementation:

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
* a ``PollingScheduler`` is the scheduler of a paper run: timers fire only when ``run_due``
  is called, in ``(due time, registration order)``, never early; a due time in the past is not
  an error; the same schedule gives the same callback order as on a ``ReplayClock``.

SYNTHETIC FIXTURE: every instant, schedule and clock reading in this file is invented for
testing.  Nothing here is market data, broker data or a measurement.
"""

from __future__ import annotations

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

from gie.core.clock import Clock, ClockMode, PollingScheduler, ReplayClock, Scheduler, SystemClock
from gie.core.errors import ClockRegressionError, ContractValidationError, GieError
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


