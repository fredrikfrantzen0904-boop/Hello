"""Independent tests for ``gie.contracts.ledger.LifecycleLedger`` - sequence rules at one place.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (section 4 "at most one
plan of a group is ever executed (enforced by the lifecycle ledger on entry orders)", section 9.2
"Sequence rules - enforced when a record is published") and the class docstring - not from the
implementation:

* ``check`` validates a record against its chain without recording it, ``admit`` validates and
  records it;
* a refused record never changes the ledger;
* admitting the newest record of a chain again is a no-op, reported by ``False``;
* a second ENTRY of an alternative group is refused while the first is working or was executed,
  and allowed once the first ended REJECTED / CANCELED / EXPIRED without any fill.

Rework 2 (change notice section 4, module docstring of ``gie.contracts.ledger``):
``LifecycleLedger(referential=True)`` - the journal's ledger - also enforces the reference rules
(candidate -> evidence, plan -> candidate and a target naming a scenario -> that destination of
the candidate with the same price, risk decision -> plan, entry order -> plan and approval,
reduce-only order -> a position whose entry has filled, position -> its entry order and its
protective order) and the aggregates (one executed alternative per group, one working protective
stop per position, a position never exceeds the fill of its entry, the daily risk state of the run
across all risk producers, the registry rules), and offers the lookups.  ``referential=False`` is
the documented fragment mode: the chain-mechanics tests use it so that each examines one chain.

SYNTHETIC FIXTURE: every record in this module is built with the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``).  Orders, fills, positions, plans,
approvals and model versions are invented.  They are not market data, not owner decisions and not
evidence of any trading performance.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    Candidate,
    DailyState,
    Evidence,
    LifecycleLedger,
    ModelStatus,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderOriginator,
    OrderState,
    OrderType,
    PlanStatus,
    Position,
    Record,
    RiskDecision,
    Side,
    Target,
    TradePlan,
    identity,
)
from gie.contracts.risk_decision import advance_daily_risk
from gie.core.errors import IllegalTransitionError
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

pytestmark = pytest.mark.requirement("S00-ASG-15", "S00-ASG-16", "S00-ASG-17")

E = OrderEventType
S = OrderState
T0 = samples.T0
_NEEDS_REASON = (E.REJECTED, E.CANCEL_REJECTED, E.REPLACE_REJECTED, E.RECONCILED)


# --------------------------------------------------------------------------- builders
def _next(
    previous: OrderEvent, event_type: OrderEventType, order_state: OrderState, **overrides: Any
) -> OrderEvent:
    """The following event of the same order: parameters and fills carried over, counters advanced."""
    fields = {spec.name: getattr(previous, spec.name) for spec in OrderEvent.FIELD_SPECS}
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
        fill_tick_source=None,
        fill_tick_seq=None,
        # OEV-15: an order created by the simulated broker keeps stating why it exists.
        reason_code="FIXTURE_REASON"
        if event_type in _NEEDS_REASON
        else (previous.reason_code if previous.originator is OrderOriginator.BROKER else None),
    )
    fields.update(overrides)
    fields["order_event_id"] = identity.order_event_id(fields["client_order_id"], fields["order_seq"])
    return OrderEvent(**fields)


def _fill(previous: OrderEvent, lots: str) -> OrderEvent:
    quantity = D(lots)
    cumulative = previous.cumulative_filled_lots + quantity
    complete = cumulative == previous.volume_lots
    state = S.FILLED if complete else S.PARTIALLY_FILLED
    return _next(
        previous,
        E.FILLED if complete else E.PARTIALLY_FILLED,
        state,
        cumulative_filled_lots=cumulative,
        last_fill_lots=quantity,
        last_fill_price=D("2649.80"),
        average_fill_price=D("2649.80"),
        fill_tick_source="fixture",  # OEV-9: the quote a fill was matched against
        fill_tick_seq=previous.order_seq,
    )


def _entry(variant: int = 0, *, attempt: int = 0, **overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of an entry for plan *variant* of the sample opportunity (one group)."""
    plan = samples.trade_plan(variant=variant)
    decision = samples.risk_decision(plan, attempt=attempt)
    fields: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "risk_decision_id": decision.decision_id,
        "alternative_group_id": plan.alternative_group_id,
    }
    fields.update(overrides)
    return samples.order_event(**fields)


def _chain_to(outcome: str, submit: OrderEvent | None = None) -> list[OrderEvent]:
    """Events of an entry order (default: the first plan) up to the named situation."""
    submit = _entry() if submit is None else submit
    working = _next(submit, E.ACCEPTED, S.WORKING)
    partial = _fill(working, "0.20")
    chains: dict[str, list[OrderEvent]] = {
        "pending_submit": [submit],
        "working": [submit, working],
        "partially_filled": [submit, working, partial],
        "filled": [submit, working, _fill(working, "0.50")],
        "pending_cancel": [submit, working, _next(working, E.CANCEL_REQUESTED, S.PENDING_CANCEL)],
        "pending_replace": [
            submit,
            working,
            _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.40")),
        ],
        "canceled_after_partial_fill": [submit, working, partial, _next(partial, E.CANCELED, S.CANCELED)],
        "expired_after_partial_fill": [submit, working, partial, _next(partial, E.EXPIRED, S.EXPIRED)],
        "rejected": [submit, _next(submit, E.REJECTED, S.REJECTED)],
        "canceled": [submit, working, _next(working, E.CANCELED, S.CANCELED)],
        "expired": [submit, working, _next(working, E.EXPIRED, S.EXPIRED)],
        "expired_unacknowledged": [submit, _next(submit, E.EXPIRED, S.EXPIRED)],
        "reconciled_canceled": [submit, working, _next(working, E.RECONCILED, S.CANCELED)],
    }
    return chains[outcome]


def _fragment() -> LifecycleLedger:
    """A ledger without the reference rules (``referential=False``): for a fragment of a history.

    The chain and aggregate tests below examine one order or one position chain at a time, which is
    exactly what the documented fragment mode exists for; the reference rules have their own tests.
    """
    return LifecycleLedger(referential=False)


def _admit_all(ledger: LifecycleLedger, records: list[Any]) -> None:
    for record in records:
        assert ledger.admit(record) is True


