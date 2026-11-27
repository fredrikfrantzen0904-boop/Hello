"""OrderEvent contract - one step in the life of one order (event sourced)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from gie.contracts import identity
from gie.contracts._spec import Dec, EnumOf, Id, Int, Opt, Str, TimeNs, fld, inv
from gie.contracts.common import (
    PRICE,
    REASON_CODE_PATTERN,
    ZERO,
    ExecutionVenue,
    OrderType,
    RunScoped,
    Side,
    symbol_field,
)
from gie.contracts.registry import contract
from gie.core.errors import IllegalTransitionError


class OrderIntent(str, Enum):
    """Why the order exists.  Only ENTRY creates risk and needs a risk approval."""

    ENTRY = "ENTRY"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"


class OrderEventType(str, Enum):
    SUBMIT_REQUESTED = "SUBMIT_REQUESTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    REPLACE_REQUESTED = "REPLACE_REQUESTED"
    REPLACED = "REPLACED"
    REPLACE_REJECTED = "REPLACE_REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILED = "RECONCILED"


class OrderState(str, Enum):
    """State of the order *after* the event has been applied."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL_ORDER_STATES: Final[frozenset[OrderState]] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)

_S = OrderState
_E = OrderEventType
_OPEN = (_S.WORKING, _S.PARTIALLY_FILLED)
_PENDING_CHANGE = (_S.PENDING_CANCEL, _S.PENDING_REPLACE)


def _build_transitions() -> dict[tuple[OrderState | None, OrderEventType], frozenset[OrderState]]:
    table: dict[tuple[OrderState | None, OrderEventType], frozenset[OrderState]] = {
        (None, _E.SUBMIT_REQUESTED): frozenset({_S.PENDING_SUBMIT}),
        (_S.PENDING_SUBMIT, _E.ACCEPTED): frozenset({_S.WORKING}),
        (_S.PENDING_SUBMIT, _E.REJECTED): frozenset({_S.REJECTED}),
        # A market order can fill without a separate acknowledgement.
        (_S.PENDING_SUBMIT, _E.PARTIALLY_FILLED): frozenset({_S.PARTIALLY_FILLED}),
        (_S.PENDING_SUBMIT, _E.FILLED): frozenset({_S.FILLED}),
    }
    for state in _OPEN:
        table[(state, _E.PARTIALLY_FILLED)] = frozenset({_S.PARTIALLY_FILLED})
        table[(state, _E.FILLED)] = frozenset({_S.FILLED})
        table[(state, _E.CANCEL_REQUESTED)] = frozenset({_S.PENDING_CANCEL})
        table[(state, _E.REPLACE_REQUESTED)] = frozenset({_S.PENDING_REPLACE})
        table[(state, _E.EXPIRED)] = frozenset({_S.EXPIRED})
        # The broker may cancel on its own initiative (e.g. session close).
        table[(state, _E.CANCELED)] = frozenset({_S.CANCELED})
    for state in _PENDING_CHANGE:
        # Fills can race with a pending cancel or replace.
        table[(state, _E.PARTIALLY_FILLED)] = frozenset({state})
        table[(state, _E.FILLED)] = frozenset({_S.FILLED})
        table[(state, _E.EXPIRED)] = frozenset({_S.EXPIRED})
        table[(state, _E.CANCELED)] = frozenset({_S.CANCELED})
    table[(_S.PENDING_CANCEL, _E.CANCEL_REJECTED)] = frozenset(_OPEN)
    table[(_S.PENDING_REPLACE, _E.REPLACED)] = frozenset(_OPEN)
    table[(_S.PENDING_REPLACE, _E.REPLACE_REJECTED)] = frozenset(_OPEN)
    # A cancel may be requested while a replace is pending.
    table[(_S.PENDING_REPLACE, _E.CANCEL_REQUESTED)] = frozenset({_S.PENDING_CANCEL})
    # Restart reconciliation adopts the simulated broker's authoritative state.
    for state in OrderState:
        if state not in TERMINAL_ORDER_STATES:
            table[(state, _E.RECONCILED)] = frozenset(OrderState) - {_S.PENDING_SUBMIT}
    return table


