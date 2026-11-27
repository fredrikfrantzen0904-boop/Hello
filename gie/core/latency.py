"""Latency measurement (requirements G03-10 "measure p50/p95/p99/max", G03-11).

Durations are measured with ``time.perf_counter_ns`` - never with the injected
clock, whose time is virtual in replay.  Percentiles use the *nearest-rank*
definition on the retained samples, so a reported percentile is always a value
that was actually observed.

The recorder keeps the most recent ``capacity`` samples.  ``count``, ``min``,
``max`` and ``total`` are exact over *all* samples ever recorded; percentiles
are computed over the retained window and ``truncated`` says when the two
differ.  Nothing here promises a latency: it only reports what was measured.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fractions import Fraction

from gie.core.errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class LatencySummary:
    name: str
    count: int
    retained: int
    truncated: bool
    min_ns: int | None
    mean_ns: float | None
    p50_ns: int | None
    p95_ns: int | None
    p99_ns: int | None
    max_ns: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def nearest_rank(sorted_samples: list[int], percentile: float) -> int:
    """Nearest-rank percentile of an ascending, non-empty list (0 < percentile <= 100)."""
    if not sorted_samples:
        raise ContractValidationError("percentile of an empty sample is undefined")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ContractValidationError("percentile must be a number", actual_type=type(percentile).__name__)
    if not 0 < percentile <= 100:  # also refuses NaN
        raise ContractValidationError("percentile must be in (0, 100]", value=percentile)
    # Exact rational arithmetic: binary floating point would turn 7 % of 100 samples into rank 8.
    rank = math.ceil(Fraction(str(percentile)) * len(sorted_samples) / 100)
    return sorted_samples[max(rank, 1) - 1]


class LatencyRecorder:
    """Collects durations in nanoseconds for one named measurement point."""

    def __init__(self, name: str, capacity: int = 1_000_000) -> None:
        if capacity < 1:
            raise ContractValidationError("capacity must be >= 1", value=capacity)
        self.name = name
        self._capacity = capacity
        self._samples: list[int] = []
        self._next = 0
        self._count = 0
        self._total = 0
        self._min: int | None = None
        self._max: int | None = None

    def record(self, duration_ns: int) -> None:
        if isinstance(duration_ns, bool) or not isinstance(duration_ns, int):
            raise ContractValidationError(
                "duration must be an int of nanoseconds", actual_type=type(duration_ns).__name__
            )
        if duration_ns < 0:
            raise ContractValidationError("duration must be >= 0", value=duration_ns)
        self._count += 1
        self._total += duration_ns
        if self._min is None or duration_ns < self._min:
            self._min = duration_ns
        if self._max is None or duration_ns > self._max:
            self._max = duration_ns
        if len(self._samples) < self._capacity:
            self._samples.append(duration_ns)
        else:
            self._samples[self._next] = duration_ns
            self._next = (self._next + 1) % self._capacity

    @contextmanager
    def measure(self) -> Iterator[None]:
        """Record the wall duration of the ``with`` block (also when it raises)."""
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            self.record(time.perf_counter_ns() - started)

    @property
    def count(self) -> int:
        return self._count

    def summary(self) -> LatencySummary:
        ordered = sorted(self._samples)
        have = bool(ordered)
        return LatencySummary(
            name=self.name,
            count=self._count,
            retained=len(ordered),
            truncated=self._count > len(ordered),
            min_ns=self._min,
            mean_ns=(self._total / self._count) if self._count else None,
            p50_ns=nearest_rank(ordered, 50) if have else None,
            p95_ns=nearest_rank(ordered, 95) if have else None,
            p99_ns=nearest_rank(ordered, 99) if have else None,
            max_ns=self._max,
        )
