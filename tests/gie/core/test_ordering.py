"""Independent tests for ``gie.core.ordering`` (requirement S00-ASG-25, "event ordering").

The expected behaviour is taken from the docstrings of ``gie/core/ordering.py`` and from
``specifications/SHARED_CONTRACTS.md`` section 6, not from the implementation:

* *input order*: the exogenous inputs of a run are delivered in ascending
  ``EventKey = (time_ns, rank, stream, seq)`` where ``time_ns`` is the availability instant;
  *journal order* (publication order) is the total order of a run, and ``EventKey`` is only the
  causal fallback for records of different journals;
* ``StreamRank`` breaks ties at the same instant, inputs first and then GIE's own records in
  data-flow order: run manifest, clock, tick, order report, position, evidence, candidate, trade
  plan, risk decision, order request, model version, other;
* ``merge_ordered`` merges sorted streams deterministically and refuses unsorted
  (``OutOfOrderError``) or ambiguous (``SequenceConflictError``) input;
* ``SequenceTracker`` classifies each arrival according to its documented table (a stream starts
  at ``first_seq``, default 0; ``None`` joins anywhere), refuses a decreasing ordering time, can be
  re-established after a restart with ``restore`` and never changes state on a refused arrival.

SYNTHETIC FIXTURE: every key, stream name, sequence number, payload and fingerprint in this
file is invented for testing.  Nothing here is market data or broker data.
"""

from __future__ import annotations

import itertools
import operator
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from gie.core.errors import (
    ContractValidationError,
    GieError,
    OutOfOrderError,
    SequenceConflictError,
    SequenceError,
    SequenceGapError,
)
from gie.core.ordering import EventKey, SeqOutcome, SequenceTracker, StreamRank, merge_ordered
from gie.core.timeutil import MAX_TIME_NS, NS_PER_SECOND

#: SYNTHETIC FIXTURE instant (2026-11-26 09:30:00 UTC), the same arbitrary instant as tests/gie/samples.py.
T0 = 1_795_685_400 * NS_PER_SECOND

#: Deterministic hypothesis runs: no random seed, no example database, no wall-clock deadline.
_PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=250)

#: SYNTHETIC FIXTURE fingerprints (stand-ins for SHA-256 content hashes).
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64

#: The tie-break order documented in SHARED_CONTRACTS.md section 6.1 and the ``StreamRank`` docstring:
#: inputs first, then GIE's own records in data-flow order.
DOCUMENTED_RANK_ORDER = [
    "RUN_MANIFEST",
    "CLOCK",
    "TICK",
    "ORDER_REPORT",
    "POSITION",
    "EVIDENCE",
    "CANDIDATE",
    "TRADE_PLAN",
    "RISK_DECISION",
    "ORDER_REQUEST",
    "MODEL_VERSION",
    "OTHER",
]

#: The rank numbers of the table in SHARED_CONTRACTS.md section 6.1 (``OTHER`` is not in that table).
DOCUMENTED_RANK_VALUES = {
    "RUN_MANIFEST": 0,
    "CLOCK": 5,
    "TICK": 10,
    "ORDER_REPORT": 20,
    "POSITION": 30,
    "EVIDENCE": 40,
    "CANDIDATE": 50,
    "TRADE_PLAN": 60,
    "RISK_DECISION": 70,
    "ORDER_REQUEST": 80,
    "MODEL_VERSION": 90,
}

#: Exogenous inputs of a run and the records GIE derives from them, each in data-flow order.
INPUT_RANKS = ["RUN_MANIFEST", "CLOCK", "TICK", "ORDER_REPORT", "POSITION"]
DECISION_CHAIN_RANKS = ["EVIDENCE", "CANDIDATE", "TRADE_PLAN", "RISK_DECISION", "ORDER_REQUEST"]

Pair = tuple[EventKey, Any]


def _as_tuple(key: EventKey) -> tuple[int, int, str, int]:
    """The documented order written out, so that references never rely on ``EventKey.__lt__``."""
    return (key.time_ns, int(key.rank), key.stream, key.seq)


def _merge(*streams: Iterable[Pair]) -> list[Pair]:
    return list(merge_ordered(*streams))


def _reference_merge(streams: Iterable[Iterable[Pair]]) -> list[Pair]:
    return sorted(itertools.chain.from_iterable(streams), key=lambda pair: _as_tuple(pair[0]))


# Small domains on purpose: many keys share a prefix, so every tie-break level is exercised.
_KEYS = st.builds(
    EventKey,
    time_ns=st.integers(0, 4),
    rank=st.sampled_from([0, 10, 20, 40, 90]),
    stream=st.sampled_from(["a", "b", "tick/fixture/XAUUSD", "tick/fixture/XAUUSD.x"]),
    seq=st.integers(0, 3),
)


@st.composite
def _sorted_streams(draw: st.DrawFn, min_streams: int = 0, min_keys: int = 0) -> list[list[Pair]]:
    """Up to five streams, each strictly ascending, with keys that are unique across all of them."""
    keys = draw(st.lists(_KEYS, unique=True, min_size=min_keys, max_size=40))
    count = draw(st.integers(max(min_streams, 0), 5))
    assume(count > 0 or not keys)
    owners = draw(st.lists(st.integers(0, max(count - 1, 0)), min_size=len(keys), max_size=len(keys)))
    streams: list[list[Pair]] = [[] for _ in range(count)]
    for payload, (key, owner) in enumerate(zip(keys, owners, strict=True)):
        streams[owner].append((key, {"payload": payload}))  # dicts cannot be ordered
    for stream in streams:
        stream.sort(key=lambda pair: _as_tuple(pair[0]))
    return streams


# =========================================================================== EventKey


@pytest.mark.requirement("S00-ASG-25")
def test_event_key_exposes_its_four_components() -> None:
    key = EventKey(T0, StreamRank.TICK, "tick/fixture/XAUUSD", 7)
    assert key.time_ns == T0
    assert key.rank == StreamRank.TICK
    assert key.stream == "tick/fixture/XAUUSD"
    assert key.seq == 7
    assert key == EventKey(time_ns=T0, rank=StreamRank.TICK, stream="tick/fixture/XAUUSD", seq=7)