#: ``(state before, event type) -> states allowed after``.  ``None`` is "no order yet".
#: Terminal states accept no further events.
ORDER_TRANSITIONS: Final[dict[tuple[OrderState | None, OrderEventType], frozenset[OrderState]]] = (
    _build_transitions()
)

_FILL_EVENTS = (_E.PARTIALLY_FILLED, _E.FILLED)
_REASON_EVENTS = (_E.REJECTED, _E.CANCEL_REJECTED, _E.REPLACE_REJECTED, _E.RECONCILED)
_STATES_AFTER: Final[dict[OrderEventType, frozenset[OrderState]]] = {
    event: frozenset().union(*(after for (_, e), after in ORDER_TRANSITIONS.items() if e is event))
    for event in OrderEventType
}
_PRICE_FIELDS_BY_TYPE = {
    OrderType.MARKET: (False, False),
    OrderType.LIMIT: (True, False),
    OrderType.STOP: (False, True),
}


def _order_price(event: OrderEvent) -> Decimal | None:
    return event.limit_price if event.limit_price is not None else event.trigger_price


def _protection_geometry(event: OrderEvent) -> bool:
    price = _order_price(event)
    if price is None:
        return True
    buy = event.side is Side.BUY
    if event.attached_stop_price is not None and not (
        event.attached_stop_price < price if buy else event.attached_stop_price > price
    ):
        return False
    return event.attached_take_profit_price is None or (
        event.attached_take_profit_price > price if buy else event.attached_take_profit_price < price
    )


def _fill_consistency(event: OrderEvent) -> bool:
    is_fill = event.event_type in _FILL_EVENTS
    if is_fill != (event.last_fill_lots is not None and event.last_fill_price is not None):
        return False
    if not is_fill and (event.last_fill_lots is not None or event.last_fill_price is not None):
        return False
    if event.cumulative_filled_lots > event.volume_lots:
        return False
    if (event.average_fill_price is not None) != (event.cumulative_filled_lots > ZERO):
        return False
    if event.last_fill_lots is not None and event.last_fill_lots > event.cumulative_filled_lots:
        return False
    if (
        event.event_type is _E.PARTIALLY_FILLED
        and not ZERO < event.cumulative_filled_lots < event.volume_lots
    ):
        return False
    if event.order_state in (_S.PENDING_SUBMIT, _S.WORKING) and event.cumulative_filled_lots != ZERO:
        return False
    if event.order_state is _S.PARTIALLY_FILLED and event.cumulative_filled_lots == ZERO:
        return False
    return (event.order_state is _S.FILLED) == (event.cumulative_filled_lots == event.volume_lots)


