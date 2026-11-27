"""Stream names and ordering keys of contract records (S00-ASG-25), part of acceptance test S00-ACC-02.

SYNTHETIC FIXTURE DATA ONLY.  All records come from the labelled synthetic builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); instants, prices and sequence numbers are
invented.  Nothing here is market data or evidence about latency or performance.

Expected behaviour: ``specifications/SHARED_CONTRACTS.md`` (revision 2) section 6 - the table of
section 6.1 (stream name, ordering time, rank per contract, two ranks for ``OrderEvent``) and the
rule of section 6.2 that the ranks follow the data flow so that the key is a causal fallback order.
Rework 2 (change notice sections 2 and 6, docstring of ``gie.contracts.ordering``): the ordering
time of a ``RunManifest`` is ``data_start_ns`` in a replay (which has no creation time) and
``created_time_ns`` in a paper run; a broker report is observed strictly later than the request it
answers.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

import pytest

from gie.contracts import (
    OrderEvent,
    OrderEventType,
    OrderState,
    Record,
    RunMode,
    Tick,
    all_contracts,
    identity,
    ordering_key,
    ordering_time_ns,
    stream_name,
    validate_order_transition,
)
from gie.contracts.ordering import STREAMS
from gie.core.errors import ContractValidationError, IllegalTransitionError, UnknownContractError
from gie.core.ordering import EventKey, StreamRank, merge_ordered
from tests.gie import samples
from tests.gie.samples import T0

pytestmark = [pytest.mark.acceptance("S00-ACC-02"), pytest.mark.requirement("S00-ASG-25")]

SPECIFICATION = Path(__file__).resolve().parents[3] / "specifications" / "SHARED_CONTRACTS.md"

#: SHARED_CONTRACTS.md section 6.1: contract -> (stream prefix, ordering-time field, rank).
EXPECTED = {
    "RunManifest": ("run_manifest", "data_start_ns | created_time_ns", 0),
    "Clock": ("clock", "wall_time_ns", 5),
    "Tick": ("tick", "receive_time_ns", 10),
    "OrderEvent": ("order_event", "receive_time_ns", 20),
    "Position": ("position", "decision_time_ns", 30),
    "Evidence": ("evidence", "decision_time_ns", 40),
    "Candidate": ("candidate", "decision_time_ns", 50),
    "TradePlan": ("trade_plan", "decision_time_ns", 60),
    "RiskDecision": ("risk_decision", "decision_time_ns", 70),
    "ModelVersion": ("model_version", "status_time_ns", 90),
}
ORDER_REQUEST_RANK = 80
#: Rework 2: the two alternative ordering-time fields of a RunManifest (replay | paper).
MANIFEST_TIME_FIELDS = ("data_start_ns", "created_time_ns")

#: Built once: records are immutable.
ALL_VARIANTS = samples.variants()
VARIANTS = [
    pytest.param(name, key, id=f"{name}:{key}") for name, group in ALL_VARIANTS.items() for key in group
]


def _variant(name: str, key: str) -> Any:
    return ALL_VARIANTS[name][key]


def _documented_time(name: str, record: Any) -> int:
    """The ordering time the table documents for *record*."""
    if name == "RunManifest":
        # A replay has a data period and no creation time; a paper run the reverse (RUN-2).
        field = "data_start_ns" if record.mode is RunMode.REPLAY else "created_time_ns"
        return int(getattr(record, field))
    return int(getattr(record, EXPECTED[name][1]))


def _order_event_of_type(event_type: OrderEventType) -> OrderEvent:
    """A valid synthetic order event of the given type (later than the sample submit request)."""
    if event_type is OrderEventType.SUBMIT_REQUESTED:
        return samples.order_event()
    broker = identity.broker_order_id(samples.order_event().client_order_id)
    later = {"seq": 5, "order_seq": 5, "event_time_ns": T0 + 10**9, "receive_time_ns": T0 + 10**9 + 1}
    shapes: dict[OrderEventType, dict[str, Any]] = {
        OrderEventType.ACCEPTED: {"order_state": OrderState.WORKING},
        OrderEventType.REJECTED: {"order_state": OrderState.REJECTED, "reason_code": "MARKET_CLOSED"},
        OrderEventType.PARTIALLY_FILLED: {},
        OrderEventType.FILLED: {},
        OrderEventType.CANCEL_REQUESTED: {"order_state": OrderState.PENDING_CANCEL},
        OrderEventType.CANCELED: {"order_state": OrderState.CANCELED},
        OrderEventType.CANCEL_REJECTED: {"order_state": OrderState.WORKING, "reason_code": "TOO_LATE"},
        OrderEventType.REPLACE_REQUESTED: {},
        OrderEventType.REPLACED: {},
        OrderEventType.REPLACE_REJECTED: {"order_state": OrderState.WORKING, "reason_code": "TOO_LATE"},
        OrderEventType.EXPIRED: {"order_state": OrderState.EXPIRED},
        OrderEventType.RECONCILED: {"order_state": OrderState.WORKING, "reason_code": "RESTART"},
    }
    from_samples = {
        OrderEventType.PARTIALLY_FILLED: samples.order_lifecycle()[2],
        OrderEventType.FILLED: samples.order_lifecycle()[3],
        OrderEventType.REPLACE_REQUESTED: samples.protective_stop_lifecycle()[2],
        OrderEventType.REPLACED: samples.protective_stop_lifecycle()[3],
    }
    if event_type in from_samples:
        return from_samples[event_type]
    return samples.order_event(event_type=event_type, broker_order_id=broker, **later, **shapes[event_type])


def _same_instant_records() -> dict[str, Record]:
    """One record per row of the section 6.1 table, all available at the instant T0.

    The records are independent samples (the broker report does not answer the request listed here).
    """
    plan = samples.trade_plan(decision_time_ns=T0, as_of_time_ns=T0)
    request = samples.order_event(event_time_ns=T0, receive_time_ns=T0)
    report = samples.order_event(
        seq=1,
        order_seq=1,
        event_type=OrderEventType.ACCEPTED,
        order_state=OrderState.WORKING,
        event_time_ns=T0,
        receive_time_ns=T0,
        broker_order_id=identity.broker_order_id(request.client_order_id),
    )
    return {
        "RunManifest": samples.run_manifest(data_start_ns=T0),  # a replay: ordered by its data start
        "Clock": samples.clock_reading(wall_time_ns=T0),
        "Tick": samples.tick(event_time_ns=T0, receive_time_ns=T0),
        "OrderEvent (broker report)": report,
        "Position": samples.position(decision_time_ns=T0, as_of_time_ns=T0, opened_time_ns=T0),
        "Evidence": samples.evidence(decision_time_ns=T0, as_of_time_ns=T0),
        "Candidate": samples.candidate(decision_time_ns=T0, as_of_time_ns=T0),
        "TradePlan": plan,
        "RiskDecision": samples.risk_decision(plan, decision_time_ns=T0, as_of_time_ns=T0),
        "OrderEvent (*_REQUESTED)": request,
        "ModelVersion": samples.model_version(status_time_ns=T0, created_time_ns=T0 - 1),
    }


# =========================================================================== the table of section 6.1
def test_expected_table_is_the_table_of_the_specification() -> None:
    text = SPECIFICATION.read_text(encoding="utf-8")
    section = text.split("### 6.1 Streams and sequence numbers", 1)[1].split("### 6.2", 1)[0]
    row = re.compile(r"^\| `(\w+)`(?: \(([^)]+)\))? \| `(\w+)/<(\w+)>(/<symbol>)?` \| ([^|]+?) \| (\d+) \|$")
    parsed: dict[str, tuple[str, str, int]] = {}
    request_rows = []
    manifest_time: set[str] = set()
    for line in section.splitlines():
        match = row.match(line)
        if match is None:
            continue
        contract, qualifier, prefix, scope, symbol_part, time_cell, rank = match.groups()
        assert (scope, symbol_part) == (("source", "/<symbol>") if contract == "Tick" else ("producer", None))
        time_fields = re.findall(r"`(\w+)`", time_cell)
        if contract == "RunManifest":
            manifest_time = set(time_fields)
            time_field = EXPECTED["RunManifest"][1]
        else:
            assert time_cell == f"`{time_fields[0]}`"
            time_field = time_fields[0]
        if qualifier == "`*_REQUESTED`":
            request_rows.append((contract, prefix, time_field, int(rank)))
            continue
        assert contract not in parsed
        parsed[contract] = (prefix, time_field, int(rank))
    assert parsed == EXPECTED
    # SHARED_CONTRACTS.md is being rewritten for rework 2: the manifest row names the paper-run field
    # (revision 2) or both fields of the rework-2 rule; it never names any other field.
    assert "created_time_ns" in manifest_time
    assert manifest_time <= set(MANIFEST_TIME_FIELDS)
    assert request_rows == [("OrderEvent", "order_event", "receive_time_ns", ORDER_REQUEST_RANK)]


def test_streams_table_covers_exactly_the_registered_contracts() -> None:
    assert set(STREAMS) == set(all_contracts().values())
    assert len(STREAMS) == 10
    assert {record_type.CONTRACT for record_type in STREAMS} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_streams_entry_equals_the_documented_row(name: str) -> None:
    prefix, time_field, rank = STREAMS[all_contracts()[name]]
    assert (prefix, time_field, int(rank)) == EXPECTED[name]
    assert isinstance(rank, StreamRank)
    fields = {spec.name for spec in all_contracts()[name].FIELD_SPECS}
    names = MANIFEST_TIME_FIELDS if name == "RunManifest" else (time_field,)
    assert set(names) <= fields


def test_stream_prefixes_and_ranks_are_unique() -> None:
    prefixes = [prefix for prefix, _time_field, _rank in STREAMS.values()]
    ranks = [int(rank) for _prefix, _time_field, rank in STREAMS.values()]
    assert len(set(prefixes)) == len(prefixes)
    assert len(set(ranks)) == len(ranks)
    assert ORDER_REQUEST_RANK not in ranks
    assert int(StreamRank.ORDER_REQUEST) == ORDER_REQUEST_RANK
    assert int(StreamRank.ORDER_REPORT) == EXPECTED["OrderEvent"][2]


def test_ranks_follow_the_data_flow() -> None:
    order = ["RunManifest", "Clock", "Tick", "OrderEvent", "Position", "Evidence", "Candidate", "TradePlan"]
    order += ["RiskDecision"]
    ranks = [EXPECTED[name][2] for name in order]
    assert ranks == sorted(ranks)
    assert ranks[-1] < ORDER_REQUEST_RANK < EXPECTED["ModelVersion"][2]


# =========================================================================== stream_name / ordering_time_ns
@pytest.mark.parametrize(("name", "key"), VARIANTS)
def test_stream_name_of_every_sample(name: str, key: str) -> None:
    record = _variant(name, key)
    prefix = EXPECTED[name][0]
    if name == "Tick":
        assert stream_name(record) == f"tick/{record.source}/{record.symbol}"
    else:
        assert stream_name(record) == f"{prefix}/{record.producer}"
    assert stream_name(record) == stream_name(record)


@pytest.mark.parametrize(("name", "key"), VARIANTS)
def test_ordering_time_of_every_sample_is_the_documented_field(name: str, key: str) -> None:
    record = _variant(name, key)
    assert ordering_time_ns(record) == _documented_time(name, record)
    assert isinstance(ordering_time_ns(record), int)


@pytest.mark.parametrize(("name", "key"), VARIANTS)
def test_ordering_key_of_every_sample(name: str, key: str) -> None:
    record = _variant(name, key)
    expected_rank = EXPECTED[name][2]
    if name == "OrderEvent" and record.event_type.value.endswith("_REQUESTED"):
        expected_rank = ORDER_REQUEST_RANK
    key_value = ordering_key(record)
    assert isinstance(key_value, EventKey)
    assert key_value == EventKey(
        _documented_time(name, record), expected_rank, stream_name(record), record.seq
    )
    assert (key_value.time_ns, int(key_value.rank), key_value.stream, key_value.seq) == (
        ordering_time_ns(record),
        expected_rank,
        stream_name(record),
        record.seq,
    )


def test_sample_variants_cover_every_contract() -> None:
    assert set(ALL_VARIANTS) == set(EXPECTED)
    assert len(VARIANTS) >= 40


def test_ticks_of_different_feeds_and_symbols_are_different_streams() -> None:
    gold = samples.tick()
    assert stream_name(gold) == "tick/fixture/XAUUSD"
    assert stream_name(samples.tick(symbol="XAGUSD", bid=samples.D("31.1"), ask=samples.D("31.2"))) == (
        "tick/fixture/XAGUSD"
    )
    assert stream_name(samples.tick(source="fixture-v2")) == "tick/fixture-v2/XAUUSD"
    assert stream_name(gold) == gold.stream


def test_records_of_different_producers_are_different_streams() -> None:
    first = samples.evidence()
    second = samples.variants()["Evidence"]["cavemen_breakout"]
    assert first.producer != second.producer
    assert stream_name(first) == "evidence/strategy_intelligence.sweep_reversal"
    assert stream_name(second) == "evidence/strategy_intelligence.cavemen"
    assert stream_name(samples.run_manifest()) == "run_manifest/app.run"
    assert stream_name(samples.clock_reading()) == "clock/core.clock"
    assert stream_name(samples.model_version()) == "model_version/learning.registry"
    assert stream_name(samples.risk_decision()) == "risk_decision/risk.gate"
    assert stream_name(samples.position()) == "position/execution.sim"
    assert stream_name(samples.trade_plan()) == "trade_plan/decision.planner"
    assert stream_name(samples.candidate()) == "candidate/prediction.scorer"


def test_requests_and_reports_of_an_order_share_one_stream() -> None:
    chain = samples.order_lifecycle()
    assert {stream_name(event) for event in chain} == {"order_event/execution.sim"}
    assert [event.seq for event in chain] == [0, 1, 2, 3]


def test_tick_is_ordered_by_receive_time_never_by_source_time() -> None:
    # Section 6.2: the availability time is the receive time - the rule that prevents look-ahead.
    late_source = samples.variants()["Tick"]["event_after_receive"]
    assert late_source.event_time_ns > late_source.receive_time_ns
    assert ordering_time_ns(late_source) == late_source.receive_time_ns
    assert ordering_key(late_source).time_ns == late_source.receive_time_ns
    typical = samples.tick()
    assert typical.event_time_ns < typical.receive_time_ns
    assert ordering_key(typical).time_ns == typical.receive_time_ns
    # An earlier source time does not move a tick in front of one that was received earlier.
    received_first = samples.tick(seq=0, event_time_ns=T0, receive_time_ns=T0 + 1)
    received_second = samples.tick(seq=1, event_time_ns=T0 - 10**9, receive_time_ns=T0 + 2)
    assert ordering_key(received_first) < ordering_key(received_second)


def test_order_event_is_ordered_by_receive_time_never_by_broker_time() -> None:
    accepted = samples.order_lifecycle()[1]
    assert accepted.event_time_ns < accepted.receive_time_ns
    assert ordering_time_ns(accepted) == accepted.receive_time_ns
    assert ordering_key(accepted).time_ns == accepted.receive_time_ns


# =========================================================================== the two ranks of OrderEvent
def test_exactly_three_order_event_types_are_requests() -> None:
    assert sorted(e.value for e in OrderEventType if e.value.endswith("_REQUESTED")) == [
        "CANCEL_REQUESTED",
        "REPLACE_REQUESTED",
        "SUBMIT_REQUESTED",
    ]
    assert len(OrderEventType) == 13


@pytest.mark.parametrize("event_type", list(OrderEventType), ids=lambda event_type: event_type.value)
def test_order_event_rank_is_80_for_requests_and_20_for_broker_reports(event_type: OrderEventType) -> None:
    event = _order_event_of_type(event_type)
    assert event.event_type is event_type
    expected = ORDER_REQUEST_RANK if event_type.value.endswith("_REQUESTED") else EXPECTED["OrderEvent"][2]
    assert int(ordering_key(event).rank) == expected
    assert event.is_request is event_type.value.endswith("_REQUESTED")
    assert ordering_key(event).stream == "order_event/execution.sim"
    assert ordering_key(event).time_ns == event.receive_time_ns
    assert ordering_key(event).seq == event.seq


# =========================================================================== causal order at one instant
def test_same_instant_decision_chain_sorts_causally() -> None:
    records = _same_instant_records()
    chain = [
        records[name]
        for name in ("Tick", "Evidence", "Candidate", "TradePlan", "RiskDecision", "OrderEvent (*_REQUESTED)")
    ]
    assert {ordering_time_ns(record) for record in chain} == {T0}
    for permutation in itertools.permutations(chain):
        assert sorted(permutation, key=ordering_key) == chain
    keys = [ordering_key(record) for record in chain]
    assert [int(key.rank) for key in keys] == [10, 40, 50, 60, 70, 80]
    assert all(earlier < later for earlier, later in itertools.pairwise(keys))


def test_all_record_kinds_at_one_instant_sort_in_the_order_of_the_table() -> None:
    records = _same_instant_records()
    names = list(records)
    assert {ordering_time_ns(record) for record in records.values()} == {T0}
    for arrangement in (names[::-1], names[5:] + names[:5], names[1::2] + names[0::2], sorted(names)):
        ordered = sorted(arrangement, key=lambda name: ordering_key(records[name]))
        assert ordered == names
    assert [int(ordering_key(records[name]).rank) for name in names] == [
        0,
        5,
        10,
        20,
        30,
        40,
        50,
        60,
        70,
        80,
        90,
    ]


def test_inputs_at_an_instant_sort_before_the_decisions_made_at_that_instant() -> None:
    records = _same_instant_records()
    inputs = ["RunManifest", "Clock", "Tick", "OrderEvent (broker report)", "Position"]
    decisions = ["Evidence", "Candidate", "TradePlan", "RiskDecision", "OrderEvent (*_REQUESTED)"]
    for exogenous in inputs:
        for decision in decisions:
            assert ordering_key(records[exogenous]) < ordering_key(records[decision])


def test_availability_time_dominates_the_rank() -> None:
    request_earlier = samples.order_event(event_time_ns=T0 - 1, receive_time_ns=T0 - 1)
    tick_later = samples.tick(event_time_ns=T0, receive_time_ns=T0)
    manifest_latest = samples.run_manifest(data_start_ns=T0 + 1)
    assert int(ordering_key(request_earlier).rank) == 80
    assert ordering_key(request_earlier) < ordering_key(tick_later) < ordering_key(manifest_latest)
    assert sorted([manifest_latest, tick_later, request_earlier], key=ordering_key) == [
        request_earlier,
        tick_later,
        manifest_latest,
    ]


def test_ties_of_time_and_rank_are_broken_by_stream_then_seq() -> None:
    gold_0 = samples.tick(seq=0, event_time_ns=T0, receive_time_ns=T0)
    gold_1 = samples.tick(seq=1, event_time_ns=T0, receive_time_ns=T0)
    silver_0 = samples.tick(
        symbol="XAGUSD",
        bid=samples.D("31.1"),
        ask=samples.D("31.2"),
        seq=0,
        event_time_ns=T0,
        receive_time_ns=T0,
    )
    silver_9 = samples.tick(
        symbol="XAGUSD",
        bid=samples.D("31.1"),
        ask=samples.D("31.2"),
        seq=9,
        event_time_ns=T0,
        receive_time_ns=T0,
    )
    expected = [silver_0, silver_9, gold_0, gold_1]  # "tick/fixture/XAGUSD" < "tick/fixture/XAUUSD"
    for permutation in itertools.permutations(expected):
        assert sorted(permutation, key=ordering_key) == expected
    assert len({ordering_key(record) for record in expected}) == 4


@pytest.mark.parametrize(
    "chain_name", ["order_lifecycle", "protective_stop_lifecycle", "position_lifecycle", "model_lifecycle"]
)
def test_sample_lifecycles_are_strictly_ascending_and_mergeable(chain_name: str) -> None:
    chain = getattr(samples, chain_name)()
    keys = [ordering_key(record) for record in chain]
    assert all(earlier < later for earlier, later in itertools.pairwise(keys))
    assert len({key.stream for key in keys}) == 1
    assert [key.seq for key in keys] == sorted(key.seq for key in keys)
    merged = list(merge_ordered([(ordering_key(record), record) for record in chain]))
    assert [record for _key, record in merged] == chain


def test_streams_of_one_run_merge_into_one_deterministic_order() -> None:
    orders = samples.order_lifecycle()
    positions = samples.position_lifecycle()
    models = samples.model_lifecycle()
    streams = [
        [(ordering_key(record), record) for record in orders],
        [(ordering_key(record), record) for record in positions],
        [(ordering_key(record), record) for record in models],
        [(ordering_key(samples.tick()), samples.tick())],
    ]
    first = [record for _key, record in merge_ordered(*streams)]
    second = [record for _key, record in merge_ordered(*reversed(streams))]
    assert first == second
    assert first == sorted([*orders, *positions, *models, samples.tick()], key=ordering_key)
    assert first[:3] == models  # registered long before the sample instant
    assert first[3] == samples.tick()


def test_sample_order_chains_form_one_stream_with_non_decreasing_ordering_time() -> None:
    # samples.order_lifecycle() (seq 0..3) and samples.protective_stop_lifecycle() (seq 4..7) are
    # numbered as consecutive records of the one stream "order_event/execution.sim".  Section 6.1:
    # within a stream the ordering time MUST be non-decreasing.  (The stop request that reacts to the
    # fill must not be available before the fill report was received.)
    stream = [*samples.order_lifecycle(), *samples.protective_stop_lifecycle()]
    assert {stream_name(event) for event in stream} == {"order_event/execution.sim"}
    assert [event.seq for event in stream] == list(range(8))
    times = [ordering_time_ns(event) for event in stream]
    assert times == sorted(times), "ordering time decreases between consecutive records of one stream"


def test_a_report_at_the_instant_of_its_request_is_refused_and_one_later_sorts_after_it() -> None:
    # Rework 2 (change notice section 6): a report is observed strictly later than the request it
    # answers, so within one order the ordering key (request rank 80, report rank 20) can never sort a
    # report in front of its request.  The same-instant pair that revision 2 allowed is now refused.
    request = samples.order_event(event_time_ns=T0, receive_time_ns=T0)
    broker = identity.broker_order_id(request.client_order_id)

    def report(receive_time_ns: int) -> OrderEvent:
        return samples.order_event(
            seq=1,
            order_seq=1,
            event_type=OrderEventType.ACCEPTED,
            order_state=OrderState.WORKING,
            event_time_ns=T0,
            receive_time_ns=receive_time_ns,
            broker_order_id=broker,
        )

    validate_order_transition(None, request)
    with pytest.raises(IllegalTransitionError) as caught:
        validate_order_transition(request, report(T0))
    assert caught.value.details["rule"] == "a report is observed strictly later than the request it follows"
    later = report(T0 + 1)  # one nanosecond later: the smallest legal step
    validate_order_transition(request, later)
    assert stream_name(request) == stream_name(later)
    assert ordering_key(request) < ordering_key(later)
    merged = list(merge_ordered([(ordering_key(record), record) for record in (request, later)]))
    assert [record for _key, record in merged] == [request, later]


def test_ordering_time_of_a_manifest_is_the_data_start_in_a_replay_and_the_creation_time_in_paper() -> None:
    replay = samples.run_manifest(data_start_ns=T0 - 5, data_end_ns=T0 + 5)
    assert replay.mode is RunMode.REPLAY
    assert replay.created_time_ns is None
    assert ordering_time_ns(replay) == T0 - 5
    assert ordering_key(replay) == EventKey(T0 - 5, StreamRank.RUN_MANIFEST, "run_manifest/app.run", 0)
    paper = _variant("RunManifest", "paper_primary")
    assert paper.mode is RunMode.PAPER
    assert paper.data_start_ns is None
    assert paper.created_time_ns is not None
    assert ordering_time_ns(paper) == paper.created_time_ns
    assert ordering_key(paper) == EventKey(
        paper.created_time_ns, StreamRank.RUN_MANIFEST, "run_manifest/app.run", 0
    )
    # A manifest that would need both or neither of the two times cannot exist (RUN-2).
    with pytest.raises(ContractValidationError) as caught:
        samples.run_manifest(created_time_ns=T0)
    assert caught.value.details["invariant"] == "RUN-2"
    with pytest.raises(ContractValidationError) as caught:
        samples.run_manifest(mode=RunMode.PAPER, created_time_ns=T0)  # keeps the data period
    assert caught.value.details["invariant"] == "RUN-2"


# =========================================================================== unknown record types
def _local_tick() -> Any:
    class LocalTick(Tick):  # a subclass is not the registered contract (only possible inside tests)
        __slots__ = ()

    genuine = samples.tick()
    return LocalTick(**{spec.name: getattr(genuine, spec.name) for spec in Tick.FIELD_SPECS})


UNKNOWN_RECORDS = [
    pytest.param(lambda: samples.evidence().levels[0], id="nested-PriceLevel"),
    pytest.param(lambda: samples.evidence().invalidation, id="nested-PriceCondition"),
    pytest.param(lambda: samples.trade_plan().targets[0], id="nested-Target"),
    pytest.param(lambda: samples.candidate().destinations[0], id="nested-Destination"),
    pytest.param(lambda: samples.model_version().contract_versions[0], id="nested-ContractRef"),
    pytest.param(_local_tick, id="subclass-of-Tick"),
    pytest.param(lambda: "tick/fixture/XAUUSD", id="str"),
    pytest.param(lambda: None, id="None"),
    pytest.param(lambda: {"contract": "Tick", "seq": 0}, id="wire-dict"),
    pytest.param(lambda: Tick, id="the-class-itself"),
    pytest.param(lambda: 7, id="int"),
]


@pytest.mark.parametrize("function", [stream_name, ordering_time_ns, ordering_key], ids=lambda f: f.__name__)
@pytest.mark.parametrize("build", UNKNOWN_RECORDS)
def test_unknown_record_types_have_no_stream(build: Any, function: Any) -> None:
    with pytest.raises(UnknownContractError) as caught:
        function(build())
    assert caught.value.code == "GIE.CONTRACT.UNKNOWN"
