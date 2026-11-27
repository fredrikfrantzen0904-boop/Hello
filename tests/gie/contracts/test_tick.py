"""Independent tests for the ``Tick`` contract (``gie/contracts/tick.py``).

ALL DATA IN THIS MODULE IS SYNTHETIC FIXTURE DATA.  Every record is built with the labelled
builders of ``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); the prices, sizes and
instants are invented.  Nothing here is market data, broker data or evidence of performance.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - "Tick 1.0.0": fields, bounds and the invariants
  TICK-1 (CROSSED flag iff ``ask < bid``) and TICK-2 (EVENT_AFTER_RECEIVE flag iff
  ``event_time_ns > receive_time_ns``); the ``TickFlag`` vocabulary.
* ``specifications/SHARED_CONTRACTS.md`` - section 2 (prices are exact decimals ``> 0``, arithmetic
  uses the exact context and is independent of the ambient decimal context; instants are ``int``
  nanoseconds in ``0 .. 2^63-1``), section 3.3 (the freshness reference of market data is the
  *receive* time, future data raises ``FutureDataError``), section 5 (invalid input is rejected with
  ``ContractValidationError`` and never repaired or coerced), section 6.1 (stream
  ``tick/<source>/<symbol>``, ordering time ``receive_time_ns``) and section 7 (canonical order of
  lists whose order carries no meaning).

No test reads a clock: every instant is a constant derived from ``samples.T0``.
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    DataOrigin,
    Tick,
    TickFlag,
    feed_silence_ns,
    from_json,
    from_wire,
    ordering_time_ns,
    stream_name,
    to_json,
    to_wire,
)
from gie.core.errors import ContractValidationError, FutureDataError, GieError, TimeError
from gie.core.timeutil import MAX_TIME_NS, NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0

#: Ambient decimal contexts that would corrupt naive ``Decimal`` arithmetic (silent rounding to very
#: few digits, tiny exponent range, trapping of inexact results).  Results must not depend on them.
HOSTILE_CONTEXTS: dict[str, decimal.Context] = {
    "prec3": decimal.Context(prec=3),
    "prec1_round_up": decimal.Context(prec=1, rounding=decimal.ROUND_UP),
    "prec3_trap_inexact": decimal.Context(prec=3, traps=[decimal.Inexact, decimal.Rounded]),
    "tiny_exponents": decimal.Context(
        prec=1,
        Emax=1,
        Emin=-1,
        traps=[decimal.Inexact, decimal.Rounded, decimal.Subnormal, decimal.Overflow, decimal.Underflow],
    ),
}


def _rejected(**overrides: Any) -> ContractValidationError:
    """Build the sample tick with *overrides* and return the contract error it must raise."""
    with pytest.raises(ContractValidationError) as info:
        samples.tick(**overrides)
    assert info.value.code == "GIE.CONTRACT.INVALID"
    return info.value


def _assert_invariant(error: ContractValidationError, invariant_id: str) -> None:
    assert error.details.get("invariant") == invariant_id, str(error)
    assert error.details.get("contract") == "Tick"


def _assert_field(error: ContractValidationError, path: str) -> None:
    assert error.details.get("path") == path, str(error)
    assert "invariant" not in error.details


# =========================================================================== inventory
@pytest.mark.requirement("S00-ASG-11")
def test_tick_declares_exactly_the_invariants_of_the_field_reference() -> None:
    assert {rule.id for rule in Tick.ALL_INVARIANTS} == {"TICK-1", "TICK-2"}
    assert (Tick.CONTRACT, Tick.VERSION) == ("Tick", "1.0.0")


@pytest.mark.requirement("S00-ASG-11")
def test_tick_flag_vocabulary_is_the_published_one() -> None:
    assert [flag.value for flag in TickFlag] == [
        "CROSSED",
        "EVENT_AFTER_RECEIVE",
        "OUT_OF_ORDER",
        "DUPLICATE",
        "GAP_BEFORE",
        "DELAYED",
        "OUTLIER",
        "OFF_SESSION",
        "RECEIVE_TIME_MODELLED",
    ]


@pytest.mark.requirement("S00-ASG-11")
def test_sample_ticks_are_labelled_synthetic_fixtures() -> None:
    variants = samples.variants()["Tick"]
    assert len(variants) >= 5
    for name, record in variants.items():
        assert type(record) is Tick, name
        assert record.origin is DataOrigin.SYNTHETIC_FIXTURE, name


@pytest.mark.requirement("S00-ASG-11")
def test_tick_is_immutable() -> None:
    tick = samples.tick()
    with pytest.raises(dataclasses.FrozenInstanceError):
        tick.bid = D("1")  # type: ignore[misc]
    assert tick.bid == D("2650.10")


# =========================================================================== prices and volumes
@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("value", ["0", "-0", "0.000", "-0.000000000001", "-2650.10"])
def test_bid_and_ask_must_be_greater_than_zero(field: str, value: str) -> None:
    error = _rejected(**{field: D(value)})
    _assert_field(error, field)
    assert error.details["rule"] == "must be > 0"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
def test_smallest_and_largest_representable_prices_are_accepted() -> None:
    smallest = samples.tick(bid=D("0.000000000001"), ask=D("0.000000000001"), flags=())
    assert smallest.bid == smallest.ask == D("1E-12")
    largest = D("999999999999999999.999999999999")
    tick = samples.tick(bid=largest, ask=largest, flags=())
    assert tick.bid == tick.ask == largest


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize("field", ["bid", "ask", "bid_volume", "ask_volume"])
@pytest.mark.parametrize(
    "value",
    [2650.10, 2650, "2650.10", True, b"2650.10"],
    ids=["float", "int", "str", "bool", "bytes"],
)
def test_decimal_quantities_are_never_coerced_from_other_types(field: str, value: object) -> None:
    error = _rejected(**{field: value})
    _assert_field(error, field)
    assert error.details["actual_type"] == type(value).__name__


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize("field", ["bid", "ask", "bid_volume", "ask_volume"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_decimals_are_rejected(field: str, value: str) -> None:
    _assert_field(_rejected(**{field: D(value)}), field)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize(
    "value",
    ["2650.1234567890123", "1000000000000000000", "0.0000000000001", "1E+18", "1E-13"],
    ids=["13_fraction_digits", "19_integer_digits", "below_resolution", "exp_too_large", "exp_too_small"],
)
def test_prices_outside_the_wire_range_are_rejected_not_rounded(value: str) -> None:
    for field in ("bid", "ask"):
        _assert_field(_rejected(**{field: D(value)}), field)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
def test_volumes_are_optional_and_never_negative() -> None:
    assert samples.tick().bid_volume is None
    assert samples.tick().ask_volume is None
    both = samples.tick(bid_volume=D("0"), ask_volume=D("12.5"))
    assert (both.bid_volume, both.ask_volume) == (D("0"), D("12.5"))
    for field in ("bid_volume", "ask_volume"):
        error = _rejected(**{field: D("-0.01")})
        _assert_field(error, field)
        assert error.details["rule"] == "must be >= 0"


# =========================================================================== TICK-1
@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    ("bid", "ask", "flags"),
    [
        pytest.param("2650.10", "2650.35", (), id="normal_no_flag"),
        pytest.param("2650.10", "2650.10", (), id="locked_quote_is_not_crossed"),
        pytest.param("2650.40", "2650.30", (TickFlag.CROSSED,), id="crossed_with_flag"),
        pytest.param("2650.000000000002", "2650.000000000001", (TickFlag.CROSSED,), id="crossed_by_one_step"),
        pytest.param(
            "2650.40",
            "2650.30",
            (TickFlag.OUTLIER, TickFlag.CROSSED, TickFlag.OFF_SESSION),
            id="crossed_among_other_flags",
        ),
    ],
)
def test_tick1_accepts_consistent_crossed_flag(bid: str, ask: str, flags: tuple[TickFlag, ...]) -> None:
    tick = samples.tick(bid=D(bid), ask=D(ask), flags=flags)
    assert (tick.ask < tick.bid) == (TickFlag.CROSSED in tick.flags)


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    ("bid", "ask", "flags"),
    [
        pytest.param("2650.40", "2650.30", (), id="crossed_without_flag"),
        pytest.param("2650.40", "2650.30", (TickFlag.OUTLIER,), id="crossed_with_other_flag_only"),
        pytest.param("2650.000000000002", "2650.000000000001", (), id="crossed_by_one_step_no_flag"),
        pytest.param("2650.10", "2650.35", (TickFlag.CROSSED,), id="flag_on_normal_quote"),
        pytest.param("2650.10", "2650.10", (TickFlag.CROSSED,), id="flag_on_locked_quote"),
        pytest.param(
            "2650.000000000001", "2650.000000000002", (TickFlag.CROSSED,), id="flag_on_one_step_spread"
        ),
    ],
)
def test_tick1_rejects_inconsistent_crossed_flag(bid: str, ask: str, flags: tuple[TickFlag, ...]) -> None:
    error = _rejected(bid=D(bid), ask=D(ask), flags=flags)
    _assert_invariant(error, "TICK-1")
    assert error.details["rule"] == "the CROSSED flag is present if and only if ask < bid"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize("context_name", sorted(HOSTILE_CONTEXTS))
def test_tick1_is_decided_exactly_under_a_hostile_decimal_context(context_name: str) -> None:
    # With three significant digits both prices would round to the same number.
    with decimal.localcontext(HOSTILE_CONTEXTS[context_name]):
        crossed = samples.tick(
            bid=D("2650.000000000002"), ask=D("2650.000000000001"), flags=(TickFlag.CROSSED,)
        )
        assert TickFlag.CROSSED in crossed.flags
        _assert_invariant(
            _rejected(bid=D("2650.000000000002"), ask=D("2650.000000000001"), flags=()), "TICK-1"
        )
        _assert_invariant(
            _rejected(bid=D("2650.000000000001"), ask=D("2650.000000000002"), flags=(TickFlag.CROSSED,)),
            "TICK-1",
        )


# =========================================================================== TICK-2
@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21")
@pytest.mark.parametrize(
    ("event_offset_ns", "flags"),
    [
        pytest.param(-2 * NS_PER_MS, (), id="event_before_receive"),
        pytest.param(0, (), id="event_equals_receive"),
        pytest.param(1, (TickFlag.EVENT_AFTER_RECEIVE,), id="event_one_ns_after_receive"),
        pytest.param(5 * NS_PER_SECOND, (TickFlag.EVENT_AFTER_RECEIVE, TickFlag.DELAYED), id="far_after"),
    ],
)
def test_tick2_accepts_consistent_event_after_receive_flag(
    event_offset_ns: int, flags: tuple[TickFlag, ...]
) -> None:
    tick = samples.tick(event_time_ns=T0 + event_offset_ns, receive_time_ns=T0, flags=flags)
    assert (tick.event_time_ns > tick.receive_time_ns) == (TickFlag.EVENT_AFTER_RECEIVE in tick.flags)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21")
@pytest.mark.parametrize(
    ("event_offset_ns", "flags"),
    [
        pytest.param(1, (), id="event_after_receive_without_flag"),
        pytest.param(1, (TickFlag.DELAYED,), id="event_after_receive_with_other_flag_only"),
        pytest.param(NS_PER_SECOND, (TickFlag.RECEIVE_TIME_MODELLED,), id="modelled_receive_time_no_flag"),
        pytest.param(0, (TickFlag.EVENT_AFTER_RECEIVE,), id="flag_when_equal"),
        pytest.param(-1, (TickFlag.EVENT_AFTER_RECEIVE,), id="flag_when_event_before_receive"),
    ],
)
def test_tick2_rejects_inconsistent_event_after_receive_flag(
    event_offset_ns: int, flags: tuple[TickFlag, ...]
) -> None:
    error = _rejected(event_time_ns=T0 + event_offset_ns, receive_time_ns=T0, flags=flags)
    _assert_invariant(error, "TICK-2")
    assert error.details["rule"] == (
        "the EVENT_AFTER_RECEIVE flag is present if and only if event_time_ns > receive_time_ns"
    )


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21")
def test_tick1_and_tick2_hold_as_biconditionals_over_the_whole_boundary_matrix() -> None:
    checked = 0
    for bid, ask, crossed_flag, event_offset, after_flag in itertools.product(
        (D("2650.10"), D("2650.11")), (D("2650.10"), D("2650.11")), (False, True), (-1, 0, 1), (False, True)
    ):
        flags = tuple(
            flag
            for flag, present in (
                (TickFlag.CROSSED, crossed_flag),
                (TickFlag.EVENT_AFTER_RECEIVE, after_flag),
            )
            if present
        )
        fields = {"bid": bid, "ask": ask, "event_time_ns": T0 + event_offset, "receive_time_ns": T0}
        crossed_ok = (ask < bid) == crossed_flag
        after_ok = (event_offset > 0) == after_flag
        if crossed_ok and after_ok:
            assert samples.tick(**fields, flags=flags).flags == flags
        else:
            # TICK-1 is declared first, so it is the one reported when both rules are broken.
            _assert_invariant(_rejected(**fields, flags=flags), "TICK-2" if crossed_ok else "TICK-1")
        checked += 1
    assert checked == 48


# =========================================================================== flags
@pytest.mark.requirement("S00-ASG-11")
def test_flags_are_empty_when_nothing_is_wrong() -> None:
    assert samples.tick(flags=()).flags == ()


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    "flags",
    [
        (TickFlag.DELAYED, TickFlag.DELAYED),
        (TickFlag.RECEIVE_TIME_MODELLED, TickFlag.OUTLIER, TickFlag.RECEIVE_TIME_MODELLED),
    ],
    ids=["adjacent_duplicate", "separated_duplicate"],
)
def test_flags_must_be_unique(flags: tuple[TickFlag, ...]) -> None:
    error = _rejected(flags=flags)
    assert error.details["rule"] == "duplicate item"
    assert str(error.details["path"]).startswith("flags[")
    assert "invariant" not in error.details


@pytest.mark.requirement("S00-ASG-11")
def test_flags_are_stored_in_canonical_order_whatever_order_the_producer_supplies() -> None:
    chosen = (TickFlag.RECEIVE_TIME_MODELLED, TickFlag.OFF_SESSION, TickFlag.DELAYED, TickFlag.GAP_BEFORE)
    expected = tuple(sorted(chosen, key=lambda flag: flag.value))
    reference = samples.tick(flags=expected)
    canonical_json = to_json(reference)
    permutations = list(itertools.permutations(chosen))
    assert len(permutations) == 24
    for order in permutations:
        tick = samples.tick(flags=order)
        assert tick.flags == expected
        assert tick == reference
        assert to_json(tick) == canonical_json


@pytest.mark.requirement("S00-ASG-11")
def test_all_flags_together_are_accepted_and_sorted_by_value() -> None:
    tick = samples.tick(
        bid=D("2650.40"),
        ask=D("2650.30"),
        event_time_ns=T0 + 1,
        receive_time_ns=T0,
        flags=tuple(reversed(TickFlag)),
    )
    assert [flag.value for flag in tick.flags] == sorted(flag.value for flag in TickFlag)
    assert len(tick.flags) == len(TickFlag) == 9


@pytest.mark.requirement("S00-ASG-11")
def test_a_list_of_flags_is_stored_as_an_immutable_tuple() -> None:
    tick = samples.tick(flags=[TickFlag.RECEIVE_TIME_MODELLED, TickFlag.DELAYED])
    assert tick.flags == (TickFlag.DELAYED, TickFlag.RECEIVE_TIME_MODELLED)
    assert type(tick.flags) is tuple


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    "flags",
    [
        ("RECEIVE_TIME_MODELLED",),
        ("NOT_A_FLAG",),
        (None,),
        (DataOrigin.SYNTHETIC_FIXTURE,),
        (1,),
    ],
    ids=["look_alike_string", "unknown_string", "none_item", "member_of_another_enum", "int"],
)
def test_flag_items_must_be_genuine_tick_flag_members(flags: tuple[object, ...]) -> None:
    error = _rejected(flags=flags)
    _assert_field(error, "flags[0]")
    assert error.details["rule"] == "must be a TickFlag member"


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    "flags",
    [None, "CROSSED", {TickFlag.RECEIVE_TIME_MODELLED}, TickFlag.RECEIVE_TIME_MODELLED, 0],
    ids=["none", "string", "set", "bare_member", "int"],
)
def test_flags_must_be_a_sequence(flags: object) -> None:
    _assert_field(_rejected(flags=flags), "flags")


# =========================================================================== symbol, source, seq, time
@pytest.mark.requirement("S00-ASG-11", "G01-01")
@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD", "EURUSD", "US10Y_REAL", "DXY", "US500.CASH", "1"])
def test_ticks_may_describe_observed_instruments_other_than_gold(symbol: str) -> None:
    tick = samples.tick(symbol=symbol)
    assert tick.symbol == symbol
    assert tick.stream == f"tick/fixture/{symbol}"


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    "symbol",
    ["", "xauusd", "XAU USD", "XAU/USD", "_XAUUSD", "X" * 25, "XAUUSD\n", "XAU\x00USD", None, 5],
    ids=[
        "empty",
        "lower",
        "space",
        "slash",
        "leading_underscore",
        "too_long",
        "newline",
        "nul",
        "none",
        "int",
    ],
)
def test_malformed_symbols_are_rejected(symbol: object) -> None:
    _assert_field(_rejected(symbol=symbol), "symbol")


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize("source", ["fixture", "broker-a:v2", "hist.2026_11", "0", "a" * 64])
def test_well_formed_sources_are_accepted_and_name_the_stream(source: str) -> None:
    tick = samples.tick(source=source)
    assert tick.stream == f"tick/{source}/XAUUSD"
    assert stream_name(tick) == tick.stream


@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize(
    "source",
    ["", "Fixture", "feed/a", "feed a", "-feed", "a" * 65, "feed\t", None],
    ids=["empty", "upper", "slash", "space", "leading_dash", "too_long", "tab", "none"],
)
def test_malformed_sources_are_rejected(source: object) -> None:
    _assert_field(_rejected(source=source), "source")


@pytest.mark.requirement("S00-ASG-11")
def test_origin_must_be_a_genuine_data_origin_member() -> None:
    for origin in DataOrigin:
        assert samples.tick(origin=origin).origin is origin
    for look_alike in ("SYNTHETIC_FIXTURE", "BROKER_FEED", None, TickFlag.CROSSED):
        _assert_field(_rejected(origin=look_alike), "origin")


@pytest.mark.requirement("S00-ASG-11")
def test_seq_is_a_non_negative_64_bit_integer() -> None:
    assert samples.tick(seq=0).seq == 0
    assert samples.tick(seq=2**63 - 1).seq == 2**63 - 1
    for bad in (-1, 2**63, True, 1.0, "1", None, D("1")):
        _assert_field(_rejected(seq=bad), "seq")


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21")
def test_instants_are_integers_within_the_supported_range() -> None:
    earliest = samples.tick(event_time_ns=0, receive_time_ns=0, flags=())
    assert (earliest.event_time_ns, earliest.receive_time_ns) == (0, 0)
    latest = samples.tick(event_time_ns=MAX_TIME_NS, receive_time_ns=MAX_TIME_NS, flags=())
    assert latest.receive_time_ns == MAX_TIME_NS == 2**63 - 1
    for field in ("event_time_ns", "receive_time_ns"):
        for bad in (-1, MAX_TIME_NS + 1, float(T0), True, str(T0), None, D(T0)):
            _assert_field(_rejected(**{field: bad}), field)


# =========================================================================== mid, spread, stream
MID_SPREAD_CASES = [
    pytest.param("2650.10", "2650.35", "2650.225", "0.25", id="sample"),
    pytest.param("2650.10", "2650.10", "2650.10", "0", id="locked"),
    pytest.param("2650.40", "2650.30", "2650.35", "-0.10", id="crossed_negative_spread"),
    pytest.param("0.000000000001", "0.000000000002", "0.0000000000015", "0.000000000001", id="smallest_step"),
    pytest.param(
        "999999999999999999.999999999999",
        "999999999999999999.999999999999",
        "999999999999999999.999999999999",
        "0",
        id="largest_magnitude",
    ),
    pytest.param(
        "999999999999999999.999999999998",
        "999999999999999999.999999999999",
        "999999999999999999.9999999999985",
        "0.000000000001",
        id="thirty_one_significant_digits",
    ),
    pytest.param(
        "1234567890.123456789012",
        "1234567890.123456789013",
        "1234567890.1234567890125",
        "0.000000000001",
        id="beyond_the_default_28_digit_context",
    ),
    pytest.param("31.115", "31.14", "31.1275", "0.025", id="silver"),
]


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize(("bid", "ask", "mid", "spread"), MID_SPREAD_CASES)
def test_mid_and_spread_are_exact(bid: str, ask: str, mid: str, spread: str) -> None:
    flags = (TickFlag.CROSSED,) if D(ask) < D(bid) else ()
    tick = samples.tick(bid=D(bid), ask=D(ask), flags=flags)
    assert tick.mid == D(mid)
    assert tick.spread == D(spread)
    assert isinstance(tick.mid, D)
    assert isinstance(tick.spread, D)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize("context_name", sorted(HOSTILE_CONTEXTS))
@pytest.mark.parametrize(("bid", "ask", "mid", "spread"), MID_SPREAD_CASES)
def test_mid_and_spread_do_not_depend_on_the_ambient_decimal_context(
    context_name: str, bid: str, ask: str, mid: str, spread: str
) -> None:
    flags = (TickFlag.CROSSED,) if D(ask) < D(bid) else ()
    outside = samples.tick(bid=D(bid), ask=D(ask), flags=flags)
    with decimal.localcontext(HOSTILE_CONTEXTS[context_name]):
        inside = samples.tick(bid=D(bid), ask=D(ask), flags=flags)
        results = (outside.mid, outside.spread, inside.mid, inside.spread)
        wire = to_json(inside)
    assert results == (D(mid), D(spread), D(mid), D(spread))
    # Compare digit for digit: Decimal equality alone would not notice a changed exponent.
    assert [str(value) for value in results[:2]] == [str(value) for value in results[2:]]
    assert inside == outside
    assert wire == to_json(outside)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
def test_mid_and_spread_satisfy_their_definitions_on_a_price_grid() -> None:
    steps = [D("0.000000000001"), D("0.01"), D("0.37"), D("1"), D("2650.123456789012")]
    checked = 0
    with decimal.localcontext(HOSTILE_CONTEXTS["prec3"]):
        for bid, delta in itertools.product(steps, steps):
            ask = decimal.Context(prec=64).add(bid, delta)
            tick = samples.tick(bid=bid, ask=ask, flags=())
            exact = decimal.Context(prec=64, traps=[decimal.Inexact])
            assert exact.multiply(tick.mid, D(2)) == exact.add(bid, ask)
            assert tick.spread == delta
            assert bid < tick.mid < ask
            checked += 1
    assert checked == 25


@pytest.mark.requirement("S00-ASG-11")
def test_stream_name_is_tick_source_symbol() -> None:
    tick = samples.tick(source="hist.2026_11", symbol="XAGUSD", bid=D("31.115"), ask=D("31.14"))
    assert tick.stream == "tick/hist.2026_11/XAGUSD"
    assert stream_name(tick) == "tick/hist.2026_11/XAGUSD"
    assert samples.tick().stream == "tick/fixture/XAUUSD"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21")
def test_ordering_time_of_a_tick_is_its_receive_time_never_its_event_time() -> None:
    late_source_clock = samples.tick(
        event_time_ns=T0 + NS_PER_SECOND, receive_time_ns=T0, flags=(TickFlag.EVENT_AFTER_RECEIVE,)
    )
    assert ordering_time_ns(late_source_clock) == T0
    delayed = samples.tick(event_time_ns=T0 - 5 * NS_PER_SECOND, receive_time_ns=T0)
    assert ordering_time_ns(delayed) == T0


# =========================================================================== feed_silence_ns
@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
@pytest.mark.parametrize("silence", [0, 1, NS_PER_MS, 7 * NS_PER_SECOND, 86_400 * NS_PER_SECOND])
def test_feed_silence_is_now_minus_receive_time(silence: int) -> None:
    tick = samples.tick()
    result = feed_silence_ns(tick, tick.receive_time_ns + silence)
    assert result == silence
    assert type(result) is int


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
def test_feed_silence_reference_is_the_receive_time_not_the_source_event_time() -> None:
    delayed = samples.tick(
        event_time_ns=T0 - 5 * NS_PER_SECOND, receive_time_ns=T0, flags=(TickFlag.DELAYED,)
    )
    assert feed_silence_ns(delayed, T0 + 7) == 7
    # A source clock that runs ahead: the event time lies after "now", the receive time does not.
    ahead = samples.tick(
        event_time_ns=T0 + 10 * NS_PER_SECOND, receive_time_ns=T0, flags=(TickFlag.EVENT_AFTER_RECEIVE,)
    )
    assert feed_silence_ns(ahead, T0 + 5) == 5
    assert feed_silence_ns(ahead, T0) == 0


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
@pytest.mark.parametrize("lead_ns", [1, NS_PER_MS, 3600 * NS_PER_SECOND])
def test_feed_silence_refuses_a_tick_received_after_now(lead_ns: int) -> None:
    tick = samples.tick(receive_time_ns=T0, event_time_ns=T0 - NS_PER_MS)
    with pytest.raises(FutureDataError) as info:
        feed_silence_ns(tick, T0 - lead_ns)
    error = info.value
    assert isinstance(error, TimeError)
    assert isinstance(error, GieError)
    assert error.code == "GIE.TIME.FUTURE"
    assert error.details["now_ns"] == T0 - lead_ns
    assert error.details["receive_time_ns"] == T0
    assert error.details["stream"] == "tick/fixture/XAUUSD"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
def test_feed_silence_refuses_future_data_even_when_the_event_time_is_in_the_past() -> None:
    # Only the receive time decides: an old source timestamp does not make future data acceptable.
    tick = samples.tick(event_time_ns=T0 - 3600 * NS_PER_SECOND, receive_time_ns=T0)
    with pytest.raises(FutureDataError):
        feed_silence_ns(tick, T0 - 1)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
def test_feed_silence_refuses_a_negative_now() -> None:
    # Fail closed: a negative "now" is before every receive time (and is not an instant at all).
    with pytest.raises((ContractValidationError, FutureDataError)):
        feed_silence_ns(samples.tick(), -1)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21", "S00-ASG-23")
@pytest.mark.parametrize(
    "now_ns",
    [float(T0 + NS_PER_SECOND), True, 2**63],
    ids=["float", "bool", "one_past_int64"],
)
def test_feed_silence_refuses_a_now_that_is_not_an_instant(now_ns: object) -> None:
    # Section 2: an instant is an int of nanoseconds in 0 .. 2^63-1; section 5: invalid input is
    # rejected with ContractValidationError and never coerced (gie.core.freshness does exactly that).
    tick = samples.tick(event_time_ns=0, receive_time_ns=0, flags=())
    with pytest.raises(ContractValidationError):
        feed_silence_ns(tick, now_ns)  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-21", "S00-ASG-23")
@pytest.mark.parametrize(
    "now_ns",
    [-1, -(2**63), 2**63, 2**64, None, "0", D("0"), D(T0), 0.0, float("nan"), False],
    ids=repr,
)
def test_feed_silence_validates_now_with_a_contract_error(now_ns: object) -> None:
    # Rework 2 (change notice section 11) and the docstring: now_ns is an int in 0 .. 2**63 - 1,
    # refused with ContractValidationError and never coerced (also not compared as a FutureDataError).
    tick = samples.tick(event_time_ns=0, receive_time_ns=0, flags=())
    with pytest.raises(ContractValidationError) as info:
        feed_silence_ns(tick, now_ns)  # type: ignore[arg-type]
    assert not isinstance(info.value, FutureDataError)
    assert info.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
def test_feed_silence_accepts_the_bounds_of_now() -> None:
    earliest = samples.tick(event_time_ns=0, receive_time_ns=0, flags=())
    assert feed_silence_ns(earliest, 0) == 0
    assert feed_silence_ns(earliest, MAX_TIME_NS) == MAX_TIME_NS
    latest = samples.tick(event_time_ns=MAX_TIME_NS, receive_time_ns=MAX_TIME_NS, flags=())
    assert feed_silence_ns(latest, MAX_TIME_NS) == 0


class _TickLookAlike(Tick):  # type: ignore[misc]  # defined below "tests."; never registered
    __slots__ = ()


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: None, id="none"),
        pytest.param(lambda: to_wire(samples.tick()), id="wire_dict"),
        pytest.param(lambda: samples.tick().receive_time_ns, id="receive_time_int"),
        pytest.param(samples.clock_reading, id="clock_reading_record"),
        pytest.param(samples.evidence, id="evidence_record"),
        pytest.param(
            lambda: _TickLookAlike(
                **{field.name: getattr(samples.tick(), field.name) for field in dataclasses.fields(Tick)}
            ),
            id="tick_subclass",
        ),
    ],
)
def test_feed_silence_validates_the_type_of_the_last_tick(build: Any) -> None:
    # Rework 2: last_tick must be a Tick of exactly that type; anything else is a contract error.
    last_tick = build()
    with pytest.raises(ContractValidationError) as info:
        feed_silence_ns(last_tick, T0 + NS_PER_SECOND)
    assert info.value.details.get("actual_type") == type(last_tick).__name__


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-23")
def test_feed_silence_cannot_be_fooled_into_accepting_future_data_by_a_float_now() -> None:
    # 30 ns before the receive time.  As a double the instant rounds UP to the next multiple of
    # 256 ns, which lies after the receive time; future data must still be refused (fail closed).
    tick = samples.tick()
    now_ns = tick.receive_time_ns - 30
    assert float(now_ns) > tick.receive_time_ns  # the hazard this test is about
    with pytest.raises(GieError) as info:
        feed_silence_ns(tick, float(now_ns))  # type: ignore[arg-type]
    assert isinstance(info.value, (ContractValidationError, FutureDataError))


# =========================================================================== wire form
@pytest.mark.requirement("S00-ASG-11")
@pytest.mark.parametrize("name", sorted(samples.variants()["Tick"]))
def test_every_sample_tick_survives_the_wire_round_trip(name: str) -> None:
    tick = samples.variants()["Tick"][name]
    assert from_wire(to_wire(tick), Tick) == tick
    assert from_json(to_json(tick), Tick) == tick
    assert to_json(from_json(to_json(tick), Tick)) == to_json(tick)


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
def test_wire_form_carries_prices_as_decimal_strings_and_flags_as_values() -> None:
    wire = to_wire(samples.variants()["Tick"]["crossed"])
    assert wire["contract"] == "Tick"
    assert wire["schema_version"] == "1.0.0"
    assert (wire["bid"], wire["ask"]) == ("2650.4", "2650.3")
    assert wire["flags"] == ["CROSSED", "RECEIVE_TIME_MODELLED"]
    assert wire["bid_volume"] is None
    assert "ask_volume" in wire
    assert type(wire["receive_time_ns"]) is int


@pytest.mark.requirement("S00-ASG-11")
def test_decoding_enforces_the_invariants() -> None:
    crossed = to_wire(samples.variants()["Tick"]["crossed"])
    with pytest.raises(ContractValidationError) as info:
        from_wire({**crossed, "flags": ["RECEIVE_TIME_MODELLED"]}, Tick)
    _assert_invariant(info.value, "TICK-1")

    late = to_wire(samples.variants()["Tick"]["event_after_receive"])
    with pytest.raises(ContractValidationError) as info:
        from_wire({**late, "flags": []}, Tick)
    _assert_invariant(info.value, "TICK-2")


@pytest.mark.requirement("S00-ASG-11")
def test_decoding_accepts_any_flag_order_and_refuses_duplicates_and_unknown_flags() -> None:
    record = samples.variants()["Tick"]["crossed"]
    wire = to_wire(record)
    assert from_wire({**wire, "flags": ["RECEIVE_TIME_MODELLED", "CROSSED"]}, Tick) == record
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "flags": ["CROSSED", "RECEIVE_TIME_MODELLED", "CROSSED"]}, Tick)
    assert info.value.details["rule"] == "duplicate item"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "flags": ["CROSSED", "HALTED"]}, Tick)
    assert info.value.details["path"] == "flags[1]"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
@pytest.mark.parametrize(
    "bid",
    [2650.4, 2650, "2.6504E+3", "+2650.4", "02650.4", " 2650.4", "2650.4000000000001", None],
    ids=["json_float", "json_int", "exponent", "plus_sign", "leading_zero", "space", "13_digits", "null"],
)
def test_decoding_refuses_prices_that_are_not_plain_decimal_strings(bid: object) -> None:
    wire = to_wire(samples.tick())
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "bid": bid}, Tick)
    assert info.value.details["path"] == "bid"


@pytest.mark.requirement("S00-ASG-11", "S00-ASG-20")
def test_decoding_accepts_trailing_zeros_and_yields_the_same_record() -> None:
    record = samples.tick()
    wire = to_wire(record)
    assert wire["bid"] == "2650.1"
    decoded = from_wire({**wire, "bid": "2650.100", "ask": "2650.350000000000"}, Tick)
    assert decoded == record
    assert to_json(decoded) == to_json(record)