@contract("OrderEvent", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class OrderEvent(RunScoped):
    """One lifecycle event of one order at the simulated venue.

    Stream: ``order_event/<producer>``; additionally ``order_seq`` numbers the
    events of one ``client_order_id`` from 0.  Ordering time: ``receive_time_ns``.
    Every event repeats the order's current parameters, so the newest event of
    an order is a complete description of it.  ``venue`` can only be SIMULATED:
    a live order cannot be represented in this contract version.
    """

    order_event_id: str = fld(Id("oev"), "identity.order_event_id(client_order_id, order_seq).")
    client_order_id: str = fld(
        Id("ord"), "Idempotency key of the order: identity.client_order_id(run, origin, intent, attempt)."
    )
    order_seq: int = fld(
        Int(ge=0, label="sequence number"), "Index of the event within its order: 0, 1, 2, ..."
    )
    event_type: OrderEventType = fld(EnumOf(OrderEventType), "What happened.")
    order_state: OrderState = fld(EnumOf(OrderState), "State of the order after this event.")
    event_time_ns: int = fld(
        TimeNs(),
        "When it happened: at GIE for *_REQUESTED events, at the simulated broker otherwise.",
        unit="ns since epoch, UTC",
    )
    receive_time_ns: int = fld(
        TimeNs(), "When GIE observed the event (includes modelled latency).", unit="ns since epoch, UTC"
    )
    venue: ExecutionVenue = fld(EnumOf(ExecutionVenue), "Always SIMULATED (routing lock).")
    symbol: str = symbol_field("Always XAUUSD: GIE trades only gold.", gold_only=True)
    side: Side = fld(EnumOf(Side), "Direction of the order.")
    order_type: OrderType = fld(EnumOf(OrderType), "MARKET, LIMIT or STOP.")
    intent: OrderIntent = fld(EnumOf(OrderIntent), "Why the order exists.")
    volume_lots: Decimal = fld(Dec(gt="0", label="volume"), "Total order quantity.", unit="lots")
    limit_price: Decimal | None = fld(
        Opt(PRICE), "Limit price; set if and only if order_type is LIMIT.", unit="USD per troy ounce"
    )
    trigger_price: Decimal | None = fld(
        Opt(PRICE), "Stop trigger price; set if and only if order_type is STOP.", unit="USD per troy ounce"
    )
    attached_stop_price: Decimal | None = fld(
        Opt(PRICE),
        "Protective stop held by the (simulated) broker for the resulting position; mandatory for entries.",
        unit="USD per troy ounce",
    )
    attached_take_profit_price: Decimal | None = fld(
        Opt(PRICE), "Take profit held by the (simulated) broker, if any.", unit="USD per troy ounce"
    )
    valid_until_ns: int | None = fld(
        Opt(TimeNs()),
        "Instant at which a working order expires; null for no expiry.",
        unit="ns since epoch, UTC",
    )
    plan_id: str | None = fld(Opt(Id("pln")), "Plan behind an entry order.")
    risk_decision_id: str | None = fld(Opt(Id("rsk")), "Risk approval behind an entry order.")
    position_id: str | None = fld(Opt(Id("pos")), "Position a protective or exit order belongs to.")
    broker_order_id: str | None = fld(
        Opt(Id("brk")), "Identifier assigned by the simulated broker once it knows the order."
    )
    cumulative_filled_lots: Decimal = fld(
        Dec(ge="0", label="volume"), "Quantity filled so far including this event.", unit="lots"
    )
    last_fill_lots: Decimal | None = fld(
        Opt(Dec(gt="0", label="volume")),
        "Quantity filled by this event; set only on fill events.",
        unit="lots",
    )
    last_fill_price: Decimal | None = fld(
        Opt(PRICE), "Price of this fill; set only on fill events.", unit="USD per troy ounce"
    )
    average_fill_price: Decimal | None = fld(
        Opt(PRICE), "Volume-weighted average price of all fills so far.", unit="USD per troy ounce"
    )
    last_fill_cost_amount: Decimal | None = fld(
        Opt(Dec(ge="0", label="money")),
        "Commission and fees charged for this fill.",
        unit="account currency",
    )
    reason_code: str | None = fld(
        Opt(Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code")),
        "Reason of a rejection, cancellation, expiry or reconciliation.",
    )

    INVARIANTS = (
        inv(
            "OEV-1",
            "order_event_id equals identity.order_event_id(client_order_id, order_seq)",
            lambda r: r.order_event_id == identity.order_event_id(r.client_order_id, r.order_seq),
        ),
        inv(
            "OEV-2",
            "order_seq is 0 if and only if event_type is SUBMIT_REQUESTED",
            lambda r: (r.order_seq == 0) == (r.event_type is _E.SUBMIT_REQUESTED),
        ),
        inv(
            "OEV-3",
            "order_state is a state that event_type can lead to (see ORDER_TRANSITIONS)",
            lambda r: r.order_state in _STATES_AFTER[r.event_type],
        ),
        inv(
            "OEV-4",
            "limit_price is set iff order_type is LIMIT; trigger_price is set iff order_type is STOP",
            lambda r: (
                ((r.limit_price is not None), (r.trigger_price is not None))
                == _PRICE_FIELDS_BY_TYPE[r.order_type]
            ),
        ),
        inv(
            "OEV-5",
            "an ENTRY order references its plan and risk approval and carries a protective stop",
            lambda r: (
                r.intent is not OrderIntent.ENTRY
                or (
                    r.plan_id is not None
                    and r.risk_decision_id is not None
                    and r.attached_stop_price is not None
                )
            ),
        ),
        inv(
            "OEV-6",
            "a non-ENTRY order references the position it protects or closes and has no attached protection",
            lambda r: (
                r.intent is OrderIntent.ENTRY
                or (
                    r.position_id is not None
                    and r.attached_stop_price is None
                    and r.attached_take_profit_price is None
                )
            ),
        ),
        inv(
            "OEV-7",
            "fill fields are consistent: last_fill_* only on fill events, 0 <= cumulative <= volume, "
            "average_fill_price iff cumulative > 0, order_state is FILLED iff cumulative = volume, "
            "PENDING_SUBMIT and WORKING have no fills, PARTIALLY_FILLED has some",
            _fill_consistency,
        ),
        inv(
            "OEV-8",
            "reason_code is set on REJECTED, CANCEL_REJECTED, REPLACE_REJECTED and RECONCILED events",
            lambda r: r.event_type not in _REASON_EVENTS or r.reason_code is not None,
        ),
        inv(
            "OEV-9",
            "attached protection is on the correct side of the order price (stop on the losing side, "
            "take profit on the winning side)",
            _protection_geometry,
        ),
        inv(
            "OEV-10",
            "event_time_ns <= receive_time_ns "
            "(the venue is simulated in-process, so there is no clock offset)",
            lambda r: r.event_time_ns <= r.receive_time_ns,
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.order_state in TERMINAL_ORDER_STATES


#: Fields that never change during the life of an order.
_IMMUTABLE = (
    "client_order_id",
    "run_id",
    "mode",
    "venue",
    "symbol",
    "side",
    "intent",
    "plan_id",
    "risk_decision_id",
    "position_id",
)
#: Fields that may only change with a REPLACED or RECONCILED event.
_REPLACEABLE = (
    "order_type",
    "volume_lots",
    "limit_price",
    "trigger_price",
    "attached_stop_price",
    "attached_take_profit_price",
    "valid_until_ns",
)


def validate_order_transition(previous: OrderEvent | None, event: OrderEvent) -> None:
    """Check that *event* may follow *previous* in the life of one order.

    Raises :class:`IllegalTransitionError` naming the violated rule.  Used by the
    execution engine before it journals an event and by QA on recorded traces.
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal order transition: {rule}",
            client_order_id=event.client_order_id,
            order_seq=event.order_seq,
            rule=rule,
        )

    before = None if previous is None else previous.order_state
    allowed = ORDER_TRANSITIONS.get((before, event.event_type))
    if previous is None:
        if event.order_seq != 0 or allowed is None:
            raise fail("the first event of an order must be SUBMIT_REQUESTED with order_seq 0")
        if event.cumulative_filled_lots != ZERO:
            raise fail("a new order has no fills")
        return
    if previous.order_state in TERMINAL_ORDER_STATES:
        raise fail(f"no event may follow terminal state {previous.order_state.value}")
    if event.order_seq != previous.order_seq + 1:
        raise fail("order_seq must increase by exactly 1")
    if allowed is None or event.order_state not in allowed:
        raise fail(
            f"{previous.order_state.value} --{event.event_type.value}--> "
            f"{event.order_state.value} is not allowed"
        )
    for name in _IMMUTABLE:
        if getattr(previous, name) != getattr(event, name):
            raise fail(f"{name} may not change during the life of an order")
    if event.event_type not in (_E.REPLACED, _E.RECONCILED):
        for name in _REPLACEABLE:
            if getattr(previous, name) != getattr(event, name):
                raise fail(f"{name} may only change with a REPLACED or RECONCILED event")
    if event.broker_order_id != previous.broker_order_id and previous.broker_order_id is not None:
        raise fail("broker_order_id may not change once assigned")
    if event.event_time_ns < previous.event_time_ns or event.receive_time_ns < previous.receive_time_ns:
        raise fail("event and receive times may not decrease")
    if event.cumulative_filled_lots < previous.cumulative_filled_lots:
        raise fail("cumulative_filled_lots may not decrease")
    if event.event_type in _FILL_EVENTS:
        assert event.last_fill_lots is not None
        if previous.cumulative_filled_lots + event.last_fill_lots != event.cumulative_filled_lots:
            raise fail("cumulative_filled_lots must equal the previous value plus last_fill_lots")
    elif event.event_type is not _E.RECONCILED and (
        event.cumulative_filled_lots != previous.cumulative_filled_lots
    ):
        raise fail("cumulative_filled_lots may only change with a fill or RECONCILED event")
    if event.volume_lots < event.cumulative_filled_lots:
        raise fail("volume_lots may not be reduced below the filled quantity")