@pytest.mark.requirement("S00-ASG-25")
def test_event_key_orders_by_time_then_rank_then_stream_then_seq() -> None:
    # Each pair differs first in the component under test; every later component points the other way.
    assert EventKey(1, 90, "z", 9) < EventKey(2, 0, "a", 0)  # time_ns decides first
    assert EventKey(5, 10, "z", 9) < EventKey(5, 20, "a", 0)  # then rank
    assert EventKey(5, 10, "a", 9) < EventKey(5, 10, "b", 0)  # then stream
    assert EventKey(5, 10, "a", 1) < EventKey(5, 10, "a", 2)  # then seq
    assert not EventKey(5, 10, "a", 2) < EventKey(5, 10, "a", 2)


@pytest.mark.requirement("S00-ASG-25")
def test_availability_time_dominates_every_other_component() -> None:
    # Ordering by availability instant is what prevents look-ahead: a model version that became
    # available earlier is processed before a clock reading that became available later.
    earlier = EventKey(T0, StreamRank.MODEL_VERSION, "zzz", 10**9)
    later = EventKey(T0 + 1, StreamRank.CLOCK, "aaa", 0)
    assert earlier < later
    assert sorted([later, earlier]) == [earlier, later]


@pytest.mark.requirement("S00-ASG-25")
@_PROPERTY
@given(left=_KEYS, right=_KEYS)
def test_event_key_comparisons_are_the_lexicographic_order_of_its_components(
    left: EventKey, right: EventKey
) -> None:
    for compare in (operator.lt, operator.le, operator.gt, operator.ge, operator.eq, operator.ne):
        assert compare(left, right) == compare(_as_tuple(left), _as_tuple(right))
    # Total order: exactly one of <, ==, > holds.
    assert [left < right, left == right, left > right].count(True) == 1


@pytest.mark.requirement("S00-ASG-25")
@_PROPERTY
@given(keys=st.lists(_KEYS, max_size=40))
def test_sorting_event_keys_gives_the_documented_total_order(keys: list[EventKey]) -> None:
    assert [_as_tuple(key) for key in sorted(keys)] == sorted(_as_tuple(key) for key in keys)


@pytest.mark.requirement("S00-ASG-25")
def test_equal_event_keys_are_equal_and_hash_alike() -> None:
    first = EventKey(T0, 10, "tick/fixture/XAUUSD", 3)
    second = EventKey(T0, 10, "tick/fixture/XAUUSD", 3)
    assert first == second
    assert hash(first) == hash(second)
    assert first <= second
    assert first >= second
    assert len({first, second}) == 1
    assert first != EventKey(T0, 10, "tick/fixture/XAUUSD", 4)
    assert first != EventKey(T0, 10, "tick/fixture/XAUUSD2", 3)
    assert first != EventKey(T0, 20, "tick/fixture/XAUUSD", 3)
    assert first != EventKey(T0 + 1, 10, "tick/fixture/XAUUSD", 3)


@pytest.mark.requirement("S00-ASG-25")
def test_event_key_is_immutable() -> None:
    key = EventKey(T0, 10, "tick/fixture/XAUUSD", 3)
    for name, value in (("time_ns", 0), ("rank", 0), ("stream", "other"), ("seq", 0)):
        with pytest.raises(AttributeError):
            setattr(key, name, value)
    assert key == EventKey(T0, 10, "tick/fixture/XAUUSD", 3)


@pytest.mark.requirement("S00-ASG-25")
def test_stream_names_compare_as_plain_strings() -> None:
    def key(stream: str) -> EventKey:
        return EventKey(T0, 10, stream, 0)

    assert key("tick/a/XAUUSD") < key("tick/b/XAUUSD")
    assert key("tick/a") < key("tick/a/XAUUSD")  # a prefix sorts first
    assert key("Tick/z") < key("tick/a")  # case sensitive, no locale
    assert key("order_event/execution.sim") < key("tick/fixture/XAUUSD")


@pytest.mark.requirement("S00-ASG-25")
def test_event_key_accepts_the_boundary_values() -> None:
    assert _as_tuple(EventKey(0, 0, "s", 0)) == (0, 0, "s", 0)
    largest = EventKey(MAX_TIME_NS, 90, "s", 2**63 - 1)
    assert largest.time_ns == MAX_TIME_NS
    assert largest.seq == 2**63 - 1
    assert EventKey(0, 0, "s", 0) < largest


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-21")
@pytest.mark.parametrize("component", ["time_ns", "rank", "seq"])
@pytest.mark.parametrize("value", [-1, -(10**18), True, False, 1.0, 0.5, "1", None, [1]])
def test_event_key_rejects_numeric_components_that_are_not_non_negative_ints(
    component: str, value: Any
) -> None:
    fields: dict[str, Any] = {"time_ns": T0, "rank": 10, "stream": "tick/fixture/XAUUSD", "seq": 0}
    fields[component] = value
    with pytest.raises(ContractValidationError):
        EventKey(**fields)


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("stream", ["", None, 1, b"tick/fixture/XAUUSD", ["tick"], ("tick",)])
def test_event_key_rejects_a_stream_that_is_not_a_non_empty_string(stream: Any) -> None:
    with pytest.raises(ContractValidationError):
        EventKey(T0, 10, stream, 0)


# =========================================================================== StreamRank


@pytest.mark.requirement("S00-ASG-25")
def test_stream_rank_order_is_the_documented_tie_break_order() -> None:
    ranks = [StreamRank[name] for name in DOCUMENTED_RANK_ORDER]
    for lower, higher in itertools.pairwise(ranks):
        assert lower < higher, f"{lower.name} must rank before {higher.name}"
    assert len({int(rank) for rank in ranks}) == len(DOCUMENTED_RANK_ORDER)


@pytest.mark.requirement("S00-ASG-25")
def test_every_stream_rank_is_a_valid_event_key_rank() -> None:
    assert len({int(rank) for rank in StreamRank}) == len(list(StreamRank))  # no two kinds tie
    for rank in StreamRank:
        key = EventKey(T0, rank, "s", 0)
        assert key.rank == int(rank)
        assert key == EventKey(T0, int(rank), "s", 0)


