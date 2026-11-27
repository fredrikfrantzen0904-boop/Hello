"""Independent tests for the ``Position`` contract and ``validate_position_transition``.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (sections 9.1, 9.2, 9.4),
``specifications/contracts/FIELDS.md`` (Position fields and invariants POS-1..8), the class
docstrings and the owner's specification G04 ("no martingale, averaging down or widening
protective stops; hard protective stops; monotonic profit ratchet based on executable net P&L")
- not from the implementation.

SYNTHETIC FIXTURE: every record in this module is built with the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``).  Prices, volumes, P&L amounts and
instants are invented.  They are not market data, not account data and not evidence of any
trading performance.  "EXTERNAL" positions are fixtures too: no other system was observed.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    Position,
    PositionOwner,
    PositionState,
    RunMode,
    Side,
    from_wire,
    identity,
    to_wire,
    validate_position_transition,
)
from gie.core.errors import ContractValidationError, IllegalTransitionError
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

pytestmark = pytest.mark.requirement("S00-ASG-17")

T0 = samples.T0
OPEN = PositionState.OPEN
CLOSED = PositionState.CLOSED


# --------------------------------------------------------------------------- builders
def _violates(invariant: str, build: Callable[[], object]) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as caught:
        build()
    assert caught.value.details.get("invariant") == invariant, caught.value
    assert caught.value.details.get("contract") == "Position"
    return caught.value


def _refused(previous: Position | None, snapshot: Position) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        validate_position_transition(previous, snapshot)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert caught.value.details.get("position_id") == snapshot.position_id
    return caught.value


def _rule(error: IllegalTransitionError) -> str:
    return str(error.details.get("rule"))


def _fields(position: Position) -> dict[str, Any]:
    return {spec.name: getattr(position, spec.name) for spec in Position.FIELD_SPECS}


def _after(previous: Position, **overrides: Any) -> Position:
    """The next snapshot of the same position, one second later, with *overrides* applied."""
    fields = _fields(previous)
    moment = previous.decision_time_ns + NS_PER_SECOND
    fields.update(
        seq=previous.seq + 1,
        revision=previous.revision + 1,
        decision_time_ns=moment,
        as_of_time_ns=moment - NS_PER_MS,
    )
    fields.update(overrides)
    return Position(**fields)


def _closed(previous: Position, **overrides: Any) -> Position:
    fields: dict[str, Any] = {
        "state": CLOSED,
        "volume_lots": D("0"),
        "mark_price": None,
        "realized_net_pnl_amount": previous.net_pnl_amount,
        "unrealized_net_pnl_amount": D("0"),
        "closed_time_ns": previous.decision_time_ns + NS_PER_SECOND,
        "close_reason": "PROTECTIVE_STOP" if previous.owner is PositionOwner.GIE else None,
    }
    fields.update(overrides)
    return _after(previous, **fields)


def _short(**overrides: Any) -> Position:
    """An open GIE short position: entry 2649.80, initial stop 2651.80 (above the entry)."""
    fields: dict[str, Any] = {
        "side": Side.SELL,
        "initial_stop_price": D("2651.80"),
        "stop_price": D("2651.80"),
        "mark_price": D("2649.00"),
    }
    fields.update(overrides)
    return samples.position(**fields)


def _external(**overrides: Any) -> Position:
    """An observed position of another system, with a stop, so that widening can be shown."""
    fields: dict[str, Any] = {"initial_stop_price": D("1.0900"), "stop_price": D("1.0900")}
    fields.update(overrides)
    return samples.external_position(**fields)


# --------------------------------------------------------------------------- samples
def test_the_sample_lifecycle_is_a_valid_chain() -> None:
    opened, tightened, closed = samples.position_lifecycle()
    validate_position_transition(None, opened)
    validate_position_transition(opened, tightened)
    validate_position_transition(tightened, closed)
    assert [p.revision for p in (opened, tightened, closed)] == [0, 1, 2]
    assert (opened.stop_price, tightened.stop_price) == (D("2647.80"), D("2649.90"))
    assert (opened.state, closed.state) == (OPEN, CLOSED)


def test_every_named_sample_position_survives_the_wire() -> None:
    variants = samples.variants()["Position"]
    assert set(variants) == {"open", "tightened_partial", "closed", "external_other_bot"}
    for record in variants.values():
        assert type(record) is Position
        assert from_wire(to_wire(record), Position) == record


@pytest.mark.requirement("G04-13")
def test_executable_net_pnl_is_realised_plus_unrealised_exactly() -> None:
    position = samples.position(
        realized_net_pnl_amount=D("-2.10"),
        unrealized_net_pnl_amount=D("38.30"),
        peak_net_pnl_amount=D("36.20"),
    )
    assert position.net_pnl_amount == D("36.20")
    assert isinstance(position.net_pnl_amount, D)
    assert position.net_r == pytest.approx(36.20 / 104.00)
    assert samples.external_position().net_r is None


# --------------------------------------------------------------------------- POS-1
def test_pos1_open_means_volume_and_no_close_time() -> None:
    opened = samples.position()
    assert (opened.state, opened.volume_lots > 0, opened.closed_time_ns) == (OPEN, True, None)
    closed = samples.position_lifecycle()[2]
    assert (closed.state, closed.volume_lots, closed.closed_time_ns is not None) == (CLOSED, D("0"), True)
    _violates("POS-1", lambda: samples.position(volume_lots=D("0")))
    _violates("POS-1", lambda: samples.position(closed_time_ns=T0 + 3 * NS_PER_SECOND))
    _violates("POS-1", lambda: samples.position(state=CLOSED))
    _violates("POS-1", lambda: _closed(samples.position(), volume_lots=D("0.10")))
    _violates("POS-1", lambda: _closed(samples.position(), closed_time_ns=None))


# --------------------------------------------------------------------------- POS-2
@pytest.mark.requirement("G04-09", "G04-10")
def test_pos2_the_open_volume_never_exceeds_the_initial_volume() -> None:
    assert samples.position(volume_lots=D("0.50"), initial_volume_lots=D("0.50")).volume_lots == D("0.50")
    assert samples.position(volume_lots=D("0.25")).volume_lots == D("0.25")
    _violates("POS-2", lambda: samples.position(volume_lots=D("0.500000000001")))
    _violates("POS-2", lambda: samples.position(volume_lots=D("1.00")))
    _violates("POS-2", lambda: samples.external_position(volume_lots=D("0.51")))


# --------------------------------------------------------------------------- POS-3
@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    "broken",
    [
        {"symbol": "XAGUSD"},
        {"plan_id": None},
        {"initial_stop_price": None},
        {"initial_risk_amount": None},
        {"external_ref": "FIXTURE-TICKET-1"},
        {"stop_price": None},
        {"mark_price": None},
    ],
    ids=["not_gold", "no_plan", "no_initial_stop", "no_initial_risk", "external_ref", "no_stop", "no_mark"],
)
def test_pos3_a_gie_position_is_gold_with_plan_order_stop_risk_and_mark(broken: dict[str, Any]) -> None:
    _violates("POS-3", lambda: samples.position(**broken))


@pytest.mark.requirement("G04-12")
def test_pos3_a_gie_position_needs_its_opening_order() -> None:
    good = samples.position()
    assert good.opening_client_order_id == samples.order_event().client_order_id
    _violates("POS-3", lambda: samples.position(opening_client_order_id=None, position_id=good.position_id))


def test_pos3_the_id_of_a_gie_position_is_derived_from_run_and_opening_order() -> None:
    good = samples.position()
    assert good.opening_client_order_id is not None
    assert good.position_id == identity.position_id(good.run_id, good.opening_client_order_id)
    other_order = samples.protective_stop_lifecycle()[0].client_order_id
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    for foreign in (
        identity.position_id(good.run_id, other_order),
        identity.position_id(other_run, good.opening_client_order_id),
        identity.external_position_id("fixture-account", "FIXTURE-TICKET-1"),
    ):
        _violates("POS-3", lambda foreign=foreign: samples.position(position_id=foreign))


def test_pos3_a_closed_gie_position_needs_neither_stop_nor_mark() -> None:
    closed = _closed(samples.position(), stop_price=None, mark_price=None)
    assert (closed.stop_price, closed.mark_price) == (None, None)


@pytest.mark.parametrize(
    "broken",
    [
        {"external_ref": None},
        {"plan_id": samples.trade_plan().plan_id},
        {"opening_client_order_id": samples.order_event().client_order_id},
    ],
    ids=["no_external_ref", "plan", "opening_order"],
)
def test_pos3_an_external_position_has_a_ticket_and_no_gie_origin(broken: dict[str, Any]) -> None:
    assert samples.external_position().external_ref == "FIXTURE-TICKET-1"
    _violates("POS-3", lambda: samples.external_position(**broken))


def test_pos3_an_external_position_may_be_in_any_symbol_and_without_stop_or_mark() -> None:
    silver = samples.external_position(symbol="XAGUSD", mark_price=None)
    assert (silver.symbol, silver.stop_price, silver.initial_risk_amount) == ("XAGUSD", None, None)
    gold = samples.external_position(symbol="XAUUSD")
    assert gold.owner is PositionOwner.EXTERNAL


def test_an_external_reference_is_never_free_text() -> None:
    for reference in ("ticket 1", "ticket\n1", "x" * 65, ""):
        with pytest.raises(ContractValidationError) as caught:
            samples.external_position(external_ref=reference)
        assert caught.value.details.get("path") == "external_ref"


# --------------------------------------------------------------------------- POS-4
@pytest.mark.requirement("G04-11", "G04-12")
@pytest.mark.parametrize(
    ("initial", "stop", "accepted"),
    [
        ("2647.80", "2647.80", True),
        ("2647.80", "2647.800000000001", True),
        ("2647.80", "2649.90", True),
        ("2647.80", "2650.50", True),
        ("2649.799999999999", "2649.799999999999", True),
        ("2649.80", "2649.80", False),
        ("2650.00", "2650.00", False),
        ("2647.80", "2647.799999999999", False),
        ("2647.80", "2640.00", False),
    ],
    ids=[
        "at_initial",
        "one_ulp_tighter",
        "tightened",
        "locked_in_profit",
        "initial_just_below_entry",
        "initial_at_entry",
        "initial_above_entry",
        "one_ulp_wider",
        "wider",
    ],
)
def test_pos4_long_position_stop_geometry(initial: str, stop: str, accepted: bool) -> None:
    fields = {"initial_stop_price": D(initial), "stop_price": D(stop)}
    if accepted:
        assert samples.position(**fields).stop_price == D(stop)
    else:
        _violates("POS-4", lambda: samples.position(**fields))


@pytest.mark.requirement("G04-11", "G04-12")
@pytest.mark.parametrize(
    ("initial", "stop", "accepted"),
    [
        ("2651.80", "2651.80", True),
        ("2651.80", "2651.799999999999", True),
        ("2651.80", "2649.00", True),
        ("2649.800000000001", "2649.800000000001", True),
        ("2649.80", "2649.80", False),
        ("2649.00", "2649.00", False),
        ("2651.80", "2651.800000000001", False),
        ("2651.80", "2660.00", False),
    ],
)
def test_pos4_short_position_stop_geometry(initial: str, stop: str, accepted: bool) -> None:
    fields = {"initial_stop_price": D(initial), "stop_price": D(stop)}
    if accepted:
        assert _short(**fields).stop_price == D(stop)
    else:
        _violates("POS-4", lambda: _short(**fields))


@pytest.mark.requirement("G04-11")
def test_pos4_applies_to_the_stop_of_a_closed_gie_position_too() -> None:
    _violates("POS-4", lambda: _closed(samples.position(), stop_price=D("2640.00")))


def test_pos4_does_not_judge_what_another_system_did_with_its_stop() -> None:
    widened = samples.external_position(initial_stop_price=D("1.0900"), stop_price=D("1.1000"))
    assert widened.stop_price == D("1.1000")
    wrong_side = samples.external_position(initial_stop_price=D("1.0800"), stop_price=D("1.0800"))
    assert wrong_side.initial_stop_price == D("1.0800")


# --------------------------------------------------------------------------- POS-5 .. POS-8
@pytest.mark.requirement("G04-13")
def test_pos5_the_net_pnl_lies_between_trough_and_peak() -> None:
    at_both = samples.position(peak_net_pnl_amount=D("36.00"), trough_net_pnl_amount=D("36.00"))
    assert at_both.net_pnl_amount == at_both.peak_net_pnl_amount == at_both.trough_net_pnl_amount
    _violates("POS-5", lambda: samples.position(peak_net_pnl_amount=D("35.99")))
    _violates("POS-5", lambda: samples.position(peak_net_pnl_amount=D("35.999999999999")))
    _violates("POS-5", lambda: samples.position(trough_net_pnl_amount=D("36.01")))
    _violates("POS-5", lambda: samples.position(trough_net_pnl_amount=D("36.000000000001")))
    _violates(
        "POS-5",
        lambda: samples.external_position(peak_net_pnl_amount=D("39.99")),
    )


def test_pos6_a_closed_position_has_nothing_unrealised() -> None:
    opened = samples.position()
    assert _closed(opened).unrealized_net_pnl_amount == 0
    _violates(
        "POS-6",
        lambda: _closed(opened, realized_net_pnl_amount=D("30.00"), unrealized_net_pnl_amount=D("6.00")),
    )
    _violates(
        "POS-6",
        lambda: _closed(
            samples.external_position(),
            realized_net_pnl_amount=D("30.00"),
            unrealized_net_pnl_amount=D("10.00"),
        ),
    )


def test_pos7_times_of_a_position_are_ordered() -> None:
    opened = samples.position()
    assert opened.opened_time_ns <= opened.peak_time_ns <= opened.decision_time_ns
    at_open = samples.position(peak_time_ns=opened.opened_time_ns)
    assert at_open.peak_time_ns == at_open.opened_time_ns
    _violates("POS-7", lambda: samples.position(peak_time_ns=opened.opened_time_ns - 1))
    _violates("POS-7", lambda: samples.position(peak_time_ns=opened.decision_time_ns + 1))
    _violates("POS-7", lambda: samples.position(opened_time_ns=opened.decision_time_ns + 1))
    closing = _closed(opened)
    assert closing.closed_time_ns == closing.decision_time_ns
    _violates("POS-7", lambda: _closed(opened, closed_time_ns=opened.opened_time_ns - 1))
    _violates("POS-7", lambda: _closed(opened, closed_time_ns=closing.decision_time_ns + 1))


def test_pos8_the_close_reason_exists_exactly_for_a_closed_gie_position() -> None:
    opened = samples.position()
    assert opened.close_reason is None
    assert _closed(opened, close_reason="RATCHET").close_reason == "RATCHET"
    _violates("POS-8", lambda: samples.position(close_reason="RATCHET"))
    _violates("POS-8", lambda: _closed(opened, close_reason=None))
    _violates("POS-8", lambda: samples.external_position(close_reason="OBSERVED_CLOSE"))
    assert _closed(samples.external_position()).close_reason is None


def test_a_snapshot_cannot_depend_on_data_received_after_it_was_made() -> None:
    opened = samples.position()
    with pytest.raises(ContractValidationError) as caught:
        samples.position(as_of_time_ns=opened.decision_time_ns + 1)
    assert caught.value.details.get("invariant") == "CAUSAL-1"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("volume_lots", D("-0.1")),
        ("volume_lots", 0.5),
        ("initial_volume_lots", D("0")),
        ("average_entry_price", D("0")),
        ("stop_price", 2647.8),
        ("initial_risk_amount", D("0")),
        ("costs_amount", D("-0.01")),
        ("realized_net_pnl_amount", -2.0),
        ("owner", "GIE"),
        ("state", "OPEN"),
        ("side", "BUY"),
        ("mode", "LIVE"),
        ("account_currency", "usd"),
        ("close_reason", "stopped out"),
        ("revision", -1),
    ],
)
def test_invalid_field_values_are_refused_and_never_coerced(path: str, value: object) -> None:
    with pytest.raises(ContractValidationError) as caught:
        samples.position(**{path: value})
    assert caught.value.details.get("path") == path


# --------------------------------------------------------------------------- transitions: GIE
def test_the_first_snapshot_has_revision_zero() -> None:
    validate_position_transition(None, samples.position())
    _refused(None, samples.position(revision=1))
    _refused(None, samples.position_lifecycle()[1])


def test_revisions_increase_by_exactly_one() -> None:
    opened = samples.position()
    validate_position_transition(opened, _after(opened))
    assert "revision" in _rule(_refused(opened, _after(opened, revision=2)))
    _refused(opened, _after(opened, revision=0))
    _refused(_after(opened), _after(opened, seq=2))


@pytest.mark.requirement("G04-11")
@pytest.mark.parametrize(
    ("before", "after", "allowed"),
    [
        ("2649.90", "2649.90", True),
        ("2649.90", "2649.900000000001", True),
        ("2649.90", "2651.00", True),
        ("2649.90", "2649.899999999999", False),
        ("2649.90", "2647.80", False),
    ],
    ids=["unchanged", "one_ulp_tighter", "tighter", "one_ulp_wider", "back_to_initial"],
)
def test_the_stop_of_a_long_gie_position_only_moves_up(before: str, after: str, allowed: bool) -> None:
    previous = samples.position(stop_price=D(before))
    snapshot = _after(previous, stop_price=D(after))
    if allowed:
        validate_position_transition(previous, snapshot)
    else:
        assert "stop" in _rule(_refused(previous, snapshot))


@pytest.mark.requirement("G04-11")
@pytest.mark.parametrize(
    ("before", "after", "allowed"),
    [
        ("2650.50", "2650.50", True),
        ("2650.50", "2650.499999999999", True),
        ("2650.50", "2648.00", True),
        ("2650.50", "2650.500000000001", False),
        ("2650.50", "2651.80", False),
    ],
    ids=["unchanged", "one_ulp_tighter", "tighter", "one_ulp_wider", "back_to_initial"],
)
def test_the_stop_of_a_short_gie_position_only_moves_down(before: str, after: str, allowed: bool) -> None:
    previous = _short(stop_price=D(before))
    snapshot = _after(previous, stop_price=D(after))
    if allowed:
        validate_position_transition(previous, snapshot)
    else:
        assert "stop" in _rule(_refused(previous, snapshot))


@pytest.mark.requirement("G04-11", "G04-12")
def test_the_stop_of_an_open_gie_position_cannot_be_removed() -> None:
    opened = samples.position()
    _violates("POS-3", lambda: _after(opened, stop_price=None))


@pytest.mark.requirement("G04-09", "G04-10")
def test_the_volume_of_a_gie_position_never_increases() -> None:
    opened, reduced, _ = samples.position_lifecycle()
    assert (opened.volume_lots, reduced.volume_lots) == (D("0.50"), D("0.25"))
    validate_position_transition(reduced, _after(reduced))
    validate_position_transition(reduced, _after(reduced, volume_lots=D("0.10")))
    for grown in ("0.250000000001", "0.40", "0.50"):
        snapshot = _after(reduced, volume_lots=D(grown))
        assert "volume_lots" in _rule(_refused(reduced, snapshot))


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize(
    "changed",
    [
        {"initial_volume_lots": D("1.00")},
        {"average_entry_price": D("2649.00")},
        {"average_entry_price": D("2650.20")},
        {"initial_stop_price": D("2647.00")},
        {"initial_risk_amount": D("208.00")},
    ],
    ids=["initial_volume", "averaged_down", "averaged_up", "initial_stop", "initial_risk"],
)
def test_what_was_fixed_at_the_opening_of_a_gie_position_stays_fixed(changed: dict[str, Any]) -> None:
    opened = samples.position()
    validate_position_transition(opened, _after(opened))
    _refused(opened, _after(opened, **changed))


@pytest.mark.parametrize(
    "changed",
    [
        {"side": Side.SELL, "initial_stop_price": D("2651.80"), "stop_price": D("2651.80")},
        {"account_currency": "EUR"},
        {"opened_time_ns": T0 + NS_PER_SECOND},
        {"mode": RunMode.PAPER},
        {"plan_id": samples.trade_plan(variant=1).plan_id},
    ],
    ids=["side", "account_currency", "opened_time", "mode", "plan"],
)
def test_the_identity_of_a_gie_position_never_changes(changed: dict[str, Any]) -> None:
    opened = samples.position()
    snapshot = _after(opened, **changed)
    assert snapshot.position_id == opened.position_id
    _refused(opened, snapshot)


def test_a_snapshot_of_another_position_cannot_continue_this_one() -> None:
    opened = samples.position()
    other_order = samples.protective_stop_lifecycle()[0].client_order_id
    other = _after(
        opened,
        opening_client_order_id=other_order,
        position_id=identity.position_id(opened.run_id, other_order),
    )
    assert "position_id" in _rule(_refused(opened, other))
    assert opened.opening_client_order_id is not None
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    foreign = _after(
        opened,
        run_id=other_run,
        position_id=identity.position_id(other_run, opened.opening_client_order_id),
    )
    _refused(opened, foreign)


@pytest.mark.requirement("G04-13")
def test_the_peak_never_decreases() -> None:
    opened = samples.position()
    assert opened.peak_net_pnl_amount == D("36.00")
    lower_now = {"unrealized_net_pnl_amount": D("10.00")}
    validate_position_transition(opened, _after(opened, **lower_now))
    for lower in ("35.999999999999", "20.00", "8.00"):
        snapshot = _after(opened, peak_net_pnl_amount=D(lower), **lower_now)
        assert "peak_net_pnl_amount" in _rule(_refused(opened, snapshot))


@pytest.mark.requirement("G04-13")
def test_a_new_peak_carries_the_decision_time_of_the_snapshot_that_reached_it() -> None:
    opened = samples.position()
    higher = {"unrealized_net_pnl_amount": D("52.00"), "peak_net_pnl_amount": D("50.00")}
    moment = opened.decision_time_ns + NS_PER_SECOND
    reached = _after(opened, peak_time_ns=moment, **higher)
    assert reached.peak_time_ns == reached.decision_time_ns
    validate_position_transition(opened, reached)
    for stale in (opened.peak_time_ns, moment - 1, opened.opened_time_ns):
        assert "peak" in _rule(_refused(opened, _after(opened, peak_time_ns=stale, **higher)))


@pytest.mark.requirement("G04-13")
def test_the_peak_time_stays_while_no_new_peak_is_reached() -> None:
    opened = samples.position()
    moment = opened.decision_time_ns + NS_PER_SECOND
    assert "peak_time_ns" in _rule(_refused(opened, _after(opened, peak_time_ns=moment)))
    _refused(opened, _after(opened, peak_time_ns=opened.opened_time_ns))


def test_the_trough_never_increases() -> None:
    opened = samples.position()
    assert opened.trough_net_pnl_amount == D("-14.50")
    validate_position_transition(opened, _after(opened, trough_net_pnl_amount=D("-20.00")))
    for higher in ("-14.499999999999", "0", "36.00"):
        snapshot = _after(opened, trough_net_pnl_amount=D(higher))
        assert "trough_net_pnl_amount" in _rule(_refused(opened, snapshot))


@pytest.mark.parametrize("field", ["decision_time_ns", "as_of_time_ns", "seq"])
def test_time_and_stream_position_of_snapshots_never_go_back(field: str) -> None:
    previous = samples.position(seq=3)
    overrides: dict[str, Any] = {field: getattr(previous, field) - 1}
    if field == "decision_time_ns":
        overrides["as_of_time_ns"] = previous.as_of_time_ns
        overrides["peak_time_ns"] = previous.opened_time_ns
        previous = samples.position(seq=3, peak_time_ns=previous.opened_time_ns)
    validate_position_transition(previous, _after(previous))
    assert field in _rule(_refused(previous, _after(previous, **overrides)))


def test_costs_charged_so_far_never_decrease() -> None:
    opened = samples.position()
    validate_position_transition(opened, _after(opened, costs_amount=D("2.50")))
    _refused(opened, _after(opened, costs_amount=D("1.99")))


@pytest.mark.parametrize("owner", [PositionOwner.GIE, PositionOwner.EXTERNAL])
def test_nothing_follows_a_closed_position(owner: PositionOwner) -> None:
    opened = samples.position() if owner is PositionOwner.GIE else samples.external_position()
    closed = _closed(opened)
    validate_position_transition(opened, closed)
    again = _after(closed, closed_time_ns=closed.closed_time_ns)
    assert "CLOSED" in _rule(_refused(closed, again))
    reopened = _after(
        closed,
        state=OPEN,
        volume_lots=D("0.10"),
        closed_time_ns=None,
        close_reason=None,
        stop_price=opened.stop_price,
        mark_price=opened.mark_price,
    )
    _refused(closed, reopened)


def test_closing_a_gie_position_keeps_its_history() -> None:
    _opened, tightened, closed = samples.position_lifecycle()
    assert closed.peak_net_pnl_amount == tightened.peak_net_pnl_amount
    assert closed.peak_time_ns == tightened.peak_time_ns
    validate_position_transition(tightened, closed)
    _refused(tightened, _closed(tightened, initial_stop_price=D("2647.00"), stop_price=D("2649.90")))


# --------------------------------------------------------------------------- transitions: EXTERNAL
def test_an_external_position_is_observed_not_judged() -> None:
    observed = _external()
    assert (observed.side, observed.stop_price) == (Side.SELL, D("1.0900"))
    widened = _after(observed, stop_price=D("1.1000"))
    removed = _after(observed, stop_price=None)
    grown = _after(observed, volume_lots=D("0.80"), initial_volume_lots=D("0.80"))
    averaged = _after(observed, average_entry_price=D("1.0870"), initial_stop_price=D("1.0950"))
    for snapshot in (widened, removed, grown, averaged, _after(observed)):
        validate_position_transition(observed, snapshot)


def test_an_external_position_may_be_reduced_and_closed() -> None:
    observed = _external()
    reduced = _after(observed, volume_lots=D("0.20"))
    validate_position_transition(observed, reduced)
    back_up = _after(reduced, volume_lots=D("0.50"))
    validate_position_transition(reduced, back_up)
    validate_position_transition(back_up, _closed(back_up))


def test_the_largest_observed_volume_of_an_external_position_never_decreases() -> None:
    observed = _external()
    shrunk = _after(observed, volume_lots=D("0.20"), initial_volume_lots=D("0.20"))
    assert "volume" in _rule(_refused(observed, shrunk))


@pytest.mark.parametrize(
    "changed",
    [
        {"external_ref": "FIXTURE-TICKET-2"},
        {"position_id": identity.external_position_id("fixture-account", "FIXTURE-TICKET-2")},
        {"symbol": "GBPUSD"},
        {"side": Side.BUY},
        {"account_currency": "EUR"},
        {"opened_time_ns": T0 + NS_PER_SECOND},
        {"mode": RunMode.PAPER},
    ],
    ids=["ticket", "position_id", "symbol", "side", "currency", "opened_time", "mode"],
)
def test_the_identity_of_an_external_position_is_still_enforced(changed: dict[str, Any]) -> None:
    observed = _external()
    validate_position_transition(observed, _after(observed))
    _refused(observed, _after(observed, **changed))


def test_an_external_position_cannot_become_a_gie_position() -> None:
    observed = samples.external_position(symbol="XAUUSD", average_entry_price=D("2649.80"))
    order = samples.order_event()
    adopted = _after(
        observed,
        owner=PositionOwner.GIE,
        external_ref=None,
        plan_id=order.plan_id,
        opening_client_order_id=order.client_order_id,
        position_id=identity.position_id(observed.run_id, order.client_order_id),
        initial_stop_price=D("2651.80"),
        stop_price=D("2651.80"),
        mark_price=D("2649.00"),
        initial_risk_amount=D("104.00"),
    )
    _refused(observed, adopted)


@pytest.mark.parametrize("field", ["decision_time_ns", "as_of_time_ns", "seq"])
def test_time_order_of_external_snapshots_is_still_enforced(field: str) -> None:
    previous = _external(seq=3)
    overrides: dict[str, Any] = {field: getattr(previous, field) - 1}
    if field == "decision_time_ns":
        previous = _external(seq=3, peak_time_ns=previous.opened_time_ns)
        overrides["as_of_time_ns"] = previous.as_of_time_ns
    assert field in _rule(_refused(previous, _after(previous, **overrides)))


def test_revisions_of_external_snapshots_are_still_continuous() -> None:
    observed = _external()
    _refused(observed, _after(observed, revision=2))
    _refused(None, _external(revision=1))