def _refused(ledger: LifecycleLedger, record: Record) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        ledger.check(record)
    with pytest.raises(IllegalTransitionError) as caught:
        ledger.admit(record)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    return caught.value


# --------------------------------------------------------------------------- admit / check
def test_a_new_ledger_knows_nothing() -> None:
    ledger = _fragment()
    submit = samples.order_event()
    assert ledger.order(submit.client_order_id) is None
    assert ledger.position(samples.position().position_id) is None
    assert ledger.model(samples.MODEL_ID) is None
    assert submit.alternative_group_id is not None
    assert ledger.group_entry_order(submit.alternative_group_id) is None
    assert ledger.open_orders() == ()


def test_admit_records_the_newest_event_of_an_order() -> None:
    ledger = _fragment()
    chain = samples.order_lifecycle()
    for event in chain:
        assert ledger.admit(event) is True
        assert ledger.order(event.client_order_id) is event
    assert ledger.order(chain[0].client_order_id) is chain[-1]


def test_check_validates_without_recording() -> None:
    ledger = _fragment()
    submit, accepted, *_ = samples.order_lifecycle()
    assert ledger.check(submit) is True
    assert ledger.check(submit) is True
    assert ledger.order(submit.client_order_id) is None
    assert ledger.open_orders() == ()
    # Nothing was recorded, so the second event still has no predecessor.
    with pytest.raises(IllegalTransitionError):
        ledger.check(accepted)
    assert ledger.admit(submit) is True
    assert ledger.check(accepted) is True
    assert ledger.order(submit.client_order_id) is submit


@pytest.mark.requirement("G08-07")
def test_admitting_the_newest_record_again_is_a_reported_no_op() -> None:
    ledger = _fragment()
    submit, accepted, partial, filled = samples.order_lifecycle()
    assert ledger.admit(submit) is True
    assert ledger.admit(submit) is False
    assert ledger.check(submit) is False
    assert ledger.order(submit.client_order_id) is submit
    # An equal record built independently (a re-sent request) is the same record.
    assert ledger.admit(samples.order_event()) is False
    assert ledger.order(submit.client_order_id) is submit
    _admit_all(ledger, [accepted, partial, filled])
    assert ledger.admit(filled) is False
    assert ledger.order(submit.client_order_id) is filled


@pytest.mark.requirement("G08-07")
def test_an_older_event_of_a_chain_is_not_mistaken_for_a_repeat() -> None:
    ledger = _fragment()
    submit, accepted, partial, _filled = samples.order_lifecycle()
    _admit_all(ledger, [submit, accepted, partial])
    for older in (submit, accepted):
        _refused(ledger, older)
    assert ledger.order(submit.client_order_id) is partial


def test_other_content_at_the_position_of_the_newest_event_is_refused() -> None:
    ledger = _fragment()
    submit, accepted, *_ = samples.order_lifecycle()
    _admit_all(ledger, [submit, accepted])
    conflicting = _next(submit, E.REJECTED, S.REJECTED)
    assert conflicting.order_seq == accepted.order_seq
    _refused(ledger, conflicting)
    assert ledger.order(submit.client_order_id) is accepted


def test_open_orders_lists_exactly_the_orders_that_are_not_terminal() -> None:
    ledger = _fragment()
    entry = samples.order_lifecycle()
    stop = samples.protective_stop_lifecycle()
    # The sample stop protects the full 0.50 lots, so it can only follow the complete fill.
    _admit_all(ledger, [entry[0], entry[1]])
    assert ledger.open_orders() == (entry[1],)
    _admit_all(ledger, [entry[2], entry[3], stop[0], stop[1]])
    assert ledger.open_orders() == (stop[1],)
    _admit_all(ledger, [stop[2], stop[3]])
    assert ledger.open_orders() == (stop[3],)
    assert ledger.admit(_next(stop[3], E.CANCELED, S.CANCELED)) is True
    assert ledger.open_orders() == ()


# --------------------------------------------------------------------------- refusals change nothing
@pytest.mark.requirement("G04-09", "G04-11")
def test_a_refused_order_event_never_changes_the_ledger() -> None:
    ledger = _fragment()
    submit, accepted, partial, filled = samples.order_lifecycle()
    _admit_all(ledger, [submit, accepted])
    faulty = [
        filled,  # order_seq 3 directly after order_seq 1
        _next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.60")),
        _next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_limit_price=D("2649.90")),
        _next(accepted, E.RECONCILED, S.WORKING, volume_lots=D("5")),
        _next(accepted, E.ACCEPTED, S.WORKING),
    ]
    for record in faulty:
        _refused(ledger, record)
        assert ledger.order(submit.client_order_id) is accepted
        assert ledger.open_orders() == (accepted,)
    # The chain continues exactly where it was.
    _admit_all(ledger, [partial, filled])
    assert ledger.order(submit.client_order_id) is filled


def test_an_order_that_does_not_begin_with_its_submit_request_is_not_recorded() -> None:
    ledger = _fragment()
    submit, accepted, *_ = samples.order_lifecycle()
    _refused(ledger, accepted)
    assert ledger.order(accepted.client_order_id) is None
    assert ledger.open_orders() == ()
    assert accepted.alternative_group_id is not None
    assert ledger.group_entry_order(accepted.alternative_group_id) is None
    _admit_all(ledger, [submit, accepted])