@pytest.mark.requirement("S00-ASG-25")
def test_events_of_the_same_instant_sort_by_rank_whatever_their_stream_or_seq() -> None:
    # Stream names and sequence numbers are chosen to point the opposite way.
    count = len(DOCUMENTED_RANK_ORDER)
    keys = [
        EventKey(T0, StreamRank[name], f"stream-{count - position:02d}", count - position)
        for position, name in enumerate(DOCUMENTED_RANK_ORDER)
    ]
    for shuffled in (list(reversed(keys)), keys[1::2] + keys[0::2], keys[4:] + keys[:4]):
        assert [StreamRank(key.rank).name for key in sorted(shuffled)] == DOCUMENTED_RANK_ORDER


@pytest.mark.requirement("S00-ASG-25")
def test_stream_rank_has_exactly_the_documented_members_in_data_flow_order() -> None:
    assert [rank.name for rank in sorted(StreamRank)] == DOCUMENTED_RANK_ORDER
    assert [rank.name for rank in StreamRank] == DOCUMENTED_RANK_ORDER  # declared in that order too
    assert set(StreamRank.__members__) == set(DOCUMENTED_RANK_ORDER)  # __members__ would reveal aliases
    # The names of the previous revision are gone: an order event is a report or a request.
    assert "ORDER_EVENT" not in StreamRank.__members__


@pytest.mark.requirement("S00-ASG-25")
def test_stream_rank_values_are_the_ranks_of_the_specification_table() -> None:
    assert {name: int(StreamRank[name]) for name in DOCUMENTED_RANK_VALUES} == DOCUMENTED_RANK_VALUES
    # OTHER has no row in the table; it sorts after every rank that has one.
    assert all(StreamRank.OTHER > StreamRank[name] for name in DOCUMENTED_RANK_VALUES)


@pytest.mark.requirement("S00-ASG-25")
def test_inputs_rank_before_the_records_gie_derives_at_the_same_instant() -> None:
    assert INPUT_RANKS + DECISION_CHAIN_RANKS == DOCUMENTED_RANK_ORDER[:10]
    for input_name, derived_name in itertools.product(INPUT_RANKS, DECISION_CHAIN_RANKS):
        # Stream names and sequence numbers point the other way; the rank decides.
        assert EventKey(T0, StreamRank[input_name], "zzz", 99) < EventKey(T0, StreamRank[derived_name], "a", 0)
    for rank in StreamRank:
        if rank is not StreamRank.RUN_MANIFEST:
            assert rank > StreamRank.RUN_MANIFEST  # the run manifest is the first record of a run
        if rank is not StreamRank.OTHER:
            assert rank < StreamRank.OTHER
    assert StreamRank.CLOCK < StreamRank.TICK  # a timer due at t fires before an input available at t
    assert StreamRank.TICK < StreamRank.ORDER_REPORT  # market data before the broker's reports ...
    assert StreamRank.ORDER_REPORT < StreamRank.POSITION  # ... and the position state they imply


@pytest.mark.requirement("S00-ASG-25")
def test_a_decision_chain_that_shares_one_instant_sorts_in_causal_order() -> None:
    # The normal case in replay: evidence, candidate, plan, approval and order request all carry the
    # same instant.  The fallback order must still be causal - each record after the one it was
    # derived from - although stream names and sequence numbers are chosen to point the other way.
    count = len(DECISION_CHAIN_RANKS)
    chain = [
        (EventKey(T0, StreamRank[name], f"stream-{count - position}", count - position), name)
        for position, name in enumerate(DECISION_CHAIN_RANKS)
    ]
    assert [name for _, name in sorted(chain, key=lambda pair: _as_tuple(pair[0]))] == DECISION_CHAIN_RANKS
    for arguments in itertools.permutations([[pair] for pair in chain]):
        assert [name for _, name in _merge(*arguments)] == DECISION_CHAIN_RANKS
    # An approval precedes the order it authorises; the broker's report of an order made at an
    # earlier instant is an input and precedes everything GIE derives at this instant.
    assert StreamRank.RISK_DECISION < StreamRank.ORDER_REQUEST
    assert StreamRank.ORDER_REPORT < StreamRank.EVIDENCE
    assert StreamRank.ORDER_REPORT != StreamRank.ORDER_REQUEST


# =========================================================================== merge_ordered


@pytest.mark.requirement("S00-ASG-25")
def test_merging_nothing_yields_nothing() -> None:
    assert _merge() == []
    assert _merge([]) == []
    assert _merge([], (), iter([])) == []


@pytest.mark.requirement("S00-ASG-25")
def test_a_single_sorted_stream_passes_through_unchanged() -> None:
    stream = [
        (EventKey(T0 + offset, 10, "tick/fixture/XAUUSD", offset), f"tick-{offset}") for offset in range(5)
    ]
    assert _merge(stream) == stream
    assert _merge([], stream, []) == stream


@pytest.mark.requirement("S00-ASG-25")
def test_merge_interleaves_streams_in_event_key_order() -> None:
    ticks = [
        (EventKey(T0 + 1, StreamRank.TICK, "tick/fixture/XAUUSD", 0), "tick-0"),
        (EventKey(T0 + 4, StreamRank.TICK, "tick/fixture/XAUUSD", 1), "tick-1"),
        (EventKey(T0 + 9, StreamRank.TICK, "tick/fixture/XAUUSD", 2), "tick-2"),
    ]
    orders = [
        (EventKey(T0 + 2, StreamRank.ORDER_REPORT, "order_event/execution.sim", 0), "submit"),
        (EventKey(T0 + 4, StreamRank.ORDER_REPORT, "order_event/execution.sim", 1), "fill"),
    ]
    decisions = [
        (EventKey(T0 + 4, StreamRank.RISK_DECISION, "risk_decision/risk.gate", 0), "decision"),
        (EventKey(T0 + 10, StreamRank.RISK_DECISION, "risk_decision/risk.gate", 1), "decision-2"),
    ]
    expected = ["tick-0", "submit", "tick-1", "fill", "decision", "tick-2", "decision-2"]
    for arguments in itertools.permutations([ticks, orders, decisions]):
        merged = _merge(*arguments)
        assert [item for _, item in merged] == expected
        assert [key for key, _ in merged] == sorted(key for key, _ in ticks + orders + decisions)


