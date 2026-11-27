"""OrderEvent contract - one step in the life of one order (event sourced)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import Dec, EnumOf, Id, Int, Invariant, Opt, Str, TimeNs, fld, inv
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
from gie.contracts.tick import SOURCE_PATTERN
from gie.core.canonical import EXACT
from gie.core.errors import IllegalTransitionError


class OrderIntent(str, Enum):
    """Why the order exists.

    Only ``ENTRY`` creates risk; it needs a plan and a risk approval.  The other
    intents belong to an existing GIE position and may only reduce it:
    ``PROTECTIVE_STOP`` is the hard stop held by the simulated broker (a STOP
    order), ``TAKE_PROFIT`` a LIMIT order at a target, ``EXIT`` a discretionary
    close by the reflex or decision engine.
    """

    ENTRY = "ENTRY"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"


class OrderOriginator(str, Enum):
    """Who created the order.

    ``EXECUTION``: the execution engine of GIE (session 07), the only writer of
    requests.  ``BROKER``: the simulated broker, which creates reduce-only orders
    on GIE's behalf - the protective stop and take profit that were *attached* to
    an entry, at the entry's first fill, and broker-side closes such as a margin
    stop-out.  The broker never creates an entry.
    """

    EXECUTION = "EXECUTION"
    BROKER = "BROKER"


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
#: Event types that originate in GIE (requests); all others are reports of the simulated broker.
ORDER_REQUEST_EVENTS: Final[frozenset[OrderEventType]] = frozenset(
    {OrderEventType.SUBMIT_REQUESTED, OrderEventType.CANCEL_REQUESTED, OrderEventType.REPLACE_REQUESTED}
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
        # A request that was never acknowledged can be withdrawn or lapse.
        (_S.PENDING_SUBMIT, _E.CANCEL_REQUESTED): frozenset({_S.PENDING_CANCEL}),
        (_S.PENDING_SUBMIT, _E.EXPIRED): frozenset({_S.EXPIRED}),
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
    # Reports that were already under way when the cancel was requested (latency races): the
    # acknowledgement or rejection of a submit that was cancelled before it was acknowledged, and
    # the outcome of a replacement that was still pending.  The cancel stays pending.
    table[(_S.PENDING_CANCEL, _E.ACCEPTED)] = frozenset({_S.PENDING_CANCEL})
    table[(_S.PENDING_CANCEL, _E.REJECTED)] = frozenset({_S.REJECTED})
    table[(_S.PENDING_CANCEL, _E.REPLACED)] = frozenset({_S.PENDING_CANCEL})
    table[(_S.PENDING_CANCEL, _E.REPLACE_REJECTED)] = frozenset({_S.PENDING_CANCEL})
    # Restart reconciliation adopts the simulated broker's authoritative state.
    reconciled = frozenset(OrderState) - {_S.PENDING_SUBMIT, *_PENDING_CHANGE}
    for state in OrderState:
        if state not in TERMINAL_ORDER_STATES:
            # Fills are never taken back: a partially filled order cannot be reconciled to WORKING.
            table[(state, _E.RECONCILED)] = (
                reconciled - {_S.WORKING} if state is _S.PARTIALLY_FILLED else reconciled
            )
    return table


#: ``(state before, event type) -> states allowed after``.  ``None`` is "no order yet".
#: Terminal states accept no further events.
ORDER_TRANSITIONS: Final[dict[tuple[OrderState | None, OrderEventType], frozenset[OrderState]]] = (
    _build_transitions()
)

_FILL_EVENTS = (_E.PARTIALLY_FILLED, _E.FILLED)
#: States in which a replacement can still be outstanding (its requested values are carried).
_REPLACE_OUTSTANDING = (_S.PENDING_REPLACE, _S.PENDING_CANCEL)
_REASON_EVENTS = (_E.REJECTED, _E.CANCEL_REJECTED, _E.REPLACE_REJECTED, _E.RECONCILED)
_STATES_AFTER: Final[dict[OrderEventType, frozenset[OrderState]]] = {
    event: frozenset().union(*(after for (_, e), after in ORDER_TRANSITIONS.items() if e is event))
    for event in OrderEventType
}
_PRICE_FIELDS_BY_TYPE: Final[dict[OrderType, tuple[bool, bool]]] = {
    OrderType.MARKET: (False, False),
    OrderType.LIMIT: (True, False),
    OrderType.STOP: (False, True),
}
_TYPE_BY_INTENT: Final[dict[OrderIntent, OrderType]] = {
    OrderIntent.PROTECTIVE_STOP: OrderType.STOP,
    OrderIntent.TAKE_PROFIT: OrderType.LIMIT,
}
_REQUESTED = (
    "requested_volume_lots",
    "requested_limit_price",
    "requested_trigger_price",
    "requested_valid_until_ns",
)


def _order_price(event: OrderEvent) -> Decimal | None:
    if event.limit_price is not None:
        return event.limit_price
    if event.trigger_price is not None:
        return event.trigger_price
    return event.reference_price


def _on_losing_side(side: Side, stop: Decimal, price: Decimal) -> bool:
    return stop < price if side is Side.BUY else stop > price


def _protection_geometry(event: OrderEvent) -> bool:
    """Attached protection against the prices GIE *asked for* (never against a fill price).

    A fill can gap through the attached stop; that is a fact the venue reports,
    not something an order may be refused for afterwards (see ``Position``).
    """
    price = _order_price(event)
    if price is None:
        return True
    if event.attached_stop_price is not None and not _on_losing_side(
        event.side, event.attached_stop_price, price
    ):
        return False
    return event.attached_take_profit_price is None or (
        event.attached_take_profit_price != price
        and not _on_losing_side(event.side, event.attached_take_profit_price, price)
    )


def _entry_price_geometry(event: OrderEvent) -> bool:
    """A resting entry rests on the far side of the market it was priced against.

    BUY LIMIT at or below the reference ask, SELL LIMIT at or above the reference
    bid; BUY STOP at or above, SELL STOP at or below.  A limit through the market
    would fill at once at a price that can lie beyond its own stop: such an entry
    is a MARKET order and must be planned and approved as one.
    """
    if event.intent is not OrderIntent.ENTRY or event.reference_price is None:
        return True
    reference = event.reference_price
    if event.limit_price is not None:
        return event.limit_price <= reference if event.side is Side.BUY else event.limit_price >= reference
    if event.trigger_price is not None:
        return (
            event.trigger_price >= reference if event.side is Side.BUY else event.trigger_price <= reference
        )
    return True


def _fill_consistency(event: OrderEvent) -> bool:
    is_fill = event.event_type in _FILL_EVENTS
    may_report_fill = is_fill or event.event_type is _E.RECONCILED  # a fill found by reconciliation
    has_fill = event.last_fill_lots is not None
    fill_fields = (event.last_fill_price, event.fill_tick_source, event.fill_tick_seq)
    if has_fill != all(value is not None for value in fill_fields) or (
        not has_fill and any(value is not None for value in fill_fields)
    ):
        return False
    if is_fill and not has_fill:
        return False
    if has_fill and not may_report_fill:
        return False
    if event.last_fill_cost_amount is not None and not has_fill:
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


def _origin_and_identity(event: OrderEvent) -> bool:
    if event.intent is OrderIntent.ENTRY:
        origin = event.risk_decision_id
        if event.attempt != 0:
            return False
    else:
        origin = event.position_id
    return origin is not None and event.client_order_id == identity.client_order_id(
        event.run_id, origin, event.intent.value, event.attempt
    )


def _expiry_rules(event: OrderEvent) -> bool:
    if event.intent is OrderIntent.PROTECTIVE_STOP and event.valid_until_ns is not None:
        return False
    if (
        event.intent is OrderIntent.ENTRY
        and event.order_type is not OrderType.MARKET
        and event.valid_until_ns is None
    ):
        return False
    if event.valid_until_ns is None:
        return True
    if event.event_type is _E.SUBMIT_REQUESTED or event.last_fill_lots is not None:
        # an order cannot be requested, and cannot fill, at or after the instant it expires
        return event.event_time_ns < event.valid_until_ns
    return True


@final
@contract("OrderEvent", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class OrderEvent(RunScoped):
    """One lifecycle event of one order at the simulated venue.

    Stream: ``order_event/<producer>``; additionally ``order_seq`` numbers the
    events of one ``client_order_id`` from 0.  Ordering time: ``receive_time_ns``.
    Every event repeats the order's current parameters, so the newest event of
    an order is a complete description of it.  ``venue`` can only be SIMULATED:
    a live order cannot be represented in this contract version.

    **Writers.**  Order events are written by session 07 only: its execution
    engine issues the requests and its simulated broker the reports.  The reflex
    engine never writes order events; it asks the execution engine through
    ``gie.contracts.interfaces.ProtectionPort``.

    **Replacement.**  A replacement is asked for with ``REPLACE_REQUESTED``, which
    carries the wanted values in the ``requested_*`` fields while the order's
    parameters stay as they are; ``REPLACED`` then shows the new parameters.
    One replacement can be outstanding per order: a newer target waits for the
    outcome of the pending one (the engine keeps only the newest target), and
    protection that cannot wait is an ``EXIT``.

    **Protection.**  The hard stop of a position is its own ``PROTECTIVE_STOP``
    order.  It is created by the simulated broker (``originator`` BROKER) from
    ``attached_stop_price`` at the first fill of the entry, so no position exists
    without a stop order; the same holds for an attached take profit.  It can be
    tightened by a replacement and never widened, it never expires, and its
    volume follows the position.

    **Prices.**  A LIMIT or STOP order is evaluated against the executable side
    of the quote: a SELL against the bid, a BUY against the ask.  A report is
    never observed at the instant of the request it answers: the simulated
    broker delivers it at least one nanosecond later (modelled latency).
    """

    order_event_id: str = fld(Id("oev"), "identity.order_event_id(client_order_id, order_seq).")
    client_order_id: str = fld(
        Id("ord"),
        "Idempotency key: identity.client_order_id(run_id, origin, intent, attempt), where origin is "
        "the risk decision for an entry and the position otherwise.",
    )
    originator: OrderOriginator = fld(
        EnumOf(OrderOriginator),
        "Who created the order: EXECUTION (GIE) or BROKER (attached protection, broker-side close).",
    )
    attempt: int = fld(
        Int(ge=0, le=65535, label="attempt"),
        "Always 0 for an entry (one approval, one order); numbers the orders of an intent for a position.",
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
    reference_price: Decimal | None = fld(
        Opt(PRICE),
        "Executable price when the order was requested (ask for BUY, bid for SELL); mandatory for "
        "entries and for every MARKET order.",
        unit="USD per troy ounce",
    )
    max_slippage: Decimal | None = fld(
        Opt(Dec(ge="0", label="price distance")),
        "Entry only: largest adverse distance between the price asked for and a fill that the "
        "simulated broker may accept (copied from the plan); null = no limit was planned.",
        unit="USD per troy ounce",
    )
    attached_stop_price: Decimal | None = fld(
        Opt(PRICE),
        "Protective stop the simulated broker holds for the resulting position; mandatory for entries.",
        unit="USD per troy ounce",
    )
    attached_take_profit_price: Decimal | None = fld(
        Opt(PRICE), "Take profit held by the simulated broker, if any.", unit="USD per troy ounce"
    )
    valid_until_ns: int | None = fld(
        Opt(TimeNs()),
        "Instant at which the order expires.  Mandatory for a resting entry; always null for a "
        "protective stop.",
        unit="ns since epoch, UTC",
    )
    requested_volume_lots: Decimal | None = fld(
        Opt(Dec(gt="0", label="volume")), "Volume asked for by a pending replacement.", unit="lots"
    )
    requested_limit_price: Decimal | None = fld(
        Opt(PRICE), "Limit price asked for by a pending replacement.", unit="USD per troy ounce"
    )
    requested_trigger_price: Decimal | None = fld(
        Opt(PRICE), "Trigger price asked for by a pending replacement.", unit="USD per troy ounce"
    )
    requested_valid_until_ns: int | None = fld(
        Opt(TimeNs()), "Expiry asked for by a pending replacement.", unit="ns since epoch, UTC"
    )
    plan_id: str | None = fld(Opt(Id("pln")), "Plan revision behind an entry order.")
    risk_decision_id: str | None = fld(Opt(Id("rsk")), "Risk approval behind an entry order.")
    alternative_group_id: str | None = fld(
        Opt(Id("grp")), "Alternative group of the plan behind an entry order."
    )
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
    fill_tick_source: str | None = fld(
        Opt(Str(pattern=SOURCE_PATTERN, max_len=64, label="feed id")),
        "Feed of the quote this fill was matched against (stream tick/<symbol>/<source>).",
    )
    fill_tick_seq: int | None = fld(
        Opt(Int(ge=0, label="sequence number")),
        "Sequence number of that quote in its stream; with fill_tick_source it makes every fill "
        "traceable to the market data that produced it.",
    )
    reason_code: str | None = fld(
        Opt(Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code")),
        "Reason of a rejection, cancellation, expiry or reconciliation; on the request of an EXIT or "
        "protective order, the reason for it (e.g. RATCHET, TIME_EXIT, EMERGENCY).",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
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
            "an ENTRY order carries its plan, risk approval, alternative group, reference price and "
            "protective stop, and no position",
            lambda r: (
                r.intent is not OrderIntent.ENTRY
                or (
                    r.plan_id is not None
                    and r.risk_decision_id is not None
                    and r.alternative_group_id is not None
                    and r.reference_price is not None
                    and r.attached_stop_price is not None
                    and r.position_id is None
                )
            ),
        ),
        inv(
            "OEV-6",
            "a non-ENTRY order names the position it protects or closes and carries no plan, approval, "
            "alternative group or attached protection",
            lambda r: (
                r.intent is OrderIntent.ENTRY
                or (
                    r.position_id is not None
                    and r.plan_id is None
                    and r.risk_decision_id is None
                    and r.alternative_group_id is None
                    and r.attached_stop_price is None
                    and r.attached_take_profit_price is None
                )
            ),
        ),
        inv(
            "OEV-7",
            "client_order_id equals identity.client_order_id(run_id, origin, intent, attempt) with "
            "origin = risk_decision_id and attempt = 0 for ENTRY, origin = position_id otherwise",
            _origin_and_identity,
        ),
        inv(
            "OEV-8",
            "a PROTECTIVE_STOP is a STOP order and a TAKE_PROFIT is a LIMIT order",
            lambda r: r.intent not in _TYPE_BY_INTENT or _TYPE_BY_INTENT[r.intent] is r.order_type,
        ),
        inv(
            "OEV-9",
            "fill fields are consistent: last_fill_lots, last_fill_price and the matched quote "
            "(fill_tick_source, fill_tick_seq) are set together, always on PARTIALLY_FILLED and FILLED, "
            "optionally on RECONCILED (a fill found by reconciliation) and never otherwise; "
            "last_fill_cost_amount only with a fill; 0 <= cumulative <= volume; average_fill_price iff "
            "cumulative > 0; order_state is FILLED iff cumulative = volume; PENDING_SUBMIT and WORKING "
            "have no fills, PARTIALLY_FILLED has some",
            _fill_consistency,
        ),
        inv(
            "OEV-10",
            "reason_code is set on REJECTED, CANCEL_REJECTED, REPLACE_REJECTED and RECONCILED events",
            lambda r: r.event_type not in _REASON_EVENTS or r.reason_code is not None,
        ),
        inv(
            "OEV-11",
            "attached protection is on the correct side of the price asked for (limit, trigger, or the "
            "reference price of a market order): stop on the losing side, take profit on the winning "
            "side; a fill price is a fact and is not judged",
            _protection_geometry,
        ),
        inv(
            "OEV-12",
            "event_time_ns <= receive_time_ns "
            "(the venue is simulated in-process, so there is no clock offset)",
            lambda r: r.event_time_ns <= r.receive_time_ns,
        ),
        inv(
            "OEV-13",
            "a LIMIT or STOP entry has valid_until_ns; a protective stop never has one; an order is "
            "requested, and fills, strictly before valid_until_ns",
            _expiry_rules,
        ),
        inv(
            "OEV-14",
            "requested_* values are carried while a replacement is outstanding: always in "
            "PENDING_REPLACE, possibly in PENDING_CANCEL (cancel requested before the replacement "
            "was answered), never in any other state",
            lambda r: (
                any(getattr(r, name) is not None for name in _REQUESTED)
                if r.order_state is _S.PENDING_REPLACE
                else (
                    r.order_state is _S.PENDING_CANCEL or all(getattr(r, name) is None for name in _REQUESTED)
                )
            ),
        ),
        inv(
            "OEV-15",
            "only GIE's execution engine creates an ENTRY (originator EXECUTION); an order created by "
            "the simulated broker states why (reason_code)",
            lambda r: (
                r.originator is OrderOriginator.EXECUTION
                if r.intent is OrderIntent.ENTRY
                else (r.originator is OrderOriginator.EXECUTION or r.reason_code is not None)
            ),
        ),
        inv(
            "OEV-16",
            "a MARKET order carries the reference price it was requested against; max_slippage only "
            "on an ENTRY",
            lambda r: (
                (r.order_type is not OrderType.MARKET or r.reference_price is not None)
                and (r.max_slippage is None or r.intent is OrderIntent.ENTRY)
            ),
        ),
        inv(
            "OEV-17",
            "a resting entry rests beyond the market it was priced against: BUY LIMIT <= reference, "
            "SELL LIMIT >= reference, BUY STOP >= reference, SELL STOP <= reference",
            _entry_price_geometry,
        ),
        inv(
            "OEV-18",
            "broker_order_id, when present, equals identity.broker_order_id(client_order_id)",
            lambda r: (
                r.broker_order_id is None or r.broker_order_id == identity.broker_order_id(r.client_order_id)
            ),
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.order_state in TERMINAL_ORDER_STATES

    @property
    def is_request(self) -> bool:
        """True for ``*_REQUESTED`` events; all other events are reports of the simulated broker."""
        return self.event_type in ORDER_REQUEST_EVENTS

    @property
    def is_report(self) -> bool:
        return self.event_type not in ORDER_REQUEST_EVENTS


#: Fields that never change during the life of an order.
ORDER_IMMUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "client_order_id",
    "originator",
    "attempt",
    "run_id",
    "mode",
    "venue",
    "symbol",
    "side",
    "intent",
    "order_type",
    "reference_price",
    "max_slippage",
    "plan_id",
    "risk_decision_id",
    "alternative_group_id",
    "position_id",
)
#: Fields that may only change with a REPLACED or RECONCILED event.
ORDER_REPLACEABLE_FIELDS: Final[tuple[str, ...]] = (
    "volume_lots",
    "limit_price",
    "trigger_price",
    "attached_stop_price",
    "attached_take_profit_price",
    "valid_until_ns",
)

_Fail = Callable[[str], IllegalTransitionError]


def _check_no_added_risk(
    previous: OrderEvent,
    *,
    volume_lots: Decimal,
    limit_price: Decimal | None,
    trigger_price: Decimal | None,
    valid_until_ns: int | None,
    attached_stop_price: Decimal | None,
    attached_take_profit_price: Decimal | None,
    fail: _Fail,
) -> None:
    """Rules for what a replacement or reconciliation may change: it can never add risk.

    * An ENTRY order is bound to the plan and stop that risk approved: its volume
      never increases, its prices and attached protection are fixed, and its
      expiry may only be moved earlier.  A different entry or stop is a new plan
      and needs a new risk decision and a new order.
    * A PROTECTIVE_STOP may only be tightened: the trigger of a SELL stop
      (protecting a long position) may only rise, that of a BUY stop only fall,
      and it never gets an expiry.
    * The volume of a reduce-only order (PROTECTIVE_STOP, TAKE_PROFIT, EXIT) may
      change in either direction: it follows the position.  That it never
      exceeds the open volume is checked against the position where the order is
      authorised (``gie.contracts.authorization``), not here.
    """
    if previous.intent is OrderIntent.ENTRY:
        if volume_lots > previous.volume_lots:
            raise fail("volume_lots of an ENTRY order may not increase")
        fixed = (
            ("limit_price", limit_price),
            ("trigger_price", trigger_price),
            ("attached_stop_price", attached_stop_price),
            ("attached_take_profit_price", attached_take_profit_price),
        )
        for name, value in fixed:
            if getattr(previous, name) != value:
                raise fail(f"{name} of an ENTRY order may not change (it is bound to the approved plan)")
        before = previous.valid_until_ns
        if before is not None and (valid_until_ns is None or valid_until_ns > before):
            raise fail("the expiry of an ENTRY order may only be moved earlier")
    else:
        if attached_stop_price is not None or attached_take_profit_price is not None:
            raise fail("only an ENTRY order carries attached protection")
        if previous.intent is OrderIntent.PROTECTIVE_STOP:
            before_trigger = previous.trigger_price
            if before_trigger is None or trigger_price is None:
                raise fail("a protective stop must keep a trigger price")
            widened = (
                trigger_price < before_trigger
                if previous.side is Side.SELL
                else trigger_price > before_trigger
            )
            if widened:
                raise fail("a protective stop may only be tightened, never widened")
            if valid_until_ns is not None:
                raise fail("a protective stop may not be given an expiry")


def _requested_values(event: OrderEvent, current: OrderEvent) -> dict[str, Decimal | int | None]:
    """Parameters an order would have if the pending replacement of *event* were applied to *current*."""
    return {
        "volume_lots": event.requested_volume_lots or current.volume_lots,
        "limit_price": event.requested_limit_price or current.limit_price,
        "trigger_price": event.requested_trigger_price or current.trigger_price,
        "valid_until_ns": (
            event.requested_valid_until_ns
            if event.requested_valid_until_ns is not None
            else current.valid_until_ns
        ),
    }


def _replacement_outstanding(event: OrderEvent) -> bool:
    return any(getattr(event, name) is not None for name in _REQUESTED)


def validate_order_transition(previous: OrderEvent | None, event: OrderEvent) -> None:
    """Check that *event* may follow *previous* in the life of one order.

    Raises :class:`IllegalTransitionError` naming the violated rule.  Every
    journal and the order gateway call it (through the lifecycle ledger) before
    an event becomes part of the history; QA runs it over recorded traces.

    Time: ``receive_time_ns`` - when GIE observed the event - never decreases
    along an order, and a report is observed strictly later than the request
    that precedes it.  ``event_time_ns`` of a report is the broker's time and
    may lie before an earlier request of GIE (a fill that happened while the
    cancel was on its way).  ``seq`` is compared only between events of the same
    producer.
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
    if event.intent is OrderIntent.PROTECTIVE_STOP and event.event_type is _E.EXPIRED:
        raise fail("a protective stop never expires")
    for name in ORDER_IMMUTABLE_FIELDS:
        if getattr(previous, name) != getattr(event, name):
            raise fail(f"{name} may not change during the life of an order")
    if event.broker_order_id != previous.broker_order_id and previous.broker_order_id is not None:
        raise fail("broker_order_id may not change once assigned")
    if event.receive_time_ns < previous.receive_time_ns:
        raise fail("receive_time_ns may not decrease")
    if previous.is_request and event.is_report and event.receive_time_ns <= previous.receive_time_ns:
        raise fail("a report is observed strictly later than the request it follows")
    if event.producer == previous.producer and event.seq < previous.seq:
        raise fail("seq may not decrease within the stream of one producer")

    # ---- reports that were under way when a cancel was requested ---------------------------
    if previous.order_state is _S.PENDING_CANCEL:
        if event.event_type in (_E.ACCEPTED, _E.REJECTED) and previous.broker_order_id is not None:
            raise fail("an order that was already acknowledged cannot be acknowledged or rejected again")
        if event.event_type in (_E.REPLACED, _E.REPLACE_REJECTED) and not _replacement_outstanding(previous):
            raise fail("no replacement is outstanding for this order")

    # ---- parameters -----------------------------------------------------------------------
    if event.event_type is _E.REPLACED:
        wanted = _requested_values(previous, previous)
        for name, value in wanted.items():
            if getattr(event, name) != value:
                raise fail(f"REPLACED must apply exactly the requested {name}")
        for name in ("attached_stop_price", "attached_take_profit_price"):
            if getattr(previous, name) != getattr(event, name):
                raise fail(f"{name} may not change with a replacement")
    elif event.event_type is _E.RECONCILED:
        _check_no_added_risk(
            previous,
            volume_lots=event.volume_lots,
            limit_price=event.limit_price,
            trigger_price=event.trigger_price,
            valid_until_ns=event.valid_until_ns,
            attached_stop_price=event.attached_stop_price,
            attached_take_profit_price=event.attached_take_profit_price,
            fail=fail,
        )
    else:
        for name in ORDER_REPLACEABLE_FIELDS:
            if getattr(previous, name) != getattr(event, name):
                raise fail(f"{name} may only change with a REPLACED or RECONCILED event")

    # ---- pending replacement --------------------------------------------------------------
    if event.event_type is _E.REPLACE_REQUESTED:
        wanted = _requested_values(event, previous)
        if all(getattr(previous, name) == value for name, value in wanted.items()):
            raise fail("a replacement must change at least one parameter")
        if event.requested_limit_price is not None and previous.order_type is not OrderType.LIMIT:
            raise fail("requested_limit_price is only meaningful for a LIMIT order")
        if event.requested_trigger_price is not None and previous.order_type is not OrderType.STOP:
            raise fail("requested_trigger_price is only meaningful for a STOP order")
        volume = wanted["volume_lots"]
        assert isinstance(volume, Decimal)
        if volume <= previous.cumulative_filled_lots:
            raise fail("requested volume must exceed the quantity already filled")
        limit, trigger, expiry = wanted["limit_price"], wanted["trigger_price"], wanted["valid_until_ns"]
        assert limit is None or isinstance(limit, Decimal)
        assert trigger is None or isinstance(trigger, Decimal)
        assert expiry is None or isinstance(expiry, int)
        _check_no_added_risk(
            previous,
            volume_lots=volume,
            limit_price=limit,
            trigger_price=trigger,
            valid_until_ns=expiry,
            attached_stop_price=previous.attached_stop_price,
            attached_take_profit_price=previous.attached_take_profit_price,
            fail=fail,
        )
    elif event.event_type in (_E.REPLACED, _E.REPLACE_REJECTED):
        if _replacement_outstanding(event):
            raise fail("the outcome of a replacement ends it: requested_* values are cleared")
    elif previous.order_state in _REPLACE_OUTSTANDING and event.order_state in _REPLACE_OUTSTANDING:
        # A fill, the cancel request or a late acknowledgement while the replacement is still
        # outstanding: its requested values are carried unchanged.
        for name in _REQUESTED:
            if getattr(previous, name) != getattr(event, name):
                raise fail(f"{name} may not change while the replacement is outstanding")
    elif _replacement_outstanding(event):
        raise fail("requested_* values may only be carried while a replacement is outstanding")

    # ---- fills ----------------------------------------------------------------------------
    if event.cumulative_filled_lots < previous.cumulative_filled_lots:
        raise fail("cumulative_filled_lots may not decrease")
    if event.last_fill_lots is not None:
        if EXACT.add(previous.cumulative_filled_lots, event.last_fill_lots) != event.cumulative_filled_lots:
            raise fail("cumulative_filled_lots must equal the previous value plus last_fill_lots")
    elif event.event_type is not _E.RECONCILED and (
        event.cumulative_filled_lots != previous.cumulative_filled_lots
    ):
        raise fail("cumulative_filled_lots may only change with a fill or RECONCILED event")
    if event.volume_lots < event.cumulative_filled_lots:
        raise fail("volume_lots may not be reduced below the filled quantity")
