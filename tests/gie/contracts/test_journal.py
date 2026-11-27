"""Independent tests for the run journal: ``JournalEntry``, ``MemoryJournal``, ``FileJournal``.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/SHARED_CONTRACTS.md`` section 6.1 (streams, sequence numbers, duplicates,
  conflicts, gaps, ordering time), section 6.2 (the journal is the total order of a run; what a
  record must pass before it is appended; "a refused record leaves the journal unchanged"; the
  durable ``FileJournal`` and the explicit repair of a torn last line), section 9.2 (sequence rules
  enforced through the ``LifecycleLedger`` by every journal) and section 5 (error semantics);
* the module and class docstrings of ``gie/contracts/journal.py`` and the ``RecordSink`` /
  ``RecordSource`` docstrings of ``gie/contracts/interfaces.py``;
* owner specification G08 (persistent state, restart reconciliation, fail closed) and brief 10
  (deterministic replay, no look-ahead).

SYNTHETIC FIXTURE: every record published in this file is invented test data built with
``tests/gie/samples.py`` (origin ``SYNTHETIC_FIXTURE``).  The "runs" journaled here were never
executed; nothing in this file is market data, broker data or evidence of performance.  Journal
files are written only below pytest's ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from gie.contracts import identity
from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.clock import ClockMode
from gie.contracts.codec import fingerprint, from_json, from_wire, to_json, to_wire
from gie.contracts.common import NamedValue, RunMode
from gie.contracts.evidence import Evidence, PriceLevel
from gie.contracts.journal import FileJournal, JournalEntry, MemoryJournal, repair_torn_tail
from gie.contracts.model_version import ContractRef, ModelStatus, ModelVersion
from gie.contracts.order_event import OrderEvent, OrderEventType, OrderState
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.run import RunManifest, RunRole
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import (
    ContractError,
    ContractValidationError,
    FutureDataError,
    GieError,
    IllegalTransitionError,
    JournalCorruptError,
    OutOfOrderError,
    SequenceConflictError,
    SequenceGapError,
    UnknownContractError,
)
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
MS = NS_PER_MS
SEC = NS_PER_SECOND
OTHER_RUN_ID = identity.run_id("REPLAY", "1" * 64, "other")
PAPER_RUN_ID = identity.run_id("PAPER", samples.CONFIG_SHA, "fixture-paper")
ENTRY_ORDER_ID = samples.order_event().client_order_id
POSITION_ID = samples.position().position_id
STOP_STREAM = "reflex.protection"


# =========================================================================== builders (all synthetic)
def _tick_at(seq: int, receive_time_ns: int, **overrides: Any) -> Tick:
    fields: dict[str, Any] = {
        "seq": seq,
        "event_time_ns": receive_time_ns - 2 * MS,
        "receive_time_ns": receive_time_ns,
    }
    fields.update(overrides)
    return samples.tick(**fields)


def _other_evidence(seq: int, kind: str = "breakout", *, at: int = T0, **overrides: Any) -> Evidence:
    """Evidence of another market event in the stream of the sample evidence."""
    fields: dict[str, Any] = {
        "seq": seq,
        "kind": kind,
        "decision_time_ns": at,
        "as_of_time_ns": at - MS,
        "valid_until_ns": at + 300 * SEC,
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _restream(records: list[OrderEvent], producer: str) -> list[OrderEvent]:
    """The same order events as the stream of another producer (seq 0, 1, 2, ...)."""
    return [dataclasses.replace(record, producer=producer, seq=seq) for seq, record in enumerate(records)]


def _stops() -> list[OrderEvent]:
    return _restream(samples.protective_stop_lifecycle(), STOP_STREAM)


def _decision_chain() -> list[Record]:
    """Tick -> evidence -> candidate -> plan -> approval -> entry order, all at the sample instant."""
    return [
        samples.tick(),
        samples.evidence(),
        samples.candidate(),
        samples.trade_plan(),
        samples.risk_decision(),
        *samples.order_lifecycle(),
    ]


def _run_records() -> list[Record]:
    """A complete synthetic run in a valid publication order (24 records, manifest first)."""
    positions = samples.position_lifecycle()
    return [
        samples.run_manifest(),
        samples.clock_reading(),
        *samples.model_lifecycle(),
        *_decision_chain(),
        _tick_at(1, T0 + 5 * SEC - MS),
        positions[0],
        *_stops(),
        _tick_at(2, T0 + 60 * SEC - MS),
        positions[1],
        _tick_at(3, T0 + 300 * SEC - MS),
        positions[2],
    ]


def _journal(*records: Record) -> MemoryJournal:
    journal = MemoryJournal()
    journal.publish(samples.run_manifest())
    for record in records:
        journal.publish(record)
    return journal


def _full_journal() -> MemoryJournal:
    journal = MemoryJournal()
    for record in _run_records():
        journal.publish(record)
    return journal


def _state(journal: MemoryJournal) -> tuple[Any, ...]:
    """Everything observable about a journal: entries, streams and the ledger behind them."""
    ledger = journal.ledger
    return (
        len(journal),
        journal.streams(),
        tuple(journal.canonical_lines()),
        tuple(journal.read()),
        journal.manifest,
        journal.run_id,
        journal.tick_gaps,
        ledger.open_orders(),
        ledger.order(ENTRY_ORDER_ID),
        ledger.position(POSITION_ID),
        ledger.model(samples.MODEL_ID),
        ledger.group_entry_order(samples.trade_plan().alternative_group_id),
    )


def _late_fill_report() -> OrderEvent:
    """Another FILLED report for the sample entry order, which is already in its final state."""
    filled = samples.order_lifecycle()[3]
    return dataclasses.replace(
        filled,
        seq=4,
        order_seq=4,
        order_event_id=identity.order_event_id(filled.client_order_id, 4),
        event_time_ns=T0 + 400 * SEC,
        receive_time_ns=T0 + 400 * SEC,
    )


def _future_evidence(seq: int = 1) -> Evidence:
    """Evidence whose watermark lies after every tick of ``_run_records``."""
    return _other_evidence(seq, at=T0 + 400 * SEC, as_of_time_ns=T0 + 400 * SEC)


# =========================================================================== first record, run and mode
@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_a_new_journal_is_empty_and_belongs_to_no_run() -> None:
    journal = MemoryJournal()
    assert len(journal) == 0
    assert journal.manifest is None
    assert journal.run_id is None
    assert journal.streams() == ()
    assert list(journal.read()) == []
    assert journal.canonical_lines() == []
    assert journal.tick_gaps == 0


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
@pytest.mark.parametrize(
    "build",
    [
        samples.tick,
        samples.evidence,
        samples.candidate,
        samples.trade_plan,
        samples.risk_decision,
        samples.order_event,
        samples.position,
        samples.model_version,
        samples.clock_reading,
    ],
    ids=lambda build: build.__name__,
)
def test_the_first_record_must_be_the_run_manifest(build: Callable[[], Record]) -> None:
    journal = MemoryJournal()
    with pytest.raises(ContractValidationError) as excinfo:
        journal.publish(build())
    assert "RunManifest" in excinfo.value.message
    assert len(journal) == 0
    assert journal.run_id is None
    assert journal.streams() == ()
    # The journal is still usable: the manifest is accepted as record 0 afterwards.
    assert journal.publish(samples.run_manifest()) == 0


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_the_manifest_is_record_0_and_names_the_run() -> None:
    journal = MemoryJournal()
    manifest = samples.run_manifest()
    assert journal.publish(manifest) == 0
    assert journal.manifest == manifest
    assert journal.run_id == samples.RUN_ID
    assert len(journal) == 1
    assert journal.streams() == ("run_manifest/app.run",)


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_a_journal_belongs_to_exactly_one_run() -> None:
    journal = _journal(samples.tick())
    before = _state(journal)
    assert journal.publish(samples.run_manifest()) == 0  # the same manifest again: idempotent
    for other in (
        samples.run_manifest(label="other"),
        samples.run_manifest(seed=8),
        samples.run_manifest(role=RunRole.SHADOW, label="fixture-shadow", shadow_of_run_id=samples.RUN_ID),
        samples.variants()["RunManifest"]["paper_primary"],
    ):
        with pytest.raises(SequenceConflictError):
            journal.publish(other)
    assert _state(journal) == before


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
@pytest.mark.parametrize(
    "build",
    [
        samples.evidence,
        samples.candidate,
        samples.trade_plan,
        samples.risk_decision,
        samples.order_event,
        samples.position,
        samples.clock_reading,
    ],
    ids=lambda build: build.__name__,
)
def test_a_record_of_another_run_is_refused(build: Callable[..., Record]) -> None:
    journal = _journal()
    with pytest.raises(ContractValidationError) as excinfo:
        journal.publish(build(run_id=OTHER_RUN_ID))
    assert excinfo.value.details["run_id"] == OTHER_RUN_ID
    assert len(journal) == 1
    assert journal.publish(build()) == 1


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_a_record_of_another_mode_is_refused() -> None:
    journal = _journal()
    paper_records = [
        samples.evidence(mode=RunMode.PAPER),
        samples.candidate(mode=RunMode.PAPER),
        samples.risk_decision(mode=RunMode.PAPER),
        samples.clock_reading(mode=RunMode.PAPER, clock_mode=ClockMode.SYSTEM),
    ]
    for record in paper_records:
        with pytest.raises(ContractValidationError) as excinfo:
            journal.publish(record)
        assert "mode" in excinfo.value.message
    assert len(journal) == 1


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_a_paper_journal_accepts_paper_records_and_refuses_replay_records() -> None:
    journal = MemoryJournal()
    manifest = samples.variants()["RunManifest"]["paper_primary"]
    assert journal.publish(manifest) == 0
    assert journal.run_id == PAPER_RUN_ID
    reading = samples.clock_reading(run_id=PAPER_RUN_ID, mode=RunMode.PAPER, clock_mode=ClockMode.SYSTEM)
    assert journal.publish(reading) == 1
    with pytest.raises(ContractValidationError):
        journal.publish(samples.evidence(run_id=PAPER_RUN_ID))  # REPLAY record claiming the paper run
    with pytest.raises(ContractValidationError):
        journal.publish(samples.evidence())  # record of the replay run
    assert len(journal) == 2


# =========================================================================== idempotent publish
@pytest.mark.requirement("S00-ASG-25", "G08-05")
def test_publishing_the_same_record_twice_has_no_further_effect() -> None:
    records = _run_records()
    journal = MemoryJournal()
    positions = [journal.publish(record) for record in records]
    assert positions == list(range(len(records)))
    before = _state(journal)
    for expected, record in enumerate(records):
        assert journal.publish(record) == expected
    assert _state(journal) == before
    assert len(journal) == len(records)


@pytest.mark.requirement("S00-ASG-25", "G08-05")
def test_an_equal_record_built_again_is_the_same_record() -> None:
    journal = _full_journal()
    before = _state(journal)
    # Rebuilt from scratch and decoded from canonical JSON: equal content, different objects.
    for expected, record in enumerate(_run_records()):
        assert journal.publish(record) == expected
        assert journal.publish(from_json(to_json(record))) == expected
    assert _state(journal) == before


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(samples.tick, lambda: samples.tick(bid=D("2650.11")), id="tick-other-bid"),
        pytest.param(samples.tick, lambda: samples.tick(flags=()), id="tick-other-flags"),
        pytest.param(
            samples.clock_reading, lambda: samples.clock_reading(monotonic_ns=1), id="clock-other-monotonic"
        ),
        pytest.param(samples.evidence, lambda: _other_evidence(0), id="evidence-of-another-event"),
        pytest.param(
            samples.risk_decision,
            lambda: samples.risk_decision(samples.trade_plan(variant=1)),
            id="approval-of-another-plan",
        ),
    ],
)
def test_different_content_at_a_journaled_stream_position_is_a_conflict(
    first: Callable[[], Record], second: Callable[[], Record]
) -> None:
    journal = _journal(first())
    before = _state(journal)
    assert stream_name(first()) == stream_name(second())
    assert fingerprint(first()) != fingerprint(second())
    with pytest.raises(SequenceConflictError) as excinfo:
        journal.publish(second())
    assert excinfo.value.code == "GIE.SEQ.CONFLICT"
    assert excinfo.value.details["stream"] == stream_name(first())
    assert _state(journal) == before
    assert journal.publish(first()) == 1


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            samples.evidence, lambda: samples.evidence(bullish_score=0.8), id="evidence-other-score"
        ),
        pytest.param(
            samples.position, lambda: samples.position(mark_price=D("2650.70")), id="position-other-mark"
        ),
        pytest.param(
            samples.order_event, lambda: samples.order_event(volume_lots=D("0.40")), id="order-other-volume"
        ),
    ],
)
def test_changed_content_of_a_journaled_lifecycle_record_is_a_conflict_too(
    first: Callable[[], Record], second: Callable[[], Record]
) -> None:
    # RecordSink: "different content at the same position raises SequenceConflictError".  The
    # stream sequence is checked before the lifecycle chain (journal check 2 before check 3), so a
    # second version of the record at an already journaled position is a conflict, whatever the
    # lifecycle rules would say about it.
    journal = _journal(first())
    before = _state(journal)
    assert stream_name(first()) == stream_name(second())
    with pytest.raises(SequenceConflictError):
        journal.publish(second())
    assert _state(journal) == before


# =========================================================================== stream sequence
@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("seq", [2, 3, 50])
def test_a_gap_in_a_stream_of_the_run_is_refused(seq: int) -> None:
    journal = _journal(samples.tick(), samples.evidence())
    before = _state(journal)
    with pytest.raises(SequenceGapError) as excinfo:
        journal.publish(_other_evidence(seq))
    assert excinfo.value.code == "GIE.SEQ.GAP"
    assert excinfo.value.details["stream"] == "evidence/strategy_intelligence.sweep_reversal"
    assert _state(journal) == before
    assert journal.publish(_other_evidence(1)) == 3


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    "build",
    [
        lambda: _other_evidence(1),
        lambda: samples.candidate(seq=1),
        lambda: samples.risk_decision(seq=3),
        lambda: samples.clock_reading(seq=1),
        lambda: samples.position(seq=2),
    ],
    ids=["evidence", "candidate", "risk-decision", "clock", "position"],
)
def test_a_stream_of_the_run_starts_at_sequence_number_0(build: Callable[[], Record]) -> None:
    journal = _journal()
    with pytest.raises(SequenceGapError):
        journal.publish(build())
    assert len(journal) == 1


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_a_gap_in_a_tick_stream_is_accepted_and_counted() -> None:
    journal = _journal(_tick_at(0, T0))
    assert journal.tick_gaps == 0
    assert journal.publish(_tick_at(1, T0 + SEC)) == 2
    assert journal.tick_gaps == 0
    assert journal.publish(_tick_at(5, T0 + 2 * SEC)) == 3  # 2, 3 and 4 were lost by the feed
    assert journal.tick_gaps == 1
    assert journal.publish(_tick_at(6, T0 + 3 * SEC)) == 4
    assert journal.tick_gaps == 1
    assert journal.publish(_tick_at(9, T0 + 4 * SEC)) == 5
    assert journal.tick_gaps == 2
    # A duplicate of a tick after a gap is still only a duplicate.
    assert journal.publish(_tick_at(9, T0 + 4 * SEC)) == 5
    assert journal.tick_gaps == 2
    assert len(journal) == 6


@pytest.mark.requirement("S00-ASG-25")
def test_a_tick_stream_is_joined_at_its_current_position() -> None:
    # Tick.seq is assigned by the tick store and never restarts: a run joins the stream in the middle.
    journal = _journal(_tick_at(48_213, T0))
    assert journal.tick_gaps == 0
    assert journal.publish(_tick_at(48_214, T0 + MS)) == 2
    assert journal.tick_gaps == 0


@pytest.mark.requirement("S00-ASG-25")
def test_an_older_tick_position_that_was_never_journaled_is_refused() -> None:
    journal = _journal(_tick_at(5, T0), _tick_at(9, T0 + SEC))
    before = _state(journal)
    with pytest.raises(OutOfOrderError):
        journal.publish(_tick_at(7, T0 + SEC))
    with pytest.raises(OutOfOrderError):
        journal.publish(_tick_at(4, T0 + SEC))
    assert _state(journal) == before


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    ("first", "late"),
    [
        pytest.param(lambda: _tick_at(0, T0), lambda: _tick_at(1, T0 - 1), id="tick-receive-time"),
        pytest.param(samples.evidence, lambda: _other_evidence(1, at=T0 - MS), id="evidence-decision-time"),
        pytest.param(
            samples.clock_reading,
            lambda: samples.clock_reading(seq=1, wall_time_ns=T0 - 1),
            id="clock-wall-time",
        ),
        pytest.param(
            # Two different orders in one stream: the second request is older than the first.
            lambda: samples.order_lifecycle()[0],
            lambda: dataclasses.replace(
                samples.protective_stop_lifecycle()[0], seq=1, event_time_ns=T0, receive_time_ns=T0
            ),
            id="order-event-receive-time",
        ),
    ],
)
def test_a_decreasing_ordering_time_within_a_stream_is_refused(
    first: Callable[[], Record], late: Callable[[], Record]
) -> None:
    journal = _journal(first())
    before = _state(journal)
    assert ordering_time_ns(late()) < ordering_time_ns(first())
    with pytest.raises(OutOfOrderError) as excinfo:
        journal.publish(late())
    assert excinfo.value.code == "GIE.SEQ.OUT_OF_ORDER"
    assert _state(journal) == before


@pytest.mark.requirement("S00-ASG-25")
def test_an_equal_ordering_time_within_a_stream_is_accepted() -> None:
    journal = _journal(_tick_at(0, T0), _tick_at(1, T0), samples.evidence())
    assert journal.publish(_other_evidence(1)) == 4  # same decision instant as the first evidence
    assert journal.publish(samples.clock_reading()) == 5
    assert journal.publish(samples.clock_reading(seq=1)) == 6


@pytest.mark.requirement("S00-ASG-25")
def test_streams_are_sequenced_independently() -> None:
    variants = samples.variants()
    journal = _journal(
        samples.tick(),
        variants["Tick"]["observed_silver"],  # tick/fixture/XAGUSD, also seq 0
        variants["Evidence"]["active_bullish"],  # evidence/strategy_intelligence.sweep_reversal seq 0
        variants["Evidence"]["cavemen_breakout"],  # evidence/strategy_intelligence.cavemen seq 0
        variants["Evidence"]["cavemen_pending_limit"],  # ... seq 1
        variants["Evidence"]["intermarket_real_yield"],  # evidence/intermarket.lead_lag seq 0
    )
    assert len(journal) == 7
    assert journal.streams() == (
        "evidence/intermarket.lead_lag",
        "evidence/strategy_intelligence.cavemen",
        "evidence/strategy_intelligence.sweep_reversal",
        "run_manifest/app.run",
        "tick/fixture/XAGUSD",
        "tick/fixture/XAUUSD",
    )
    assert journal.tick_gaps == 0


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    ("setup", "record"),
    [
        pytest.param(
            lambda: [samples.order_lifecycle()[0]],
            lambda: samples.order_lifecycle()[2],
            id="gap-and-skipped-order-step",
        ),
        pytest.param(
            lambda: [samples.tick(), samples.evidence()],
            lambda: _other_evidence(2, at=T0 + SEC, as_of_time_ns=T0 + SEC),
            id="gap-and-look-ahead",
        ),
    ],
)
def test_the_stream_sequence_is_checked_before_lifecycle_and_look_ahead(
    setup: Callable[[], list[Record]], record: Callable[[], Record]
) -> None:
    # Journal module: "Before a record is appended it must pass, in this order: ... 2. its stream
    # sequence ... 3. the sequence rules of its lifecycle chain ... 4. the look-ahead guard".  A
    # record that skips a stream position is therefore reported as a gap.
    journal = _journal(*setup())
    with pytest.raises(SequenceGapError):
        journal.publish(record())


# =========================================================================== refused records
REFUSALS: list[Any] = [
    pytest.param(lambda: _other_evidence(1, run_id=OTHER_RUN_ID), ContractValidationError, id="another-run"),
    pytest.param(lambda: _other_evidence(1, mode=RunMode.PAPER), ContractValidationError, id="another-mode"),
    pytest.param(lambda: samples.run_manifest(label="other"), SequenceConflictError, id="another-manifest"),
    pytest.param(
        lambda: _tick_at(3, T0 + 300 * SEC - MS, bid=D("2650.11")), SequenceConflictError, id="conflict"
    ),
    pytest.param(lambda: _other_evidence(5, at=T0 + 100 * SEC), SequenceGapError, id="gap"),
    pytest.param(lambda: _tick_at(4, T0), OutOfOrderError, id="decreasing-ordering-time"),
    pytest.param(_late_fill_report, IllegalTransitionError, id="event-after-terminal-order-state"),
    pytest.param(_future_evidence, FutureDataError, id="look-ahead"),
    pytest.param(
        lambda: NamedValue(name="not_a_contract", value=1.0), UnknownContractError, id="nested-record"
    ),
]


@pytest.mark.requirement("S00-ASG-25", "G08-04")
@pytest.mark.parametrize(("build", "error"), REFUSALS)
def test_a_refused_record_leaves_length_streams_and_ledger_unchanged(
    build: Callable[[], Any], error: type[GieError]
) -> None:
    journal = _full_journal()
    before = _state(journal)
    with pytest.raises(error) as excinfo:
        journal.publish(build())
    assert type(excinfo.value) is error
    assert _state(journal) == before
    # Nothing was half-recorded: the next valid records take the next positions.
    assert journal.publish(_tick_at(4, T0 + 400 * SEC)) == len(_run_records())
    assert journal.publish(_future_evidence()) == len(_run_records()) + 1


@pytest.mark.requirement("S00-ASG-25", "G08-04")
@pytest.mark.parametrize(("build", "error"), REFUSALS)
def test_a_refused_record_is_not_written_to_the_journal_file(
    tmp_path: Path, build: Callable[[], Any], error: type[GieError]
) -> None:
    path = tmp_path / "run.journal"
    with FileJournal(path) as journal:
        for record in _run_records():
            journal.publish(record)
        written = path.read_bytes()
        before = _state(journal)
        with pytest.raises(error):
            journal.publish(build())
        assert _state(journal) == before
        assert path.read_bytes() == written
    with FileJournal(path) as reopened:
        assert _state(reopened) == before


# =========================================================================== only contract instances
@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize(
    "value",
    [
        NamedValue(name="oos_mean_net_r", value=0.0),
        ContractRef(contract="Tick", version="1.0.0"),
        PriceLevel(role="SWEPT_LEVEL", price=D("2648.40"), identity=True),
    ],
    ids=["NamedValue", "ContractRef", "PriceLevel"],
)
def test_a_nested_value_object_is_not_a_publishable_contract(value: Record) -> None:
    journal = _journal()
    with pytest.raises(UnknownContractError) as excinfo:
        journal.publish(value)
    assert excinfo.value.code == "GIE.CONTRACT.UNKNOWN"
    assert len(journal) == 1


@pytest.mark.requirement("S00-ASG-25")
def test_a_subclass_of_a_contract_is_not_the_registered_contract() -> None:
    class _TickLookAlike(Tick):
        """SYNTHETIC FIXTURE: a look-alike type; only exact contract classes are accepted."""

    genuine = samples.tick()
    look_alike = _TickLookAlike(**{f.name: getattr(genuine, f.name) for f in dataclasses.fields(genuine)})
    journal = _journal()
    with pytest.raises(UnknownContractError):
        journal.publish(look_alike)
    assert len(journal) == 1
    assert journal.publish(genuine) == 1


@pytest.mark.requirement("S00-ASG-25")
def test_an_object_that_is_no_record_is_refused_with_a_contract_error() -> None:
    # Section 5: all errors derive from GieError and are journalable data; "only exact instances of
    # the registered contract classes are accepted anywhere".
    journal = _journal()
    tick = samples.tick()
    for not_a_record in (to_wire(tick), to_json(tick), None, 7, (tick,), Tick):
        with pytest.raises(ContractError):
            journal.publish(not_a_record)  # type: ignore[arg-type]
    assert len(journal) == 1


# =========================================================================== lifecycle rules (ledger)
@pytest.mark.requirement("S00-ASG-25", "G07-09", "G07-10")
def test_valid_lifecycle_chains_are_journaled_and_tracked_by_the_ledger() -> None:
    journal = _full_journal()
    ledger = journal.ledger
    entry = ledger.order(ENTRY_ORDER_ID)
    assert entry is not None
    assert entry.order_state is OrderState.FILLED
    stop = _stops()[3]
    assert ledger.order(stop.client_order_id) == stop
    assert ledger.open_orders() == (stop,)
    position = ledger.position(POSITION_ID)
    assert position is not None
    assert (position.revision, position.stop_price) == (2, D("2649.90"))
    model = ledger.model(samples.MODEL_ID)
    assert model is not None
    assert model.status is ModelStatus.ACTIVE
    assert ledger.order("ord_" + "0" * 32) is None


@pytest.mark.requirement("S00-ASG-25", "COM-07")
def test_sample_order_and_stop_chains_form_one_publishable_stream() -> None:
    # tests/gie/samples.py documents both chains as valid and numbers the protective-stop events
    # 4..7 in the stream of the entry-order events 0..3 (same producer).  Within a stream the
    # ordering time must not decrease, so the chains must be journalable one after the other.
    events = [*samples.order_lifecycle(), *samples.protective_stop_lifecycle()]
    assert {stream_name(event) for event in events} == {"order_event/execution.sim"}
    assert [event.seq for event in events] == list(range(8))
    journal = _journal()
    assert [journal.publish(event) for event in events] == list(range(1, 9))


@pytest.mark.requirement("S00-ASG-25")
def test_widening_a_protective_stop_by_replacement_is_refused() -> None:
    stops = _stops()
    journal = _journal(*samples.order_lifecycle(), stops[0], stops[1])
    before = _state(journal)
    widened_request = dataclasses.replace(stops[2], requested_trigger_price=D("2647.00"))
    with pytest.raises(IllegalTransitionError) as excinfo:
        journal.publish(widened_request)
    assert excinfo.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert excinfo.value.details["client_order_id"] == stops[0].client_order_id
    assert _state(journal) == before
    # The tightening request is accepted, but a report that applies a wider stop than requested is not.
    assert journal.publish(stops[2]) == 7
    with pytest.raises(IllegalTransitionError):
        journal.publish(dataclasses.replace(stops[3], trigger_price=D("2647.00")))
    assert journal.publish(stops[3]) == 8
    assert journal.ledger.order(stops[0].client_order_id) == stops[3]


@pytest.mark.requirement("S00-ASG-25")
def test_a_position_snapshot_with_a_widened_stop_is_refused() -> None:
    positions = samples.position_lifecycle()
    journal = _journal(positions[0], positions[1])
    before = _state(journal)
    later = T0 + 120 * SEC
    widened = dataclasses.replace(
        positions[1],
        revision=2,
        seq=2,
        decision_time_ns=later,
        as_of_time_ns=later - MS,
        stop_price=D("2648.50"),  # wider than 2649.90, still tighter than the initial 2647.80
    )
    with pytest.raises(IllegalTransitionError) as excinfo:
        journal.publish(widened)
    assert excinfo.value.details["position_id"] == POSITION_ID
    assert _state(journal) == before
    kept = dataclasses.replace(widened, stop_price=D("2649.90"))
    assert journal.publish(kept) == 3


@pytest.mark.requirement("S00-ASG-25")
def test_an_order_event_after_a_terminal_state_is_refused() -> None:
    submit = samples.order_event()
    rejected = samples.variants()["OrderEvent"]["entry_rejected"]
    journal = _journal(submit, rejected)
    before = _state(journal)
    accepted_late = samples.order_event(
        seq=2,
        order_seq=2,
        event_type=OrderEventType.ACCEPTED,
        order_state=OrderState.WORKING,
        broker_order_id=identity.broker_order_id(submit.client_order_id),
        event_time_ns=T0 + 70 * SEC,
        receive_time_ns=T0 + 70 * SEC,
    )
    with pytest.raises(IllegalTransitionError):
        journal.publish(accepted_late)
    assert _state(journal) == before
    filled = _full_journal()
    with pytest.raises(IllegalTransitionError):
        filled.publish(_late_fill_report())


@pytest.mark.requirement("S00-ASG-25", "G07-09")
def test_a_model_cannot_skip_shadow_validation_in_a_journal() -> None:
    research, shadow, active = samples.model_lifecycle()
    journal = _journal(research)
    with pytest.raises(IllegalTransitionError):
        journal.publish(dataclasses.replace(active, seq=1))
    assert len(journal) == 2
    assert journal.publish(shadow) == 2
    assert journal.publish(active) == 3


def _registry(
    model: dict[str, Any], seq: int, status: ModelStatus, revision: int, **extra: Any
) -> ModelVersion:
    fields: dict[str, Any] = {
        "seq": seq,
        "revision": revision,
        "status": status,
        "status_time_ns": T0 - 3600 * SEC + seq * SEC,
    }
    if status is ModelStatus.ACTIVE:
        active = samples.model_lifecycle()[2]
        fields.update(
            evaluation_report_uri=active.evaluation_report_uri,
            evaluation_report_sha256=active.evaluation_report_sha256,
            promotion_criteria_sha256=active.promotion_criteria_sha256,
            shadow_run_ids=active.shadow_run_ids,
            approved_by=samples.FIXTURE_APPROVER,
            approval_reference=samples.FIXTURE_DECISION,
        )
    if status in (ModelStatus.RETIRED, ModelStatus.ROLLED_BACK):
        fields["status_reasons"] = ("FIXTURE_REASON",)
    if status is ModelStatus.ROLLED_BACK:
        fields.update(approved_by=samples.FIXTURE_APPROVER, approval_reference="D-998")
    fields.update(model)
    fields.update(extra)
    return samples.model_version(**fields)


MODEL_A: dict[str, Any] = {"artifact_sha256": "aa" * 32, "semver": "0.1.0"}
MODEL_B: dict[str, Any] = {"artifact_sha256": "bb" * 32, "semver": "0.2.0"}
MODEL_A_ID = identity.model_version_id(samples.MODEL_NAME, "0.1.0", "aa" * 32)
MODEL_B_ID = identity.model_version_id(samples.MODEL_NAME, "0.2.0", "bb" * 32)


@pytest.mark.requirement("S00-ASG-25", "G07-10")
def test_a_second_active_version_of_a_model_family_is_refused_by_the_journal() -> None:
    # Section 9.2: "one ACTIVE version per model family" is a sequence rule "enforced by the
    # LifecycleLedger, through which every journal ... admit[s] records".
    journal = _journal(
        _registry(MODEL_A, 0, ModelStatus.RESEARCH, 0),
        _registry(MODEL_A, 1, ModelStatus.SHADOW, 1),
        _registry(MODEL_A, 2, ModelStatus.ACTIVE, 2),
        _registry(MODEL_B, 3, ModelStatus.RESEARCH, 0),
        _registry(MODEL_B, 4, ModelStatus.SHADOW, 1),
    )
    second_active = _registry(MODEL_B, 5, ModelStatus.ACTIVE, 2, replaces_model_version_id=MODEL_A_ID)
    with pytest.raises(IllegalTransitionError):
        journal.publish(second_active)
    assert len(journal) == 6


@pytest.mark.requirement("S00-ASG-25", "G07-10", "G07-11")
def test_a_reactivation_with_a_used_approval_is_refused_by_the_journal() -> None:
    # Section 9.2: "a re-activation names the rolled-back version and needs a new approval".
    journal = _journal(
        _registry(MODEL_A, 0, ModelStatus.RESEARCH, 0),
        _registry(MODEL_A, 1, ModelStatus.SHADOW, 1),
        _registry(MODEL_A, 2, ModelStatus.ACTIVE, 2),
        _registry(MODEL_B, 3, ModelStatus.RESEARCH, 0),
        _registry(MODEL_B, 4, ModelStatus.SHADOW, 1),
        _registry(MODEL_A, 5, ModelStatus.RETIRED, 3),
        _registry(MODEL_B, 6, ModelStatus.ACTIVE, 2, replaces_model_version_id=MODEL_A_ID),
        _registry(MODEL_B, 7, ModelStatus.ROLLED_BACK, 3),
    )
    links = {"replaces_model_version_id": MODEL_B_ID, "rollback_of_model_version_id": MODEL_B_ID}
    reused = _registry(MODEL_A, 8, ModelStatus.ACTIVE, 4, **links)
    assert reused.approval_reference == samples.FIXTURE_DECISION  # the reference of the first activation
    with pytest.raises(IllegalTransitionError):
        journal.publish(reused)
    assert (
        journal.publish(_registry(MODEL_A, 8, ModelStatus.ACTIVE, 4, approval_reference="D-998", **links))
        == 9
    )


# =========================================================================== look-ahead guard
@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
@pytest.mark.parametrize(
    "build",
    [
        lambda at: _other_evidence(0, at=at, as_of_time_ns=at),
        lambda at: samples.candidate(decision_time_ns=at, as_of_time_ns=at),
        lambda at: samples.trade_plan(decision_time_ns=at, as_of_time_ns=at),
        lambda at: samples.risk_decision(decision_time_ns=at, as_of_time_ns=at),
        lambda at: samples.position(decision_time_ns=at, as_of_time_ns=at),
    ],
    ids=["evidence", "candidate", "trade-plan", "risk-decision", "position"],
)
def test_a_derived_record_beyond_the_newest_journaled_tick_is_look_ahead(
    build: Callable[[int], Record],
) -> None:
    watermark = T0 + 10 * SEC
    journal = _journal(_tick_at(0, watermark))
    before = _state(journal)
    with pytest.raises(FutureDataError) as excinfo:
        journal.publish(build(watermark + 1))
    assert excinfo.value.code == "GIE.TIME.FUTURE"
    assert excinfo.value.details["as_of_time_ns"] == watermark + 1
    assert excinfo.value.details["tick_watermark_ns"] == watermark
    assert _state(journal) == before
    # At the watermark itself the data has been delivered: accepted.
    assert journal.publish(build(watermark)) == 2


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
def test_the_record_is_accepted_once_the_tick_it_depends_on_was_journaled() -> None:
    journal = _journal(samples.tick())
    position = samples.position()  # watermark T0 + 5 s - 1 ms, the sample tick was received at T0 - 1 ms
    with pytest.raises(FutureDataError):
        journal.publish(position)
    assert journal.publish(_tick_at(1, position.as_of_time_ns - 1)) == 2
    with pytest.raises(FutureDataError):
        journal.publish(position)
    assert journal.publish(_tick_at(2, position.as_of_time_ns)) == 3
    assert journal.publish(position) == 4


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
def test_the_watermark_is_the_receive_time_never_the_source_event_time() -> None:
    # A tick whose source timestamp (T0) lies after its receive time (T0 - 1 ms).
    tick = samples.variants()["Tick"]["event_after_receive"]
    assert isinstance(tick, Tick)
    assert tick.event_time_ns > tick.receive_time_ns
    journal = _journal(tick)
    with pytest.raises(FutureDataError):
        journal.publish(_other_evidence(0, at=tick.event_time_ns, as_of_time_ns=tick.event_time_ns))
    assert journal.publish(_other_evidence(0, at=tick.event_time_ns, as_of_time_ns=tick.receive_time_ns)) == 2


@pytest.mark.requirement("S10-ACC-02")
def test_the_watermark_is_the_newest_tick_of_any_stream_and_never_moves_back() -> None:
    silver = samples.tick(symbol="XAGUSD", bid=D("31.115"), ask=D("31.14"), **_times(T0 + 10 * SEC))
    journal = _journal(silver, _tick_at(0, T0))  # the gold tick is older than the silver tick
    assert journal.publish(_other_evidence(0, at=T0 + 10 * SEC, as_of_time_ns=T0 + 10 * SEC)) == 3
    with pytest.raises(FutureDataError):
        journal.publish(_other_evidence(1, at=T0 + 10 * SEC + 1, as_of_time_ns=T0 + 10 * SEC + 1))


def _times(receive_time_ns: int) -> dict[str, int]:
    return {"event_time_ns": receive_time_ns - 2 * MS, "receive_time_ns": receive_time_ns}


@pytest.mark.requirement("S10-ACC-02")
def test_records_that_are_not_derived_from_market_data_are_not_look_ahead() -> None:
    journal = _journal(samples.tick())
    # Broker reports, clock readings and registry entries carry no market-data watermark.
    for record in (*samples.order_lifecycle(), samples.clock_reading(wall_time_ns=T0 + 900 * SEC)):
        assert ordering_time_ns(record) > samples.tick().receive_time_ns
        journal.publish(record)
    assert len(journal) == 7


# =========================================================================== reading
@pytest.mark.requirement("S00-ASG-25")
def test_read_returns_the_entries_in_journal_order() -> None:
    records = _run_records()
    journal = _full_journal()
    entries = list(journal.read())
    assert len(entries) == len(journal) == len(records) == 24
    for number, (entry, record) in enumerate(zip(entries, records, strict=True)):
        assert type(entry) is JournalEntry
        assert entry.journal_seq == number
        assert entry.record == record
        assert entry.stream == stream_name(record)
        assert entry.fingerprint == fingerprint(record)
    assert list(journal.read(0)) == entries


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("start", [0, 1, 7, 23, 24, 25, 10_000])
def test_read_from_a_journal_position(start: int) -> None:
    journal = _full_journal()
    entries = list(journal.read(start))
    assert [entry.journal_seq for entry in entries] == list(range(start, 24))
    assert entries == list(journal.read())[start:]


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("start", [-1, -24, True, False, 1.0, "1", None])
def test_read_refuses_an_invalid_position(start: object) -> None:
    journal = _full_journal()
    with pytest.raises(ContractValidationError):
        list(journal.read(start))  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-25")
def test_streams_lists_every_stream_once_in_sorted_order() -> None:
    journal = _full_journal()
    assert journal.streams() == (
        "candidate/prediction.scorer",
        "clock/core.clock",
        "evidence/strategy_intelligence.sweep_reversal",
        "model_version/learning.registry",
        "order_event/execution.sim",
        f"order_event/{STOP_STREAM}",
        "position/execution.sim",
        "risk_decision/risk.gate",
        "run_manifest/app.run",
        "tick/fixture/XAUUSD",
        "trade_plan/decision.planner",
    )
    assert set(journal.streams()) == {entry.stream for entry in journal.read()}


@pytest.mark.requirement("S00-ASG-25")
def test_journal_entry_wire_form_and_canonical_lines() -> None:
    journal = _full_journal()
    lines = journal.canonical_lines()
    assert len(lines) == 24
    for entry, line in zip(journal.read(), lines, strict=True):
        wire = entry.to_wire()
        assert set(wire) == {"journal_seq", "stream", "fingerprint", "record"}
        assert wire["record"] == to_wire(entry.record)
        assert line == canonical_dumps(wire)
        assert "\n" not in line
        assert strict_loads(line) == strict_loads(canonical_dumps(wire))
        assert from_wire(strict_loads(line)["record"]) == entry.record
    entry = next(iter(journal.read()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.journal_seq = 5  # type: ignore[misc]


# =========================================================================== replay determinism
@pytest.mark.requirement("S00-ASG-25", "S10-ACC-01")
def test_the_same_records_published_into_two_journals_give_identical_canonical_lines() -> None:
    first, second = _full_journal(), _full_journal()
    assert first.canonical_lines() == second.canonical_lines()
    assert len(first.canonical_lines()) == 24
    assert [entry.fingerprint for entry in first.read()] == [entry.fingerprint for entry in second.read()]
    # Re-sent duplicates during one of the runs do not change the journal either.
    third = MemoryJournal()
    for record in _run_records():
        third.publish(record)
        third.publish(record)
    assert third.canonical_lines() == first.canonical_lines()


@pytest.mark.requirement("S00-ASG-25", "S10-ACC-01")
def test_records_decoded_from_canonical_bytes_reproduce_the_journal_byte_for_byte() -> None:
    original = _full_journal()
    replayed = MemoryJournal()
    for line in original.canonical_lines():
        replayed.publish(from_wire(strict_loads(line)["record"]))
    assert replayed.canonical_lines() == original.canonical_lines()
    joined = "\n".join(original.canonical_lines()).encode("utf-8")
    assert joined == "\n".join(replayed.canonical_lines()).encode("utf-8")


@pytest.mark.requirement("S00-ASG-25", "S10-ACC-01")
def test_every_sample_variant_has_a_stable_canonical_journal_line() -> None:
    def lines() -> list[str]:
        return [
            canonical_dumps(JournalEntry(0, stream_name(record), fingerprint(record), record).to_wire())
            for group in samples.variants().values()
            for record in group.values()
        ]

    assert lines() == lines()
    assert len(lines()) >= 45
    assert len(set(lines())) == len(lines())


@pytest.mark.requirement("S00-ASG-25", "S10-ACC-01")
def test_a_same_instant_decision_chain_keeps_its_causal_publication_order() -> None:
    chain: list[Record] = [
        samples.evidence(),
        samples.candidate(),
        samples.trade_plan(),
        samples.risk_decision(),
    ]
    # The normal case in replay: the whole chain shares one decision instant.
    assert {ordering_time_ns(record) for record in chain} == {T0}
    journal = _journal(samples.tick(), *chain)
    entries = list(journal.read(2))
    assert [type(entry.record) for entry in entries] == [Evidence, Candidate, TradePlan, RiskDecision]
    assert [entry.journal_seq for entry in entries] == [2, 3, 4, 5]
    assert [entry.record for entry in entries] == chain
    # Journal order is publication order - it is not re-sorted by stream name ...
    assert [entry.stream for entry in entries] != sorted(entry.stream for entry in entries)
    # ... and the EventKey fallback (ranks follow the data flow) orders the chain the same way.
    assert sorted(chain, key=ordering_key) == chain
    assert sorted([chain[2], chain[0], chain[3], chain[1]], key=ordering_key) == chain


@pytest.mark.requirement("S00-ASG-25", "S10-ACC-01")
def test_journal_order_is_publication_order_also_across_instants() -> None:
    # Published in causal order, the positions follow the publication, not the timestamps: the
    # manifest (created on the system clock, a day later than the replayed data) stays record 0.
    journal = _full_journal()
    records = [entry.record for entry in journal.read()]
    assert isinstance(records[0], RunManifest)
    assert ordering_time_ns(records[0]) > max(ordering_time_ns(record) for record in records[1:])
    assert [entry.journal_seq for entry in journal.read()] == list(range(24))
    order_of_types = [type(record).__name__ for record in records[5:10]]
    assert order_of_types == ["Tick", "Evidence", "Candidate", "TradePlan", "RiskDecision"]
    assert isinstance(records[9], RiskDecision)
    assert isinstance(records[15], Position)


# =========================================================================== FileJournal
def _write_run(path: Path, count: int | None = None, *, fsync: bool = True) -> list[Record]:
    records = _run_records()[:count]
    with FileJournal(path, fsync=fsync) as journal:
        for record in records:
            journal.publish(record)
    return records


def _lines(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data.endswith(b"\n")
    return data[:-1].split(b"\n")


def _rewrite(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def _refused_on_open(path: Path) -> JournalCorruptError:
    """Open a journal file that must be refused; never leaves a file handle behind."""
    try:
        journal = FileJournal(path)
    except JournalCorruptError as error:
        return error
    journal.close()
    pytest.fail(f"a damaged journal file was opened without an error ({len(journal)} entries)")


def _edited(line: bytes, edit: Callable[[dict[str, Any]], None]) -> bytes:
    """A journal line whose content was changed and written back in canonical form."""
    wire = strict_loads(line)
    edit(wire)
    return canonical_dumps(wire).encode("utf-8")


@pytest.mark.requirement("G08-04", "S00-ASG-25")
def test_a_file_journal_writes_one_canonical_json_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    assert not path.exists()
    records = _run_records()
    with FileJournal(path) as journal:
        assert journal.path == path
        assert path.exists()
        assert path.read_bytes() == b""
        for number, record in enumerate(records):
            assert journal.publish(record) == number
            # Durable before publish returns: the line is in the file already.
            assert path.read_bytes().count(b"\n") == number + 1
    data = path.read_bytes()
    assert data == "".join(line + "\n" for line in journal.canonical_lines()).encode("utf-8")
    for number, (line, record) in enumerate(zip(_lines(path), records, strict=True)):
        wire = strict_loads(line)
        assert set(wire) == {"journal_seq", "stream", "fingerprint", "record"}
        assert wire["journal_seq"] == number
        assert wire["stream"] == stream_name(record)
        assert wire["fingerprint"] == fingerprint(record)
        assert from_wire(wire["record"]) == record
        assert canonical_dumps(wire).encode("utf-8") == line
        assert b" " not in line.replace(b"SYNTHETIC FIXTURE", b"")


@pytest.mark.requirement("G08-04", "S10-ACC-01")
def test_file_and_memory_journal_of_the_same_run_are_byte_identical(tmp_path: Path) -> None:
    one, two = tmp_path / "one.journal", tmp_path / "two.journal"
    _write_run(one)
    _write_run(two, fsync=False)
    memory = _full_journal()
    expected = "".join(line + "\n" for line in memory.canonical_lines()).encode("utf-8")
    assert one.read_bytes() == expected
    assert two.read_bytes() == expected


@pytest.mark.requirement("G08-04", "G08-05")
def test_reopening_reproduces_the_entries_and_continues_the_sequence(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    records = _write_run(path, 12)
    written = path.read_bytes()
    reference = MemoryJournal()
    for record in records:
        reference.publish(record)
    with FileJournal(path) as reopened:
        assert path.read_bytes() == written  # opening does not rewrite anything
        assert _state(reopened) == _state(reference)
        assert reopened.run_id == samples.RUN_ID
        assert reopened.manifest == samples.run_manifest()
        for number, record in enumerate(_run_records()[12:], start=12):
            assert reopened.publish(record) == number
        assert _state(reopened) == _state(_full_journal())
    assert path.read_bytes().startswith(written)
    assert len(_lines(path)) == 24
    assert path.read_bytes() == "".join(line + "\n" for line in _full_journal().canonical_lines()).encode()


@pytest.mark.requirement("G08-04", "G08-05")
def test_a_restarted_journal_treats_a_resent_record_as_a_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    records = _write_run(path)
    written = path.read_bytes()
    with FileJournal(path) as restarted:
        for number, record in enumerate(records):
            assert restarted.publish(record) == number
        assert len(restarted) == len(records)
    assert path.read_bytes() == written


@pytest.mark.requirement("G08-05", "S00-ASG-25")
def test_a_restarted_journal_enforces_the_rules_of_the_whole_run(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    _write_run(path)
    with FileJournal(path) as plain:
        gaps_before = plain.tick_gaps
    with FileJournal(path) as restarted:
        assert restarted.tick_gaps == gaps_before == 0
        with pytest.raises(SequenceConflictError):
            restarted.publish(_tick_at(3, T0 + 300 * SEC - MS, bid=D("2650.11")))
        with pytest.raises(SequenceGapError):
            restarted.publish(_other_evidence(3, at=T0 + 100 * SEC))
        with pytest.raises(OutOfOrderError):
            restarted.publish(_tick_at(4, T0))
        with pytest.raises(IllegalTransitionError):
            restarted.publish(_late_fill_report())
        with pytest.raises(FutureDataError):
            restarted.publish(_future_evidence())
        with pytest.raises(ContractValidationError):
            restarted.publish(_other_evidence(1, run_id=OTHER_RUN_ID))
        with pytest.raises(SequenceConflictError):
            restarted.publish(samples.run_manifest(label="other"))
        assert restarted.publish(_tick_at(9, T0 + 400 * SEC)) == 24
        assert restarted.tick_gaps == 1
    with FileJournal(path) as again:
        assert len(again) == 25
        assert again.tick_gaps == 1


@pytest.mark.requirement("G08-04")
def test_the_first_record_of_a_file_journal_is_the_manifest_too(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    with FileJournal(path) as journal:
        with pytest.raises(ContractValidationError):
            journal.publish(samples.tick())
        assert path.read_bytes() == b""
        assert journal.publish(samples.run_manifest()) == 0
    assert len(_lines(path)) == 1


@pytest.mark.requirement("G08-04", "G08-05")
def test_an_existing_empty_file_is_an_empty_journal(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    path.write_bytes(b"")
    with FileJournal(path) as journal:
        assert len(journal) == 0
        assert journal.run_id is None
        assert journal.publish(samples.run_manifest()) == 0
    assert len(_lines(path)) == 1


# --------------------------------------------------------------------------- torn tail
@pytest.mark.requirement("G08-04", "G08-05", "G08-06")
@pytest.mark.parametrize("missing", [1, 2, 40, 400], ids=lambda n: f"last-{n}-bytes-missing")
def test_a_torn_last_line_is_refused_until_it_is_repaired_explicitly(tmp_path: Path, missing: int) -> None:
    path = tmp_path / "run.journal"
    records = _write_run(path)
    intact = path.read_bytes()
    complete_part = b"".join(line + b"\n" for line in _lines(path)[:-1])
    torn = intact[:-missing]
    assert torn.startswith(complete_part)
    assert len(torn) > len(complete_part)  # the cut lies inside the last line
    path.write_bytes(torn)

    for _ in range(2):  # opening never repairs anything by itself
        assert _refused_on_open(path).code == "GIE.JOURNAL.CORRUPT"
        assert path.read_bytes() == torn

    assert repair_torn_tail(path) == len(torn) - len(complete_part)
    assert path.read_bytes() == complete_part  # only the incomplete line is gone
    assert repair_torn_tail(path) == 0
    with FileJournal(path) as journal:
        assert len(journal) == len(records) - 1
        assert [entry.record for entry in journal.read()] == records[:-1]
        # The record on the torn line was never durably published: it is published again.
        assert journal.publish(records[-1]) == len(records) - 1
    assert path.read_bytes() == intact


@pytest.mark.requirement("G08-04", "G08-05")
def test_repair_never_touches_a_complete_journal(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    _write_run(path)
    intact = path.read_bytes()
    assert repair_torn_tail(path) == 0
    assert path.read_bytes() == intact
    empty = tmp_path / "empty.journal"
    empty.write_bytes(b"")
    assert repair_torn_tail(empty) == 0
    assert empty.read_bytes() == b""


@pytest.mark.requirement("G08-04", "G08-05")
def test_a_torn_manifest_line_leaves_an_empty_journal_after_repair(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    _write_run(path, 1)
    torn = path.read_bytes()[:-9]
    path.write_bytes(torn)
    _refused_on_open(path)
    assert repair_torn_tail(path) == len(torn)
    assert path.read_bytes() == b""
    with FileJournal(path) as journal:
        assert len(journal) == 0
        assert journal.publish(samples.run_manifest()) == 0


# --------------------------------------------------------------------------- modified files
def _set(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def edit(wire: dict[str, Any]) -> None:
        wire[key] = value

    return edit


def _set_in_record(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def edit(wire: dict[str, Any]) -> None:
        assert wire["record"][key] != value
        wire["record"][key] = value

    return edit


def _swap(first: int, second: int) -> Callable[[list[bytes]], list[bytes]]:
    def modify(lines: list[bytes]) -> list[bytes]:
        lines[first], lines[second] = lines[second], lines[first]
        return lines

    return modify


def _edit_line(number: int, edit: Callable[[dict[str, Any]], None]) -> Callable[[list[bytes]], list[bytes]]:
    def modify(lines: list[bytes]) -> list[bytes]:
        lines[number] = _edited(lines[number], edit)
        return lines

    return modify


def _replace_line(number: int, make: Callable[[bytes], bytes]) -> Callable[[list[bytes]], list[bytes]]:
    def modify(lines: list[bytes]) -> list[bytes]:
        lines[number] = make(lines[number])
        return lines

    return modify


#: Line numbers in the file of ``_run_records``: 5 = first tick, 6 = evidence, 9 = risk decision.
MODIFICATIONS: list[Any] = [
    pytest.param(_edit_line(5, _set_in_record("bid", "2650.11")), id="changed-price"),
    pytest.param(_edit_line(9, _set_in_record("volume_lots", "5")), id="changed-approved-volume"),
    pytest.param(_edit_line(6, _set_in_record("valid_until_ns", T0 + 3000 * SEC)), id="changed-expiry"),
    pytest.param(_edit_line(0, _set_in_record("seed", 8)), id="changed-manifest"),
    pytest.param(_edit_line(4, _set_in_record("approved_by", None)), id="removed-model-approval"),
    pytest.param(_swap(5, 6), id="swapped-neighbouring-lines"),
    pytest.param(_swap(1, 20), id="swapped-distant-lines"),
    pytest.param(_swap(0, 1), id="manifest-not-first"),
    pytest.param(_edit_line(7, _set("journal_seq", 8)), id="wrong-journal-seq"),
    pytest.param(_edit_line(23, _set("journal_seq", 99)), id="wrong-last-journal-seq"),
    pytest.param(_edit_line(7, _set("journal_seq", "7")), id="journal-seq-as-text"),
    pytest.param(_edit_line(7, _set("fingerprint", "0" * 64)), id="wrong-fingerprint"),
    pytest.param(_edit_line(0, _set("fingerprint", "f" * 64)), id="wrong-manifest-fingerprint"),
    pytest.param(_edit_line(7, _set("stream", "candidate/prediction.other")), id="wrong-stream"),
    pytest.param(_edit_line(7, _set("comment", "edited")), id="extra-key"),
    pytest.param(_edit_line(7, lambda wire: wire.pop("fingerprint")), id="missing-key"),
    pytest.param(_edit_line(7, lambda wire: wire["record"].pop("support_count")), id="missing-record-field"),
    pytest.param(_edit_line(6, _set_in_record("run_id", OTHER_RUN_ID)), id="record-of-another-run"),
    pytest.param(_edit_line(6, _set_in_record("contract", "Position")), id="record-of-another-contract"),
    pytest.param(_edit_line(6, _set_in_record("schema_version", "2.0.0")), id="record-of-another-major"),
    pytest.param(lambda lines: lines[:10] + lines[11:], id="deleted-line"),
    pytest.param(lambda lines: lines[1:], id="deleted-manifest"),
    pytest.param(lambda lines: [*lines[:10], lines[9], *lines[10:]], id="duplicated-line"),
    pytest.param(lambda lines: [*lines, lines[-1]], id="duplicated-last-line"),
    pytest.param(lambda lines: [*lines[:10], b"", *lines[10:]], id="blank-line"),
    pytest.param(lambda lines: [*lines, b""], id="trailing-blank-line"),
    pytest.param(
        _replace_line(4, lambda line: line.replace(b'":', b'": ', 1)), id="not-canonical-whitespace"
    ),
    pytest.param(
        _replace_line(
            4,
            lambda line: (
                b'{"stream"'
                + line.split(b',"stream"')[1][:-1]
                + b","
                + line[1:].split(b',"stream"')[0]
                + b"}"
            ),
        ),
        id="not-canonical-key-order",
    ),
    pytest.param(_replace_line(4, lambda line: line[:-1]), id="truncated-inner-line"),
    pytest.param(_replace_line(4, lambda line: b"not json"), id="not-json"),
    pytest.param(_replace_line(4, lambda line: b"[" + line + b"]"), id="line-is-a-list"),
    pytest.param(_replace_line(4, lambda line: b"\xff\xfe" + line), id="invalid-utf-8"),
    pytest.param(_replace_line(4, lambda line: line + line), id="two-objects-on-one-line"),
]


@pytest.mark.requirement("G08-04", "G08-05", "G08-06")
@pytest.mark.parametrize("modify", MODIFICATIONS)
def test_a_modified_journal_file_is_refused(
    tmp_path: Path, modify: Callable[[list[bytes]], list[bytes]]
) -> None:
    path = tmp_path / "run.journal"
    _write_run(path)
    intact = path.read_bytes()
    _rewrite(path, modify(_lines(path)))
    modified = path.read_bytes()
    assert modified != intact
    assert _refused_on_open(path).code == "GIE.JOURNAL.CORRUPT"
    assert path.read_bytes() == modified  # a refused file is left exactly as it was found
    # Not a torn tail: the explicit repair has nothing to remove and the file stays refused.
    assert repair_torn_tail(path) == 0
    _refused_on_open(path)


@pytest.mark.requirement("G08-04", "G08-06")
def test_any_single_changed_byte_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    _write_run(path, 12)
    intact = path.read_bytes()
    target = tmp_path / "changed.journal"
    checked = 0
    for position in range(0, len(intact), 97):
        for replacement in (b"0", b"x"):
            if intact[position : position + 1] == replacement:
                continue
            target.write_bytes(intact[:position] + replacement + intact[position + 1 :])
            _refused_on_open(target)
            checked += 1
    assert checked > 100


@pytest.mark.requirement("G08-04", "G08-06")
@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(lambda data: data.replace(b"\n", b"\r\n"), id="crlf-line-ends"),
        pytest.param(lambda data: data.replace(b"\n", b"\r", 1), id="one-terminator-replaced-by-cr"),
    ],
)
def test_changed_line_terminators_are_detected(tmp_path: Path, rewrite: Callable[[bytes], bytes]) -> None:
    # "One canonical JSON line per record": the file is the canonical lines, each ended by a line
    # feed.  A file whose terminators were rewritten is not the file that was written (and
    # repair_torn_tail, which looks for line feeds only, would no longer see the same lines), so it
    # must be refused like every other modification - input is never repaired or stripped.
    path = tmp_path / "run.journal"
    _write_run(path, 12)
    intact = path.read_bytes()
    path.write_bytes(rewrite(intact))
    assert path.read_bytes() != intact
    _refused_on_open(path)


# --------------------------------------------------------------------------- closing
@pytest.mark.requirement("G08-04")
def test_publishing_after_close_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    journal = FileJournal(path)
    journal.publish(samples.run_manifest())
    journal.close()
    journal.close()  # closing twice is harmless
    written = path.read_bytes()
    with pytest.raises(JournalCorruptError) as excinfo:
        journal.publish(samples.tick())
    assert excinfo.value.code == "GIE.JOURNAL.CORRUPT"
    assert path.read_bytes() == written
    with FileJournal(path) as reopened:
        assert len(reopened) == 1
        assert reopened.publish(samples.tick()) == 1


@pytest.mark.requirement("G08-04")
def test_leaving_the_context_closes_the_journal(tmp_path: Path) -> None:
    path = tmp_path / "run.journal"
    with FileJournal(path) as journal:
        assert journal.publish(samples.run_manifest()) == 0
    with pytest.raises(JournalCorruptError):
        journal.publish(samples.tick())
    assert len(_lines(path)) == 1


@pytest.mark.requirement("G08-04", "S00-ASG-25")
def test_a_publication_refused_by_a_closed_journal_leaves_it_unchanged(tmp_path: Path) -> None:
    # "A refused record leaves the journal unchanged" - and a record that was never written to the
    # file must not be reported as published when it is sent again.
    path = tmp_path / "run.journal"
    journal = FileJournal(path)
    journal.publish(samples.run_manifest())
    journal.close()
    before = _state(journal)
    with pytest.raises(JournalCorruptError):
        journal.publish(samples.tick())
    assert len(journal) == 1
    assert _state(journal) == before
    with pytest.raises(JournalCorruptError):
        journal.publish(samples.tick())
    assert len(_lines(path)) == 1