@pytest.mark.requirement("S00-ASG-25")
def test_merge_yields_the_original_key_and_item_objects() -> None:
    first_item, second_item = object(), object()  # payloads without any ordering
    first = (EventKey(T0, 10, "a", 0), first_item)
    second = (EventKey(T0, 10, "b", 0), second_item)
    merged = _merge([second], [first])
    assert len(merged) == 2
    assert merged[0][0] is first[0]
    assert merged[0][1] is first_item
    assert merged[1][0] is second[0]
    assert merged[1][1] is second_item


@pytest.mark.requirement("S00-ASG-25")
def test_merge_accepts_any_iterable_as_input() -> None:
    keys = [EventKey(T0 + offset, 10, "s", offset) for offset in range(6)]

    def generator() -> Iterator[Pair]:
        yield keys[0], "g0"
        yield keys[3], "g3"

    merged = _merge(generator(), ((keys[1], "t1"), (keys[4], "t4")), iter([(keys[2], "i2"), (keys[5], "i5")]))
    assert merged == [
        (keys[0], "g0"),
        (keys[1], "t1"),
        (keys[2], "i2"),
        (keys[3], "g3"),
        (keys[4], "t4"),
        (keys[5], "i5"),
    ]


@pytest.mark.requirement("S00-ASG-25")
def test_merge_of_many_streams_matches_a_plain_sort() -> None:
    # SYNTHETIC FIXTURE: 40 streams x 150 events with colliding instants and ranks.
    streams: list[list[Pair]] = []
    for number in range(40):
        name = f"stream-{(number * 7) % 40:02d}"
        rank = (StreamRank.TICK, StreamRank.ORDER_REPORT, StreamRank.EVIDENCE)[number % 3]
        streams.append(
            [
                (EventKey(T0 + (seq * (number % 5 + 1)) // 3, rank, name, seq), (number, seq))
                for seq in range(150)
            ]
        )
    merged = _merge(*streams)
    assert len(merged) == 40 * 150
    assert merged == _reference_merge(streams)
    assert merged == _merge(*reversed(streams))


@pytest.mark.requirement("S00-ASG-25")
@_PROPERTY
@given(streams=_sorted_streams())
def test_merge_equals_sorting_all_events_by_event_key(streams: list[list[Pair]]) -> None:
    merged = _merge(*streams)
    assert merged == _reference_merge(streams)
    keys = [_as_tuple(key) for key, _ in merged]
    assert all(earlier < later for earlier, later in itertools.pairwise(keys))  # strictly ascending


@pytest.mark.requirement("S00-ASG-25")
@_PROPERTY
@given(data=st.data(), streams=_sorted_streams(min_streams=1))
def test_merge_result_does_not_depend_on_the_argument_order(
    data: st.DataObject, streams: list[list[Pair]]
) -> None:
    reordered = data.draw(st.permutations(streams))
    assert _merge(*reordered) == _merge(*streams)


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_merge_refuses_an_input_that_is_not_sorted() -> None:
    low = (EventKey(T0, 10, "tick/fixture/XAUUSD", 0), "low")
    high = (EventKey(T0 + 5, 10, "tick/fixture/XAUUSD", 1), "high")
    other = (EventKey(T0 + 2, 20, "order_event/execution.sim", 0), "other")
    assert _merge([low, high], [other]) == [low, other, high]  # the sorted variant is fine
    with pytest.raises(OutOfOrderError):
        _merge([high, low])
    with pytest.raises(OutOfOrderError):
        _merge([high, low], [other])
    with pytest.raises(OutOfOrderError):
        _merge([other], [high, low])  # also when it is not the first argument


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (EventKey(5, 10, "a", 1), EventKey(4, 10, "a", 2)),  # time goes back
        (EventKey(5, 20, "a", 1), EventKey(5, 10, "a", 2)),  # rank goes back
        (EventKey(5, 10, "b", 1), EventKey(5, 10, "a", 2)),  # stream goes back
        (EventKey(5, 10, "a", 2), EventKey(5, 10, "a", 1)),  # seq goes back
    ],
)
def test_merge_refuses_a_regression_in_any_key_component(first: EventKey, second: EventKey) -> None:
    assert _merge([(second, "x"), (first, "y")]) == [(second, "x"), (first, "y")]
    with pytest.raises(OutOfOrderError):
        _merge([(first, "x"), (second, "y")])


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_merge_refuses_a_key_repeated_within_one_input() -> None:
    key = EventKey(T0, 10, "tick/fixture/XAUUSD", 3)
    before = (EventKey(T0 - 1, 10, "tick/fixture/XAUUSD", 2), "before")
    with pytest.raises(OutOfOrderError):
        _merge([(key, "same"), (key, "same")])  # identical content does not make it acceptable
    with pytest.raises(OutOfOrderError):
        _merge([before, (key, "one"), (key, "two")])
    with pytest.raises(OutOfOrderError):
        _merge([before], [(key, "one"), (EventKey(T0, 10, "tick/fixture/XAUUSD", 3), "two")])


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@_PROPERTY
@given(data=st.data(), streams=_sorted_streams(min_streams=1, min_keys=2))
def test_merge_refuses_every_input_with_two_events_swapped(
    data: st.DataObject, streams: list[list[Pair]]
) -> None:
    candidates = [index for index, stream in enumerate(streams) if len(stream) >= 2]
    assume(candidates)
    victim = streams[data.draw(st.sampled_from(candidates))]
    left = data.draw(st.integers(0, len(victim) - 2))
    right = data.draw(st.integers(left + 1, len(victim) - 1))
    victim[left], victim[right] = victim[right], victim[left]
    with pytest.raises(OutOfOrderError):
        _merge(*streams)


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_merge_refuses_the_same_key_from_two_inputs() -> None:
    shared = EventKey(T0 + 5, 10, "tick/fixture/XAUUSD", 5)
    earlier = (EventKey(T0, 10, "tick/fixture/XAUUSD", 0), "earlier")
    later = (EventKey(T0 + 9, 10, "tick/fixture/XAUUSD", 9), "later")
    with pytest.raises(SequenceConflictError):
        _merge([(shared, "one")], [(shared, "two")])
    with pytest.raises(SequenceConflictError):
        _merge([(shared, "same")], [(shared, "same")])  # even with identical content
    with pytest.raises(SequenceConflictError):
        _merge([earlier, (shared, "one")], [(shared, "two"), later])
    with pytest.raises(SequenceConflictError):
        _merge([earlier], [(shared, "one"), later], [(EventKey(T0 + 5, 10, "tick/fixture/XAUUSD", 5), "two")])
    with pytest.raises(SequenceConflictError):
        _merge([earlier, later], [(shared, "one")], [later])  # conflict on the very last key


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@_PROPERTY
@given(data=st.data(), streams=_sorted_streams(min_streams=2, min_keys=1))
def test_merge_refuses_every_key_that_two_inputs_share(
    data: st.DataObject, streams: list[list[Pair]]
) -> None:
    sources = [index for index, stream in enumerate(streams) if stream]
    source = data.draw(st.sampled_from(sources))
    target = data.draw(st.sampled_from([index for index in range(len(streams)) if index != source]))
    key, _ = data.draw(st.sampled_from(streams[source]))
    streams[target].append((key, "copy"))
    streams[target].sort(key=lambda pair: _as_tuple(pair[0]))
    with pytest.raises(SequenceConflictError):
        _merge(*streams)


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_merge_refusals_are_gie_sequence_errors_with_stable_codes() -> None:
    key = EventKey(T0, 10, "tick/fixture/XAUUSD", 0)
    with pytest.raises(SequenceError) as unsorted:
        _merge([(EventKey(T0 + 1, 10, "tick/fixture/XAUUSD", 1), "x"), (key, "y")])
    assert isinstance(unsorted.value, GieError)
    assert unsorted.value.code == "GIE.SEQ.OUT_OF_ORDER"
    with pytest.raises(SequenceError) as ambiguous:
        _merge([(key, "x")], [(key, "y")])
    assert ambiguous.value.code == "GIE.SEQ.CONFLICT"


