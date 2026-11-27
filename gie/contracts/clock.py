"""Clock contract - the time-source interface and its serialisable reading.

The behavioural contract is :class:`gie.core.clock.Clock` together with
:class:`gie.core.clock.Scheduler` (both re-exported here).
:class:`ClockReading` is the data form used for heartbeats and for the
dashboard's data-freshness display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, final

from gie.contracts._spec import EnumOf, Id, Int, Invariant, TimeNs, TradingDay, fld, inv
from gie.contracts.common import RunMode, Sequenced
from gie.contracts.registry import contract
from gie.core.clock import Clock, ClockMode, PollingScheduler, ReplayClock, Scheduler, SystemClock
from gie.core.timeutil import MAX_TIME_NS, trading_day

__all__ = [
    "Clock",
    "ClockMode",
    "ClockReading",
    "PollingScheduler",
    "ReplayClock",
    "Scheduler",
    "SystemClock",
    "read_clock",
]


@final
@contract("Clock", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class ClockReading(Sequenced):
    """One reading of a run's clock.

    Stream: ``clock/<producer>``.  Ordering time: ``wall_time_ns``.  In REPLAY
    the reading is virtual time; in PAPER it is the UTC system clock.
    """

    run_id: str = fld(Id("run"), "Run the clock belongs to.")
    mode: RunMode = fld(EnumOf(RunMode), "REPLAY or PAPER.")
    clock_mode: ClockMode = fld(EnumOf(ClockMode), "REPLAY (virtual time) or SYSTEM (operating-system time).")
    wall_time_ns: int = fld(TimeNs(), "The instant 'now'.", unit="ns since epoch, UTC")
    monotonic_ns: int = fld(
        Int(ge=0, le=MAX_TIME_NS, label="monotonic reading"),
        "Reading of the monotonic scale (arbitrary origin; only differences are meaningful).",
        unit="ns",
    )
    trading_day: str = fld(TradingDay(), "Europe/Prague calendar date of wall_time_ns.")
    regressions_observed: int = fld(
        Int(ge=0, label="count"),
        "How often the time source was seen to step backwards so far (always 0 in replay).",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "CLK-1",
            "trading_day is the Europe/Prague calendar date of wall_time_ns",
            lambda r: r.trading_day == trading_day(r.wall_time_ns),
        ),
        inv(
            "CLK-2",
            "a REPLAY run uses a REPLAY clock and a PAPER run uses the SYSTEM clock",
            lambda r: (r.mode is RunMode.REPLAY) == (r.clock_mode is ClockMode.REPLAY),
        ),
        inv(
            "CLK-3",
            "a REPLAY clock never observes a regression",
            lambda r: r.clock_mode is not ClockMode.REPLAY or r.regressions_observed == 0,
        ),
    )


def read_clock(clock: Clock, *, run_id: str, mode: RunMode, producer: str, seq: int) -> ClockReading:
    """Take a :class:`ClockReading` from *clock*."""
    now_ns = clock.now_ns()
    return ClockReading(
        producer=producer,
        seq=seq,
        run_id=run_id,
        mode=mode,
        clock_mode=clock.mode,
        wall_time_ns=now_ns,
        monotonic_ns=clock.monotonic_ns(),
        trading_day=trading_day(now_ns),
        regressions_observed=clock.regressions_observed,
    )
