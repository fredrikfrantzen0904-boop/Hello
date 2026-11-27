"""Independent tests for the ``OrderEvent`` contract and ``validate_order_transition``.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (sections 4, 9.1, 9.2),
``specifications/contracts/FIELDS.md`` (OrderEvent fields, invariants OEV-1..14 and the table
"Order state machine", transcribed below as ``SPEC_TRANSITIONS``), the class docstrings and the
owner's specification (G04 "no martingale, averaging down or widening protective stops; hard
protective stops", G08 "live lock, idempotent orders") - not from the implementation.

SYNTHETIC FIXTURE: every record in this module is built from the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``).  Prices, volumes, fills and instants
are invented.  They are not market data, not broker data and not evidence of any performance.
No order built here leaves the process; the only venue that exists is SIMULATED.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    ORDER_TRANSITIONS,
    TERMINAL_ORDER_STATES,
    ExecutionVenue,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    OrderType,
    RunMode,
    Side,
    from_wire,
    identity,
    to_wire,
    validate_order_transition,
)
from gie.core.errors import ContractValidationError, IllegalTransitionError
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

pytestmark = pytest.mark.requirement("S00-ASG-16")

E = OrderEventType
S = OrderState
T0 = samples.T0
REASON = "FIXTURE_REASON"
_REQUESTED = (
    "requested_volume_lots",
    "requested_limit_price",
    "requested_trigger_price",
    "requested_valid_until_ns",
)
_NEEDS_REASON = (E.REJECTED, E.CANCEL_REJECTED, E.REPLACE_REJECTED, E.RECONCILED)

_RECONCILED_TO = frozenset({S.CANCELED, S.EXPIRED, S.FILLED, S.PARTIALLY_FILLED, S.REJECTED, S.WORKING})
_OPEN_ROWS: dict[OrderEventType, frozenset[OrderState]] = {
    E.CANCELED: frozenset({S.CANCELED}),
    E.CANCEL_REQUESTED: frozenset({S.PENDING_CANCEL}),
    E.EXPIRED: frozenset({S.EXPIRED}),
    E.FILLED: frozenset({S.FILLED}),
    E.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED}),
    E.RECONCILED: _RECONCILED_TO,
    E.REPLACE_REQUESTED: frozenset({S.PENDING_REPLACE}),
}
#: Transcription of the table "Order state machine" in specifications/contracts/FIELDS.md.
SPEC_TRANSITIONS: dict[tuple[OrderState | None, OrderEventType], frozenset[OrderState]] = {
    (None, E.SUBMIT_REQUESTED): frozenset({S.PENDING_SUBMIT}),
    (S.PENDING_SUBMIT, E.ACCEPTED): frozenset({S.WORKING}),
    (S.PENDING_SUBMIT, E.CANCEL_REQUESTED): frozenset({S.PENDING_CANCEL}),
    (S.PENDING_SUBMIT, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.PENDING_SUBMIT, E.FILLED): frozenset({S.FILLED}),
    (S.PENDING_SUBMIT, E.PARTIALLY_FILLED): frozenset({S.PARTIALLY_FILLED}),
    (S.PENDING_SUBMIT, E.RECONCILED): _RECONCILED_TO,
    (S.PENDING_SUBMIT, E.REJECTED): frozenset({S.REJECTED}),
    **{(S.WORKING, event): after for event, after in _OPEN_ROWS.items()},
    **{(S.PARTIALLY_FILLED, event): after for event, after in _OPEN_ROWS.items()},
    (S.PENDING_CANCEL, E.CANCELED): frozenset({S.CANCELED}),
    (S.PENDING_CANCEL, E.CANCEL_REJECTED): frozenset({S.PARTIALLY_FILLED, S.WORKING}),
    (S.PENDING_CANCEL, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.PENDING_CANCEL, E.FILLED): frozenset({S.FILLED}),
    (S.PENDING_CANCEL, E.PARTIALLY_FILLED): frozenset({S.PENDING_CANCEL}),
    (S.PENDING_CANCEL, E.RECONCILED): _RECONCILED_TO,
    (S.PENDING_REPLACE, E.CANCELED): frozenset({S.CANCELED}),
    (S.PENDING_REPLACE, E.CANCEL_REQUESTED): frozenset({S.PENDING_CANCEL}),
    (S.PENDING_REPLACE, E.EXPIRED): frozenset({S.EXPIRED}),
    (S.PENDING_REPLACE, E.FILLED): frozenset({S.FILLED}),
    (S.PENDING_REPLACE, E.PARTIALLY_FILLED): frozenset({S.PENDING_REPLACE}),
    (S.PENDING_REPLACE, E.RECONCILED): _RECONCILED_TO,
    (S.PENDING_REPLACE, E.REPLACED): frozenset({S.PARTIALLY_FILLED, S.WORKING}),
    (S.PENDING_REPLACE, E.REPLACE_REJECTED): frozenset({S.PARTIALLY_FILLED, S.WORKING}),
}
SPEC_TERMINAL = frozenset({S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED})


# --------------------------------------------------------------------------- builders
def _violates(invariant: str, build: Callable[[], object]) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as caught:
        build()
    assert caught.value.details.get("invariant") == invariant, caught.value
    assert caught.value.details.get("contract") == "OrderEvent"
    return caught.value


def _refused(previous: OrderEvent | None, event: OrderEvent) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        validate_order_transition(previous, event)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert caught.value.details.get("client_order_id") == event.client_order_id
    return caught.value


def _rule(error: IllegalTransitionError) -> str:
    return str(error.details.get("rule"))


def _fields(event: OrderEvent) -> dict[str, Any]:
    return {spec.name: getattr(event, spec.name) for spec in OrderEvent.FIELD_SPECS}


def _next(
    previous: OrderEvent, event_type: OrderEventType, order_state: OrderState, **overrides: Any
) -> OrderEvent:
    """The following event of the same order: parameters and fills carried over, counters advanced."""
    fields = _fields(previous)
    moment = previous.receive_time_ns + NS_PER_MS
    fields.update(
        seq=previous.seq + 1,
        order_seq=previous.order_seq + 1,
        event_type=event_type,
        order_state=order_state,
        event_time_ns=moment,
        receive_time_ns=moment,
        last_fill_lots=None,
        last_fill_price=None,
        last_fill_cost_amount=None,
        reason_code=REASON if event_type in _NEEDS_REASON else None,
    )
    if order_state is not S.PENDING_REPLACE:
        fields.update(dict.fromkeys(_REQUESTED))
    if previous.broker_order_id is None and event_type is not E.CANCEL_REQUESTED:
        fields["broker_order_id"] = identity.broker_order_id(previous.client_order_id)
    fields.update(overrides)
    if "order_event_id" not in overrides:
        fields["order_event_id"] = identity.order_event_id(fields["client_order_id"], fields["order_seq"])
    return OrderEvent(**fields)


def _price(event: OrderEvent) -> D:
    price = event.limit_price or event.trigger_price or event.reference_price
    assert price is not None
    return price


def _fill(
    previous: OrderEvent, lots: str | D, order_state: OrderState | None = None, **overrides: Any
) -> OrderEvent:
    """A fill of *lots* at the order's own price (so the average fill price stays that price)."""
    quantity = D(lots)
    cumulative = previous.cumulative_filled_lots + quantity
    complete = cumulative == previous.volume_lots
    if order_state is None:
        if complete:
            order_state = S.FILLED
        elif previous.order_state in (S.PENDING_CANCEL, S.PENDING_REPLACE):
            order_state = previous.order_state
        else:
            order_state = S.PARTIALLY_FILLED
    fields: dict[str, Any] = {
        "cumulative_filled_lots": cumulative,
        "last_fill_lots": quantity,
        "last_fill_price": _price(previous),
        "average_fill_price": _price(previous),
        "last_fill_cost_amount": D("0.40"),
    }
    fields.update(overrides)
    return _next(previous, E.FILLED if complete else E.PARTIALLY_FILLED, order_state, **fields)


def _submit(**overrides: Any) -> OrderEvent:
    return samples.order_event(**overrides)


def _working(**overrides: Any) -> OrderEvent:
    return _next(_submit(**overrides), E.ACCEPTED, S.WORKING)