# =========================================================================== SequenceTracker: table rows


@pytest.mark.requirement("S00-ASG-25")
def test_a_new_tracker_knows_no_stream() -> None:
    tracker = SequenceTracker()
    assert tracker.last_seq("tick/fixture/XAUUSD") is None
    assert tracker.last_seq("") is None


@pytest.mark.requirement("S00-ASG-25")
def test_seq_outcome_vocabulary() -> None:
    assert {outcome.name: outcome.value for outcome in SeqOutcome} == {
        "ACCEPTED": "ACCEPTED",
        "DUPLICATE": "DUPLICATE",
        "ACCEPTED_AFTER_GAP": "ACCEPTED_AFTER_GAP",
    }


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("allow_gaps", [False, True])
@pytest.mark.parametrize("seq", [0, 1, 7, 10**12])
def test_the_first_event_of_a_stream_is_accepted_when_no_first_seq_is_required(
    seq: int, allow_gaps: bool
) -> None:
    # ``first_seq=None`` is the documented way to join a stream in the middle.
    tracker = SequenceTracker(allow_gaps=allow_gaps, first_seq=None)
    result = tracker.observe("s", seq, FP_A)
    assert result.outcome is SeqOutcome.ACCEPTED
    assert result.missing == 0
    assert tracker.last_seq("s") == seq


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("allow_gaps", [False, True])
@pytest.mark.parametrize("first_seq", [0, 1, 5])
def test_the_first_event_is_accepted_when_it_equals_first_seq(first_seq: int, allow_gaps: bool) -> None:
    tracker = SequenceTracker(first_seq=first_seq, allow_gaps=allow_gaps)
    result = tracker.observe("s", first_seq, FP_A)
    assert result.outcome is SeqOutcome.ACCEPTED
    assert result.missing == 0
    assert tracker.last_seq("s") == first_seq
    assert tracker.observe("s", first_seq + 1, FP_B).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@pytest.mark.parametrize("allow_gaps", [False, True])
@pytest.mark.parametrize(("first_seq", "seq"), [(0, 1), (0, 2), (0, 10**6), (5, 0), (5, 4), (5, 6), (1, 0)])
def test_a_first_event_that_is_not_first_seq_is_refused_without_changing_state(
    first_seq: int, seq: int, allow_gaps: bool
) -> None:
    tracker = SequenceTracker(first_seq=first_seq, allow_gaps=allow_gaps)
    for _ in range(2):
        with pytest.raises(SequenceError):
            tracker.observe("s", seq, FP_A)
        assert tracker.last_seq("s") is None
    # The stream is still "new": its correct first event is accepted ...
    assert tracker.observe("s", first_seq, FP_B).outcome is SeqOutcome.ACCEPTED
    assert tracker.last_seq("s") == first_seq
    # ... and the refused arrival was not remembered as content of any position.
    assert tracker.observe("s", first_seq, FP_B).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", first_seq + 1, FP_C).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25")
def test_first_seq_applies_to_each_stream_separately() -> None:
    tracker = SequenceTracker(first_seq=0)
    assert tracker.observe("a", 0, FP_A).outcome is SeqOutcome.ACCEPTED
    assert tracker.observe("a", 1, FP_A).outcome is SeqOutcome.ACCEPTED
    with pytest.raises(SequenceError):
        tracker.observe("b", 2, FP_A)  # stream b has not started; a's position is irrelevant
    assert tracker.last_seq("b") is None
    assert tracker.observe("b", 0, FP_A).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25")
