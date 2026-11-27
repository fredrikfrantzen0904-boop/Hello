"""Independent tests for ``gie.core.latency`` (requirements G03-10 and G03-11).

The expected behaviour is taken from the docstrings of ``gie/core/latency.py`` and from
``specifications/SHARED_CONTRACTS.md`` section 3.5, not from the implementation:

* percentiles use the *nearest-rank* definition - the value at rank ``ceil(p / 100 * n)`` of
  the ascending samples - so a reported percentile is always a value that was observed;
* ``count``, ``min``, ``max`` and the mean are exact over *all* samples ever recorded, the
  percentiles are computed over the most recent ``capacity`` samples and ``truncated`` says
  when the two differ;
* ``measure()`` records the duration of its block with ``time.perf_counter_ns``, also when
  the block raises.

SYNTHETIC FIXTURE: every duration in this file is an invented number.  Nothing here is a
measurement of GIE or evidence of any latency.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from gie.core.errors import ContractValidationError, GieError
from gie.core.latency import LatencyRecorder, LatencySummary, nearest_rank

#: Deterministic hypothesis runs: no random seed, no example database, no wall-clock deadline.
_PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=250)

_ONE_TO_HUNDRED = list(range(1, 101))
_ONE_TO_THOUSAND = list(range(1, 1001))
_FIVE = [15, 20, 35, 40, 50]

_SUMMARY_FIELDS = {
    "name",
    "count",
    "retained",
    "truncated",
    "min_ns",
    "mean_ns",
    "p50_ns",
    "p95_ns",
    "p99_ns",
    "max_ns",
}


class _Boom(Exception):
    """SYNTHETIC FIXTURE failure raised on purpose inside a measured block."""


class _FakeCounter:
    """SYNTHETIC FIXTURE stand-in for ``time.perf_counter_ns``: scripted readings, last one repeats."""

    def __init__(self, readings: Sequence[int]) -> None:
        self._readings = list(readings)
        self.calls = 0

    def __call__(self) -> int:
        value = self._readings[min(self.calls, len(self._readings) - 1)]
        self.calls += 1
        return value


def _exact_rank(percentile: int, size: int) -> int:
    """``ceil(percentile / 100 * size)`` in exact integer arithmetic (1-based rank)."""
    return -(-percentile * size // 100)


def _expected_summary(name: str, samples: Sequence[int], capacity: int) -> LatencySummary:
    """What the documentation promises for *samples* recorded in order with the given capacity."""
    window = sorted(samples[-capacity:])
    return LatencySummary(
        name=name,
        count=len(samples),
        retained=len(window),
        truncated=len(samples) > len(window),
        min_ns=min(samples),
        mean_ns=sum(samples) / len(samples),
        p50_ns=window[_exact_rank(50, len(window)) - 1],
        p95_ns=window[_exact_rank(95, len(window)) - 1],
        p99_ns=window[_exact_rank(99, len(window)) - 1],
        max_ns=max(samples),
    )


def _recorded(samples: Sequence[int], capacity: int | None = None, name: str = "fixture") -> LatencyRecorder:
    recorder = LatencyRecorder(name) if capacity is None else LatencyRecorder(name, capacity=capacity)
    for sample in samples:
        recorder.record(sample)
    return recorder


# =========================================================================== nearest_rank


@pytest.mark.requirement("G03-10")
@pytest.mark.parametrize(
    ("samples", "percentile", "expected"),
    [
        # n = 1: every percentile is the only sample.
        ([7], 0.001, 7),
        ([7], 1, 7),
        ([7], 50, 7),
        ([7], 99.999, 7),
        ([7], 100, 7),
        # n = 2: rank = ceil(p / 50).
        ([10, 20], 1, 10),
        ([10, 20], 50, 10),
        ([10, 20], 50.01, 20),
        ([10, 20], 100, 20),
        # n = 4: rank = ceil(p / 25).
        ([1, 2, 3, 4], 25, 1),
        ([1, 2, 3, 4], 26, 2),
        ([1, 2, 3, 4], 50, 2),
        ([1, 2, 3, 4], 51, 3),
        ([1, 2, 3, 4], 75, 3),
        ([1, 2, 3, 4], 76, 4),
        ([1, 2, 3, 4], 100, 4),
        # n = 5, the classic textbook example: rank = ceil(p / 20).
        (_FIVE, 5, 15),
        (_FIVE, 20, 15),
        (_FIVE, 30, 20),
        (_FIVE, 40, 20),
        (_FIVE, 50, 35),
        (_FIVE, 60, 35),
        (_FIVE, 80, 40),
        (_FIVE, 81, 50),
        (_FIVE, 100, 50),
        # Repeated values.
        ([5, 5, 5, 9], 75, 5),
        ([5, 5, 5, 9], 76, 9),
        # n = 100 with the samples 1..100: rank = ceil(p), and the value equals its rank.
        (_ONE_TO_HUNDRED, 0.5, 1),
        (_ONE_TO_HUNDRED, 1, 1),
        (_ONE_TO_HUNDRED, 50, 50),
        (_ONE_TO_HUNDRED, 50.5, 51),
        (_ONE_TO_HUNDRED, 95, 95),
        (_ONE_TO_HUNDRED, 99, 99),
        (_ONE_TO_HUNDRED, 99.5, 100),
        (_ONE_TO_HUNDRED, 100, 100),
        # n = 1000 with the samples 1..1000: rank = ceil(10 * p).
        (_ONE_TO_THOUSAND, 0.1, 1),
        (_ONE_TO_THOUSAND, 0.11, 2),
        (_ONE_TO_THOUSAND, 50, 500),
        (_ONE_TO_THOUSAND, 99, 990),
        (_ONE_TO_THOUSAND, 99.9, 999),
        (_ONE_TO_THOUSAND, 99.91, 1000),
        (_ONE_TO_THOUSAND, 100, 1000),
    ],
)
def test_nearest_rank_matches_hand_computed_examples(
    samples: list[int], percentile: float, expected: int
) -> None:
    assert nearest_rank(samples, percentile) == expected


@pytest.mark.requirement("G03-10")
def test_whole_percentiles_use_the_exact_nearest_rank() -> None:
    # With the samples 1..n the value at a rank is the rank itself, so the p-th percentile must be
    # ceil(p * n / 100) - computed here in exact integer arithmetic.  For n = 100 that is simply p.
    wrong: list[tuple[int, int, int, int]] = []
    for size in (1, 2, 3, 7, 10, 20, 25, 50, 64, 75, 100, 200, 1000):
        samples = list(range(1, size + 1))
        for percentile in range(1, 101):
            reported = nearest_rank(samples, percentile)
            if reported != _exact_rank(percentile, size):
                wrong.append((size, percentile, reported, _exact_rank(percentile, size)))
    assert wrong == [], f"(n, percentile, reported, nearest rank): {wrong[:12]}"


@pytest.mark.requirement("G03-10")
def test_percentiles_with_one_decimal_use_the_exact_nearest_rank() -> None:
    # A percentile written as k / 10 (0.1, 0.2, ..., 99.9, 100.0) means exactly that decimal number:
    # with the samples 1..n the result must be ceil(k * n / 1000), e.g. p99.9 of 1000 samples is the
    # 999th sample, not the 1000th.
    wrong: list[tuple[int, float, int, int]] = []
    for size in (1, 10, 100, 250, 1000, 2000):
        samples = list(range(1, size + 1))
        for tenths in range(1, 1001):
            reported = nearest_rank(samples, tenths / 10)
            expected = -(-tenths * size // 1000)
            if reported != expected:
                wrong.append((size, tenths / 10, reported, expected))
    assert wrong == [], f"(n, percentile, reported, nearest rank): {wrong[:12]}"


@pytest.mark.requirement("G03-10")
@pytest.mark.parametrize("percentile", [True, False, "50", b"50", None, [50], (50,), {"p": 50}])
def test_nearest_rank_rejects_percentiles_that_are_not_numbers(percentile: Any) -> None:
    with pytest.raises(ContractValidationError):
        nearest_rank(_ONE_TO_HUNDRED, percentile)


@pytest.mark.requirement("G03-10")
@pytest.mark.parametrize("percentile", [0, 0.0, -0.0, -1, -50.5, 100.000001, 101, 1000])
def test_nearest_rank_rejects_percentiles_outside_zero_exclusive_to_hundred_inclusive(
    percentile: float,
) -> None:
    with pytest.raises(ContractValidationError):
        nearest_rank(_ONE_TO_HUNDRED, percentile)
    with pytest.raises(ContractValidationError):
        nearest_rank([7], percentile)


@pytest.mark.requirement("G03-10")
@pytest.mark.parametrize("percentile", [float("nan"), float("inf"), float("-inf")])
def test_nearest_rank_rejects_non_finite_percentiles(percentile: float) -> None:
    with pytest.raises(ContractValidationError):
        nearest_rank(_ONE_TO_HUNDRED, percentile)


@pytest.mark.requirement("G03-10")
@pytest.mark.parametrize("percentile", [50, 100, 0.001, 0, 150])
def test_nearest_rank_of_an_empty_sample_is_an_error(percentile: float) -> None:
    with pytest.raises(ContractValidationError):
        nearest_rank([], percentile)


@pytest.mark.requirement("G03-10")
def test_nearest_rank_accepts_the_whole_documented_percentile_range() -> None:
    assert nearest_rank(_ONE_TO_HUNDRED, 1e-9) == 1  # just above 0: the smallest sample
    assert nearest_rank(_ONE_TO_HUNDRED, 100) == 100  # 100 is inclusive: the largest sample
    assert nearest_rank(_ONE_TO_HUNDRED, 100.0) == 100


@pytest.mark.requirement("G03-10")
def test_nearest_rank_does_not_modify_its_input() -> None:
    samples = [3, 3, 8, 21, 21, 40]
    snapshot = list(samples)
    for percentile in (1, 50, 95, 99, 100):
        nearest_rank(samples, percentile)
    assert samples == snapshot


@pytest.mark.requirement("G03-10")
@_PROPERTY
@given(
    samples=st.lists(st.integers(0, 10**12), min_size=1, max_size=80).map(sorted),
    low=st.floats(min_value=1e-6, max_value=100, allow_nan=False),
    high=st.floats(min_value=1e-6, max_value=100, allow_nan=False),
)
def test_nearest_rank_is_an_observed_value_and_monotone_in_the_percentile(
    samples: list[int], low: float, high: float
) -> None:
    low, high = min(low, high), max(low, high)
    at_low, at_high = nearest_rank(samples, low), nearest_rank(samples, high)
    assert at_low in samples
    assert at_high in samples
    assert samples[0] <= at_low <= at_high <= samples[-1]
    assert nearest_rank(samples, 100) == samples[-1]


@pytest.mark.requirement("G03-10")
@_PROPERTY
@given(
    samples=st.lists(st.integers(0, 10**12), min_size=1, max_size=80).map(sorted),
    eighths=st.integers(1, 8),
)
def test_nearest_rank_is_exact_for_percentiles_that_are_binary_fractions(
    samples: list[int], eighths: int
) -> None:
    # 12.5, 25, ..., 100 percent: p / 100 is exactly eighths / 8, so no rounding is involved and the
    # rank is the smallest r with r / n >= eighths / 8.
    rank = -(-eighths * len(samples) // 8)
    assert nearest_rank(samples, eighths * 12.5) == samples[rank - 1]


# =========================================================================== LatencyRecorder: statistics


@pytest.mark.requirement("G03-10", "G03-11")
def test_recorder_reports_the_hand_computed_statistics_of_one_to_hundred() -> None:
    recorder = LatencyRecorder("tick_to_decision")
    for index in range(100):
        recorder.record(index * 37 % 100 + 1)  # the numbers 1..100 in a scrambled order
    assert recorder.count == 100
    assert recorder.summary() == LatencySummary(
        name="tick_to_decision",
        count=100,
        retained=100,
        truncated=False,
        min_ns=1,
        mean_ns=50.5,
        p50_ns=50,
        p95_ns=95,
        p99_ns=99,
        max_ns=100,
    )


@pytest.mark.requirement("G03-10", "G03-11")
def test_recorder_statistics_for_a_small_hand_computed_sample() -> None:
    summary = _recorded([40, 15, 50, 20, 35], name="protection").summary()
    assert summary.name == "protection"
    assert (summary.count, summary.retained, summary.truncated) == (5, 5, False)
    assert (summary.min_ns, summary.max_ns) == (15, 50)
    assert summary.mean_ns == 32.0
    # Ascending: 15 20 35 40 50.  Ranks: ceil(2.5) = 3, ceil(4.75) = 5, ceil(4.95) = 5.
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (35, 50, 50)


@pytest.mark.requirement("G03-10", "G03-11")
@pytest.mark.parametrize("duration_ns", [0, 1, 999_999, 10**15])
def test_a_single_sample_is_every_statistic(duration_ns: int) -> None:
    summary = _recorded([duration_ns]).summary()
    assert summary == LatencySummary(
        name="fixture",
        count=1,
        retained=1,
        truncated=False,
        min_ns=duration_ns,
        mean_ns=float(duration_ns),
        p50_ns=duration_ns,
        p95_ns=duration_ns,
        p99_ns=duration_ns,
        max_ns=duration_ns,
    )


@pytest.mark.requirement("G03-10", "G03-11")
def test_summary_of_an_empty_recorder_reports_nothing_measured() -> None:
    recorder = LatencyRecorder("never_used")
    assert recorder.count == 0
    assert recorder.summary() == LatencySummary(
        name="never_used",
        count=0,
        retained=0,
        truncated=False,
        min_ns=None,
        mean_ns=None,
        p50_ns=None,
        p95_ns=None,
        p99_ns=None,
        max_ns=None,
    )


@pytest.mark.requirement("G03-10", "G03-11")
def test_count_follows_every_recorded_sample() -> None:
    recorder = LatencyRecorder("fixture")
    for expected, duration_ns in enumerate([5, 0, 5, 123_456], start=1):
        recorder.record(duration_ns)
        assert recorder.count == expected
        assert recorder.summary().count == expected


@pytest.mark.requirement("G03-10", "G03-11")
def test_recorder_percentiles_are_the_exact_nearest_rank_for_every_sample_count() -> None:
    recorder = LatencyRecorder("fixture")
    for size in range(1, 401):
        recorder.record(size)  # samples are 1..size, so the value at a rank is the rank
        summary = recorder.summary()
        reported = (summary.p50_ns, summary.p95_ns, summary.p99_ns)
        assert reported == (_exact_rank(50, size), _exact_rank(95, size), _exact_rank(99, size)), size
        assert (summary.min_ns, summary.max_ns, summary.count) == (1, size, size)
        assert summary.mean_ns == (size + 1) / 2


@pytest.mark.requirement("G03-10", "G03-11")
def test_the_mean_is_computed_from_the_exact_total() -> None:
    # 2**53 + 1 + 1 cannot be accumulated in a binary float without losing the two small samples;
    # the exact total is 2**53 + 2 and the correctly rounded mean differs from the lossy one.
    samples = [2**53, 1, 1]
    summary = _recorded(samples).summary()
    assert summary.mean_ns == (2**53 + 2) / 3
    assert summary.mean_ns != float(2**53) / 3
    assert (summary.min_ns, summary.max_ns, summary.count) == (1, 2**53, 3)


@pytest.mark.requirement("G03-10", "G03-11")
@pytest.mark.parametrize("duration_ns", [-1, -1_000_000, -(2**63)])
def test_a_negative_duration_is_rejected_and_not_recorded(duration_ns: int) -> None:
    empty = LatencyRecorder("fixture")
    with pytest.raises(ContractValidationError):
        empty.record(duration_ns)
    assert empty.count == 0
    assert empty.summary() == LatencyRecorder("fixture").summary()

    recorder = _recorded([10, 20, 30])
    before = recorder.summary()
    with pytest.raises(ContractValidationError) as caught:
        recorder.record(duration_ns)
    assert isinstance(caught.value, GieError)
    assert recorder.count == 3
    assert recorder.summary() == before
    recorder.record(40)  # still usable
    assert recorder.summary().max_ns == 40


@pytest.mark.requirement("G03-10", "G03-11")
@pytest.mark.parametrize(
    "duration_ns",
    [1.5, 1.0, 0.0, float("nan"), float("inf"), float("-inf"), True, False, "5", b"5", None, [5], Decimal(5)],
)
def test_a_duration_that_is_not_an_int_of_nanoseconds_is_rejected_and_not_recorded(duration_ns: Any) -> None:
    # Durations are int nanoseconds; NaN and infinities are invalid everywhere; bool is not an int
    # here.  Accepting such a value would poison min, max and mean for the rest of the run.
    empty = LatencyRecorder("fixture")
    with pytest.raises(ContractValidationError):
        empty.record(duration_ns)
    assert empty.count == 0
    assert empty.summary() == LatencyRecorder("fixture").summary()

    recorder = _recorded([10, 20, 30])
    before = recorder.summary()
    with pytest.raises(ContractValidationError):
        recorder.record(duration_ns)
    assert recorder.count == 3
    assert recorder.summary() == before
    assert json.loads(json.dumps(recorder.summary().to_dict(), allow_nan=False)) == before.to_dict()


@pytest.mark.requirement("G03-10", "G03-11")
def test_summary_is_an_immutable_snapshot() -> None:
    recorder = _recorded([10, 20, 30])
    first = recorder.summary()
    recorder.record(1_000)
    second = recorder.summary()
    assert (first.count, first.max_ns, first.mean_ns) == (3, 30, 20.0)  # the old snapshot did not move
    assert (second.count, second.max_ns, second.mean_ns) == (4, 1_000, 265.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.count = 99  # type: ignore[misc]
    assert recorder.summary() == second  # asking for a summary changes nothing


@pytest.mark.requirement("G03-10", "G03-11")
def test_recorders_are_independent_and_keep_their_names() -> None:
    fast = _recorded([1, 2, 3], name="tick")
    slow = _recorded([1_000_000], name="tactical")
    assert (fast.name, slow.name) == ("tick", "tactical")
    assert (fast.summary().name, slow.summary().name) == ("tick", "tactical")
    assert (fast.count, slow.count) == (3, 1)
    assert fast.summary().max_ns == 3
    assert slow.summary().min_ns == 1_000_000


# =========================================================================== LatencyRecorder: window


@pytest.mark.requirement("G03-10", "G03-11")
def test_truncated_is_set_exactly_when_more_samples_were_recorded_than_are_retained() -> None:
    recorder = LatencyRecorder("fixture", capacity=3)
    observed = []
    for duration_ns in (10, 20, 30, 40, 50):
        recorder.record(duration_ns)
        summary = recorder.summary()
        observed.append((summary.count, summary.retained, summary.truncated))
    assert observed == [(1, 1, False), (2, 2, False), (3, 3, False), (4, 3, True), (5, 3, True)]


@pytest.mark.requirement("G03-10", "G03-11")
def test_the_ring_buffer_evicts_the_oldest_sample_first() -> None:
    recorder = LatencyRecorder("fixture", capacity=3)
    for duration_ns in (100, 200, 300):
        recorder.record(duration_ns)
    summary = recorder.summary()
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (200, 300, 300)

    recorder.record(1)  # evicts 100 -> window 200 300 1
    summary = recorder.summary()
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (200, 300, 300)

    recorder.record(2)  # evicts 200 -> window 300 1 2
    summary = recorder.summary()
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (2, 300, 300)

    recorder.record(3)  # evicts 300 -> window 1 2 3
    summary = recorder.summary()
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (2, 3, 3)
    # min, max, mean and count still cover everything that was ever recorded.
    assert (summary.min_ns, summary.max_ns, summary.count) == (1, 300, 6)
    assert summary.mean_ns == 606 / 6
    assert (summary.retained, summary.truncated) == (3, True)

    recorder.record(50)  # second lap of the ring: evicts 1 -> window 2 3 50
    summary = recorder.summary()
    assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (3, 50, 50)


@pytest.mark.requirement("G03-10", "G03-11")
@pytest.mark.parametrize("capacity", [1, 2, 3, 4, 7, 16])
def test_the_window_is_always_the_most_recent_capacity_samples(capacity: int) -> None:
    # SYNTHETIC FIXTURE durations in a scrambled, repeating order so that eviction order matters.
    samples = [index * 7919 % 101 + index % 3 for index in range(120)]
    recorder = LatencyRecorder("fixture", capacity=capacity)
    for position, duration_ns in enumerate(samples, start=1):
        recorder.record(duration_ns)
        assert recorder.summary() == _expected_summary("fixture", samples[:position], capacity), position


@pytest.mark.requirement("G03-10", "G03-11")
def test_capacity_one_keeps_only_the_latest_sample_for_percentiles() -> None:
    recorder = LatencyRecorder("fixture", capacity=1)
    for duration_ns in (500, 7, 90):
        recorder.record(duration_ns)
        summary = recorder.summary()
        assert (summary.p50_ns, summary.p95_ns, summary.p99_ns) == (duration_ns,) * 3
    assert (summary.min_ns, summary.max_ns, summary.count, summary.retained) == (7, 500, 3, 1)
    assert summary.truncated is True
    assert summary.mean_ns == 199.0


@pytest.mark.requirement("G03-10", "G03-11")
def test_nothing_is_dropped_below_the_capacity() -> None:
    samples = [index * 31 % 5_003 for index in range(5_000)]
    exact = _recorded(samples, capacity=5_000).summary()
    assert (exact.count, exact.retained, exact.truncated) == (5_000, 5_000, False)
    assert exact == _expected_summary("fixture", samples, 5_000)
    # The default capacity is far larger than this sample, so the result is the same.
    assert _recorded(samples).summary() == exact


@pytest.mark.requirement("G03-10", "G03-11")
@pytest.mark.parametrize("capacity", [0, -1, -1_000_000])
def test_capacity_must_be_at_least_one(capacity: int) -> None:
    with pytest.raises(ContractValidationError):
        LatencyRecorder("fixture", capacity=capacity)


@pytest.mark.requirement("G03-10", "G03-11")
@_PROPERTY
@given(
    samples=st.lists(st.integers(0, 2**62), min_size=1, max_size=60),
    capacity=st.integers(1, 80),
)
@example(samples=[9, 9, 9, 1], capacity=3)
@example(samples=[2**62, 0, 2**62 - 1], capacity=2)
def test_summary_matches_the_documented_definition_for_any_samples_and_capacity(
    samples: list[int], capacity: int
) -> None:
    summary = _recorded(samples, capacity=capacity).summary()
    assert summary == _expected_summary("fixture", samples, capacity)
    # A reported percentile is always a value that was actually observed - and still retained.
    window = samples[-capacity:]
    assert summary.p50_ns in window
    assert summary.p95_ns in window
    assert summary.p99_ns in window
    assert summary.min_ns <= summary.p50_ns <= summary.p95_ns <= summary.p99_ns <= summary.max_ns  # type: ignore[operator]


# =========================================================================== LatencyRecorder: measure


@pytest.mark.requirement("G03-10", "G03-11")
def test_measure_records_the_elapsed_perf_counter_time(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LatencyRecorder("stage")
    with monkeypatch.context() as patch:
        patch.setattr(time, "perf_counter_ns", _FakeCounter([1_000, 1_750]))
        with recorder.measure():
            pass
    summary = recorder.summary()
    assert (summary.count, summary.min_ns, summary.max_ns, summary.mean_ns) == (1, 750, 750, 750.0)


@pytest.mark.requirement("G03-10", "G03-11")
def test_measure_records_also_when_the_block_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LatencyRecorder("stage")
    failure = _Boom("synthetic fixture failure")
    with monkeypatch.context() as patch:
        patch.setattr(time, "perf_counter_ns", _FakeCounter([5_000, 5_420]))
        with pytest.raises(_Boom) as caught, recorder.measure():
            raise failure
    assert caught.value is failure  # the exception is neither swallowed nor replaced
    summary = recorder.summary()
    assert (summary.count, summary.min_ns, summary.max_ns) == (1, 420, 420)


@pytest.mark.requirement("G03-10", "G03-11")
def test_every_measured_block_adds_one_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LatencyRecorder("stage")
    with monkeypatch.context() as patch:
        patch.setattr(time, "perf_counter_ns", _FakeCounter([0, 10, 100, 130, 1_000, 1_005]))
        with recorder.measure():
            pass
        with pytest.raises(_Boom), recorder.measure():
            raise _Boom("synthetic fixture failure")
        with recorder.measure():
            pass
    summary = recorder.summary()
    assert (summary.count, summary.min_ns, summary.max_ns, summary.mean_ns) == (3, 5, 30, 15.0)
    assert summary.p50_ns == 10


@pytest.mark.requirement("G03-10", "G03-11")
def test_measured_blocks_can_be_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = LatencyRecorder("stage")
    with monkeypatch.context() as patch:
        # outer starts at 0, inner runs from 10 to 15, outer ends at 30
        patch.setattr(time, "perf_counter_ns", _FakeCounter([0, 10, 15, 30]))
        with recorder.measure(), recorder.measure():
            pass
    summary = recorder.summary()
    assert (summary.count, summary.min_ns, summary.max_ns, summary.mean_ns) == (2, 5, 30, 17.5)


@pytest.mark.requirement("G03-10", "G03-11")
def test_measure_with_the_real_performance_counter_records_one_non_negative_integer() -> None:
    recorder = LatencyRecorder("stage")
    with recorder.measure():
        sum(range(100))
    summary = recorder.summary()
    assert summary.count == 1
    assert type(summary.min_ns) is int
    assert summary.min_ns >= 0
    assert summary.min_ns == summary.max_ns == summary.p50_ns == summary.p99_ns


# =========================================================================== LatencySummary.to_dict


@pytest.mark.requirement("G03-10", "G03-11")
def test_to_dict_is_json_serialisable_and_carries_every_statistic() -> None:
    summary = _recorded([40, 15, 50, 20, 35], name="protection").summary()
    as_dict = summary.to_dict()
    assert as_dict == {
        "name": "protection",
        "count": 5,
        "retained": 5,
        "truncated": False,
        "min_ns": 15,
        "mean_ns": 32.0,
        "p50_ns": 35,
        "p95_ns": 50,
        "p99_ns": 50,
        "max_ns": 50,
    }
    text = json.dumps(as_dict, allow_nan=False, sort_keys=True)
    assert json.loads(text) == as_dict


@pytest.mark.requirement("G03-10", "G03-11")
def test_to_dict_of_an_empty_recorder_uses_null_for_unknown_statistics() -> None:
    as_dict = LatencyRecorder("never_used").summary().to_dict()
    assert set(as_dict) == _SUMMARY_FIELDS
    assert as_dict["count"] == 0
    assert as_dict["truncated"] is False
    unknown = ("min_ns", "mean_ns", "p50_ns", "p95_ns", "p99_ns", "max_ns")
    assert [as_dict[field] for field in unknown] == [None] * 6  # unknown is null, never 0
    text = json.dumps(as_dict, allow_nan=False, sort_keys=True)
    assert json.loads(text) == as_dict
    assert '"min_ns": null' in text


@pytest.mark.requirement("G03-10", "G03-11")
def test_to_dict_of_a_truncated_recorder_is_json_serialisable() -> None:
    summary = _recorded([2**62, 3, 2**61, 9], capacity=2).summary()
    as_dict = summary.to_dict()
    assert set(as_dict) == _SUMMARY_FIELDS
    assert (as_dict["count"], as_dict["retained"], as_dict["truncated"]) == (4, 2, True)
    assert (as_dict["min_ns"], as_dict["max_ns"]) == (3, 2**62)
    # Integers survive JSON exactly, also beyond 2**53.
    assert json.loads(json.dumps(as_dict, allow_nan=False)) == as_dict


@pytest.mark.requirement("G03-10", "G03-11")
def test_to_dict_returns_an_independent_dictionary() -> None:
    summary = _recorded([1, 2, 3]).summary()
    first = summary.to_dict()
    first["count"] = 999
    first["extra"] = "mutated by the caller"
    assert summary.count == 3
    assert summary.to_dict()["count"] == 3
    assert set(summary.to_dict()) == _SUMMARY_FIELDS
