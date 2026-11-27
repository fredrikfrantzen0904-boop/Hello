"""Independent tests for the ``Clock`` contract: ``ClockReading`` and ``read_clock``.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - section "Clock 1.0.0" with the invariants CLK-1 .. CLK-3;
* ``specifications/SHARED_CONTRACTS.md`` section 3.2 (clock and scheduler: a REPLAY run uses a REPLAY
  clock and a PAPER run the SYSTEM clock; the system clock never decreases and never stands still
  and counts every backward step in ``regressions_observed``), section 3.4 (trading day) and
  section 6.1 (stream ``clock/<producer>``, ordering time ``wall_time_ns``, rank 5);
* the docstrings of ``gie/contracts/clock.py`` and ``gie/core/clock.py``.

SYNTHETIC FIXTURE: every instant, run id and clock source in this file is invented test data.  The
"system clock" used here is a ``SystemClock`` driven by scripted fake sources; the operating-system
clock is never read.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable
from typing import Any

import pytest

from gie.contracts.clock import (
    Clock,
    ClockMode,
    ClockReading,
    PollingScheduler,
    ReplayClock,
    Scheduler,
    SystemClock,
    read_clock,
)
from gie.contracts.codec import fingerprint, from_json, from_wire, to_json, to_wire
from gie.contracts.common import RunMode
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.core.clock import ClockView, PaperClock
from gie.core.errors import ContractValidationError
from gie.core.timeutil import MAX_TIME_NS, NS_PER_SECOND, trading_day
from tests.gie import samples

T0 = samples.T0
#: 2026-11-26 23:00:00 UTC is midnight in Prague (CET = UTC+1): the trading day changes here.
PRAGUE_MIDNIGHT = T0 + 48_600 * NS_PER_SECOND
PAPER_RUN_ID = samples.variants()["RunManifest"]["paper_primary"].run_id


def _utc_ns(year: int, month: int, day: int, hour: int) -> int:
    moment = datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)
    return int(moment.timestamp()) * NS_PER_SECOND


class _Script:
    """A scripted fake time source: returns the given readings in order, then repeats the last."""

    def __init__(self, *readings: int) -> None:
        self._readings = readings
        self.calls = 0

    def __call__(self) -> int:
        value = self._readings[min(self.calls, len(self._readings) - 1)]
        self.calls += 1
        return value


def _system_clock(wall: _Script, monotonic: _Script) -> SystemClock:
    return SystemClock(wall_source=wall, monotonic_source=monotonic)


def _paper(**overrides: Any) -> ClockReading:
    fields: dict[str, Any] = {
        "run_id": PAPER_RUN_ID,
        "mode": RunMode.PAPER,
        "clock_mode": ClockMode.SYSTEM,
        "monotonic_ns": 123_456_789,
    }
    fields.update(overrides)
    return samples.clock_reading(**fields)


def _invariant(excinfo: pytest.ExceptionInfo[ContractValidationError]) -> object:
    return excinfo.value.details.get("invariant")


# =========================================================================== invariants CLK-1..3
ACCEPTED: list[Any] = [
    pytest.param("CLK-1", samples.clock_reading, id="CLK-1-sample-instant"),
    pytest.param(
        "CLK-1",
        lambda: samples.clock_reading(wall_time_ns=PRAGUE_MIDNIGHT - 1, trading_day="2026-11-26"),
        id="CLK-1-last-nanosecond-of-the-prague-day",
    ),
    pytest.param(
        "CLK-1",
        lambda: samples.clock_reading(wall_time_ns=PRAGUE_MIDNIGHT, trading_day="2026-11-27"),
        id="CLK-1-prague-midnight-starts-the-next-day",
    ),
    pytest.param(
        "CLK-1",
        lambda: samples.clock_reading(wall_time_ns=_utc_ns(2026, 7, 1, 22), trading_day="2026-07-02"),
        id="CLK-1-summer-time-midnight",
    ),
    pytest.param(
        "CLK-1", lambda: samples.clock_reading(wall_time_ns=0, trading_day="1970-01-01"), id="CLK-1-epoch"
    ),
    pytest.param("CLK-2", samples.clock_reading, id="CLK-2-replay-run-replay-clock"),
    pytest.param("CLK-2", _paper, id="CLK-2-paper-run-system-clock"),
    pytest.param("CLK-3", samples.clock_reading, id="CLK-3-replay-without-regression"),
    pytest.param("CLK-3", lambda: _paper(regressions_observed=3), id="CLK-3-system-clock-may-report-steps"),
    pytest.param("CLK-3", lambda: _paper(regressions_observed=0), id="CLK-3-system-clock-without-steps"),
]

VIOLATIONS: list[Any] = [
    pytest.param("CLK-1", samples.clock_reading, {"trading_day": "2026-11-27"}, id="CLK-1-next-day"),
    pytest.param("CLK-1", samples.clock_reading, {"trading_day": "2026-11-25"}, id="CLK-1-previous-day"),
    pytest.param(
        "CLK-1",
        lambda: samples.clock_reading(wall_time_ns=PRAGUE_MIDNIGHT),
        {"trading_day": "2026-11-26"},
        id="CLK-1-utc-date-instead-of-prague-date",
    ),
    pytest.param(
        "CLK-1",
        lambda: samples.clock_reading(wall_time_ns=PRAGUE_MIDNIGHT - 1),
        {"trading_day": "2026-11-27"},
        id="CLK-1-one-nanosecond-before-midnight",
    ),
    pytest.param(
        "CLK-2", samples.clock_reading, {"clock_mode": ClockMode.SYSTEM}, id="CLK-2-replay-run-system-clock"
    ),
    pytest.param("CLK-2", _paper, {"clock_mode": ClockMode.REPLAY}, id="CLK-2-paper-run-replay-clock"),
    pytest.param("CLK-2", samples.clock_reading, {"mode": RunMode.PAPER}, id="CLK-2-replay-clock-paper-run"),
    pytest.param("CLK-2", _paper, {"mode": RunMode.REPLAY}, id="CLK-2-system-clock-replay-run"),
    pytest.param("CLK-3", samples.clock_reading, {"regressions_observed": 1}, id="CLK-3-replay-regression"),
    pytest.param(
        "CLK-3", samples.clock_reading, {"regressions_observed": 12}, id="CLK-3-replay-many-regressions"
    ),
]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(("invariant", "build"), ACCEPTED)
def test_conforming_reading_is_accepted_and_round_trips(
    invariant: str, build: Callable[[], ClockReading]
) -> None:
    reading = build()
    assert type(reading) is ClockReading
    assert invariant in {rule.id for rule in ClockReading.ALL_INVARIANTS}
    assert from_wire(to_wire(reading)) == reading
    assert from_json(to_json(reading)) == reading


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_reading_cannot_be_constructed(
    invariant: str, build: Callable[[], ClockReading], change: dict[str, Any]
) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(build(), **change)
    assert _invariant(excinfo) == invariant
    assert excinfo.value.details["contract"] == "ClockReading"


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_reading_cannot_be_decoded(
    invariant: str, build: Callable[[], ClockReading], change: dict[str, Any]
) -> None:
    wire = to_wire(build())
    for name, value in change.items():
        wire[name] = getattr(value, "value", value)
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert _invariant(excinfo) == invariant


@pytest.mark.requirement("S00-ASG-19")
def test_every_invariant_of_the_field_reference_has_an_accepted_and_a_rejected_case() -> None:
    documented = {"CLK-1", "CLK-2", "CLK-3"}
    assert {rule.id for rule in ClockReading.ALL_INVARIANTS} == documented
    assert {case.values[0] for case in ACCEPTED} == documented
    assert {case.values[0] for case in VIOLATIONS} == documented


# =========================================================================== fields
@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wall_time_ns", -1),
        ("wall_time_ns", MAX_TIME_NS + 1),
        ("wall_time_ns", float(T0)),
        ("wall_time_ns", True),
        ("wall_time_ns", str(T0)),
        ("monotonic_ns", -1),
        ("monotonic_ns", MAX_TIME_NS + 1),
        ("monotonic_ns", 1.5),
        ("regressions_observed", -1),
        ("regressions_observed", None),
        ("trading_day", "2026-11-31"),
        ("trading_day", "2026-13-01"),
        ("trading_day", "26-11-2026"),
        ("trading_day", "2026-11-26T09:30:00Z"),
        ("trading_day", 20261126),
        ("mode", "REPLAY"),
        ("mode", "LIVE"),
        ("mode", ClockMode.REPLAY),
        ("clock_mode", "REPLAY"),
        ("clock_mode", RunMode.REPLAY),
        ("run_id", samples.MODEL_ID),
        ("run_id", "run_" + "G" * 32),
        ("run_id", ""),
        ("producer", "Core.Clock"),
        ("producer", "core clock"),
        ("producer", ""),
        ("seq", -1),
        ("seq", 1.0),
    ],
)
def test_malformed_field_is_refused_with_its_path(field: str, value: object) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(samples.clock_reading(), **{field: value})
    assert excinfo.value.details["path"] == field


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize("missing", ["regressions_observed", "trading_day", "clock_mode", "run_id", "seq"])
def test_a_reading_with_a_missing_field_cannot_be_decoded(missing: str) -> None:
    wire = to_wire(samples.clock_reading())
    del wire[missing]
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == missing


@pytest.mark.requirement("S00-ASG-19")
def test_a_reading_with_an_unknown_field_or_live_mode_cannot_be_decoded() -> None:
    wire = to_wire(samples.clock_reading())
    with pytest.raises(ContractValidationError):
        from_wire({**wire, "utc_offset": 0})
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire({**wire, "mode": "LIVE"})
    assert excinfo.value.details["path"] == "mode"
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire({**wire, "clock_mode": "NTP"})
    assert excinfo.value.details["path"] == "clock_mode"


@pytest.mark.requirement("S00-ASG-19")
def test_wire_form_carries_the_contract_name_clock_and_integer_nanoseconds() -> None:
    wire = to_wire(samples.clock_reading())
    assert wire["contract"] == "Clock"
    assert wire["schema_version"] == "1.0.0"
    assert wire["wall_time_ns"] == T0
    assert type(wire["wall_time_ns"]) is int
    assert wire["mode"] == "REPLAY"
    assert wire["clock_mode"] == "REPLAY"
    assert wire["trading_day"] == "2026-11-26"
    assert f'"wall_time_ns":{T0}' in to_json(samples.clock_reading())


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-25")
def test_stream_ordering_time_and_rank_of_a_reading() -> None:
    reading = samples.clock_reading(seq=4, wall_time_ns=T0 + 5)
    assert stream_name(reading) == "clock/core.clock"
    assert ordering_time_ns(reading) == T0 + 5
    key = ordering_key(reading)
    assert (key.time_ns, key.rank, key.stream, key.seq) == (T0 + 5, 5, "clock/core.clock", 4)
    # Rank 5: a clock reading sorts before a tick (rank 10) that became available at the same instant.
    tick = samples.tick(receive_time_ns=T0 + 5, event_time_ns=T0 + 5)
    assert ordering_key(reading) < ordering_key(tick)


@pytest.mark.requirement("S00-ASG-19", "COM-07")
def test_sample_readings_are_valid_and_distinct() -> None:
    readings = samples.variants()["Clock"]
    assert set(readings) == {"replay", "paper_system"}
    replay, paper = readings["replay"], readings["paper_system"]
    assert isinstance(replay, ClockReading)
    assert isinstance(paper, ClockReading)
    assert (replay.mode, replay.clock_mode, replay.regressions_observed) == (
        RunMode.REPLAY,
        ClockMode.REPLAY,
        0,
    )
    assert (paper.mode, paper.clock_mode, paper.regressions_observed) == (RunMode.PAPER, ClockMode.SYSTEM, 1)
    assert fingerprint(replay) != fingerprint(paper)
    assert fingerprint(replay) == fingerprint(samples.clock_reading())


# =========================================================================== read_clock: replay
@pytest.mark.requirement("S00-ASG-19")
def test_reading_a_replay_clock() -> None:
    clock = ReplayClock(T0)
    reading = read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=0)
    assert type(reading) is ClockReading
    assert reading == samples.clock_reading()
    assert (reading.wall_time_ns, reading.monotonic_ns, reading.regressions_observed) == (T0, 0, 0)
    assert reading.clock_mode is ClockMode.REPLAY
    assert reading.trading_day == "2026-11-26"


@pytest.mark.requirement("S00-ASG-19")
def test_replay_readings_follow_virtual_time_only() -> None:
    clock = ReplayClock(T0)
    first = read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=0)
    again = read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=0)
    assert again == first  # virtual time does not advance while code runs
    clock.advance_to(PRAGUE_MIDNIGHT - 1)
    before = read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=1)
    clock.advance_by(1)
    after = read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=2)
    assert (before.wall_time_ns, before.trading_day) == (PRAGUE_MIDNIGHT - 1, "2026-11-26")
    assert (after.wall_time_ns, after.trading_day) == (PRAGUE_MIDNIGHT, "2026-11-27")
    assert before.monotonic_ns == PRAGUE_MIDNIGHT - 1 - T0
    assert after.monotonic_ns - before.monotonic_ns == after.wall_time_ns - before.wall_time_ns == 1
    assert [first.seq, before.seq, after.seq] == [0, 1, 2]
    assert after.regressions_observed == 0


@pytest.mark.requirement("S00-ASG-19")
def test_reading_inside_a_timer_callback_shows_the_due_time() -> None:
    clock = ReplayClock(T0)
    seen: list[ClockReading] = []

    def on_timer(due_ns: int) -> None:
        seen.append(
            read_clock(
                clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=len(seen)
            )
        )
        assert seen[-1].wall_time_ns == due_ns

    clock.call_at(T0 + 10, on_timer)
    clock.call_after(20, on_timer)
    clock.advance_to(T0 + 1_000)
    assert [reading.wall_time_ns for reading in seen] == [T0 + 10, T0 + 20]


@pytest.mark.requirement("S00-ASG-19")
def test_a_replay_clock_cannot_be_read_as_the_clock_of_a_paper_run() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        read_clock(ReplayClock(T0), run_id=PAPER_RUN_ID, mode=RunMode.PAPER, producer="core.clock", seq=0)
    assert _invariant(excinfo) == "CLK-2"


# =========================================================================== read_clock: system clock
@pytest.mark.requirement("S00-ASG-19")
def test_reading_a_system_clock_driven_by_fake_sources() -> None:
    wall, monotonic = _Script(T0), _Script(1_000, 1_500)
    reading = read_clock(
        _system_clock(wall, monotonic), run_id=PAPER_RUN_ID, mode=RunMode.PAPER, producer="core.clock", seq=7
    )
    assert reading.mode is RunMode.PAPER
    assert reading.clock_mode is ClockMode.SYSTEM
    assert reading.wall_time_ns == T0
    assert reading.monotonic_ns == 1_500
    assert reading.trading_day == trading_day(T0)
    assert reading.regressions_observed == 0
    assert reading.seq == 7
    assert wall.calls == 1


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-23")
def test_a_backward_step_of_the_system_clock_is_propagated_into_the_reading() -> None:
    # The operating-system clock is stepped back by one second between the first two readings and
    # has caught up again at the fourth.
    wall = _Script(T0, T0 - NS_PER_SECOND, T0 - NS_PER_SECOND + 5, T0 + 10 * NS_PER_SECOND)
    monotonic = _Script(100, 150, 300, 350, 420, 470, 900, 950)
    clock = _system_clock(wall, monotonic)

    def read(seq: int) -> ClockReading:
        return read_clock(clock, run_id=PAPER_RUN_ID, mode=RunMode.PAPER, producer="core.clock", seq=seq)

    first, second, third, fourth = read(0), read(1), read(2), read(3)
    assert [r.regressions_observed for r in (first, second, third, fourth)] == [0, 1, 1, 1]
    assert first.wall_time_ns == T0
    # Time never decreases and never stands still: it continues at the pace of the monotonic source.
    assert second.wall_time_ns == T0 + (300 - 100)
    assert third.wall_time_ns == second.wall_time_ns + (420 - 300)
    assert fourth.wall_time_ns == T0 + 10 * NS_PER_SECOND
    walls = [r.wall_time_ns for r in (first, second, third, fourth)]
    assert walls == sorted(set(walls))
    assert [r.monotonic_ns for r in (first, second, third, fourth)] == [150, 350, 470, 950]
    assert all(r.trading_day == trading_day(r.wall_time_ns) for r in (first, second, third, fourth))


@pytest.mark.requirement("S00-ASG-19")
def test_each_separate_backward_step_is_counted_once() -> None:
    wall = _Script(T0, T0 - 5, T0 + 100, T0 + 50, T0 + 200)
    monotonic = _Script(*range(10, 200, 10))
    clock = _system_clock(wall, monotonic)
    counts = [
        read_clock(
            clock, run_id=PAPER_RUN_ID, mode=RunMode.PAPER, producer="core.clock", seq=seq
        ).regressions_observed
        for seq in range(5)
    ]
    assert counts == [0, 1, 1, 2, 2]


@pytest.mark.requirement("S00-ASG-19")
def test_a_system_clock_cannot_be_read_as_the_clock_of_a_replay_run() -> None:
    clock = _system_clock(_Script(T0), _Script(1))
    with pytest.raises(ContractValidationError) as excinfo:
        read_clock(clock, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=0)
    assert _invariant(excinfo) == "CLK-2"


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("run_id", "run_not-an-id"),
        ("run_id", samples.MODEL_ID),
        ("producer", "Core Clock"),
        ("seq", -1),
        ("mode", "PAPER"),
        ("mode", "LIVE"),
    ],
)
def test_read_clock_refuses_an_invalid_header(argument: str, value: object) -> None:
    arguments: dict[str, Any] = {
        "run_id": samples.RUN_ID,
        "mode": RunMode.REPLAY,
        "producer": "core.clock",
        "seq": 0,
    }
    assert read_clock(ReplayClock(T0), **arguments).seq == 0
    arguments[argument] = value
    with pytest.raises(ContractValidationError) as excinfo:
        read_clock(ReplayClock(T0), **arguments)
    assert excinfo.value.details["path"] == argument


# =========================================================================== behavioural contract
@pytest.mark.requirement("S00-ASG-19")
def test_reference_clocks_and_schedulers_satisfy_the_protocols() -> None:
    replay = ReplayClock(T0)
    system = _system_clock(_Script(T0), _Script(1))
    paper = PaperClock(system)
    polling = PollingScheduler(paper)  # rework 2: the polling scheduler runs on the paper clock
    assert isinstance(replay, Clock)
    assert isinstance(replay, Scheduler)
    assert isinstance(system, Clock)
    assert isinstance(paper, Clock)
    assert isinstance(polling, Scheduler)
    assert not isinstance(system, Scheduler)
    assert not isinstance(paper, Scheduler)
    assert not isinstance(polling, Clock)
    assert replay.mode is ClockMode.REPLAY
    assert system.mode is ClockMode.SYSTEM
    assert paper.mode is ClockMode.SYSTEM
    assert [member.value for member in ClockMode] == ["REPLAY", "SYSTEM"]


@pytest.mark.requirement("S00-ASG-19")
@pytest.mark.parametrize(
    "not_a_paper_clock",
    [_system_clock(_Script(T0), _Script(1)), ReplayClock(T0), None],
    ids=["system-clock", "replay-clock", "none"],
)
def test_the_polling_scheduler_refuses_anything_but_the_paper_clock(not_a_paper_clock: object) -> None:
    with pytest.raises(ContractValidationError):
        PollingScheduler(not_a_paper_clock)  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        PollingScheduler(ClockView(PaperClock(_system_clock(_Script(T0), _Script(1)))))  # type: ignore[arg-type]


# =========================================================================== read_clock: paper step time
@pytest.mark.requirement("S00-ASG-19")
def test_reading_a_paper_clock_shows_the_step_instant_not_the_system_clock() -> None:
    # Rework 2 (D-024): a paper run sees the instant of the input being processed; only the run loop
    # moves it.  The system clock runs ahead (T0 + 5 s) but the reading stays at the step instant.
    wall = _Script(T0, T0 + 5 * NS_PER_SECOND)
    paper = PaperClock(_system_clock(wall, _Script(10, 20, 30, 40)))

    def read(clock: Clock, seq: int) -> ClockReading:
        return read_clock(clock, run_id=PAPER_RUN_ID, mode=RunMode.PAPER, producer="core.clock", seq=seq)

    first = read(paper, 0)
    assert (first.wall_time_ns, first.clock_mode) == (T0, ClockMode.SYSTEM)
    assert read(paper, 1).wall_time_ns == T0  # does not move while a step is processed
    assert paper.begin_step(T0 + NS_PER_SECOND) == T0 + NS_PER_SECOND
    assert read(ClockView(paper), 2).wall_time_ns == T0 + NS_PER_SECOND  # same through the view
    with pytest.raises(ContractValidationError) as excinfo:
        read_clock(paper, run_id=samples.RUN_ID, mode=RunMode.REPLAY, producer="core.clock", seq=3)
    assert _invariant(excinfo) == "CLK-2"


@pytest.mark.requirement("S00-ASG-19")
def test_an_object_without_the_clock_members_is_not_a_clock() -> None:
    class _NowOnly:
        def now_ns(self) -> int:
            return T0

    assert not isinstance(_NowOnly(), Clock)
    assert not isinstance(object(), Scheduler)