def test_the_next_sequence_number_is_accepted() -> None:
    tracker = SequenceTracker()
    for seq in range(200):
        result = tracker.observe("tick/fixture/XAUUSD", seq, f"{seq:064x}")
        assert result.outcome is SeqOutcome.ACCEPTED
        assert result.missing == 0
        assert tracker.last_seq("tick/fixture/XAUUSD") == seq


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@pytest.mark.parametrize("missing", [1, 2, 100, 10**9])
def test_a_gap_is_refused_when_gaps_are_not_allowed(missing: int) -> None:
    tracker = SequenceTracker(first_seq=3)
    for seq in (3, 4, 5):
        tracker.observe("s", seq, f"fp-{seq}")
    late = 5 + missing + 1
    with pytest.raises(SequenceGapError) as caught:
        tracker.observe("s", late, FP_A)
    assert caught.value.details["missing"] == missing
    assert caught.value.code == "GIE.SEQ.GAP"
    # Nothing changed: the tracker still waits for 6 ...
    assert tracker.last_seq("s") == 5
    assert tracker.observe("s", 5, "fp-5").outcome is SeqOutcome.DUPLICATE
    with pytest.raises(SequenceGapError):
        tracker.observe("s", late, FP_A)
    assert tracker.observe("s", 6, "fp-6").outcome is SeqOutcome.ACCEPTED
    assert tracker.last_seq("s") == 6


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_a_refused_gap_arrival_is_not_remembered_as_content() -> None:
    tracker = SequenceTracker()
    tracker.observe("s", 0, FP_A)
    with pytest.raises(SequenceGapError):
        tracker.observe("s", 2, FP_B)
    assert tracker.observe("s", 1, FP_A).outcome is SeqOutcome.ACCEPTED
    # Position 2 was refused before, so its real arrival - with other content - is a plain accept.
    assert tracker.observe("s", 2, FP_C).outcome is SeqOutcome.ACCEPTED
    assert tracker.observe("s", 2, FP_C).outcome is SeqOutcome.DUPLICATE
    with pytest.raises(SequenceConflictError):
        tracker.observe("s", 2, FP_B)


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("missing", [1, 2, 100, 10**9])
def test_a_gap_is_accepted_and_reported_when_gaps_are_allowed(missing: int) -> None:
    tracker = SequenceTracker(allow_gaps=True, first_seq=3)
    for seq in (3, 4, 5):
        assert tracker.observe("s", seq, f"fp-{seq}").missing == 0
    late = 5 + missing + 1
    result = tracker.observe("s", late, FP_A)
    assert result.outcome is SeqOutcome.ACCEPTED_AFTER_GAP
    assert result.missing == missing
    assert tracker.last_seq("s") == late
    # The stream continues normally after the gap.
    following = tracker.observe("s", late + 1, FP_B)
    assert following.outcome is SeqOutcome.ACCEPTED
    assert following.missing == 0
    assert tracker.observe("s", late, FP_A).outcome is SeqOutcome.DUPLICATE


@pytest.mark.requirement("S00-ASG-25")
def test_an_arrival_with_the_same_fingerprint_is_an_idempotent_duplicate() -> None:
    tracker = SequenceTracker()
    for seq, fingerprint in enumerate((FP_A, FP_B, FP_C)):
        tracker.observe("s", seq, fingerprint)
    for _ in range(3):
        for seq, fingerprint in enumerate((FP_A, FP_B, FP_C)):
            result = tracker.observe("s", seq, fingerprint)
            assert result.outcome is SeqOutcome.DUPLICATE
            assert result.missing == 0
            assert tracker.last_seq("s") == 2
    assert tracker.observe("s", 3, FP_A).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_different_content_for_an_accepted_position_is_a_conflict() -> None:
    tracker = SequenceTracker()
    for seq, fingerprint in enumerate((FP_A, FP_B, FP_C)):
        tracker.observe("s", seq, fingerprint)
    for seq, original in enumerate((FP_A, FP_B, FP_C)):
        other = FP_C if original != FP_C else FP_A
        with pytest.raises(SequenceConflictError) as caught:
            tracker.observe("s", seq, other)
        assert caught.value.code == "GIE.SEQ.CONFLICT"
        assert tracker.last_seq("s") == 2
        # The conflicting content did not replace the accepted one.
        assert tracker.observe("s", seq, original).outcome is SeqOutcome.DUPLICATE
        with pytest.raises(SequenceConflictError):
            tracker.observe("s", seq, other)
    assert tracker.observe("s", 3, FP_A).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_a_late_arrival_outside_the_window_is_out_of_order_whatever_its_content() -> None:
    tracker = SequenceTracker(window=2)
    for seq, fingerprint in enumerate((FP_A, FP_B, FP_C, FP_A)):
        tracker.observe("s", seq, fingerprint)
    # Positions 0 and 1 are no longer kept: neither a duplicate nor a conflict can be proven.
    for seq, original in ((0, FP_A), (1, FP_B)):
        for fingerprint in (original, "f" * 64):
            with pytest.raises(OutOfOrderError) as caught:
                tracker.observe("s", seq, fingerprint)
            assert caught.value.code == "GIE.SEQ.OUT_OF_ORDER"
            assert tracker.last_seq("s") == 3
    # Nothing changed: the kept positions still verify and the stream continues.
    assert tracker.observe("s", 2, FP_C).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 3, FP_A).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 4, FP_B).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_an_arrival_before_the_first_event_of_the_stream_is_out_of_order() -> None:
    tracker = SequenceTracker(first_seq=None)  # joined in the middle, at 100
    tracker.observe("s", 100, FP_A)
    for seq in (99, 50, 0):
        with pytest.raises(OutOfOrderError):
            tracker.observe("s", seq, FP_A)
    assert tracker.last_seq("s") == 100
    assert tracker.observe("s", 101, FP_B).outcome is SeqOutcome.ACCEPTED


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_a_late_arrival_of_a_skipped_position_is_out_of_order() -> None:
    tracker = SequenceTracker(allow_gaps=True)
    tracker.observe("s", 0, FP_A)
    assert tracker.observe("s", 3, FP_B).missing == 2
    # 1 and 2 were skipped: they were never accepted, so they cannot be proven to be duplicates.
    for seq in (1, 2):
        with pytest.raises(OutOfOrderError):
            tracker.observe("s", seq, FP_C)
    assert tracker.last_seq("s") == 3
    assert tracker.observe("s", 0, FP_A).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 3, FP_B).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 4, FP_C).outcome is SeqOutcome.ACCEPTED


# =========================================================================== SequenceTracker: window


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("window", [1, 2, 3, 8])
@pytest.mark.parametrize("extra", [0, 1, 3])
def test_exactly_the_most_recent_window_arrivals_can_be_verified(window: int, extra: int) -> None:
    tracker = SequenceTracker(window=window)
    total = window + extra
    for seq in range(total):
        tracker.observe("s", seq, f"fp-{seq}")
    for seq in range(total):
        if seq >= total - window:
            assert tracker.observe("s", seq, f"fp-{seq}").outcome is SeqOutcome.DUPLICATE
            with pytest.raises(SequenceConflictError):
                tracker.observe("s", seq, "other")
        else:
            with pytest.raises(OutOfOrderError):
                tracker.observe("s", seq, f"fp-{seq}")
            with pytest.raises(OutOfOrderError):
                tracker.observe("s", seq, "other")
    assert tracker.last_seq("s") == total - 1


