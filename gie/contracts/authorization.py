"""Authorisation checks for orders (who may create risk, and that nothing else can).

An *entry* is the only order that creates risk.  It is admitted only against
the selected plan and the risk approval it cites, and only as long as both are
valid.  Every other order belongs to an open GIE position and may only reduce
it.  The order gateway (``gie.contracts.guarded``) calls these checks before
any request reaches an adapter; QA runs them over journals.
"""

from __future__ import annotations

from gie.contracts.codec import fingerprint
from gie.contracts.common import OrderType, Side
from gie.contracts.order_event import OrderEvent, OrderEventType, OrderIntent
from gie.contracts.position import Position, PositionOwner, PositionState
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.trade_plan import PlanStatus, TradePlan
from gie.core.errors import IllegalTransitionError, StaleDataError


def _refuse(rule: str, request: OrderEvent) -> IllegalTransitionError:
    return IllegalTransitionError(
        f"order not authorised: {rule}",
        client_order_id=request.client_order_id,
        intent=request.intent.value,
        rule=rule,
    )


def validate_entry_authorization(
    plan: TradePlan, decision: RiskDecision, request: OrderEvent, *, now_ns: int
) -> None:
    """Check an ENTRY ``SUBMIT_REQUESTED`` against its plan and risk approval.

    Raises :class:`StaleDataError` when the plan, the approval or the request
    has expired, and :class:`IllegalTransitionError` for every other mismatch.
    """
    if type(plan) is not TradePlan or type(decision) is not RiskDecision or type(request) is not OrderEvent:
        raise TypeError("validate_entry_authorization expects a TradePlan, a RiskDecision and an OrderEvent")
    if request.intent is not OrderIntent.ENTRY or request.event_type is not OrderEventType.SUBMIT_REQUESTED:
        raise _refuse("only the SUBMIT_REQUESTED event of an ENTRY order is authorised here", request)
    for record in (plan, decision):
        if record.run_id != request.run_id or record.mode is not request.mode:
            raise _refuse("plan, approval and order belong to the same run and mode", request)
    if plan.status is not PlanStatus.SELECTED:
        raise _refuse("an entry is only built from a SELECTED plan", request)
    if not decision.approved:
        raise _refuse("the cited risk decision is a rejection", request)
    if not (decision.plan_id == plan.plan_id == request.plan_id):
        raise _refuse("plan_id of plan, approval and order are the same", request)
    if decision.plan_fingerprint != fingerprint(plan):
        raise _refuse("the approval was given for exactly this plan content (plan_fingerprint)", request)
    if request.risk_decision_id != decision.decision_id:
        raise _refuse("the order cites this risk decision", request)
    if not (plan.alternative_group_id == decision.alternative_group_id == request.alternative_group_id):
        raise _refuse("alternative_group_id of plan, approval and order are the same", request)
    if decision.decision_time_ns < plan.decision_time_ns:
        raise _refuse("the approval was given after the plan was made", request)
    if request.side is not plan.side or request.symbol != plan.symbol:
        raise _refuse("side and symbol of the order are those of the plan", request)
    if request.order_type is not plan.entry_order_type:
        raise _refuse("the order type is the plan's entry order type", request)
    order_price = request.limit_price if request.limit_price is not None else request.trigger_price
    if request.order_type is not OrderType.MARKET and order_price != plan.entry_price:
        raise _refuse("the order price is the plan's entry price", request)
    if request.attached_stop_price != plan.stop_price:
        raise _refuse("the attached protective stop is the plan's stop", request)
    if request.volume_lots > decision.volume_lots:
        raise _refuse("the order volume does not exceed the granted volume", request)
    limit_ns = min(plan.valid_until_ns, decision.valid_until_ns)
    if now_ns >= limit_ns or request.event_time_ns >= limit_ns:
        raise StaleDataError(
            "plan or risk approval has expired",
            what="entry authorisation",
            now_ns=now_ns,
            valid_until_ns=limit_ns,
        )
    if request.valid_until_ns is not None and request.valid_until_ns > limit_ns:
        raise _refuse("the order does not outlive its plan or its approval", request)


def validate_reducing_order(position: Position, request: OrderEvent) -> None:
    """Check a non-entry request against the position it names: it may only reduce that position.

    Applies to ``SUBMIT_REQUESTED`` and ``REPLACE_REQUESTED`` events of
    PROTECTIVE_STOP, TAKE_PROFIT and EXIT orders.  A protective stop always
    covers the whole open volume and is never looser than the position's stop.
    """
    if type(position) is not Position or type(request) is not OrderEvent:
        raise TypeError("validate_reducing_order expects a Position and an OrderEvent")
    if request.intent is OrderIntent.ENTRY:
        raise _refuse(
            "an ENTRY order is authorised by its plan and risk approval, not by a position", request
        )
    if position.owner is not PositionOwner.GIE:
        raise _refuse("GIE never places orders for positions of other systems", request)
    if position.state is not PositionState.OPEN:
        raise _refuse("the position is not open", request)
    if request.position_id != position.position_id:
        raise _refuse("the order names this position", request)
    if request.run_id != position.run_id or request.mode is not position.mode:
        raise _refuse("order and position belong to the same run and mode", request)
    if request.symbol != position.symbol:
        raise _refuse("order and position are in the same symbol", request)
    if request.side is position.side:
        raise _refuse("a reducing order is on the opposite side of the position", request)
    volume = request.requested_volume_lots or request.volume_lots
    trigger = request.requested_trigger_price or request.trigger_price
    limit = request.requested_limit_price or request.limit_price
    if volume > position.volume_lots:
        raise _refuse("a reducing order does not exceed the open volume", request)
    long = position.side is Side.BUY
    if request.intent is OrderIntent.PROTECTIVE_STOP:
        if volume != position.volume_lots:
            raise _refuse("a protective stop covers the whole open volume", request)
        assert trigger is not None  # OEV-8: a protective stop is a STOP order
        current = position.stop_price
        if current is not None and (trigger < current if long else trigger > current):
            raise _refuse("a protective stop is never looser than the position's stop", request)
    if request.intent is OrderIntent.TAKE_PROFIT:
        assert limit is not None  # OEV-8: a take profit is a LIMIT order
        mark = position.mark_price
        if mark is not None and (limit <= mark if long else limit >= mark):
            raise _refuse("a take profit lies on the winning side of the current mark", request)


def validate_protective_cancel(position: Position, request: OrderEvent) -> None:
    """A protective stop may only be cancelled once its position is closed."""
    if type(position) is not Position or type(request) is not OrderEvent:
        raise TypeError("validate_protective_cancel expects a Position and an OrderEvent")
    if request.intent is not OrderIntent.PROTECTIVE_STOP:
        return
    if request.position_id != position.position_id:
        raise _refuse("the order names this position", request)
    if position.state is PositionState.OPEN:
        raise _refuse("the protective stop of an open position may not be cancelled", request)