@pytest.mark.requirement("G04-11")
def test_a_widening_stop_replacement_is_refused_and_the_stop_stays() -> None:
    ledger = _fragment()
    submit, working, tighten, replaced = samples.protective_stop_lifecycle()
    _admit_all(ledger, [submit, working])
    widen = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2640.00"))
    _refused(ledger, widen)
    assert ledger.order(submit.client_order_id) is working
    _admit_all(ledger, [tighten, replaced])
    newest = ledger.order(submit.client_order_id)
    assert newest is replaced
    assert newest.trigger_price == D("2649.90")
    # The tightened stop cannot be loosened back to where it was.
    back = _next(replaced, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2647.80"))
    _refused(ledger, back)
    assert ledger.order(submit.client_order_id) is replaced


def test_nothing_is_admitted_after_a_terminal_state() -> None:
    ledger = _fragment()
    chain = _chain_to("rejected")
    _admit_all(ledger, chain)
    rejected = chain[-1]
    _refused(ledger, _next(rejected, E.ACCEPTED, S.WORKING))
    # Not even the very same submit request again: one approval, one order, once.
    _refused(ledger, chain[0])
    assert ledger.order(rejected.client_order_id) is rejected
    assert ledger.open_orders() == ()


# --------------------------------------------------------------------------- chains per contract
def test_orders_are_separate_chains_keyed_by_client_order_id() -> None:
    ledger = _fragment()
    entry = samples.order_lifecycle()
    stop = samples.protective_stop_lifecycle()
    assert entry[0].client_order_id != stop[0].client_order_id
    # Interleaved: the stop (0.50 lots) follows the complete fill, the entry chain continues around it.
    _admit_all(ledger, [entry[0], entry[1], entry[2]])
    _admit_all(ledger, [entry[3], stop[0], stop[1], stop[2], stop[3]])
    assert ledger.order(entry[0].client_order_id) is entry[3]
    assert ledger.order(stop[0].client_order_id) is stop[3]
    # A further event of the stop order extends the stop's chain only.
    assert ledger.admit(_next(stop[3], E.CANCELED, S.CANCELED)) is True
    assert ledger.order(entry[0].client_order_id) is entry[3]
    assert ledger.open_orders() == ()


@pytest.mark.requirement("G04-10", "G04-11", "G04-13")
def test_positions_are_chains_keyed_by_position_id() -> None:
    ledger = _fragment()
    opened, tightened, closed = samples.position_lifecycle()
    observed = samples.external_position()
    assert ledger.admit(opened) is True
    assert ledger.admit(opened) is False
    assert ledger.admit(observed) is True
    assert ledger.position(opened.position_id) is opened
    assert ledger.position(observed.position_id) is observed
    assert ledger.admit(tightened) is True
    assert ledger.position(opened.position_id) is tightened
    fields = {spec.name: getattr(tightened, spec.name) for spec in Position.FIELD_SPECS}
    moment = tightened.decision_time_ns + NS_PER_SECOND
    fields.update(seq=2, revision=2, decision_time_ns=moment, as_of_time_ns=moment)
    widened = Position(**{**fields, "stop_price": D("2648.00")})
    grown = Position(**{**fields, "volume_lots": D("0.50")})
    lower_peak = Position(
        **{**fields, "unrealized_net_pnl_amount": D("70.00"), "peak_net_pnl_amount": D("145.99")}
    )
    for faulty in (widened, grown, lower_peak, opened):
        _refused(ledger, faulty)
        assert ledger.position(opened.position_id) is tightened
    assert ledger.admit(closed) is True
    assert ledger.admit(closed) is False
    _refused(ledger, Position(**{**fields, "revision": 3, "seq": 3}))
    assert ledger.position(opened.position_id) is closed
    assert ledger.position(observed.position_id) is observed


def test_model_versions_are_chains_keyed_by_model_version_id() -> None:
    ledger = _fragment()
    research, shadow, active = samples.model_lifecycle()
    assert research.status is ModelStatus.RESEARCH
    assert ledger.admit(research) is True
    assert ledger.admit(research) is False
    # ACTIVE directly after RESEARCH skips shadow validation.
    _refused(ledger, active)
    assert ledger.model(research.model_version_id) is research
    assert ledger.admit(shadow) is True
    assert ledger.admit(active) is True
    assert ledger.model(research.model_version_id) is active
    assert ledger.admit(active) is False


def test_evidence_candidates_and_plans_are_admitted_through_their_chains() -> None:
    ledger = _fragment()
    variants = samples.variants()
    evidence = variants["Evidence"]
    candidates = variants["Candidate"]
    plan = variants["TradePlan"]["limit_buy_selected"]
    for first, revision in (
        (evidence["active_bullish"], evidence["invalidated_revision"]),
        (candidates["scored"], candidates["rejected_revision"]),
    ):
        assert ledger.admit(first) is True
        assert ledger.admit(first) is False
        assert ledger.admit(revision) is True
        assert ledger.admit(revision) is False
        _refused(ledger, first)
    assert ledger.admit(plan) is True
    assert ledger.admit(plan) is False
    assert plan.status is PlanStatus.SELECTED
    # A revision that is not the next one of its chain is refused.
    _refused(ledger, samples.trade_plan(revision=2, seq=1))


@pytest.mark.requirement("G05-04", "G05-06")
def test_risk_decisions_of_a_gate_are_checked_against_the_daily_state_rules() -> None:
    ledger = _fragment()
    halt = samples.rejected_risk_decision(
        daily_state=DailyState.EMERGENCY_HALT, daily_pnl_amount=D("-3000"), reason_codes=("EMERGENCY_HALT",)
    )
    later = T0 + NS_PER_SECOND
    approval_after_halt = samples.risk_decision(
        seq=1, attempt=1, decision_time_ns=later, as_of_time_ns=later, valid_until_ns=later + NS_PER_SECOND
    )
    assert ledger.admit(halt) is True
    assert ledger.admit(halt) is False
    _refused(ledger, approval_after_halt)
    # The refused approval did not replace the halt: the next one is refused as well.
    another = samples.risk_decision(
        seq=1,
        attempt=2,
        decision_time_ns=later + 1,
        as_of_time_ns=later + 1,
        valid_until_ns=later + NS_PER_SECOND,
    )
    _refused(ledger, another)
    next_day = T0 + 86_400 * NS_PER_SECOND
    tomorrow = samples.risk_decision(
        seq=1,
        attempt=3,
        decision_time_ns=next_day,
        as_of_time_ns=next_day,
        valid_until_ns=next_day + NS_PER_SECOND,
        trading_day="2026-11-27",
    )
    assert ledger.admit(tomorrow) is True


@pytest.mark.requirement("G05-06")
def test_a_lowered_profit_floor_is_refused_and_the_floor_stays() -> None:
    ledger = _fragment()
    protect = DailyState.PROFIT_PROTECTION

    def protected(seq: int, floor: str) -> Any:
        moment = T0 + seq * NS_PER_SECOND
        return samples.risk_decision(
            seq=seq,
            attempt=seq,
            decision_time_ns=moment,
            as_of_time_ns=moment,
            valid_until_ns=moment + NS_PER_SECOND,
            daily_state=protect,
            daily_pnl_amount=D("9000"),
            profit_floor_amount=D(floor),
        )

    assert ledger.admit(protected(0, "5000")) is True
    _refused(ledger, protected(1, "4000"))
    assert ledger.admit(protected(1, "6000")) is True
    _refused(ledger, protected(2, "5999.999999999999"))
    assert ledger.admit(protected(2, "6000")) is True


# --------------------------------------------------------------------------- alternative groups
_GROUP = pytest.mark.requirement("S08-ACC-06", "G04-09", "G04-10")


def test_the_two_sample_plans_belong_to_one_alternative_group() -> None:
    first, second = _entry(0), _entry(1)
    assert first.alternative_group_id == second.alternative_group_id
    assert first.plan_id != second.plan_id
    assert first.client_order_id != second.client_order_id
    assert first.alternative_group_id == identity.alternative_group_id(samples.trade_plan().opportunity_id)


@_GROUP
@pytest.mark.parametrize(
    "situation",
    [
        "pending_submit",
        "working",
        "partially_filled",
        "filled",
        "pending_cancel",
        "pending_replace",
        "canceled_after_partial_fill",
        "expired_after_partial_fill",
    ],
)
def test_a_second_entry_of_the_group_is_refused_while_the_first_is_working_or_was_executed(
    situation: str,
) -> None:
    ledger = _fragment()
    chain = _chain_to(situation)
    _admit_all(ledger, chain)
    first, second = chain[-1], _entry(1)
    assert first.alternative_group_id is not None
    error = _refused(ledger, second)
    assert error.details.get("alternative_group_id") == first.alternative_group_id
    assert error.details.get("holder") == first.client_order_id
    # The refusal changed nothing: the first order still holds the group, the second is unknown.
    assert ledger.group_entry_order(first.alternative_group_id) is first
    assert ledger.order(second.client_order_id) is None
    assert ledger.order(first.client_order_id) is first


@_GROUP
@pytest.mark.parametrize(
    "situation", ["rejected", "canceled", "expired", "expired_unacknowledged", "reconciled_canceled"]
)
def test_a_second_entry_of_the_group_is_allowed_after_the_first_ended_without_a_fill(situation: str) -> None:
    ledger = _fragment()
    chain = _chain_to(situation)
    _admit_all(ledger, chain)
    first, second = chain[-1], _entry(1)
    assert first.cumulative_filled_lots == 0
    assert first.is_terminal is True
    assert second.alternative_group_id is not None
    assert ledger.check(second) is True
    assert ledger.admit(second) is True
    assert ledger.group_entry_order(second.alternative_group_id) is second
    assert ledger.open_orders() == (second,)
    # Now the second plan holds the group: a third is refused, and so is a retry of the first plan.
    third = _entry(2)
    assert third.alternative_group_id == second.alternative_group_id
    assert _refused(ledger, third).details.get("holder") == second.client_order_id
    _refused(ledger, _entry(0, attempt=1))
    accepted = _next(second, E.ACCEPTED, S.WORKING)
    assert ledger.admit(accepted) is True
    assert ledger.group_entry_order(second.alternative_group_id) is accepted


@_GROUP
def test_a_second_approval_of_the_same_plan_cannot_open_a_second_entry_either() -> None:
    ledger = _fragment()
    _admit_all(ledger, _chain_to("working"))
    again = _entry(0, attempt=1)
    assert again.plan_id == _entry(0).plan_id
    assert again.client_order_id != _entry(0).client_order_id
    _refused(ledger, again)


@_GROUP
def test_the_group_stays_taken_through_the_whole_life_of_the_executed_entry() -> None:
    ledger = _fragment()
    second = _entry(1)
    for event in samples.order_lifecycle():
        assert ledger.admit(event) is True
        _refused(ledger, second)
    assert ledger.order(second.client_order_id) is None


@_GROUP
def test_repeating_the_entry_that_holds_the_group_is_an_idempotent_no_op() -> None:
    ledger = _fragment()
    first = _entry(0)
    assert ledger.admit(first) is True
    assert ledger.check(_entry(0)) is False
    assert ledger.admit(_entry(0)) is False
    assert first.alternative_group_id is not None
    assert ledger.group_entry_order(first.alternative_group_id) is first


@_GROUP
def test_entries_of_other_groups_and_orders_of_positions_are_independent_of_the_group_rule() -> None:
    ledger = _fragment()
    _admit_all(ledger, _chain_to("filled"))
    other_group = identity.alternative_group_id(samples.unscored_candidate().opportunity_id)
    elsewhere = _entry(2, alternative_group_id=other_group)
    assert ledger.admit(elsewhere) is True
    assert ledger.group_entry_order(other_group) is elsewhere
    for event in samples.protective_stop_lifecycle():
        assert ledger.admit(event) is True
    held = _entry(0)
    assert held.alternative_group_id is not None
    holder = ledger.group_entry_order(held.alternative_group_id)
    assert holder is not None
    assert holder.client_order_id == held.client_order_id


@_GROUP
def test_each_ledger_keeps_its_own_groups() -> None:
    one, other = _fragment(), _fragment()
    _admit_all(one, _chain_to("working"))
    assert other.admit(_entry(1)) is True
    _refused(one, _entry(1))


# =========================================================================== rework 2: the run ledger
# The journal's ledger is referential (the default): a record refers only to records that are already
# part of the run.  ``samples.run_story()`` is one coherent run of 28 records in journal order.
STORY = samples.run_story()
(
    _MANIFEST,
    _TICK0,
    EVIDENCE,
    CANDIDATE,
    PLAN,
    DECISION,
    ENTRY_SUBMIT,
    ENTRY_ACCEPTED,
    _TICK1,
    ENTRY_PARTIAL,
    STOP_SUBMIT,
    STOP_ACCEPTED,
    POSITION_FIRST,
    _TICK2,
    ENTRY_FILLED,
    STOP_GROWN,
    POSITION_FULL,
    _TICK3,
    POSITION_VALUED,
    _TICK4,
    EXIT_SUBMIT,
    EXIT_FILLED,
    POSITION_REDUCED,
    STOP_REQUEST,
    STOP_REPLACED,
    _TICK5,
    STOP_FILLED,
    POSITION_CLOSED,
) = STORY
POSITION_ID = POSITION_FIRST.position_id
GROUP = PLAN.alternative_group_id
_TRACKED = (OrderEvent, Position, Evidence, Candidate, TradePlan, RiskDecision)


def _ledger_through(record: Record, *, referential: bool = True) -> LifecycleLedger:
    """A ledger holding the run story up to and including *record*."""
    ledger = LifecycleLedger(referential=referential)
    _admit_all(ledger, STORY[: STORY.index(record) + 1])
    return ledger


def _rule_of(ledger: LifecycleLedger, record: Record) -> str:
    return str(_refused(ledger, record).details.get("rule"))


def _state(ledger: LifecycleLedger) -> tuple[object, ...]:
    """Everything the lookups report about the run story's records."""
    return (
        ledger.order(ENTRY_SUBMIT.client_order_id),
        ledger.order(STOP_SUBMIT.client_order_id),
        ledger.order(EXIT_SUBMIT.client_order_id),
        ledger.position(POSITION_ID),
        ledger.candidate(CANDIDATE.candidate_id),
        ledger.plan(PLAN.plan_id),
        ledger.newest_plan(PLAN.plan_id),
        ledger.decision(DECISION.decision_id),
        ledger.daily_risk(samples.RUN_ID),
        ledger.group_entry_order(GROUP),
        ledger.open_orders(),
        ledger.open_positions(),
        ledger.position_orders(POSITION_ID, working_only=False),
        ledger.protective_order(POSITION_ID),
        ledger.entry_order_of(STOP_SUBMIT),
        ledger.reducing_volume(POSITION_ID),
        ledger.model(samples.MODEL_ID),
    )


def _stop(**overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of a protective stop of the story's position (as the simulated broker made it)."""
    fields: dict[str, Any] = {
        "originator": OrderOriginator.BROKER,
        "side": Side.SELL,
        "order_type": OrderType.STOP,
        "intent": OrderIntent.PROTECTIVE_STOP,
        "limit_price": None,
        "trigger_price": D("2647.80"),
        "reference_price": None,
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": POSITION_ID,
        "volume_lots": D("0.20"),
        "reason_code": "ENTRY_FILLED",
        "seq": 3,
        "event_time_ns": STOP_SUBMIT.event_time_ns,
        "receive_time_ns": STOP_SUBMIT.receive_time_ns,
    }
    fields.update(overrides)
    return samples.order_event(**fields)


# --------------------------------------------------------------------------- the coherent run
@pytest.mark.requirement("G04-09", "G04-11", "G04-12")
def test_the_run_story_is_accepted_in_order_and_every_lookup_reports_its_end() -> None:
    ledger = LifecycleLedger()
    _admit_all(ledger, STORY)
    assert ledger.order(ENTRY_SUBMIT.client_order_id) is ENTRY_FILLED
    assert ledger.order(STOP_SUBMIT.client_order_id) is STOP_FILLED
    assert ledger.order(EXIT_SUBMIT.client_order_id) is EXIT_FILLED
    assert ledger.position(POSITION_ID) is POSITION_CLOSED
    assert ledger.candidate(CANDIDATE.candidate_id) is CANDIDATE
    assert ledger.plan(PLAN.plan_id) is PLAN
    assert ledger.newest_plan(PLAN.plan_id) is PLAN
    assert ledger.decision(DECISION.decision_id) is DECISION
    assert ledger.daily_risk(samples.RUN_ID) == advance_daily_risk(None, DECISION)
    assert ledger.group_entry_order(GROUP) is ENTRY_FILLED
    assert ledger.open_orders() == ()
    assert ledger.open_positions() == ()
    assert ledger.position_orders(POSITION_ID) == ()
    assert ledger.position_orders(POSITION_ID, working_only=False) == (STOP_FILLED, EXIT_FILLED)
    assert ledger.protective_order(POSITION_ID) is None
    assert ledger.entry_order_of(STOP_SUBMIT) is ENTRY_FILLED
    assert ledger.entry_order_of(EXIT_SUBMIT) is ENTRY_FILLED
    assert ledger.entry_order_of(ENTRY_SUBMIT) is None  # an entry names no position
    assert ledger.reducing_volume(POSITION_ID) == 0
    # Unknown keys are reported as unknown.
    assert ledger.order(identity.client_order_id(samples.RUN_ID, POSITION_ID, "EXIT", 9)) is None
    assert ledger.candidate(samples.unscored_candidate().candidate_id) is None
    assert ledger.plan(samples.trade_plan(variant=1).plan_id) is None
    assert ledger.newest_plan(samples.trade_plan(variant=1).plan_id) is None
    assert ledger.decision(samples.risk_decision(attempt=1).decision_id) is None
    assert ledger.daily_risk(identity.run_id("REPLAY", "1" * 64)) is None
    assert ledger.model(samples.MODEL_ID) is None


@pytest.mark.requirement("G04-09", "G04-12")
def test_the_lookups_in_the_middle_of_the_run() -> None:
    ledger = _ledger_through(EXIT_SUBMIT)
    assert set(ledger.open_orders()) == {STOP_GROWN, EXIT_SUBMIT}
    assert ledger.open_positions() == (POSITION_VALUED,)
    assert ledger.protective_order(POSITION_ID) is STOP_GROWN
    assert ledger.position_orders(POSITION_ID) == (STOP_GROWN, EXIT_SUBMIT)
    # Unfilled volume of the working EXIT and TAKE_PROFIT orders; the protective stop is not counted.
    assert ledger.reducing_volume(POSITION_ID) == D("0.25")
    assert ledger.reducing_volume(POSITION_ID, excluding=EXIT_SUBMIT.client_order_id) == 0
    assert ledger.reducing_volume(POSITION_ID, excluding=STOP_SUBMIT.client_order_id) == D("0.25")
    assert ledger.entry_order_of(EXIT_SUBMIT) is ENTRY_FILLED
    ledger.admit(EXIT_FILLED)
    assert ledger.reducing_volume(POSITION_ID) == 0
    assert ledger.position_orders(POSITION_ID) == (STOP_GROWN,)
    assert ledger.position_orders(POSITION_ID, working_only=False) == (STOP_GROWN, EXIT_FILLED)
    assert ledger.reducing_volume(samples.external_position().position_id) == 0


def test_check_changes_nothing_while_admit_records() -> None:
    ledger = LifecycleLedger()
    for record in STORY:
        before = _state(ledger)
        assert ledger.check(record) is True
        assert ledger.check(record) is True  # still unknown: checking twice is not a repeat
        assert _state(ledger) == before
        assert ledger.admit(record) is True
        if isinstance(record, _TRACKED):
            assert ledger.check(record) is False  # now it is the newest record of its chain
            assert ledger.admit(record) is False
            if not isinstance(record, Evidence):  # evidence has no lookup of its own
                assert _state(ledger) != before


def test_a_refused_record_changes_no_lookup() -> None:
    ledger = _ledger_through(STOP_ACCEPTED)
    before = _state(ledger)
    for refused in (
        _stop(attempt=1),  # a second working protective stop
        samples.position(protective_client_order_id=EXIT_SUBMIT.client_order_id),  # not yet in the run
        _entry(1),  # a second entry of the group
    ):
        _refused(ledger, refused)
        assert _state(ledger) == before


# --------------------------------------------------------------------------- reference rules
@pytest.mark.requirement("S00-ASG-16")
def test_a_candidate_refers_to_evidence_that_is_part_of_the_run() -> None:
    ledger = LifecycleLedger()
    rule = _rule_of(ledger, CANDIDATE)
    assert rule == "a candidate refers to evidence that is part of the run"
    assert ledger.candidate(CANDIDATE.candidate_id) is None
    assert ledger.admit(EVIDENCE) is True
    assert ledger.admit(CANDIDATE) is True
    assert ledger.candidate(CANDIDATE.candidate_id) is CANDIDATE
    assert _fragment().admit(CANDIDATE) is True  # the fragment mode does not resolve references


@pytest.mark.requirement("S00-ASG-16")
def test_a_plan_refers_to_a_candidate_that_is_part_of_the_run() -> None:
    ledger = LifecycleLedger()
    ledger.admit(EVIDENCE)
    assert _rule_of(ledger, PLAN) == "a plan refers to a candidate that is part of the run"
    assert ledger.plan(PLAN.plan_id) is None
    ledger.admit(CANDIDATE)
    assert ledger.admit(PLAN) is True
    assert _fragment().admit(PLAN) is True


_TARGET_RULE = (
    "a target that names a scenario is that destination of the plan's candidate, with the same price"
)


@pytest.mark.requirement("S00-ASG-16")
@pytest.mark.parametrize(
    ("target", "accepted"),
    [
        (Target(label="prior_high", scenario="reversal", price=D("2655.00"), close_frac=D("0.5")), True),
        (Target(label="prior_high", scenario="reversal", price=D("2655.0"), close_frac=D("0.5")), True),
        (Target(label="prior_high", price=D("2655.01"), close_frac=D("0.5")), True),
        (Target(label="prior_high", scenario="reversal", price=D("2655.01"), close_frac=D("0.5")), False),
        (Target(label="prior_high", scenario="reversal", price=D("2654.99"), close_frac=D("0.5")), False),
        (Target(label="prior_high", scenario="breakdown", price=D("2655.00"), close_frac=D("0.5")), False),
        (Target(label="prior_peak", scenario="reversal", price=D("2655.00"), close_frac=D("0.5")), False),
    ],
    ids=[
        "the_destination",
        "same_price_other_scale",
        "no_scenario_free_price",
        "price_above",
        "price_below",
        "unknown_scenario",
        "unknown_label",
    ],
)
def test_a_target_that_names_a_scenario_is_that_destination_of_the_candidate(
    target: Target, accepted: bool
) -> None:
    first = Target(label="first", price=D("2652.80"), close_frac=D("0.5"))
    plan = samples.trade_plan(targets=(first, target))
    ledger = _ledger_through(CANDIDATE)
    if accepted:
        assert ledger.admit(plan) is True
        assert ledger.plan(plan.plan_id) is plan
    else:
        error = _refused(ledger, plan)
        assert error.details.get("rule") == _TARGET_RULE
        assert error.details.get("target") == f"{target.scenario}/{target.label}"
        assert ledger.plan(plan.plan_id) is None


@pytest.mark.requirement("S00-ASG-15")
def test_a_risk_decision_refers_to_a_plan_that_is_part_of_the_run() -> None:
    ledger = _ledger_through(CANDIDATE)
    assert _rule_of(ledger, DECISION) == "a risk decision refers to a plan that is part of the run"
    assert ledger.decision(DECISION.decision_id) is None
    assert ledger.daily_risk(samples.RUN_ID) is None
    ledger.admit(PLAN)
    assert ledger.admit(DECISION) is True
    assert ledger.decision(DECISION.decision_id) is DECISION
    assert _fragment().admit(DECISION) is True


@pytest.mark.requirement("S00-ASG-15")
def test_a_risk_decision_is_written_once() -> None:
    ledger = _ledger_through(DECISION)
    rewritten = samples.risk_decision(PLAN, volume_lots=D("0.40"), risk_amount=D("83.20"))
    assert rewritten.decision_id == DECISION.decision_id
    rule = _rule_of(ledger, rewritten)
    assert rule == "a risk decision is written once; a re-evaluation is a new decision (attempt + 1)"
    assert ledger.decision(DECISION.decision_id) is DECISION


_CITES = "the risk decision cited by an entry order approves the plan it cites"
_PART_OF_RUN = "an entry order refers to a plan and a risk decision that are part of the run"


@pytest.mark.requirement("S08-ACC-06")
def test_an_entry_order_refers_to_its_plan_and_to_an_approval_of_that_plan() -> None:
    plan_b = samples.trade_plan(variant=1)
    approval_b = samples.risk_decision(plan_b)
    rejection = samples.rejected_risk_decision(PLAN, attempt=1, seq=1)

    def entry(plan: TradePlan, decision: RiskDecision) -> OrderEvent:
        return samples.order_event(
            plan_id=plan.plan_id,
            risk_decision_id=decision.decision_id,
            alternative_group_id=plan.alternative_group_id,
        )

    ledger = _ledger_through(CANDIDATE)
    assert _rule_of(ledger, ENTRY_SUBMIT) == _PART_OF_RUN  # neither plan nor decision
    ledger.admit(PLAN)
    assert _rule_of(ledger, ENTRY_SUBMIT) == _PART_OF_RUN  # the plan, but no decision
    _admit_all(ledger, [rejection, plan_b, approval_b])
    assert _rule_of(ledger, entry(PLAN, rejection)) == _CITES  # a rejection approves nothing
    assert _rule_of(ledger, entry(PLAN, approval_b)) == _CITES  # an approval of another plan
    assert ledger.group_entry_order(GROUP) is None
    assert ledger.admit(entry(plan_b, approval_b)) is True
    assert ledger.group_entry_order(GROUP) == entry(plan_b, approval_b)
    # The fragment mode does not resolve references.
    assert _fragment().admit(ENTRY_SUBMIT) is True


_FILLED_RULE = "a reduce-only order belongs to a position whose entry order has filled"


@pytest.mark.requirement("G04-09", "G04-10")
def test_a_reduce_only_order_belongs_to_a_position_whose_entry_order_has_filled() -> None:
    assert _rule_of(_ledger_through(DECISION), STOP_SUBMIT) == _FILLED_RULE  # no entry order at all
    assert _rule_of(_ledger_through(ENTRY_ACCEPTED), STOP_SUBMIT) == _FILLED_RULE  # nothing filled yet
    ledger = _ledger_through(ENTRY_PARTIAL)
    assert ledger.admit(STOP_SUBMIT) is True
    assert ledger.entry_order_of(STOP_SUBMIT) is ENTRY_PARTIAL
    # Fragment mode: an order of an unknown entry is accepted, of a known unfilled entry it is not.
    assert _fragment().admit(STOP_SUBMIT) is True
    fragment = _ledger_through(ENTRY_ACCEPTED, referential=False)
    assert (
        _rule_of(fragment, STOP_SUBMIT)
        == "a reduce-only order does not exceed what the entry order has filled"
    )


@pytest.mark.requirement("G04-09", "G04-10", "G04-11")
@pytest.mark.parametrize(
    ("changed", "rule"),
    [
        (
            {"volume_lots": D("0.200000000001")},
            "a reduce-only order does not exceed what the entry order has filled",
        ),
        (
            {"side": Side.BUY, "trigger_price": D("2652.80")},
            "a reduce-only order is on the opposite side of its entry",
        ),
        (
            {"trigger_price": D("2647.70")},
            "protection created by the simulated broker is the protection attached to the entry",
        ),
    ],
    ids=["more_than_filled", "same_side_as_the_entry", "not_the_attached_stop"],
)
@pytest.mark.parametrize("referential", [True, False], ids=["run", "fragment"])
def test_a_reduce_only_order_fits_the_entry_it_reduces(
    changed: dict[str, Any], rule: str, referential: bool
) -> None:
    ledger = _ledger_through(ENTRY_PARTIAL, referential=referential)
    assert _rule_of(ledger, _stop(**changed)) == rule
    assert ledger.position_orders(POSITION_ID, working_only=False) == ()
    assert ledger.admit(_stop()) is True


_ENTRY_PART_OF_RUN = "a GIE position refers to its entry order, which is part of the run"
_AT_MOST_FILLED = (
    "a position holds at most what its entry order has filled (no averaging down, no adding to a position)"
)


@pytest.mark.requirement("G04-09", "G04-10")
def test_a_position_refers_to_its_entry_order_and_holds_at_most_what_it_has_filled() -> None:
    assert _rule_of(_ledger_through(DECISION), POSITION_FIRST) == _ENTRY_PART_OF_RUN
    assert _fragment().admit(POSITION_FIRST) is True  # fragment: the entry is not in view
    ledger = _ledger_through(STOP_ACCEPTED)
    assert ledger.entry_order_of(STOP_SUBMIT) is ENTRY_PARTIAL
    more = {"initial_volume_lots": D("0.200000000001"), "initial_risk_amount": D("41.60")}
    overgrown = samples.position(**{**_position_fields(POSITION_FIRST), **more})
    assert _rule_of(ledger, overgrown) == _AT_MOST_FILLED
    assert ledger.admit(POSITION_FIRST) is True
    # Fragment mode applies the aggregate rule once the entry order is known.
    assert _rule_of(_ledger_through(STOP_ACCEPTED, referential=False), overgrown) == _AT_MOST_FILLED


@pytest.mark.requirement("G04-09", "G04-12")
def test_a_position_carries_the_side_and_plan_of_its_entry_order() -> None:
    ledger = _ledger_through(STOP_ACCEPTED)
    other_plan = samples.trade_plan(variant=1).plan_id
    wrong_plan = samples.position(**{**_position_fields(POSITION_FIRST), "plan_id": other_plan})
    assert _rule_of(ledger, wrong_plan) == "a position carries the plan of its entry order"
    short = {
        "side": Side.SELL,
        "initial_stop_price": D("2651.80"),
        "stop_price": D("2651.80"),
        "mark_price": D("2649.90"),
    }
    wrong_side = samples.position(**{**_position_fields(POSITION_FIRST), **short})
    rule = _rule_of(ledger, wrong_side)
    assert rule == "the opening order of a position is an ENTRY order on the position's side"
    assert ledger.position(POSITION_ID) is None


_PROTECTIVE_PART_OF_RUN = "the protective order named by a position is part of the run"
_PROTECTIVE_WORKING = "the protective order named by a position is a working PROTECTIVE_STOP of that position"


@pytest.mark.requirement("G04-12")
def test_a_position_names_a_working_protective_stop_of_its_own_that_is_part_of_the_run() -> None:
    ledger = _ledger_through(ENTRY_PARTIAL)
    assert _rule_of(ledger, POSITION_FIRST) == _PROTECTIVE_PART_OF_RUN
    unprotected = samples.position(
        **{
            **_position_fields(POSITION_FIRST),
            "protective_client_order_id": None,
            "unprotected_reason": "STOP_PENDING",
        }
    )
    assert ledger.check(unprotected) is True  # a position without protection says so
    assert _ledger_through(ENTRY_PARTIAL, referential=False).admit(POSITION_FIRST) is True
    _admit_all(ledger, [STOP_SUBMIT, STOP_ACCEPTED])
    assert ledger.admit(POSITION_FIRST) is True
    # An exit order is not a protective stop; a stop that has ended no longer protects.
    later = _ledger_through(EXIT_SUBMIT)
    names_exit = _after_position(POSITION_VALUED, protective_client_order_id=EXIT_SUBMIT.client_order_id)
    assert _rule_of(later, names_exit) == _PROTECTIVE_WORKING
    ended = _ledger_through(STOP_FILLED)
    assert _rule_of(ended, _after_position(POSITION_REDUCED, seq=4, revision=4)) == _PROTECTIVE_WORKING


# --------------------------------------------------------------------------- aggregate rules
@pytest.mark.requirement("G04-11", "G04-12")
def test_a_position_has_at_most_one_working_protective_stop() -> None:
    ledger = _ledger_through(STOP_ACCEPTED)
    second = _stop(attempt=1)
    assert second.client_order_id != STOP_SUBMIT.client_order_id
    assert _rule_of(ledger, second) == "a position has at most one working protective stop"
    assert ledger.protective_order(POSITION_ID) is STOP_ACCEPTED
    cancel = _next(STOP_ACCEPTED, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    _admit_all(ledger, [cancel])
    assert _rule_of(ledger, second) == "a position has at most one working protective stop"
    _admit_all(ledger, [_next(cancel, E.CANCELED, S.CANCELED)])
    assert ledger.protective_order(POSITION_ID) is None
    assert ledger.admit(second) is True
    assert ledger.protective_order(POSITION_ID) is second


@pytest.mark.requirement("G05-04", "G05-06")
def test_the_daily_risk_state_holds_for_the_run_across_all_risk_producers() -> None:
    ledger = _ledger_through(PLAN)
    protect = DailyState.PROFIT_PROTECTION

    def decision(producer: str, attempt: int, **overrides: Any) -> RiskDecision:
        moment = T0 + attempt * NS_PER_SECOND
        fields: dict[str, Any] = {
            "producer": producer,
            "seq": attempt,
            "attempt": attempt,
            "decision_time_ns": moment,
            "as_of_time_ns": moment,
            "valid_until_ns": moment + NS_PER_SECOND,
            "daily_state": protect,
            "daily_pnl_amount": D("9000"),
        }
        fields.update(overrides)
        return samples.risk_decision(PLAN, **fields)

    assert ledger.admit(decision("risk.gate", 0, profit_floor_amount=D("5000"))) is True
    other = "risk.gate_secondary"
    assert (
        _rule_of(ledger, decision(other, 1, profit_floor_amount=D("4000")))
        == "the daily profit floor may not decrease"
    )
    assert _rule_of(ledger, decision(other, 1, daily_state=DailyState.NORMAL)) == (
        "a daily profit floor, once established, may not disappear"
    )
    summary = ledger.daily_risk(samples.RUN_ID)
    assert summary is not None
    assert summary.profit_floor_amount == D("5000")
    assert summary.last_decision_time_ns == T0
    assert ledger.admit(decision(other, 1, profit_floor_amount=D("6000"))) is True
    halt = samples.rejected_risk_decision(
        PLAN,
        producer=other,
        seq=2,
        attempt=2,
        decision_time_ns=T0 + 2 * NS_PER_SECOND,
        as_of_time_ns=T0 + 2 * NS_PER_SECOND,
        daily_state=DailyState.EMERGENCY_HALT,
        daily_pnl_amount=D("-3000"),
        profit_floor_amount=D("6000"),
        reason_codes=("EMERGENCY_HALT",),
    )
    assert ledger.admit(halt) is True
    assert _rule_of(ledger, decision("risk.gate", 3, profit_floor_amount=D("6000"))) == (
        "an emergency halt stays in force until the next trading day"
    )
    summary = ledger.daily_risk(samples.RUN_ID)
    assert summary is not None
    assert summary.halted is True
    assert summary.profit_floor_amount == D("6000")


def _position_fields(position: Position) -> dict[str, Any]:
    return {spec.name: getattr(position, spec.name) for spec in Position.FIELD_SPECS}


def _after_position(previous: Position, **overrides: Any) -> Position:
    """The next snapshot of *previous* (one millisecond later)."""
    fields = _position_fields(previous)
    moment = previous.decision_time_ns + NS_PER_MS
    fields.update(seq=previous.seq + 1, revision=previous.revision + 1, decision_time_ns=moment)
    fields.update(overrides)
    return Position(**fields)


@pytest.mark.requirement("S00-ASG-17")
def test_the_registry_rules_hold_in_the_run_ledger_one_active_version_per_family() -> None:
    ledger = LifecycleLedger()
    _admit_all(ledger, samples.model_lifecycle())
    active = ledger.model(samples.MODEL_ID)
    assert active is not None
    assert active.status is ModelStatus.ACTIVE

    def successor(record: Any) -> Any:
        fields = {spec.name: getattr(record, spec.name) for spec in type(record).FIELD_SPECS}
        artifact = "ee" * 32
        fields.update(
            semver="0.2.0",
            artifact_sha256=artifact,
            model_version_id=identity.model_version_id(record.model_name, "0.2.0", artifact),
        )
        return type(record)(**fields)

    research, shadow, promoted = (successor(record) for record in samples.model_lifecycle())
    _admit_all(ledger, [research, shadow])
    rule = _rule_of(ledger, promoted)
    assert rule == "the currently ACTIVE version must be RETIRED or ROLLED_BACK first"
    assert ledger.model(promoted.model_version_id) is shadow
    assert ledger.model(samples.MODEL_ID) is active