@pytest.mark.requirement("S00-ASG-25")
def test_the_window_counts_accepted_arrivals_not_sequence_distance() -> None:
    tracker = SequenceTracker(allow_gaps=True, window=2)
    for seq in (0, 1_000, 2_000_000):
        tracker.observe("s", seq, f"fp-{seq}")
    # The two most recent arrivals are kept although they are far apart; the oldest is gone.
    assert tracker.observe("s", 1_000, "fp-1000").outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 2_000_000, "fp-2000000").outcome is SeqOutcome.DUPLICATE
    with pytest.raises(OutOfOrderError):
        tracker.observe("s", 0, "fp-0")
    with pytest.raises(OutOfOrderError):
        tracker.observe("s", 999_999, "fp-never-seen")  # close to the last one, but never accepted


@pytest.mark.requirement("S00-ASG-25")
def test_duplicates_and_refusals_do_not_push_anything_out_of_the_window() -> None:
    tracker = SequenceTracker(window=2)
    tracker.observe("s", 0, FP_A)
    tracker.observe("s", 1, FP_B)
    for _ in range(10):
        assert tracker.observe("s", 0, FP_A).outcome is SeqOutcome.DUPLICATE
        with pytest.raises(SequenceConflictError):
            tracker.observe("s", 1, FP_C)
        with pytest.raises(SequenceGapError):
            tracker.observe("s", 5, FP_C)
    assert tracker.observe("s", 0, FP_A).outcome is SeqOutcome.DUPLICATE
    assert tracker.observe("s", 1, FP_B).outcome is SeqOutcome.DUPLICATE
    assert tracker.last_seq("s") == 1


@pytest.mark.requirement("S00-ASG-25")
def test_the_window_is_kept_per_stream() -> None:
    tracker = SequenceTracker(window=2)
    tracker.observe("quiet", 0, FP_A)
    for seq in range(50):
        tracker.observe("busy", seq, f"fp-{seq}")
    # Fifty arrivals on another stream do not evict the quiet stream's only event.
    assert tracker.observe("quiet", 0, FP_A).outcome is SeqOutcome.DUPLICATE
    with pytest.raises(OutOfOrderError):
        tracker.observe("busy", 0, "fp-0")


@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("window", [0, -1, -1024])
def test_the_window_must_hold_at_least_one_arrival(window: int) -> None:
    with pytest.raises(ContractValidationError):
        SequenceTracker(window=window)


# =========================================================================== SequenceTracker: state


@pytest.mark.requirement("S00-ASG-25", "G08-06")
def test_no_kind_of_refusal_changes_tracker_state() -> None:
    tracker = SequenceTracker(window=3)
    for seq, fingerprint in enumerate((FP_A, FP_B, FP_C, FP_A, FP_B)):
        tracker.observe("s", seq, fingerprint)  # last = 4, kept = 2, 3, 4

    def assert_unchanged() -> None:
        assert tracker.last_seq("s") == 4
        assert tracker.last_seq("other") is None
        assert tracker.observe("s", 2, FP_C).outcome is SeqOutcome.DUPLICATE
        assert tracker.observe("s", 3, FP_A).outcome is SeqOutcome.DUPLICATE
        assert tracker.observe("s", 4, FP_B).outcome is SeqOutcome.DUPLICATE

    refusals: list[tuple[type[GieError], int | Any, str]] = [
        (SequenceGapError, 6, FP_A),  # skipped position 5
        (SequenceGapError, 10**9, FP_A),
        (SequenceConflictError, 4, FP_A),  # other content for a kept position
        (SequenceConflictError, 2, FP_A),
        (OutOfOrderError, 1, FP_B),  # no longer kept
        (OutOfOrderError, 0, "f" * 64),
        (ContractValidationError, -1, FP_A),  # not a sequence number at all
        (ContractValidationError, True, FP_A),
        (ContractValidationError, 5.0, FP_A),
    ]
    for _ in range(2):
        for expected, seq, fingerprint in refusals:
            with pytest.raises(expected):
                tracker.observe("s", seq, fingerprint)
            assert_unchanged()
    # After all those refusals the correct next arrival is still accepted - exactly once.
    accepted = tracker.observe("s", 5, FP_C)
    assert accepted.outcome is SeqOutcome.ACCEPTED
    assert accepted.missing == 0
    assert tracker.last_seq("s") == 5
    assert tracker.observe("s", 5, FP_C).outcome is SeqOutcome.DUPLICATE


@pytest.mark.requirement("S00-ASG-25")
def test_streams_do_not_interact() -> None:
    tracker = SequenceTracker(first_seq=None)
    tracker.observe("tick/fixture/XAUUSD", 0, FP_A)
    tracker.observe("tick/fixture/XAUUSD", 1, FP_B)
    # Another stream starts wherever it starts and may reuse numbers and content.
    assert tracker.observe("order_event/execution.sim", 7, FP_A).outcome is SeqOutcome.ACCEPTED
    assert tracker.observe("tick/fixture/XAUUSD", 2, FP_C).outcome is SeqOutcome.ACCEPTED
    assert tracker.observe("order_event/execution.sim", 8, FP_C).outcome is SeqOutcome.ACCEPTED
    assert tracker.last_seq("tick/fixture/XAUUSD") == 2
    assert tracker.last_seq("order_event/execution.sim") == 8
    # Same number, other content: a conflict only within the stream that already has it.
    with pytest.raises(SequenceConflictError):
        tracker.observe("tick/fixture/XAUUSD", 1, FP_A)
    with pytest.raises(OutOfOrderError):
        tracker.observe("order_event/execution.sim", 1, FP_B)
    with pytest.raises(SequenceGapError):
        tracker.observe("order_event/execution.sim", 10, FP_A)
    assert tracker.last_seq("tick/fixture/XAUUSD") == 2
    assert tracker.last_seq("order_event/execution.sim") == 8
    assert tracker.last_seq("tick/fixture/XAUUSD.x") is None