def _stop_submit(**overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of a protective SELL stop (for a long position), as in the samples."""
    fields = _fields(samples.protective_stop_lifecycle()[0])
    del fields["client_order_id"], fields["order_event_id"]
    fields.update(overrides)
    return samples.order_event(**fields)


def _buy_stop_submit(**overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of a protective BUY stop (for a short position)."""
    return _stop_submit(side=Side.BUY, trigger_price=D("2652.10"), **overrides)


def _child_submit(intent: OrderIntent, order_type: OrderType, **overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of a TAKE_PROFIT or EXIT order of the sample position."""
    fields: dict[str, Any] = {
        "intent": intent,
        "order_type": order_type,
        "trigger_price": D("2648.50") if order_type is OrderType.STOP else None,
        "limit_price": D("2652.80") if order_type is OrderType.LIMIT else None,
        "reason_code": "TIME_EXIT",
    }
    fields.update(overrides)
    return _stop_submit(**fields)


def _reach(state: OrderState, *, filled: bool = False) -> OrderEvent:
    """Newest event of a sample entry order that is in *state* (with or without a partial fill)."""
    submit = _submit()
    working = _next(submit, E.ACCEPTED, S.WORKING)
    base = _fill(working, "0.20") if filled else working
    if state is S.PENDING_SUBMIT:
        assert not filled
        return submit
    if state is S.WORKING:
        assert not filled
        return working
    if state is S.PARTIALLY_FILLED:
        return _fill(working, "0.20")
    if state is S.PENDING_CANCEL:
        return _next(base, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    if state is S.PENDING_REPLACE:
        return _next(base, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.40"))
    if state is S.FILLED:
        return _fill(working, "0.50")
    if state is S.CANCELED:
        return _next(base, E.CANCELED, S.CANCELED)
    if state is S.REJECTED:
        return _next(submit, E.REJECTED, S.REJECTED)
    assert state is S.EXPIRED
    return _next(base, E.EXPIRED, S.EXPIRED)


def _follow(previous: OrderEvent, event_type: OrderEventType, after: OrderState) -> OrderEvent:
    """An event of *event_type* that leads from *previous* to *after* without breaking any other rule."""
    if event_type is E.PARTIALLY_FILLED:
        return _fill(previous, "0.10", after)
    if event_type is E.FILLED:
        return _fill(previous, previous.volume_lots - previous.cumulative_filled_lots, after)
    if event_type is E.REPLACE_REQUESTED:
        return _next(previous, event_type, after, requested_volume_lots=D("0.40"))
    if event_type is E.REPLACED:
        return _next(previous, event_type, after, volume_lots=previous.requested_volume_lots)
    if event_type is E.RECONCILED:
        cumulative = previous.cumulative_filled_lots
        if after is S.FILLED:
            cumulative = previous.volume_lots
        elif after is S.PARTIALLY_FILLED and cumulative == 0:
            cumulative = D("0.10")
        return _next(
            previous,
            event_type,
            after,
            cumulative_filled_lots=cumulative,
            average_fill_price=_price(previous) if cumulative > 0 else None,
        )
    return _next(previous, event_type, after)


def _standalone(previous: OrderEvent, event_type: OrderEventType) -> OrderEvent:
    """A valid event of *event_type* numbered after *previous*, whatever state *previous* is in."""
    volume = previous.volume_lots
    no_fill = {"cumulative_filled_lots": D("0"), "average_fill_price": None}
    some_fill = {"cumulative_filled_lots": D("0.10"), "average_fill_price": _price(previous)}
    last = {"last_fill_lots": D("0.10"), "last_fill_price": _price(previous)}
    shapes: dict[OrderEventType, tuple[OrderState, dict[str, Any]]] = {
        E.ACCEPTED: (S.WORKING, no_fill),
        E.REJECTED: (S.REJECTED, no_fill),
        E.PARTIALLY_FILLED: (S.PARTIALLY_FILLED, {**some_fill, **last}),
        E.FILLED: (S.FILLED, {**some_fill, **last, "cumulative_filled_lots": volume}),
        E.CANCEL_REQUESTED: (S.PENDING_CANCEL, no_fill),
        E.CANCELED: (S.CANCELED, no_fill),
        E.CANCEL_REJECTED: (S.WORKING, no_fill),
        E.REPLACE_REQUESTED: (S.PENDING_REPLACE, {**no_fill, "requested_volume_lots": D("0.40")}),
        E.REPLACED: (S.WORKING, no_fill),
        E.REPLACE_REJECTED: (S.WORKING, no_fill),
        E.EXPIRED: (S.EXPIRED, no_fill),
        E.RECONCILED: (S.CANCELED, no_fill),
    }
    if event_type is E.SUBMIT_REQUESTED:
        # OEV-2 ties SUBMIT_REQUESTED to order_seq 0, so a "second submit" is the first event again.
        return _submit(seq=previous.seq + 1)
    state, extra = shapes[event_type]
    return _next(previous, event_type, state, **extra)


def _walk(chain: list[OrderEvent]) -> None:
    previous: OrderEvent | None = None
    for event in chain:
        validate_order_transition(previous, event)
        previous = event


# --------------------------------------------------------------------------- samples and vocabulary
def test_the_sample_lifecycles_are_valid_chains() -> None:
    entry = samples.order_lifecycle()
    stop = samples.protective_stop_lifecycle()
    _walk(entry)
    _walk(stop)
    assert [event.order_state for event in entry] == [
        S.PENDING_SUBMIT,
        S.WORKING,
        S.PARTIALLY_FILLED,
        S.FILLED,
    ]
    assert [event.order_seq for event in entry] == [0, 1, 2, 3]
    assert [event.event_type for event in stop] == [
        E.SUBMIT_REQUESTED,
        E.ACCEPTED,
        E.REPLACE_REQUESTED,
        E.REPLACED,
    ]
    assert entry[-1].is_terminal is True
    assert [event.is_terminal for event in entry[:-1]] == [False, False, False]
    assert [event.is_request for event in stop] == [True, False, True, False]


def test_every_named_sample_order_event_survives_the_wire() -> None:
    for record in samples.variants()["OrderEvent"].values():
        assert type(record) is OrderEvent
        assert from_wire(to_wire(record), OrderEvent) == record


def test_the_published_state_machine_is_the_enforced_one() -> None:
    assert len(SPEC_TRANSITIONS) == 36
    assert dict(ORDER_TRANSITIONS) == SPEC_TRANSITIONS
    assert TERMINAL_ORDER_STATES == SPEC_TERMINAL
    assert not [key for key in ORDER_TRANSITIONS if key[0] in SPEC_TERMINAL]


# --------------------------------------------------------------------------- transition table
def _needs_fills(before: OrderState, event_type: OrderEventType, after: OrderState) -> bool:
    if before is S.PARTIALLY_FILLED:
        return True
    return after is S.PARTIALLY_FILLED and event_type in (E.CANCEL_REJECTED, E.REPLACED, E.REPLACE_REJECTED)


#: A reconciliation cannot take fills back, so PARTIALLY_FILLED -> WORKING is not reachable in practice.
_UNFILL = (S.PARTIALLY_FILLED, E.RECONCILED, S.WORKING)
_ROWS = sorted(
    (
        (before, event_type, after)
        for (before, event_type), states in SPEC_TRANSITIONS.items()
        if before is not None
        for after in states
        if (before, event_type, after) != _UNFILL
    ),
    key=lambda row: (row[0].value, row[1].value, row[2].value),
)


@pytest.mark.parametrize(
    ("before", "event_type", "after"),
    _ROWS,
    ids=[f"{before.value}--{event.value}-->{after.value}" for before, event, after in _ROWS],
)
def test_every_row_of_the_state_machine_is_accepted(
    before: OrderState, event_type: OrderEventType, after: OrderState
) -> None:
    previous = _reach(before, filled=_needs_fills(before, event_type, after))
    assert previous.order_state is before
    event = _follow(previous, event_type, after)
    assert (event.event_type, event.order_state) == (event_type, after)
    validate_order_transition(previous, event)


def test_the_first_event_of_an_order_is_submit_requested() -> None:
    submit = _submit()
    assert (submit.event_type, submit.order_state, submit.order_seq) == (
        E.SUBMIT_REQUESTED,
        S.PENDING_SUBMIT,
        0,
    )
    validate_order_transition(None, submit)


def test_a_reconciliation_cannot_take_fills_back() -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    unfilled = _next(
        partially, E.RECONCILED, S.WORKING, cumulative_filled_lots=D("0"), average_fill_price=None
    )
    assert "cumulative_filled_lots" in _rule(_refused(partially, unfilled))


_NON_TERMINAL = [S.PENDING_SUBMIT, S.WORKING, S.PARTIALLY_FILLED, S.PENDING_CANCEL, S.PENDING_REPLACE]
_ABSENT = [
    (before, event_type)
    for before in _NON_TERMINAL
    for event_type in E
    if (before, event_type) not in SPEC_TRANSITIONS
]


@pytest.mark.parametrize(
    ("before", "event_type"), _ABSENT, ids=[f"{before.value}--{event.value}" for before, event in _ABSENT]
)
def test_an_event_without_a_row_in_the_state_machine_is_refused(
    before: OrderState, event_type: OrderEventType
) -> None:
    previous = _reach(before)
    _refused(previous, _standalone(previous, event_type))


@pytest.mark.parametrize("terminal", sorted(SPEC_TERMINAL, key=lambda state: state.value))
@pytest.mark.parametrize("event_type", list(E))
def test_no_event_may_follow_a_terminal_state(terminal: OrderState, event_type: OrderEventType) -> None:
    previous = _reach(terminal)
    assert previous.is_terminal is True
    _refused(previous, _standalone(previous, event_type))


@pytest.mark.parametrize("event_type", [event for event in E if event is not E.SUBMIT_REQUESTED])
def test_an_order_cannot_begin_with_anything_but_its_submit_request(event_type: OrderEventType) -> None:
    _refused(None, _standalone(_submit(), event_type))


@pytest.mark.parametrize(
    ("before", "filled", "wrong_state"),
    [
        (S.PENDING_CANCEL, False, S.PARTIALLY_FILLED),
        (S.PENDING_CANCEL, False, S.PENDING_REPLACE),
        (S.PENDING_REPLACE, False, S.PARTIALLY_FILLED),
        (S.PENDING_REPLACE, False, S.PENDING_CANCEL),
        (S.WORKING, False, S.PENDING_CANCEL),
        (S.PARTIALLY_FILLED, True, S.PENDING_CANCEL),
        (S.PENDING_SUBMIT, False, S.PENDING_CANCEL),
    ],
)
def test_a_partial_fill_leads_only_to_the_state_the_table_names(
    before: OrderState, filled: bool, wrong_state: OrderState
) -> None:
    previous = _reach(before, filled=filled)
    extra = {"requested_volume_lots": D("0.40")} if wrong_state is S.PENDING_REPLACE else {}
    _refused(previous, _fill(previous, "0.10", wrong_state, **extra))


def test_a_rejected_cancel_or_replace_returns_only_to_an_open_state() -> None:
    pending_cancel = _reach(S.PENDING_CANCEL)
    validate_order_transition(pending_cancel, _next(pending_cancel, E.CANCEL_REJECTED, S.WORKING))
    pending_replace = _reach(S.PENDING_REPLACE)
    validate_order_transition(pending_replace, _next(pending_replace, E.REPLACE_REJECTED, S.WORKING))
    # A rejected replacement leaves the order's parameters untouched.
    changed = _next(pending_replace, E.REPLACE_REJECTED, S.WORKING, volume_lots=D("0.40"))
    _refused(pending_replace, changed)


# --------------------------------------------------------------------------- sequence continuity
def test_order_seq_increases_by_exactly_one() -> None:
    submit = _submit()
    accepted = _next(submit, E.ACCEPTED, S.WORKING)
    validate_order_transition(submit, accepted)
    assert "order_seq" in _rule(_refused(submit, _next(submit, E.ACCEPTED, S.WORKING, order_seq=2)))
    _refused(submit, _next(accepted, E.CANCEL_REQUESTED, S.PENDING_CANCEL))
    # The same number again (another payload at a taken position of the order).
    _refused(accepted, _next(submit, E.EXPIRED, S.EXPIRED))
    _refused(accepted, _next(accepted, E.EXPIRED, S.EXPIRED, order_seq=1))


def test_a_first_event_with_a_later_order_seq_is_refused() -> None:
    accepted = _working()
    _refused(None, accepted)
    _refused(None, _next(accepted, E.EXPIRED, S.EXPIRED))


@pytest.mark.parametrize("field", ["event_time_ns", "receive_time_ns", "seq"])
def test_times_and_stream_position_never_go_back(field: str) -> None:
    submit = _submit(seq=5, receive_time_ns=T0 + 5 * NS_PER_MS)
    assert submit.event_time_ns < submit.receive_time_ns
    good = _next(submit, E.ACCEPTED, S.WORKING)
    validate_order_transition(submit, good)
    overrides: dict[str, Any] = {field: getattr(submit, field) - 1}
    if field == "receive_time_ns":
        overrides["event_time_ns"] = submit.event_time_ns  # keep OEV-12: event time <= receive time
    assert field in _rule(_refused(submit, _next(submit, E.ACCEPTED, S.WORKING, **overrides)))


def test_times_may_stay_equal_between_two_events() -> None:
    submit = _submit()
    same_instant = _next(
        submit,
        E.ACCEPTED,
        S.WORKING,
        event_time_ns=submit.event_time_ns,
        receive_time_ns=submit.receive_time_ns,
    )
    validate_order_transition(submit, same_instant)


# --------------------------------------------------------------------------- immutable fields
def _other_plan_ids() -> dict[str, str]:
    plan = samples.trade_plan(variant=1)
    return {"plan_id": plan.plan_id}


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize(
    "changed",
    [
        {"side": Side.SELL, "attached_stop_price": D("2651.80")},
        {"reference_price": D("2650.40")},
        {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2649.80")},
        {"order_type": OrderType.MARKET, "limit_price": None},
        {"mode": RunMode.PAPER},
        _other_plan_ids(),
        {"alternative_group_id": identity.alternative_group_id(samples.unscored_candidate().opportunity_id)},
    ],
    ids=["side", "reference_price", "order_type_stop", "order_type_market", "mode", "plan_id", "group"],
)
def test_the_identity_of_an_order_never_changes(changed: dict[str, Any]) -> None:
    submit = _submit()
    validate_order_transition(submit, _next(submit, E.ACCEPTED, S.WORKING))
    event = _next(submit, E.ACCEPTED, S.WORKING, **changed)
    assert event.client_order_id == submit.client_order_id
    _refused(submit, event)


def test_an_event_of_another_order_cannot_continue_this_order() -> None:
    submit = _submit()
    other_decision = samples.risk_decision(attempt=1, seq=1)
    other_order = _next(
        submit,
        E.ACCEPTED,
        S.WORKING,
        risk_decision_id=other_decision.decision_id,
        client_order_id=identity.client_order_id(submit.run_id, other_decision.decision_id, "ENTRY", 0),
        broker_order_id=None,
    )
    assert "client_order_id" in _rule(_refused(submit, other_order))
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    assert submit.risk_decision_id is not None
    foreign_run = _next(
        submit,
        E.ACCEPTED,
        S.WORKING,
        run_id=other_run,
        client_order_id=identity.client_order_id(other_run, submit.risk_decision_id, "ENTRY", 0),
        broker_order_id=None,
    )
    _refused(submit, foreign_run)


def test_a_protective_order_keeps_its_position_and_attempt() -> None:
    submit = _stop_submit()
    validate_order_transition(submit, _next(submit, E.ACCEPTED, S.WORKING))
    assert submit.position_id is not None
    renumbered = _next(
        submit,
        E.ACCEPTED,
        S.WORKING,
        attempt=1,
        client_order_id=identity.client_order_id(submit.run_id, submit.position_id, "PROTECTIVE_STOP", 1),
        broker_order_id=None,
    )
    _refused(submit, renumbered)


def test_broker_order_id_is_assigned_once_and_then_fixed() -> None:
    submit = _submit()
    assert submit.broker_order_id is None
    accepted = _next(submit, E.ACCEPTED, S.WORKING)
    assert accepted.broker_order_id == identity.broker_order_id(submit.client_order_id)
    validate_order_transition(submit, accepted)
    other = identity.broker_order_id(_stop_submit().client_order_id)
    changed = _next(accepted, E.CANCELED, S.CANCELED, broker_order_id=other)
    removed = _next(accepted, E.CANCELED, S.CANCELED, broker_order_id=None)
    assert "broker_order_id" in _rule(_refused(accepted, changed))
    assert "broker_order_id" in _rule(_refused(accepted, removed))


@pytest.mark.requirement("G04-09", "G04-10", "G04-11")
@pytest.mark.parametrize(
    "changed",
    [
        {"volume_lots": D("0.60")},
        {"volume_lots": D("0.40")},
        {"limit_price": D("2649.90")},
        {"attached_stop_price": D("2647.00")},
        {"attached_stop_price": D("2648.50")},
        {"attached_take_profit_price": D("2655.00")},
        {"valid_until_ns": T0 + 60 * NS_PER_SECOND},
    ],
    ids=["volume_up", "volume_down", "limit", "stop_wider", "stop_tighter", "take_profit", "expiry"],
)
@pytest.mark.parametrize("event_type", [E.ACCEPTED, E.CANCEL_REQUESTED, E.EXPIRED])
def test_parameters_change_only_with_a_replaced_or_reconciled_event(
    event_type: OrderEventType, changed: dict[str, Any]
) -> None:
    previous = _submit()
    state = {E.ACCEPTED: S.WORKING, E.CANCEL_REQUESTED: S.PENDING_CANCEL, E.EXPIRED: S.EXPIRED}[event_type]
    validate_order_transition(previous, _next(previous, event_type, state))
    _refused(previous, _next(previous, event_type, state, **changed))


# --------------------------------------------------------------------------- fills
def test_fills_accumulate_exactly() -> None:
    working = _working()
    first = _fill(working, "0.20")
    second = _fill(first, "0.10")
    last = _fill(second, "0.20")
    for previous, event in ((working, first), (first, second), (second, last)):
        validate_order_transition(previous, event)
        assert event.cumulative_filled_lots == previous.cumulative_filled_lots + event.last_fill_lots
    assert (second.cumulative_filled_lots, last.cumulative_filled_lots) == (D("0.30"), D("0.50"))
    assert last.order_state is S.FILLED


@pytest.mark.parametrize(
    ("last_fill", "cumulative"),
    [("0.10", "0.40"), ("0.20", "0.30"), ("0.10", "0.20"), ("0.000000000001", "0.30")],
    ids=["jumps_ahead", "falls_short", "no_progress", "off_by_one_ulp"],
)
def test_cumulative_must_be_the_previous_value_plus_the_last_fill(last_fill: str, cumulative: str) -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    assert partially.cumulative_filled_lots == D("0.20")
    event = _fill(partially, last_fill, cumulative_filled_lots=D(cumulative))
    assert "cumulative_filled_lots" in _rule(_refused(partially, event))


def test_a_final_fill_must_add_up_as_well() -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    wrong = _next(
        partially,
        E.FILLED,
        S.FILLED,
        cumulative_filled_lots=D("0.50"),
        last_fill_lots=D("0.20"),
        last_fill_price=D("2649.80"),
    )
    _refused(partially, wrong)


def test_cumulative_fills_never_decrease() -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    lower = _next(partially, E.CANCELED, S.CANCELED, cumulative_filled_lots=D("0.10"))
    assert "cumulative_filled_lots" in _rule(_refused(partially, lower))
    reconciled_lower = _next(partially, E.RECONCILED, S.CANCELED, cumulative_filled_lots=D("0.10"))
    _refused(partially, reconciled_lower)


def test_only_fill_and_reconciled_events_change_the_filled_quantity() -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    canceled_more = _next(partially, E.CANCELED, S.CANCELED, cumulative_filled_lots=D("0.30"))
    _refused(partially, canceled_more)
    cancel_request_more = _next(
        partially, E.CANCEL_REQUESTED, S.PENDING_CANCEL, cumulative_filled_lots=D("0.30")
    )
    _refused(partially, cancel_request_more)
    reconciled_more = _next(partially, E.RECONCILED, S.CANCELED, cumulative_filled_lots=D("0.30"))
    validate_order_transition(partially, reconciled_more)


@pytest.mark.parametrize("pending", [S.PENDING_CANCEL, S.PENDING_REPLACE])
@pytest.mark.parametrize("filled_before", [False, True])
def test_a_partial_fill_can_race_with_a_pending_cancel_or_replace(
    pending: OrderState, filled_before: bool
) -> None:
    previous = _reach(pending, filled=filled_before)
    partial = _fill(previous, "0.10")
    assert partial.order_state is pending
    assert partial.event_type is E.PARTIALLY_FILLED
    validate_order_transition(previous, partial)
    if pending is S.PENDING_REPLACE:
        assert partial.requested_volume_lots == previous.requested_volume_lots == D("0.40")
    complete = _fill(partial, partial.volume_lots - partial.cumulative_filled_lots)
    assert complete.order_state is S.FILLED
    validate_order_transition(partial, complete)


def test_the_request_of_a_pending_replacement_cannot_change_while_it_is_pending() -> None:
    pending = _reach(S.PENDING_REPLACE)
    changed = _fill(pending, "0.10", requested_volume_lots=D("0.30"))
    assert "requested_volume_lots" in _rule(_refused(pending, changed))
    added = _fill(pending, "0.10", requested_valid_until_ns=T0 + 10 * NS_PER_SECOND)
    _refused(pending, added)


@pytest.mark.requirement("G04-09")
def test_an_over_fill_cannot_be_constructed() -> None:
    working = _working()
    partially = _reach(S.PARTIALLY_FILLED)

    def fill(previous: OrderEvent, event_type: OrderEventType, state: OrderState, lots: str) -> OrderEvent:
        quantity = D(lots)
        return _next(
            previous,
            event_type,
            state,
            cumulative_filled_lots=previous.cumulative_filled_lots + quantity,
            last_fill_lots=quantity,
            last_fill_price=D("2649.80"),
            average_fill_price=D("2649.80"),
        )

    assert fill(working, E.FILLED, S.FILLED, "0.50").cumulative_filled_lots == working.volume_lots
    assert fill(partially, E.FILLED, S.FILLED, "0.30").cumulative_filled_lots == working.volume_lots
    _violates("OEV-9", lambda: fill(working, E.FILLED, S.FILLED, "0.51"))
    _violates("OEV-9", lambda: fill(working, E.FILLED, S.FILLED, "0.500000000001"))
    _violates("OEV-9", lambda: fill(working, E.PARTIALLY_FILLED, S.PARTIALLY_FILLED, "0.51"))
    _violates("OEV-9", lambda: fill(partially, E.FILLED, S.FILLED, "0.31"))
    _violates("OEV-9", lambda: fill(partially, E.PARTIALLY_FILLED, S.PARTIALLY_FILLED, "0.40"))
    _violates(
        "OEV-9",
        lambda: _next(partially, E.RECONCILED, S.FILLED, cumulative_filled_lots=D("0.500000000001")),
    )


# --------------------------------------------------------------------------- replacement protocol
@pytest.mark.requirement("G04-11")
def test_replace_requested_carries_the_wanted_values_and_leaves_the_order_as_it_is() -> None:
    _submitted, working, request, replaced = samples.protective_stop_lifecycle()
    assert request.order_state is S.PENDING_REPLACE
    assert request.requested_trigger_price == D("2649.90")
    assert request.trigger_price == working.trigger_price == D("2647.80")
    assert (request.volume_lots, request.valid_until_ns) == (working.volume_lots, working.valid_until_ns)
    assert replaced.trigger_price == D("2649.90")
    assert replaced.requested_trigger_price is None
    moved_already = _next(
        working,
        E.REPLACE_REQUESTED,
        S.PENDING_REPLACE,
        trigger_price=D("2649.90"),
        requested_trigger_price=D("2649.90"),
    )
    _refused(working, moved_already)


@pytest.mark.requirement("G04-11")
@pytest.mark.parametrize(
    "applied",
    [D("2647.80"), D("2649.89"), D("2649.91"), D("2650.50")],
    ids=["nothing_applied", "less_than_requested", "more_than_requested", "something_else"],
)
def test_replaced_must_apply_exactly_the_requested_trigger(applied: D) -> None:
    request = samples.protective_stop_lifecycle()[2]
    validate_order_transition(request, _next(request, E.REPLACED, S.WORKING, trigger_price=D("2649.90")))
    _refused(request, _next(request, E.REPLACED, S.WORKING, trigger_price=applied))


def test_replaced_must_apply_exactly_the_requested_volume_and_expiry() -> None:
    working = _working()
    earlier = T0 + 10 * NS_PER_SECOND
    request = _next(
        working,
        E.REPLACE_REQUESTED,
        S.PENDING_REPLACE,
        requested_volume_lots=D("0.30"),
        requested_valid_until_ns=earlier,
    )
    validate_order_transition(working, request)
    exact = _next(request, E.REPLACED, S.WORKING, volume_lots=D("0.30"), valid_until_ns=earlier)
    validate_order_transition(request, exact)
    for wrong in (
        {"volume_lots": D("0.30")},
        {"valid_until_ns": earlier},
        {"volume_lots": D("0.40"), "valid_until_ns": earlier},
        {"volume_lots": D("0.30"), "valid_until_ns": earlier - 1},
        {"volume_lots": D("0.20"), "valid_until_ns": earlier},
        {},
    ):
        _refused(request, _next(request, E.REPLACED, S.WORKING, **wrong))


@pytest.mark.requirement("G04-11", "G04-12")
def test_replaced_cannot_change_what_was_not_requested() -> None:
    request = _reach(S.PENDING_REPLACE)
    assert request.requested_volume_lots == D("0.40")
    validate_order_transition(request, _next(request, E.REPLACED, S.WORKING, volume_lots=D("0.40")))
    for sneaked in (
        {"limit_price": D("2649.90")},
        {"attached_stop_price": D("2647.00")},
        {"attached_stop_price": D("2648.50")},
        {"attached_take_profit_price": D("2655.00")},
        {"valid_until_ns": T0 + 20 * NS_PER_SECOND},
    ):
        _refused(request, _next(request, E.REPLACED, S.WORKING, volume_lots=D("0.40"), **sneaked))


@pytest.mark.parametrize(
    "nothing",
    [
        {"requested_volume_lots": D("0.50")},
        {"requested_limit_price": D("2649.80")},
        {"requested_valid_until_ns": T0 + 30 * NS_PER_SECOND},
        {
            "requested_volume_lots": D("0.5"),
            "requested_limit_price": D("2649.8"),
            "requested_valid_until_ns": T0 + 30 * NS_PER_SECOND,
        },
    ],
    ids=["same_volume", "same_limit", "same_expiry", "all_the_same"],
)
def test_a_replacement_that_changes_nothing_is_refused(nothing: dict[str, Any]) -> None:
    working = _working()
    _refused(working, _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, **nothing))


def test_a_stop_replacement_to_the_same_trigger_is_refused() -> None:
    working = samples.protective_stop_lifecycle()[1]
    same = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2647.80"))
    _refused(working, same)


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize("requested", ["0.500000000001", "0.51", "1.00", "5"])
def test_no_order_may_grow_by_replacement(requested: str) -> None:
    grow = {"requested_volume_lots": D(requested)}
    entry = _working()
    assert "volume_lots" in _rule(
        _refused(entry, _next(entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, **grow))
    )
    stop = samples.protective_stop_lifecycle()[1]
    _refused(stop, _next(stop, E.REPLACE_REQUESTED, S.PENDING_REPLACE, **grow))
    for child in (
        _child_submit(OrderIntent.TAKE_PROFIT, OrderType.LIMIT),
        _child_submit(OrderIntent.EXIT, OrderType.LIMIT),
    ):
        resting = _next(child, E.ACCEPTED, S.WORKING)
        _refused(resting, _next(resting, E.REPLACE_REQUESTED, S.PENDING_REPLACE, **grow))


@pytest.mark.requirement("G04-09")
def test_an_order_may_shrink_by_replacement_but_not_below_its_fills() -> None:
    entry = _working()
    smaller = _next(entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.10"))
    validate_order_transition(entry, smaller)
    partially = _reach(S.PARTIALLY_FILLED)
    assert partially.cumulative_filled_lots == D("0.20")
    above = _next(partially, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.21"))
    validate_order_transition(partially, above)
    for too_small in ("0.20", "0.10"):
        request = _next(partially, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D(too_small))
        _refused(partially, request)


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize("price", ["2649.70", "2649.90", "2649.800000000001"])
def test_the_limit_price_of_an_entry_is_fixed(price: str) -> None:
    entry = _working()
    request = _next(entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_limit_price=D(price))
    assert "limit_price" in _rule(_refused(entry, request))


@pytest.mark.requirement("G04-12")
def test_the_trigger_price_of_a_stop_entry_is_fixed() -> None:
    stop_entry = _working(order_type=OrderType.STOP, limit_price=None, trigger_price=D("2651.00"))
    for price in ("2650.90", "2651.10"):
        request = _next(stop_entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D(price))
        assert "trigger_price" in _rule(_refused(stop_entry, request))


@pytest.mark.requirement("G04-12")
def test_the_expiry_of_an_entry_may_only_move_earlier() -> None:
    entry = _working()
    assert entry.valid_until_ns == T0 + 30 * NS_PER_SECOND
    earlier = _next(
        entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_valid_until_ns=T0 + 30 * NS_PER_SECOND - 1
    )
    validate_order_transition(entry, earlier)
    for later in (T0 + 30 * NS_PER_SECOND + 1, T0 + 3600 * NS_PER_SECOND):
        request = _next(entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_valid_until_ns=later)
        assert "expiry" in _rule(_refused(entry, request))


def test_a_requested_price_must_fit_the_order_type() -> None:
    entry = _working()
    wrong_for_limit = _next(
        entry, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2649.00")
    )
    _refused(entry, wrong_for_limit)
    stop = samples.protective_stop_lifecycle()[1]
    wrong_for_stop = _next(stop, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_limit_price=D("2649.00"))
    _refused(stop, wrong_for_stop)


@pytest.mark.requirement("G04-11")
@pytest.mark.parametrize(
    ("requested", "allowed"),
    [("2647.800000000001", True), ("2649.90", True), ("2647.799999999999", False), ("2640.00", False)],
    ids=["one_ulp_tighter", "tighter", "one_ulp_wider", "wider"],
)
def test_a_sell_stop_protecting_a_long_position_may_only_be_raised(requested: str, allowed: bool) -> None:
    working = samples.protective_stop_lifecycle()[1]
    assert (working.side, working.trigger_price) == (Side.SELL, D("2647.80"))
    request = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D(requested))
    if allowed:
        validate_order_transition(working, request)
        validate_order_transition(request, _next(request, E.REPLACED, S.WORKING, trigger_price=D(requested)))
    else:
        assert "tightened" in _rule(_refused(working, request))


@pytest.mark.requirement("G04-11")
@pytest.mark.parametrize(
    ("requested", "allowed"),
    [("2652.099999999999", True), ("2650.00", True), ("2652.100000000001", False), ("2660.00", False)],
    ids=["one_ulp_tighter", "tighter", "one_ulp_wider", "wider"],
)
def test_a_buy_stop_protecting_a_short_position_may_only_be_lowered(requested: str, allowed: bool) -> None:
    working = _next(_buy_stop_submit(), E.ACCEPTED, S.WORKING)
    assert (working.side, working.trigger_price) == (Side.BUY, D("2652.10"))
    request = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D(requested))
    if allowed:
        validate_order_transition(working, request)
        validate_order_transition(request, _next(request, E.REPLACED, S.WORKING, trigger_price=D(requested)))
    else:
        assert "tightened" in _rule(_refused(working, request))


@pytest.mark.requirement("G04-11", "G04-12")
def test_a_protective_stop_never_gets_an_expiry() -> None:
    for working in (
        samples.protective_stop_lifecycle()[1],
        _next(_buy_stop_submit(), E.ACCEPTED, S.WORKING),
    ):
        request = _next(
            working,
            E.REPLACE_REQUESTED,
            S.PENDING_REPLACE,
            requested_valid_until_ns=T0 + 3600 * NS_PER_SECOND,
        )
        assert "expiry" in _rule(_refused(working, request))
        _violates(
            "OEV-13",
            lambda working=working: _next(
                working, E.RECONCILED, S.WORKING, valid_until_ns=T0 + 3600 * NS_PER_SECOND
            ),
        )
    _violates("OEV-13", lambda: _stop_submit(valid_until_ns=T0 + 3600 * NS_PER_SECOND))


def test_take_profit_and_exit_orders_may_be_repriced_in_both_directions() -> None:
    for intent in (OrderIntent.TAKE_PROFIT, OrderIntent.EXIT):
        resting = _next(_child_submit(intent, OrderType.LIMIT), E.ACCEPTED, S.WORKING)
        for price in ("2651.00", "2655.00"):
            request = _next(resting, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_limit_price=D(price))
            validate_order_transition(resting, request)
            validate_order_transition(request, _next(request, E.REPLACED, S.WORKING, limit_price=D(price)))


# --------------------------------------------------------------------------- reconciliation
@pytest.mark.requirement("G04-09", "G04-10", "G04-11", "G04-12")
@pytest.mark.parametrize(
    "adopted",
    [
        {"volume_lots": D("0.60")},
        {"limit_price": D("2649.90")},
        {"limit_price": D("2649.70")},
        {"attached_stop_price": D("2647.00")},
        {"attached_stop_price": D("2648.50")},
        {"attached_take_profit_price": D("2655.00")},
        {"valid_until_ns": T0 + 31 * NS_PER_SECOND},
    ],
    ids=["volume_up", "limit_up", "limit_down", "stop_wider", "stop_moved", "take_profit", "expiry_later"],
)
def test_a_reconciliation_cannot_add_risk_to_an_entry(adopted: dict[str, Any]) -> None:
    for previous in (_reach(S.PENDING_SUBMIT), _reach(S.WORKING), _reach(S.PENDING_CANCEL)):
        validate_order_transition(previous, _next(previous, E.RECONCILED, S.WORKING))
        _refused(previous, _next(previous, E.RECONCILED, S.WORKING, **adopted))


@pytest.mark.requirement("G04-09")
def test_a_reconciliation_may_shrink_an_entry_and_shorten_its_expiry() -> None:
    working = _working()
    adopted = _next(
        working, E.RECONCILED, S.WORKING, volume_lots=D("0.30"), valid_until_ns=T0 + 10 * NS_PER_SECOND
    )
    validate_order_transition(working, adopted)


@pytest.mark.requirement("G04-12")
def test_a_reconciliation_cannot_remove_the_expiry_of_a_resting_entry() -> None:
    working = _working()
    _violates("OEV-13", lambda: _next(working, E.RECONCILED, S.WORKING, valid_until_ns=None))
    market = _next(
        _submit(order_type=OrderType.MARKET, limit_price=None, valid_until_ns=T0 + 30 * NS_PER_SECOND),
        E.ACCEPTED,
        S.WORKING,
    )
    _refused(market, _next(market, E.RECONCILED, S.WORKING, valid_until_ns=None))


@pytest.mark.requirement("G04-11")
def test_a_reconciliation_cannot_widen_a_protective_stop() -> None:
    sell_stop = samples.protective_stop_lifecycle()[1]
    validate_order_transition(
        sell_stop, _next(sell_stop, E.RECONCILED, S.WORKING, trigger_price=D("2648.00"))
    )
    _refused(sell_stop, _next(sell_stop, E.RECONCILED, S.WORKING, trigger_price=D("2647.79")))
    _refused(sell_stop, _next(sell_stop, E.RECONCILED, S.WORKING, volume_lots=D("0.60")))
    buy_stop = _next(_buy_stop_submit(), E.ACCEPTED, S.WORKING)
    validate_order_transition(buy_stop, _next(buy_stop, E.RECONCILED, S.WORKING, trigger_price=D("2652.00")))
    _refused(buy_stop, _next(buy_stop, E.RECONCILED, S.WORKING, trigger_price=D("2652.11")))


def test_a_reconciliation_cannot_cut_an_order_below_its_fills() -> None:
    partially = _reach(S.PARTIALLY_FILLED)
    _violates("OEV-9", lambda: _next(partially, E.RECONCILED, S.CANCELED, volume_lots=D("0.10")))


# --------------------------------------------------------------------------- OEV-1 .. OEV-4
def test_oev1_the_event_id_is_derived_from_order_and_sequence() -> None:
    submit = _submit()
    assert submit.order_event_id == identity.order_event_id(submit.client_order_id, 0)
    _violates("OEV-1", lambda: _submit(order_event_id=identity.order_event_id(submit.client_order_id, 1)))
    other = identity.order_event_id(_stop_submit().client_order_id, 0)
    _violates("OEV-1", lambda: _submit(order_event_id=other))


def test_oev2_sequence_zero_is_the_submit_request_and_nothing_else() -> None:
    submit = _submit()
    assert submit.order_seq == 0
    _violates("OEV-2", lambda: _submit(order_seq=1))
    _violates("OEV-2", lambda: _next(submit, E.ACCEPTED, S.WORKING, order_seq=0))
    _violates("OEV-2", lambda: _next(submit, E.REJECTED, S.REJECTED, order_seq=0))


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        (E.SUBMIT_REQUESTED, S.WORKING),
        (E.ACCEPTED, S.PENDING_SUBMIT),
        (E.ACCEPTED, S.REJECTED),
        (E.REJECTED, S.CANCELED),
        (E.CANCEL_REQUESTED, S.CANCELED),
        (E.CANCELED, S.PENDING_CANCEL),
        (E.CANCEL_REJECTED, S.PENDING_CANCEL),
        (E.REPLACED, S.PENDING_REPLACE),
        (E.REPLACE_REJECTED, S.CANCELED),
        (E.EXPIRED, S.CANCELED),
        (E.RECONCILED, S.PENDING_SUBMIT),
        (E.RECONCILED, S.PENDING_CANCEL),
    ],
)
def test_oev3_an_event_type_cannot_lead_to_a_foreign_state(
    event_type: OrderEventType, state: OrderState
) -> None:
    submit = _submit()
    extra = {"requested_volume_lots": D("0.40")} if state is S.PENDING_REPLACE else {}
    if event_type is E.SUBMIT_REQUESTED:
        _violates("OEV-3", lambda: _submit(order_state=state))
    else:
        _violates("OEV-3", lambda: _next(submit, event_type, state, **extra))


def test_oev3_accepts_every_state_the_table_names_for_an_event_type() -> None:
    assert _next(_submit(), E.ACCEPTED, S.WORKING).order_state is S.WORKING
    assert _next(_reach(S.PENDING_CANCEL), E.CANCEL_REJECTED, S.WORKING).order_state is S.WORKING
    _violates("OEV-3", lambda: _fill(_working(), "0.50", S.PARTIALLY_FILLED))
    _violates("OEV-3", lambda: _fill(_working(), "0.10", S.WORKING))


@pytest.mark.parametrize(
    "prices",
    [
        {"order_type": OrderType.LIMIT, "limit_price": None, "trigger_price": None},
        {"order_type": OrderType.LIMIT, "limit_price": D("2649.80"), "trigger_price": D("2649.80")},
        {"order_type": OrderType.LIMIT, "limit_price": None, "trigger_price": D("2649.80")},
        {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": None},
        {"order_type": OrderType.STOP, "limit_price": D("2649.80"), "trigger_price": D("2649.80")},
        {"order_type": OrderType.STOP, "limit_price": D("2649.80"), "trigger_price": None},
        {"order_type": OrderType.MARKET, "limit_price": D("2649.80"), "trigger_price": None},
        {"order_type": OrderType.MARKET, "limit_price": None, "trigger_price": D("2649.80")},
    ],
)
def test_oev4_the_price_fields_follow_the_order_type(prices: dict[str, Any]) -> None:
    _violates("OEV-4", lambda: _submit(**prices))


def test_oev4_accepts_the_three_order_types() -> None:
    assert _submit().limit_price == D("2649.80")
    stop = _submit(order_type=OrderType.STOP, limit_price=None, trigger_price=D("2651.00"))
    assert (stop.limit_price, stop.trigger_price) == (None, D("2651.00"))
    market = _submit(order_type=OrderType.MARKET, limit_price=None, valid_until_ns=None)
    assert (market.limit_price, market.trigger_price) == (None, None)


# --------------------------------------------------------------------------- OEV-5 .. OEV-8
@pytest.mark.requirement("G04-12", "S08-ACC-06")
@pytest.mark.parametrize(
    "missing",
    ["plan_id", "risk_decision_id", "alternative_group_id", "reference_price", "attached_stop_price"],
)
def test_oev5_an_entry_must_carry_plan_approval_group_reference_and_stop(missing: str) -> None:
    good = _submit()
    assert getattr(good, missing) is not None
    _violates("OEV-5", lambda: _submit(client_order_id=good.client_order_id, **{missing: None}))


def test_oev5_an_entry_names_no_position() -> None:
    position_id = samples.position().position_id
    _violates("OEV-5", lambda: _submit(position_id=position_id))


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize(
    "intent_and_type",
    [
        (OrderIntent.PROTECTIVE_STOP, OrderType.STOP),
        (OrderIntent.TAKE_PROFIT, OrderType.LIMIT),
        (OrderIntent.EXIT, OrderType.MARKET),
    ],
)
def test_oev6_orders_other_than_entries_belong_to_a_position_only(
    intent_and_type: tuple[OrderIntent, OrderType],
) -> None:
    intent, order_type = intent_and_type
    good = _child_submit(intent, order_type)
    assert good.position_id == samples.position().position_id
    assert (good.plan_id, good.risk_decision_id, good.alternative_group_id) == (None, None, None)
    entry = _submit()
    carried = [
        {"position_id": None},
        {"plan_id": entry.plan_id},
        {"risk_decision_id": entry.risk_decision_id},
        {"alternative_group_id": entry.alternative_group_id},
        {"attached_stop_price": D("2660.00")},
        {"attached_take_profit_price": D("2640.00")},
    ]
    for extra in carried:
        _violates(
            "OEV-6",
            lambda extra=extra: _child_submit(
                intent, order_type, client_order_id=good.client_order_id, **extra
            ),
        )


@pytest.mark.requirement("S08-ACC-06", "G08-07")
def test_oev7_the_entry_order_id_is_derived_from_its_risk_decision() -> None:
    entry = _submit()
    assert entry.risk_decision_id is not None
    assert entry.client_order_id == identity.client_order_id(entry.run_id, entry.risk_decision_id, "ENTRY", 0)
    # The same approval always yields the same order id: a re-sent request is the same order.
    assert _submit().client_order_id == entry.client_order_id
    other = samples.risk_decision(attempt=1, seq=1)
    assert _submit(risk_decision_id=other.decision_id).client_order_id != entry.client_order_id


@pytest.mark.requirement("S08-ACC-06", "G08-07")
def test_oev7_an_entry_cannot_take_an_order_id_from_anything_else() -> None:
    entry = _submit()
    assert entry.risk_decision_id is not None
    position_id = samples.position().position_id
    other = samples.risk_decision(attempt=1, seq=1)
    for foreign in (
        identity.client_order_id(entry.run_id, position_id, "ENTRY", 0),
        identity.client_order_id(entry.run_id, other.decision_id, "ENTRY", 0),
        identity.client_order_id(entry.run_id, entry.risk_decision_id, "EXIT", 0),
        identity.client_order_id(
            identity.run_id("REPLAY", "1" * 64, "other-fixture"), entry.risk_decision_id, "ENTRY", 0
        ),
    ):
        _violates("OEV-7", lambda foreign=foreign: _submit(client_order_id=foreign))


@pytest.mark.requirement("S08-ACC-06", "G08-07")
@pytest.mark.parametrize("attempt", [1, 2, 65535])
def test_oev7_one_approval_yields_one_entry_order_so_attempt_must_be_zero(attempt: int) -> None:
    entry = _submit()
    assert entry.risk_decision_id is not None
    _violates("OEV-7", lambda: _submit(attempt=attempt))
    explicit = identity.client_order_id(entry.run_id, entry.risk_decision_id, "ENTRY", attempt)
    _violates("OEV-7", lambda: _submit(attempt=attempt, client_order_id=explicit))


def test_oev7_other_orders_derive_their_id_from_position_intent_and_attempt() -> None:
    position_id = samples.position().position_id
    stop = _stop_submit()
    assert stop.client_order_id == identity.client_order_id(stop.run_id, position_id, "PROTECTIVE_STOP", 0)
    third = _stop_submit(attempt=3)
    assert third.client_order_id == identity.client_order_id(stop.run_id, position_id, "PROTECTIVE_STOP", 3)
    for foreign in (
        identity.client_order_id(stop.run_id, position_id, "PROTECTIVE_STOP", 1),
        identity.client_order_id(stop.run_id, position_id, "EXIT", 0),
        _submit().client_order_id,
    ):
        _violates("OEV-7", lambda foreign=foreign: _stop_submit(client_order_id=foreign))


@pytest.mark.requirement("G04-12")
def test_oev8_a_protective_stop_is_a_stop_order_and_a_take_profit_a_limit_order() -> None:
    assert _stop_submit().order_type is OrderType.STOP
    assert _child_submit(OrderIntent.TAKE_PROFIT, OrderType.LIMIT).order_type is OrderType.LIMIT
    for order_type in (OrderType.LIMIT, OrderType.MARKET):
        _violates(
            "OEV-8",
            lambda order_type=order_type: _child_submit(OrderIntent.PROTECTIVE_STOP, order_type),
        )
    for order_type in (OrderType.STOP, OrderType.MARKET):
        _violates(
            "OEV-8",
            lambda order_type=order_type: _child_submit(OrderIntent.TAKE_PROFIT, order_type),
        )
    for order_type in OrderType:
        assert _child_submit(OrderIntent.EXIT, order_type).order_type is order_type


# --------------------------------------------------------------------------- OEV-9 .. OEV-10
def test_oev9_fill_fields_appear_on_fill_events_only() -> None:
    working = _working()
    partial = _fill(working, "0.20")
    assert (partial.last_fill_lots, partial.last_fill_price) == (D("0.20"), D("2649.80"))
    _violates("OEV-9", lambda: _fill(working, "0.20", last_fill_lots=None))
    _violates("OEV-9", lambda: _fill(working, "0.20", last_fill_price=None))
    _violates("OEV-9", lambda: _fill(working, "0.20", last_fill_lots=None, last_fill_price=None))
    for stray in (
        {"last_fill_lots": D("0.10")},
        {"last_fill_price": D("2649.80")},
        {"last_fill_cost_amount": D("0.40")},
        {"last_fill_lots": D("0.10"), "last_fill_price": D("2649.80")},
    ):
        _violates("OEV-9", lambda stray=stray: _next(_submit(), E.ACCEPTED, S.WORKING, **stray))


def test_oev9_the_average_fill_price_exists_iff_something_was_filled() -> None:
    working = _working()
    assert working.average_fill_price is None
    _violates("OEV-9", lambda: _fill(working, "0.20", average_fill_price=None))
    _violates("OEV-9", lambda: _next(_submit(), E.ACCEPTED, S.WORKING, average_fill_price=D("2649.80")))


def test_oev9_states_and_filled_quantity_agree() -> None:
    working = _working()
    partially = _fill(working, "0.20")
    # WORKING and PENDING_SUBMIT have no fills.
    _violates(
        "OEV-9",
        lambda: _next(_reach(S.PENDING_CANCEL, filled=True), E.CANCEL_REJECTED, S.WORKING),
    )
    # PARTIALLY_FILLED has some.
    _violates("OEV-9", lambda: _next(_reach(S.PENDING_CANCEL), E.CANCEL_REJECTED, S.PARTIALLY_FILLED))
    # FILLED iff cumulative == volume.
    _violates(
        "OEV-9",
        lambda: _next(
            working,
            E.FILLED,
            S.FILLED,
            cumulative_filled_lots=D("0.20"),
            last_fill_lots=D("0.20"),
            last_fill_price=D("2649.80"),
            average_fill_price=D("2649.80"),
        ),
    )
    _violates(
        "OEV-9",
        lambda: _next(partially, E.CANCELED, S.CANCELED, cumulative_filled_lots=D("0.50")),
    )
    # A single fill cannot be larger than everything filled so far.
    _violates("OEV-9", lambda: _fill(working, "0.30", cumulative_filled_lots=D("0.20")))
    # A PARTIALLY_FILLED event leaves something open.
    _violates(
        "OEV-9",
        lambda: _next(
            _reach(S.PENDING_CANCEL),
            E.PARTIALLY_FILLED,
            S.PENDING_CANCEL,
            cumulative_filled_lots=D("0.50"),
            last_fill_lots=D("0.50"),
            last_fill_price=D("2649.80"),
            average_fill_price=D("2649.80"),
        ),
    )


@pytest.mark.parametrize(
    ("before", "event_type", "state"),
    [
        (S.PENDING_SUBMIT, E.REJECTED, S.REJECTED),
        (S.PENDING_CANCEL, E.CANCEL_REJECTED, S.WORKING),
        (S.PENDING_REPLACE, E.REPLACE_REJECTED, S.WORKING),
        (S.WORKING, E.RECONCILED, S.WORKING),
    ],
)
def test_oev10_rejections_and_reconciliations_state_their_reason(
    before: OrderState, event_type: OrderEventType, state: OrderState
) -> None:
    previous = _reach(before)
    assert _next(previous, event_type, state).reason_code == REASON
    _violates("OEV-10", lambda: _next(previous, event_type, state, reason_code=None))


# --------------------------------------------------------------------------- OEV-11
@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    ("order", "stop", "accepted"),
    [
        ({}, "2649.799999999999", True),
        ({}, "2649.80", False),
        ({}, "2649.81", False),
        ({}, "2650.00", False),
        ({"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2651.00")}, "2650.99", True),
        (
            {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2651.00")},
            "2651.00",
            False,
        ),
        (
            {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2651.00")},
            "2651.50",
            False,
        ),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.34", True),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.35", False),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.36", False),
    ],
    ids=[
        "limit_just_below",
        "limit_equal",
        "limit_above",
        "limit_far_above",
        "trigger_just_below",
        "trigger_equal",
        "trigger_above",
        "market_just_below_reference",
        "market_equal_reference",
        "market_above_reference",
    ],
)
def test_oev11_the_stop_of_a_buy_entry_lies_below_its_order_price(
    order: dict[str, Any], stop: str, accepted: bool
) -> None:
    if accepted:
        assert _submit(attached_stop_price=D(stop), **order).attached_stop_price == D(stop)
    else:
        _violates("OEV-11", lambda: _submit(attached_stop_price=D(stop), **order))


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    ("order", "stop", "accepted"),
    [
        ({"limit_price": D("2650.50")}, "2650.500000000001", True),
        ({"limit_price": D("2650.50")}, "2650.50", False),
        ({"limit_price": D("2650.50")}, "2650.00", False),
        ({"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2649.00")}, "2649.01", True),
        (
            {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2649.00")},
            "2649.00",
            False,
        ),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.11", True),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.10", False),
        ({"order_type": OrderType.MARKET, "limit_price": None}, "2650.00", False),
    ],
)
def test_oev11_the_stop_of_a_sell_entry_lies_above_its_order_price(
    order: dict[str, Any], stop: str, accepted: bool
) -> None:
    fields = {"side": Side.SELL, "reference_price": D("2650.10"), "attached_stop_price": D(stop), **order}
    if accepted:
        assert _submit(**fields).attached_stop_price == D(stop)
    else:
        _violates("OEV-11", lambda: _submit(**fields))


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    ("side", "stop", "fill_price", "accepted"),
    [
        (Side.BUY, "2647.80", "2647.81", True),
        (Side.BUY, "2647.80", "2647.80", False),
        (Side.BUY, "2647.80", "2647.00", False),
        (Side.SELL, "2651.80", "2651.79", True),
        (Side.SELL, "2651.80", "2651.80", False),
        (Side.SELL, "2651.80", "2652.50", False),
    ],
)
def test_oev11_the_stop_lies_on_the_losing_side_of_the_average_fill_price(
    side: Side, stop: str, fill_price: str, accepted: bool
) -> None:
    working = _working(side=side, attached_stop_price=D(stop))
    fills = {"average_fill_price": D(fill_price), "last_fill_price": D(fill_price)}
    if accepted:
        assert _fill(working, "0.50", **fills).average_fill_price == D(fill_price)
    else:
        _violates("OEV-11", lambda: _fill(working, "0.50", **fills))
        _violates("OEV-11", lambda: _fill(working, "0.20", **fills))


@pytest.mark.parametrize(
    ("side", "stop", "take_profit", "accepted"),
    [
        (Side.BUY, "2647.80", "2649.800000000001", True),
        (Side.BUY, "2647.80", "2655.00", True),
        (Side.BUY, "2647.80", "2649.80", False),
        (Side.BUY, "2647.80", "2649.00", False),
        (Side.SELL, "2651.80", "2649.799999999999", True),
        (Side.SELL, "2651.80", "2649.80", False),
        (Side.SELL, "2651.80", "2650.50", False),
    ],
)
def test_oev11_an_attached_take_profit_lies_on_the_winning_side(
    side: Side, stop: str, take_profit: str, accepted: bool
) -> None:
    fields = {"side": side, "attached_stop_price": D(stop), "attached_take_profit_price": D(take_profit)}
    if accepted:
        assert _submit(**fields).attached_take_profit_price == D(take_profit)
    else:
        _violates("OEV-11", lambda: _submit(**fields))


# --------------------------------------------------------------------------- OEV-12 .. OEV-14
def test_oev12_an_event_is_not_observed_before_it_happened() -> None:
    assert _submit(event_time_ns=T0, receive_time_ns=T0).receive_time_ns == T0
    assert _submit(event_time_ns=T0, receive_time_ns=T0 + 5 * NS_PER_MS).event_time_ns == T0
    _violates("OEV-12", lambda: _submit(event_time_ns=T0 + 1, receive_time_ns=T0))


@pytest.mark.requirement("G04-12")
@pytest.mark.parametrize(
    "resting",
    [
        {},
        {"order_type": OrderType.STOP, "limit_price": None, "trigger_price": D("2651.00")},
    ],
    ids=["limit", "stop"],
)
def test_oev13_a_resting_entry_must_expire(resting: dict[str, Any]) -> None:
    assert _submit(**resting).valid_until_ns is not None
    _violates("OEV-13", lambda: _submit(valid_until_ns=None, **resting))
    accepted = _working(**resting)
    _violates("OEV-13", lambda: _next(accepted, E.CANCEL_REQUESTED, S.PENDING_CANCEL, valid_until_ns=None))


def test_oev13_a_market_entry_and_other_orders_need_no_expiry() -> None:
    market = _submit(order_type=OrderType.MARKET, limit_price=None, valid_until_ns=None)
    assert market.valid_until_ns is None
    assert _child_submit(OrderIntent.TAKE_PROFIT, OrderType.LIMIT).valid_until_ns is None
    assert _stop_submit().valid_until_ns is None


@pytest.mark.requirement("G04-12")
def test_oev13_a_submit_request_that_has_already_expired_is_refused() -> None:
    moment = T0 + NS_PER_MS
    assert _submit(event_time_ns=moment, receive_time_ns=moment, valid_until_ns=moment + 1).order_seq == 0
    for expiry in (moment, moment - 1, T0):
        _violates(
            "OEV-13",
            lambda expiry=expiry: _submit(
                event_time_ns=moment, receive_time_ns=moment, valid_until_ns=expiry
            ),
        )
    _violates(
        "OEV-13",
        lambda: _child_submit(
            OrderIntent.EXIT,
            OrderType.LIMIT,
            event_time_ns=moment,
            receive_time_ns=moment,
            valid_until_ns=moment,
        ),
    )


def test_oev13_later_events_may_be_reported_after_the_expiry_instant() -> None:
    working = _working()
    assert working.valid_until_ns is not None
    after_expiry = working.valid_until_ns + NS_PER_MS
    expired = _next(working, E.EXPIRED, S.EXPIRED, event_time_ns=after_expiry, receive_time_ns=after_expiry)
    validate_order_transition(working, expired)


def test_oev14_requested_values_exist_exactly_while_a_replacement_is_pending() -> None:
    working = _working()
    pending = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.40"))
    assert pending.requested_volume_lots == D("0.40")
    _violates("OEV-14", lambda: _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE))
    for name, value in (
        ("requested_volume_lots", D("0.40")),
        ("requested_limit_price", D("2649.80")),
        ("requested_trigger_price", D("2649.80")),
        ("requested_valid_until_ns", T0 + 10 * NS_PER_SECOND),
    ):
        _violates("OEV-14", lambda name=name, value=value: _submit(**{name: value}))
        _violates(
            "OEV-14",
            lambda name=name, value=value: _next(pending, E.REPLACED, S.WORKING, **{name: value}),
        )
    _violates("OEV-14", lambda: _fill(pending, "0.10", **dict.fromkeys(_REQUESTED)))


# --------------------------------------------------------------------------- routing lock vocabulary
_LOCK = pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-19", "OWN-14")


@_LOCK
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("venue", ["LIVE", "REAL", "MT5", "DEMO", "live", "simulated", "", None, 1])
def test_an_order_event_for_a_real_venue_cannot_be_decoded(venue: object) -> None:
    wire = to_wire(_submit())
    assert wire["venue"] == "SIMULATED"
    assert from_wire(dict(wire), OrderEvent) == _submit()
    wire["venue"] = venue
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire, OrderEvent)
    assert caught.value.details.get("path") == "venue"


@_LOCK
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("mode", ["LIVE", "REAL", "PRODUCTION", "DEMO", "live", "paper", "", None, True])
def test_an_order_event_of_a_live_run_mode_cannot_be_decoded(mode: object) -> None:
    wire = to_wire(_submit())
    assert wire["mode"] == "REPLAY"
    wire["mode"] = mode
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire, OrderEvent)
    assert caught.value.details.get("path") == "mode"


@_LOCK
@pytest.mark.acceptance("S00-ACC-04")
def test_an_order_event_for_a_real_venue_or_live_mode_cannot_be_constructed() -> None:
    assert [venue.value for venue in ExecutionVenue] == ["SIMULATED"]
    assert [mode.value for mode in RunMode] == ["REPLAY", "PAPER"]
    for venue in ("LIVE", "SIMULATED", None):
        with pytest.raises(ContractValidationError) as caught:
            _submit(venue=venue)
        assert caught.value.details.get("path") == "venue"
    for mode in ("LIVE", "PAPER", None):
        with pytest.raises(ContractValidationError) as caught:
            _submit(mode=mode)
        assert caught.value.details.get("path") == "mode"
    assert _submit(mode=RunMode.PAPER).venue is ExecutionVenue.SIMULATED


@pytest.mark.parametrize("symbol", ["XAGUSD", "EURUSD", "xauusd", "XAUUSD.m", "GOLD"])
def test_only_gold_can_be_ordered(symbol: str) -> None:
    with pytest.raises(ContractValidationError) as caught:
        _submit(symbol=symbol)
    assert caught.value.details.get("path") == "symbol"
    wire = to_wire(_submit())
    wire["symbol"] = symbol
    with pytest.raises(ContractValidationError):
        from_wire(wire, OrderEvent)


def test_invariants_are_enforced_when_an_event_is_decoded() -> None:
    wire = to_wire(_submit())
    wire["attached_stop_price"] = "2650.00"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire, OrderEvent)
    assert caught.value.details.get("invariant") == "OEV-11"
    wire = to_wire(_submit())
    wire["attached_stop_price"] = None
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire, OrderEvent)
    assert caught.value.details.get("invariant") == "OEV-5"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("volume_lots", D("0")),
        ("volume_lots", D("-0.10")),
        ("volume_lots", 0.5),
        ("limit_price", D("0")),
        ("limit_price", 2649.8),
        ("attached_stop_price", D("-1")),
        ("cumulative_filled_lots", D("-0.01")),
        ("event_type", "SUBMIT_REQUESTED"),
        ("order_state", "PENDING_SUBMIT"),
        ("side", "BUY"),
        ("order_type", "LIMIT"),
        ("reason_code", "lower case"),
        ("valid_until_ns", -1),
    ],
)
def test_invalid_field_values_are_refused_and_never_coerced(path: str, value: object) -> None:
    with pytest.raises(ContractValidationError) as caught:
        _submit(**{path: value})
    assert caught.value.details.get("path") == path


def test_the_helpers_of_this_module_build_what_they_claim() -> None:
    assert (NS_PER_SECOND, NS_PER_MS) == (1_000_000_000, 1_000_000)
    for state in S:
        assert _reach(state).order_state is state
    for state in (S.PARTIALLY_FILLED, S.PENDING_CANCEL, S.PENDING_REPLACE, S.CANCELED, S.EXPIRED):
        event = _reach(state, filled=True)
        assert event.order_state is state
        assert event.cumulative_filled_lots == D("0.20")
