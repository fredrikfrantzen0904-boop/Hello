"""Independent tests for ``gie.contracts.authorization`` - who may create risk, and nothing else.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (section 3.3 "Freshness",
section 9.3 "Authorisation - enforced by the order gateway"), the module and function docstrings
and the owner's specification (G04 "no martingale, averaging down or widening protective stops;
hard protective stops", G08 "idempotent orders") - not from the implementation:

* an **entry** needs its SELECTED plan and its unexpired risk approval: same run, same plan
  revision and plan content (``plan_fingerprint``), same alternative group, side, order type and
  price, the plan's stop attached, volume not above the granted volume, nothing expired (fail
  closed at the boundary), and the order not outliving plan or approval;
* **every other order** needs the open GIE position it belongs to and may only reduce it;
* the protective stop of an open position cannot be cancelled;
* GIE never places orders for positions of other systems.

SYNTHETIC FIXTURE: every record in this module is built with the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``).  Plans, approvals, orders, positions,
prices and instants are invented.  They are not market data, not owner decisions and not evidence
of any trading performance.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    EntryStyle,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    OrderType,
    PlanStatus,
    Position,
    PositionState,
    RiskDecision,
    RunMode,
    Side,
    TradePlan,
    fingerprint,
    identity,
    validate_entry_authorization,
    validate_protective_cancel,
    validate_reducing_order,
)
from gie.core.errors import GieError, IllegalTransitionError, StaleDataError
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

pytestmark = pytest.mark.requirement("S00-ASG-15", "S00-ASG-16", "S00-ASG-17")

T0 = samples.T0
#: A clock reading after the request (T0 + 1 ms) and before the approval expires (T0 + 30 s).
NOW = T0 + 2 * NS_PER_MS
APPROVAL_EXPIRY = T0 + 30 * NS_PER_SECOND
PLAN_EXPIRY = T0 + 120 * NS_PER_SECOND


def _triple(**order_overrides: Any) -> tuple[TradePlan, RiskDecision, OrderEvent]:
    plan = samples.trade_plan()
    return plan, samples.risk_decision(plan), samples.order_event(**order_overrides)


def _refused_entry(
    plan: TradePlan, decision: RiskDecision, request: OrderEvent, now_ns: int = NOW
) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        validate_entry_authorization(plan, decision, request, now_ns=now_ns)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert caught.value.details.get("client_order_id") == request.client_order_id
    assert caught.value.details.get("rule")
    return caught.value


def _stale_entry(plan: TradePlan, decision: RiskDecision, request: OrderEvent, now_ns: int) -> StaleDataError:
    with pytest.raises(StaleDataError) as caught:
        validate_entry_authorization(plan, decision, request, now_ns=now_ns)
    assert caught.value.code == "GIE.TIME.STALE"
    return caught.value


def _refused_reducing(position: Position, request: OrderEvent) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        validate_reducing_order(position, request)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert caught.value.details.get("client_order_id") == request.client_order_id
    return caught.value


def _child(
    position: Position,
    intent: OrderIntent,
    order_type: OrderType,
    *,
    price: str | None = None,
    **overrides: Any,
) -> OrderEvent:
    """SUBMIT_REQUESTED of a non-entry order of *position* on the reducing side."""
    fields: dict[str, Any] = {
        "run_id": position.run_id,
        "mode": position.mode,
        "side": Side.SELL if position.side is Side.BUY else Side.BUY,
        "intent": intent,
        "order_type": order_type,
        "volume_lots": position.volume_lots if position.volume_lots > 0 else D("0.50"),
        "limit_price": D(price) if price is not None and order_type is OrderType.LIMIT else None,
        "trigger_price": D(price) if price is not None and order_type is OrderType.STOP else None,
        "reference_price": None,
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": position.position_id,
        "reason_code": "FIXTURE_REASON",
    }
    fields.update(overrides)
    return samples.order_event(**fields)


def _replace_request(submit: OrderEvent, **requested: Any) -> OrderEvent:
    """REPLACE_REQUESTED for the order of *submit* (order_seq 2: after its acceptance)."""
    fields = {spec.name: getattr(submit, spec.name) for spec in OrderEvent.FIELD_SPECS}
    del fields["order_event_id"]
    fields.update(
        seq=submit.seq + 2,
        order_seq=2,
        event_type=OrderEventType.REPLACE_REQUESTED,
        order_state=OrderState.PENDING_REPLACE,
        event_time_ns=submit.event_time_ns + NS_PER_SECOND,
        receive_time_ns=submit.event_time_ns + NS_PER_SECOND,
        broker_order_id=identity.broker_order_id(submit.client_order_id),
    )
    fields.update(requested)
    return samples.order_event(**fields)


def _cancel_request(submit: OrderEvent) -> OrderEvent:
    fields = {spec.name: getattr(submit, spec.name) for spec in OrderEvent.FIELD_SPECS}
    del fields["order_event_id"]
    fields.update(
        seq=submit.seq + 2,
        order_seq=2,
        event_type=OrderEventType.CANCEL_REQUESTED,
        order_state=OrderState.PENDING_CANCEL,
        event_time_ns=submit.event_time_ns + NS_PER_SECOND,
        receive_time_ns=submit.event_time_ns + NS_PER_SECOND,
        broker_order_id=identity.broker_order_id(submit.client_order_id),
    )
    return samples.order_event(**fields)


def _short_position(**overrides: Any) -> Position:
    fields: dict[str, Any] = {
        "side": Side.SELL,
        "initial_stop_price": D("2651.80"),
        "stop_price": D("2651.80"),
        "mark_price": D("2649.00"),
    }
    fields.update(overrides)
    return samples.position(**fields)


def _closed_position() -> Position:
    closed = samples.position_lifecycle()[2]
    assert closed.state is PositionState.CLOSED
    return closed


# =========================================================================== entry authorisation
@pytest.mark.requirement("S08-ACC-06")
def test_the_consistent_sample_triple_is_authorised() -> None:
    plan, decision, order = _triple()
    assert plan.status is PlanStatus.SELECTED
    assert decision.approved is True
    assert decision.plan_fingerprint == fingerprint(plan)
    assert (order.plan_id, order.risk_decision_id) == (plan.plan_id, decision.decision_id)
    assert validate_entry_authorization(plan, decision, order, now_ns=NOW) is None


def test_a_consistent_market_entry_is_authorised() -> None:
    plan = samples.trade_plan(
        entry_style=EntryStyle.MARKET, entry_order_type=OrderType.MARKET, entry_price=None
    )
    decision = samples.risk_decision(plan)
    order = samples.order_event(
        order_type=OrderType.MARKET,
        limit_price=None,
        valid_until_ns=None,
        plan_id=plan.plan_id,
        risk_decision_id=decision.decision_id,
    )
    validate_entry_authorization(plan, decision, order, now_ns=NOW)


def test_a_consistent_paper_triple_is_authorised() -> None:
    plan = samples.trade_plan(mode=RunMode.PAPER)
    decision = samples.risk_decision(plan, mode=RunMode.PAPER)
    order = samples.order_event(mode=RunMode.PAPER)
    validate_entry_authorization(plan, decision, order, now_ns=NOW)


@pytest.mark.parametrize("status", [status for status in PlanStatus if status is not PlanStatus.SELECTED])
def test_an_entry_is_only_built_from_a_selected_plan(status: PlanStatus) -> None:
    plan = samples.trade_plan(status=status, status_reasons=("FIXTURE_REASON",))
    decision = samples.risk_decision(plan)
    order = samples.order_event()
    assert decision.plan_fingerprint == fingerprint(plan)
    error = _refused_entry(plan, decision, order)
    assert "SELECTED" in str(error.details["rule"])


def test_a_rejection_authorises_nothing() -> None:
    plan = samples.trade_plan()
    rejected = samples.rejected_risk_decision(plan)
    assert rejected.decision_id == samples.risk_decision(plan).decision_id
    _refused_entry(plan, rejected, samples.order_event())


def test_plan_approval_and_order_must_name_the_same_plan() -> None:
    plan, decision, order = _triple()
    other_plan = samples.trade_plan(variant=1)
    assert other_plan.plan_id != plan.plan_id
    assert other_plan.alternative_group_id == plan.alternative_group_id
    # The approval was given for another plan.
    _refused_entry(plan, samples.risk_decision(other_plan), order)
    # The plan handed in is not the one that was approved and ordered.
    _refused_entry(other_plan, decision, order)
    # The order names another plan than the one that was approved.
    _refused_entry(plan, decision, samples.order_event(plan_id=other_plan.plan_id))


@pytest.mark.requirement("S08-ACC-06")
def test_the_order_must_cite_exactly_this_approval() -> None:
    plan, decision, _order = _triple()
    second_approval = samples.risk_decision(plan, attempt=1, seq=1)
    assert second_approval.decision_id != decision.decision_id
    order_of_second = samples.order_event(risk_decision_id=second_approval.decision_id)
    validate_entry_authorization(plan, second_approval, order_of_second, now_ns=NOW)
    _refused_entry(plan, decision, order_of_second)
    _refused_entry(plan, second_approval, samples.order_event())


@pytest.mark.parametrize(
    "changed",
    [
        {"max_spread": D("0.50")},
        {"overnight_allowed": True},
        {"expected_net_r": 0.30},
        {"valid_until_ns": T0 + 121 * NS_PER_SECOND},
    ],
    ids=["max_spread", "overnight", "estimate", "expiry"],
)
def test_a_plan_whose_content_changed_after_the_approval_is_refused(changed: dict[str, Any]) -> None:
    approved_plan, decision, order = _triple()
    changed_plan = samples.trade_plan(**changed)
    assert changed_plan.plan_id == approved_plan.plan_id
    assert fingerprint(changed_plan) != decision.plan_fingerprint
    error = _refused_entry(changed_plan, decision, order)
    assert "plan_fingerprint" in str(error.details["rule"])


@pytest.mark.requirement("G04-11", "G04-12")
def test_a_plan_with_another_stop_under_the_same_id_does_not_inherit_the_approval() -> None:
    approved_plan, decision, _order = _triple()
    for stop in ("2647.00", "2648.50"):
        moved_plan = samples.trade_plan(stop_price=D(stop))
        assert moved_plan.plan_id == approved_plan.plan_id
        order = samples.order_event(attached_stop_price=D(stop))
        _refused_entry(moved_plan, decision, order)
        # With an approval of its own the moved plan is a consistent triple again.
        validate_entry_authorization(moved_plan, samples.risk_decision(moved_plan), order, now_ns=NOW)


@pytest.mark.parametrize("wrong", ["f" * 64, "0" * 64])
def test_an_approval_with_a_foreign_plan_fingerprint_is_refused(wrong: str) -> None:
    plan, _decision, order = _triple()
    forged = samples.risk_decision(plan, plan_fingerprint=wrong)
    error = _refused_entry(plan, forged, order)
    assert "plan_fingerprint" in str(error.details["rule"])


def test_plan_approval_and_order_must_be_in_the_same_alternative_group() -> None:
    plan, decision, order = _triple()
    other_group = identity.alternative_group_id(samples.unscored_candidate().opportunity_id)
    assert other_group != plan.alternative_group_id
    _refused_entry(plan, samples.risk_decision(plan, alternative_group_id=other_group), order)
    _refused_entry(plan, decision, samples.order_event(alternative_group_id=other_group))


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    "deviation",
    [
        {"side": Side.SELL, "attached_stop_price": D("2651.80"), "reference_price": D("2650.10")},
        {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2649.80")},
        {"order_type": OrderType.MARKET, "limit_price": None},
        {"limit_price": D("2649.81")},
        {"limit_price": D("2649.79")},
        {"limit_price": D("2649.800000000001")},
        {"attached_stop_price": D("2647.79")},
        {"attached_stop_price": D("2647.81")},
        {"attached_stop_price": D("2649.00")},
    ],
    ids=[
        "other_side",
        "stop_instead_of_limit",
        "market_instead_of_limit",
        "price_above",
        "price_below",
        "price_one_ulp_off",
        "stop_wider_than_planned",
        "stop_tighter_than_planned",
        "stop_elsewhere",
    ],
)
def test_the_order_must_be_the_order_the_plan_describes(deviation: dict[str, Any]) -> None:
    plan, decision, _order = _triple()
    _refused_entry(plan, decision, samples.order_event(**deviation))


@pytest.mark.requirement("G04-09", "S08-ACC-06")
@pytest.mark.parametrize(
    ("volume", "allowed"),
    [("0.50", True), ("0.5", True), ("0.01", True), ("0.500000000001", False), ("0.51", False), ("5", False)],
)
def test_the_order_volume_may_reach_but_not_exceed_the_granted_volume(volume: str, allowed: bool) -> None:
    plan, decision, _order = _triple()
    assert decision.volume_lots == D("0.50")
    order = samples.order_event(volume_lots=D(volume))
    if allowed:
        validate_entry_authorization(plan, decision, order, now_ns=NOW)
    else:
        error = _refused_entry(plan, decision, order)
        assert "volume" in str(error.details["rule"])


def test_the_reduced_grant_of_an_approved_reduced_decision_is_the_limit() -> None:
    plan = samples.trade_plan()
    reduced = samples.variants()["RiskDecision"]["approved_reduced_defensive"]
    assert reduced.volume_lots == D("0.25")
    half = samples.order_event(volume_lots=D("0.25"), risk_decision_id=reduced.decision_id)
    validate_entry_authorization(plan, reduced, half, now_ns=NOW)
    full = samples.order_event(volume_lots=D("0.50"), risk_decision_id=reduced.decision_id)
    _refused_entry(plan, reduced, full)


# --------------------------------------------------------------------------- expiry (fail closed)
def test_the_approval_is_usable_until_just_before_its_expiry() -> None:
    plan, decision, order = _triple()
    assert (decision.valid_until_ns, plan.valid_until_ns) == (APPROVAL_EXPIRY, PLAN_EXPIRY)
    validate_entry_authorization(plan, decision, order, now_ns=APPROVAL_EXPIRY - 1)


@pytest.mark.parametrize("late_ns", [0, 1, NS_PER_SECOND, 3600 * NS_PER_SECOND])
def test_an_expired_approval_is_stale_from_the_expiry_instant_on(late_ns: int) -> None:
    plan, decision, order = _triple()
    error = _stale_entry(plan, decision, order, APPROVAL_EXPIRY + late_ns)
    assert error.details.get("valid_until_ns") == APPROVAL_EXPIRY
    assert isinstance(error, GieError)


def test_an_expired_plan_is_stale_from_the_expiry_instant_on() -> None:
    plan_expiry = T0 + 10 * NS_PER_SECOND
    plan = samples.trade_plan(valid_until_ns=plan_expiry)
    decision = samples.risk_decision(plan)
    assert decision.valid_until_ns == APPROVAL_EXPIRY > plan_expiry
    order = samples.order_event(valid_until_ns=plan_expiry)
    validate_entry_authorization(plan, decision, order, now_ns=plan_expiry - 1)
    _stale_entry(plan, decision, order, plan_expiry)
    _stale_entry(plan, decision, order, plan_expiry + 1)


def test_a_request_created_at_or_after_the_expiry_is_stale_whatever_the_clock_says() -> None:
    plan, decision, _order = _triple()
    for moment in (APPROVAL_EXPIRY, APPROVAL_EXPIRY + NS_PER_MS):
        late_request = samples.order_event(
            event_time_ns=moment, receive_time_ns=moment, valid_until_ns=moment + NS_PER_SECOND
        )
        _stale_entry(plan, decision, late_request, NOW)


@pytest.mark.parametrize(
    ("order_expiry", "allowed"),
    [
        (APPROVAL_EXPIRY, True),
        (APPROVAL_EXPIRY - 1, True),
        (T0 + NS_PER_SECOND, True),
        (APPROVAL_EXPIRY + 1, False),
        (PLAN_EXPIRY, False),
        (PLAN_EXPIRY + 1, False),
    ],
)
def test_the_order_may_not_outlive_its_approval(order_expiry: int, allowed: bool) -> None:
    plan, decision, _order = _triple()
    order = samples.order_event(valid_until_ns=order_expiry)
    if allowed:
        validate_entry_authorization(plan, decision, order, now_ns=NOW)
    else:
        error = _refused_entry(plan, decision, order)
        assert "outlive" in str(error.details["rule"])


def test_the_order_may_not_outlive_its_plan() -> None:
    plan_expiry = T0 + 10 * NS_PER_SECOND
    plan = samples.trade_plan(valid_until_ns=plan_expiry)
    decision = samples.risk_decision(plan)
    validate_entry_authorization(plan, decision, samples.order_event(valid_until_ns=plan_expiry), now_ns=NOW)
    _refused_entry(plan, decision, samples.order_event(valid_until_ns=plan_expiry + 1))
    _refused_entry(plan, decision, samples.order_event(valid_until_ns=APPROVAL_EXPIRY))


# --------------------------------------------------------------------------- run, mode, shape
def test_plan_approval_and_order_must_belong_to_the_same_run() -> None:
    plan, decision, _order = _triple()
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    foreign_order = samples.order_event(run_id=other_run)
    assert foreign_order.run_id != plan.run_id
    _refused_entry(plan, decision, foreign_order)
    # Even when the order cites a decision id of its own run for the same plan.
    foreign_decision_id = identity.risk_decision_id(other_run, plan.plan_id, 0)
    _refused_entry(
        plan, decision, samples.order_event(run_id=other_run, risk_decision_id=foreign_decision_id)
    )
    foreign_plan = samples.trade_plan(run_id=other_run)
    _refused_entry(foreign_plan, decision, samples.order_event())


def test_plan_approval_and_order_must_belong_to_the_same_run_mode() -> None:
    plan, decision, order = _triple()
    _refused_entry(plan, decision, samples.order_event(mode=RunMode.PAPER))
    paper_plan = samples.trade_plan(mode=RunMode.PAPER)
    _refused_entry(paper_plan, samples.risk_decision(paper_plan), order)
    _refused_entry(plan, samples.risk_decision(plan, mode=RunMode.PAPER), order)


def test_an_approval_cannot_be_older_than_its_plan() -> None:
    plan, _decision, order = _triple()
    early = samples.risk_decision(
        plan, decision_time_ns=T0 - NS_PER_SECOND, as_of_time_ns=T0 - NS_PER_SECOND - NS_PER_MS
    )
    assert early.decision_time_ns < plan.decision_time_ns
    _refused_entry(plan, early, order)


def test_an_entry_request_cannot_be_older_than_the_approval_it_cites() -> None:
    # SHARED_CONTRACTS 3.1 / 3.3: an entry order is derived from its risk approval ("entry order <=
    # risk approval <= plan"), and a derived record is "not created before the inputs were
    # available".  A request created before its approval existed was not made against that approval.
    plan, decision, _order = _triple()
    for lead_ns in (1, NS_PER_SECOND):
        moment = decision.decision_time_ns - lead_ns
        early = samples.order_event(event_time_ns=moment, receive_time_ns=moment)
        assert early.event_time_ns < decision.decision_time_ns
        _refused_entry(plan, decision, early)


def test_only_the_submit_request_of_an_entry_is_authorised_as_an_entry() -> None:
    plan, decision, _order = _triple()
    for later_event in samples.order_lifecycle()[1:]:
        _refused_entry(plan, decision, later_event)
    for protective in samples.protective_stop_lifecycle():
        _refused_entry(plan, decision, protective)


def test_a_refused_entry_error_is_journalable() -> None:
    plan, decision, _order = _triple()
    error = _refused_entry(plan, decision, samples.order_event(volume_lots=D("0.51")))
    as_dict = error.to_dict()
    assert as_dict["code"] == "GIE.STATE.ILLEGAL_TRANSITION"
    assert error.details.get("intent") == "ENTRY"


# =========================================================================== reducing orders
@pytest.mark.requirement("G04-12")
def test_the_sample_protective_stop_is_authorised_for_its_position() -> None:
    held = samples.position()
    submit, _working, replace, _replaced = samples.protective_stop_lifecycle()
    assert (submit.side, held.side) == (Side.SELL, Side.BUY)
    assert validate_reducing_order(held, submit) is None
    assert validate_reducing_order(held, replace) is None


def test_take_profit_and_exit_orders_of_the_open_position_are_authorised() -> None:
    held = samples.position()
    validate_reducing_order(held, _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2652.80"))
    validate_reducing_order(held, _child(held, OrderIntent.EXIT, OrderType.MARKET))
    validate_reducing_order(held, _child(held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.25")))
    short = _short_position()
    validate_reducing_order(
        short, _child(short, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price="2651.80")
    )
    validate_reducing_order(short, _child(short, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2646.00"))
    validate_reducing_order(short, _child(short, OrderIntent.EXIT, OrderType.MARKET))


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize(
    ("intent", "order_type", "price"),
    [
        (OrderIntent.EXIT, OrderType.MARKET, None),
        (OrderIntent.EXIT, OrderType.LIMIT, "2648.00"),
        (OrderIntent.TAKE_PROFIT, OrderType.LIMIT, "2652.80"),
        (OrderIntent.PROTECTIVE_STOP, OrderType.STOP, "2651.00"),
    ],
)
def test_an_order_on_the_side_of_the_position_would_add_to_it_and_is_refused(
    intent: OrderIntent, order_type: OrderType, price: str | None
) -> None:
    for held in (samples.position(), _short_position()):
        adding = _child(held, intent, order_type, price=price, side=held.side)
        error = _refused_reducing(held, adding)
        assert "opposite side" in str(error.details["rule"])


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize(
    ("volume", "allowed"),
    [("0.50", True), ("0.25", True), ("0.500000000001", False), ("0.51", False), ("1.00", False)],
)
def test_a_reducing_order_may_reach_but_not_exceed_the_open_volume(volume: str, allowed: bool) -> None:
    held = samples.position()
    assert held.volume_lots == D("0.50")
    for request in (
        _child(held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D(volume)),
        _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2652.80", volume_lots=D(volume)),
    ):
        if allowed:
            validate_reducing_order(held, request)
        else:
            error = _refused_reducing(held, request)
            assert "open volume" in str(error.details["rule"])


@pytest.mark.requirement("G04-09", "G04-10")
def test_the_open_volume_not_the_initial_volume_is_the_limit() -> None:
    reduced = samples.position_lifecycle()[1]
    assert (reduced.volume_lots, reduced.initial_volume_lots) == (D("0.25"), D("0.50"))
    validate_reducing_order(reduced, _child(reduced, OrderIntent.EXIT, OrderType.MARKET))
    _refused_reducing(reduced, _child(reduced, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.50")))


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize("volume", ["0.25", "0.499999999999", "0.51"])
def test_a_protective_stop_covers_the_whole_open_volume(volume: str) -> None:
    held = samples.position()
    partial_cover = _child(
        held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price="2647.80", volume_lots=D(volume)
    )
    _refused_reducing(held, partial_cover)


@pytest.mark.requirement("G04-11", "G04-12")
@pytest.mark.parametrize(
    ("trigger", "allowed"),
    [
        ("2649.90", True),
        ("2649.900000000001", True),
        ("2650.50", True),
        ("2649.899999999999", False),
        ("2647.80", False),
        ("2640.00", False),
    ],
    ids=["at_the_stop", "one_ulp_tighter", "tighter", "one_ulp_looser", "initial_stop", "far_looser"],
)
def test_a_protective_stop_for_a_long_position_is_never_looser_than_its_stop(
    trigger: str, allowed: bool
) -> None:
    held = samples.position(stop_price=D("2649.90"))
    request = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price=trigger, attempt=1)
    if allowed:
        validate_reducing_order(held, request)
    else:
        error = _refused_reducing(held, request)
        assert "looser" in str(error.details["rule"])


@pytest.mark.requirement("G04-11", "G04-12")
@pytest.mark.parametrize(
    ("trigger", "allowed"),
    [
        ("2650.50", True),
        ("2650.499999999999", True),
        ("2649.50", True),
        ("2650.500000000001", False),
        ("2651.80", False),
        ("2660.00", False),
    ],
    ids=["at_the_stop", "one_ulp_tighter", "tighter", "one_ulp_looser", "initial_stop", "far_looser"],
)
def test_a_protective_stop_for_a_short_position_is_never_looser_than_its_stop(
    trigger: str, allowed: bool
) -> None:
    held = _short_position(stop_price=D("2650.50"))
    request = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price=trigger, attempt=1)
    if allowed:
        validate_reducing_order(held, request)
    else:
        error = _refused_reducing(held, request)
        assert "looser" in str(error.details["rule"])


@pytest.mark.requirement("G04-11", "G04-12")
def test_a_stop_replacement_is_judged_by_the_values_it_asks_for() -> None:
    held = samples.position(stop_price=D("2649.90"))
    working_stop = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price="2649.90")
    tighter = _replace_request(working_stop, requested_trigger_price=D("2650.40"))
    validate_reducing_order(held, tighter)
    looser = _replace_request(working_stop, requested_trigger_price=D("2649.00"))
    assert "looser" in str(_refused_reducing(held, looser).details["rule"])
    uncovering = _replace_request(
        working_stop, requested_trigger_price=D("2650.40"), requested_volume_lots=D("0.25")
    )
    _refused_reducing(held, uncovering)


@pytest.mark.requirement("G04-12")
def test_after_a_partial_close_the_stop_is_resized_to_the_open_volume() -> None:
    opened, reduced, _closed = samples.position_lifecycle()
    stop_of_full_size = _child(opened, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price="2647.80")
    resize = _replace_request(
        stop_of_full_size, requested_volume_lots=D("0.25"), requested_trigger_price=D("2649.90")
    )
    validate_reducing_order(reduced, resize)
    only_moved = _replace_request(stop_of_full_size, requested_trigger_price=D("2649.90"))
    _refused_reducing(reduced, only_moved)


@pytest.mark.parametrize(
    ("limit", "allowed"),
    [("2650.600000000001", True), ("2652.80", True), ("2650.60", False), ("2650.00", False)],
    ids=["one_ulp_above_mark", "above_mark", "at_mark", "below_mark"],
)
def test_a_take_profit_for_a_long_position_lies_above_the_mark(limit: str, allowed: bool) -> None:
    held = samples.position()
    assert held.mark_price == D("2650.60")
    request = _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price=limit)
    if allowed:
        validate_reducing_order(held, request)
    else:
        assert "winning side" in str(_refused_reducing(held, request).details["rule"])


@pytest.mark.parametrize(
    ("limit", "allowed"),
    [("2648.999999999999", True), ("2646.00", True), ("2649.00", False), ("2649.50", False)],
    ids=["one_ulp_below_mark", "below_mark", "at_mark", "above_mark"],
)
def test_a_take_profit_for_a_short_position_lies_below_the_mark(limit: str, allowed: bool) -> None:
    held = _short_position()
    assert held.mark_price == D("2649.00")
    request = _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price=limit)
    if allowed:
        validate_reducing_order(held, request)
    else:
        assert "winning side" in str(_refused_reducing(held, request).details["rule"])


def test_a_take_profit_replacement_is_judged_by_the_requested_limit() -> None:
    held = samples.position()
    resting = _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2652.80")
    validate_reducing_order(held, _replace_request(resting, requested_limit_price=D("2651.00")))
    _refused_reducing(held, _replace_request(resting, requested_limit_price=D("2650.60")))


@pytest.mark.parametrize("intent", [OrderIntent.EXIT, OrderIntent.PROTECTIVE_STOP, OrderIntent.TAKE_PROFIT])
def test_gie_never_places_orders_for_positions_of_other_systems(intent: OrderIntent) -> None:
    observed = samples.external_position(symbol="XAUUSD")
    order_type = {
        OrderIntent.EXIT: OrderType.MARKET,
        OrderIntent.PROTECTIVE_STOP: OrderType.STOP,
        OrderIntent.TAKE_PROFIT: OrderType.LIMIT,
    }[intent]
    price = None if order_type is OrderType.MARKET else "1.0800"
    request = _child(observed, intent, order_type, price=price)
    assert request.position_id == observed.position_id
    error = _refused_reducing(observed, request)
    assert "other systems" in str(error.details["rule"])


def test_a_closed_position_authorises_no_order() -> None:
    closed = _closed_position()
    for request in (
        _child(closed, OrderIntent.EXIT, OrderType.MARKET),
        _child(closed, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, price="2649.90"),
        _child(closed, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2655.00"),
    ):
        assert "not open" in str(_refused_reducing(closed, request).details["rule"])


def test_an_entry_is_never_authorised_by_a_position() -> None:
    held = samples.position()
    error = _refused_reducing(held, samples.order_event())
    assert error.details.get("intent") == "ENTRY"
    sell_entry = samples.order_event(side=Side.SELL, attached_stop_price=D("2651.80"))
    _refused_reducing(held, sell_entry)


def test_a_reducing_order_must_name_exactly_this_position() -> None:
    held = samples.position()
    other_order = samples.protective_stop_lifecycle()[0].client_order_id
    other_position = samples.position(
        opening_client_order_id=other_order, position_id=identity.position_id(held.run_id, other_order)
    )
    request = _child(other_position, OrderIntent.EXIT, OrderType.MARKET)
    validate_reducing_order(other_position, request)
    _refused_reducing(held, request)


def test_a_reducing_order_belongs_to_the_run_and_mode_of_its_position() -> None:
    held = samples.position()
    _refused_reducing(held, _child(held, OrderIntent.EXIT, OrderType.MARKET, mode=RunMode.PAPER))
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    _refused_reducing(held, _child(held, OrderIntent.EXIT, OrderType.MARKET, run_id=other_run))


# =========================================================================== protective cancel
@pytest.mark.requirement("G04-11", "G04-12")
def test_the_protective_stop_of_an_open_position_cannot_be_cancelled() -> None:
    held = samples.position()
    cancel = _cancel_request(samples.protective_stop_lifecycle()[0])
    assert (cancel.intent, cancel.event_type) == (
        OrderIntent.PROTECTIVE_STOP,
        OrderEventType.CANCEL_REQUESTED,
    )
    with pytest.raises(IllegalTransitionError) as caught:
        validate_protective_cancel(held, cancel)
    assert "open position" in str(caught.value.details["rule"])
    with pytest.raises(IllegalTransitionError):
        validate_protective_cancel(samples.position_lifecycle()[1], cancel)


@pytest.mark.requirement("G04-12")
def test_the_protective_stop_of_a_closed_position_may_be_cancelled() -> None:
    cancel = _cancel_request(samples.protective_stop_lifecycle()[0])
    assert validate_protective_cancel(_closed_position(), cancel) is None


@pytest.mark.requirement("G04-12")
def test_a_closed_position_does_not_release_the_stop_of_another_position() -> None:
    held = samples.position()
    other_order = samples.protective_stop_lifecycle()[0].client_order_id
    closed_other = samples.position_lifecycle()[2]
    closed_other_fields = {spec.name: getattr(closed_other, spec.name) for spec in Position.FIELD_SPECS}
    closed_other_fields.update(
        opening_client_order_id=other_order, position_id=identity.position_id(held.run_id, other_order)
    )
    closed_other = Position(**closed_other_fields)
    cancel = _cancel_request(samples.protective_stop_lifecycle()[0])
    assert cancel.position_id == held.position_id != closed_other.position_id
    with pytest.raises(IllegalTransitionError):
        validate_protective_cancel(closed_other, cancel)


def test_cancelling_other_orders_only_reduces_risk_and_is_not_restricted_here() -> None:
    held = samples.position()
    take_profit = _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, price="2652.80")
    assert validate_protective_cancel(held, _cancel_request(take_profit)) is None
    exit_order = _child(held, OrderIntent.EXIT, OrderType.LIMIT, price="2651.00")
    assert validate_protective_cancel(held, _cancel_request(exit_order)) is None
    assert validate_protective_cancel(held, _cancel_request(samples.order_event())) is None