@pytest.mark.requirement("S00-ASG-25")
def test_two_trackers_are_independent() -> None:
    first, second = SequenceTracker(), SequenceTracker()
    first.observe("s", 0, FP_A)
    first.observe("s", 1, FP_A)
    assert second.last_seq("s") is None
    # The second tracker has not seen the stream: for it the stream starts now, with other content.
    assert second.observe("s", 0, FP_B).outcome is SeqOutcome.ACCEPTED
    assert second.last_seq("s") == 0
    assert first.last_seq("s") == 1
    with pytest.raises(SequenceConflictError):
        first.observe("s", 0, FP_B)


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@pytest.mark.parametrize("seq", [-1, -(10**9), True, False, 1.0, 0.0, "1", None, [1]])
def test_values_that_are_not_sequence_numbers_are_rejected(seq: Any) -> None:
    fresh = SequenceTracker()
    with pytest.raises(ContractValidationError):
        fresh.observe("s", seq, FP_A)
    assert fresh.last_seq("s") is None
    assert fresh.observe("s", 0, FP_A).outcome is SeqOutcome.ACCEPTED

    running = SequenceTracker(allow_gaps=True)
    running.observe("s", 0, FP_A)
    with pytest.raises(ContractValidationError):
        running.observe("s", seq, FP_A)
    assert running.last_seq("s") == 0
    assert running.observe("s", 1, FP_B).outcome is SeqOutcome.ACCEPTED


# =========================================================================== SequenceTracker: model


class _ModelTracker:
    """Reference model written from the documented table, with plain lists instead of a ring.

    ``expect`` returns ``("result", outcome name, missing)`` or ``("error", exception class)``.
    """

    def __init__(self, *, allow_gaps: bool, first_seq: int | None, window: int) -> None:
        self.allow_gaps = allow_gaps
        self.first_seq = first_seq
        self.window = window
        self.last: dict[str, int] = {}
        self.kept: dict[str, list[tuple[int, str]]] = {}

    def _accept(self, stream: str, seq: int, fingerprint: str, outcome: str, missing: int) -> tuple[Any, ...]:
        self.last[stream] = seq
        arrivals = self.kept.setdefault(stream, [])
        arrivals.append((seq, fingerprint))
        del arrivals[: -self.window]
        return ("result", outcome, missing)

    def expect(self, stream: str, seq: int, fingerprint: str) -> tuple[Any, ...]:
        last = self.last.get(stream)
        if last is None:
            if self.first_seq is not None and seq != self.first_seq:
                return ("error", SequenceError)
            return self._accept(stream, seq, fingerprint, "ACCEPTED", 0)
        if seq == last + 1:
            return self._accept(stream, seq, fingerprint, "ACCEPTED", 0)
        if seq > last + 1:
            missing = seq - last - 1
            if not self.allow_gaps:
                return ("error", SequenceGapError)
            return self._accept(stream, seq, fingerprint, "ACCEPTED_AFTER_GAP", missing)
        kept = dict(self.kept[stream])
        if seq not in kept:
            return ("error", OutOfOrderError)
        if kept[seq] != fingerprint:
            return ("error", SequenceConflictError)
        return ("result", "DUPLICATE", 0)


# One arrival: (stream, how the sequence number is chosen, amount, fingerprint).
#   "next"  the position after the last accepted one (the required start for a new stream)
#   "back"  amount positions before the last accepted one (duplicates, conflicts, late arrivals)
#   "ahead" amount + 2 positions after the last accepted one (gaps)
#   "abs"   the sequence number amount itself
_ARRIVALS = st.lists(
    st.tuples(
        st.sampled_from(["a", "b"]),
        st.sampled_from(["next", "next", "next", "back", "back", "ahead", "abs"]),
        st.integers(0, 6),
        st.sampled_from(["x", "y"]),
    ),
    max_size=60,
)


@pytest.mark.requirement("S00-ASG-25", "G08-06")
@_PROPERTY
@given(
    allow_gaps=st.booleans(),
    first_seq=st.one_of(st.none(), st.integers(0, 2)),
    window=st.integers(1, 4),
    arrivals=_ARRIVALS,
)
@example(
    allow_gaps=True,
    first_seq=0,
    window=2,
    arrivals=[
        ("a", "abs", 3, "x"),  # refused: stream must start at 0
        ("a", "next", 0, "x"),  # 0 accepted
        ("a", "ahead", 1, "y"),  # 3 accepted after a gap of 2
        ("a", "back", 0, "y"),  # 3 again: duplicate
        ("a", "back", 0, "x"),  # 3 with other content: conflict
        ("a", "back", 2, "x"),  # 1 was skipped: out of order
        ("a", "next", 0, "x"),  # 4 accepted; 0 leaves the window
        ("a", "abs", 0, "x"),  # 0 no longer kept: out of order
        ("b", "next", 0, "y"),  # other stream starts at 0
    ],
)
def test_sequence_tracker_follows_the_documented_table_for_any_arrival_sequence(
    allow_gaps: bool, first_seq: int | None, window: int, arrivals: list[tuple[str, str, int, str]]
) -> None:
    tracker = SequenceTracker(allow_gaps=allow_gaps, first_seq=first_seq, window=window)
    model = _ModelTracker(allow_gaps=allow_gaps, first_seq=first_seq, window=window)
    for stream, choice, amount, fingerprint in arrivals:
        last = model.last.get(stream)
        if choice == "abs":
            seq = amount
        elif last is None:
            seq = first_seq if first_seq is not None else amount
        elif choice == "next":
            seq = last + 1
        elif choice == "back":
            seq = max(last - amount, 0)
        else:
            seq = last + 2 + amount
        expected = model.expect(stream, seq, fingerprint)
        if expected[0] == "error":
            with pytest.raises(expected[1]):
                tracker.observe(stream, seq, fingerprint)
        else:
            result = tracker.observe(stream, seq, fingerprint)
            assert (result.outcome.value, result.missing) == (expected[1], expected[2])
        for name in ("a", "b"):
            assert tracker.last_seq(name) == model.last.get(name)
