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
    DailyState,
    LifecycleLedger,
    ModelStatus,
    OrderEvent,
    OrderEventType,
    OrderState,
    PlanStatus,
    Position,
    Record,
    identity,
)
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
        reason_code="FIXTURE_REASON" if event_type in _NEEDS_REASON else None,
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
    ledger = LifecycleLedger()
    submit = samples.order_event()
    assert ledger.order(submit.client_order_id) is None
    assert ledger.position(samples.position().position_id) is None
    assert ledger.model(samples.MODEL_ID) is None
    assert submit.alternative_group_id is not None
    assert ledger.group_entry_order(submit.alternative_group_id) is None
    assert ledger.open_orders() == ()


def test_admit_records_the_newest_event_of_an_order() -> None:
    ledger = LifecycleLedger()
    chain = samples.order_lifecycle()
    for event in chain:
        assert ledger.admit(event) is True
        assert ledger.order(event.client_order_id) is event
    assert ledger.order(chain[0].client_order_id) is chain[-1]


def test_check_validates_without_recording() -> None:
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
    submit, accepted, partial, _filled = samples.order_lifecycle()
    _admit_all(ledger, [submit, accepted, partial])
    for older in (submit, accepted):
        _refused(ledger, older)
    assert ledger.order(submit.client_order_id) is partial


def test_other_content_at_the_position_of_the_newest_event_is_refused() -> None:
    ledger = LifecycleLedger()
    submit, accepted, *_ = samples.order_lifecycle()
    _admit_all(ledger, [submit, accepted])
    conflicting = _next(submit, E.REJECTED, S.REJECTED)
    assert conflicting.order_seq == accepted.order_seq
    _refused(ledger, conflicting)
    assert ledger.order(submit.client_order_id) is accepted


def test_open_orders_lists_exactly_the_orders_that_are_not_terminal() -> None:
    ledger = LifecycleLedger()
    entry = samples.order_lifecycle()
    stop = samples.protective_stop_lifecycle()
    _admit_all(ledger, [entry[0], stop[0], entry[1], stop[1]])
    assert set(ledger.open_orders()) == {entry[1], stop[1]}
    _admit_all(ledger, [entry[2], entry[3]])
    assert ledger.open_orders() == (stop[1],)
    _admit_all(ledger, [stop[2], stop[3]])
    assert ledger.open_orders() == (stop[3],)


# --------------------------------------------------------------------------- refusals change nothing
@pytest.mark.requirement("G04-09", "G04-11")
def test_a_refused_order_event_never_changes_the_ledger() -> None:
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
    submit, accepted, *_ = samples.order_lifecycle()
    _refused(ledger, accepted)
    assert ledger.order(accepted.client_order_id) is None
    assert ledger.open_orders() == ()
    assert accepted.alternative_group_id is not None
    assert ledger.group_entry_order(accepted.alternative_group_id) is None
    _admit_all(ledger, [submit, accepted])


@pytest.mark.requirement("G04-11")
def test_a_widening_stop_replacement_is_refused_and_the_stop_stays() -> None:
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
    entry = samples.order_lifecycle()
    stop = samples.protective_stop_lifecycle()
    assert entry[0].client_order_id != stop[0].client_order_id
    _admit_all(ledger, [entry[0], stop[0], stop[1], entry[1], entry[2], stop[2], entry[3], stop[3]])
    assert ledger.order(entry[0].client_order_id) is entry[3]
    assert ledger.order(stop[0].client_order_id) is stop[3]
    # A further event of the stop order extends the stop's chain only.
    assert ledger.admit(_next(stop[3], E.CANCELED, S.CANCELED)) is True
    assert ledger.order(entry[0].client_order_id) is entry[3]
    assert ledger.open_orders() == ()


@pytest.mark.requirement("G04-10", "G04-11", "G04-13")
def test_positions_are_chains_keyed_by_position_id() -> None:
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
    halt = samples.rejected_risk_decision(
        daily_state=DailyState.EMERGENCY_HALT, daily_pnl_frac=D("-0.03"), reason_codes=("EMERGENCY_HALT",)
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
    ledger = LifecycleLedger()
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
            daily_pnl_frac=D("0.09"),
            profit_floor_frac=D(floor),
        )

    assert ledger.admit(protected(0, "0.05")) is True
    _refused(ledger, protected(1, "0.04"))
    assert ledger.admit(protected(1, "0.06")) is True
    _refused(ledger, protected(2, "0.05"))
    assert ledger.admit(protected(2, "0.06")) is True


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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
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
    ledger = LifecycleLedger()
    _admit_all(ledger, _chain_to("working"))
    again = _entry(0, attempt=1)
    assert again.plan_id == _entry(0).plan_id
    assert again.client_order_id != _entry(0).client_order_id
    _refused(ledger, again)


@_GROUP
def test_the_group_stays_taken_through_the_whole_life_of_the_executed_entry() -> None:
    ledger = LifecycleLedger()
    second = _entry(1)
    for event in samples.order_lifecycle():
        assert ledger.admit(event) is True
        _refused(ledger, second)
    assert ledger.order(second.client_order_id) is None


@_GROUP
def test_repeating_the_entry_that_holds_the_group_is_an_idempotent_no_op() -> None:
    ledger = LifecycleLedger()
    first = _entry(0)
    assert ledger.admit(first) is True
    assert ledger.check(_entry(0)) is False
    assert ledger.admit(_entry(0)) is False
    assert first.alternative_group_id is not None
    assert ledger.group_entry_order(first.alternative_group_id) is first


@_GROUP
def test_entries_of_other_groups_and_orders_of_positions_are_independent_of_the_group_rule() -> None:
    ledger = LifecycleLedger()
    _admit_all(ledger, _chain_to("working"))
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
    one, other = LifecycleLedger(), LifecycleLedger()
    _admit_all(one, _chain_to("working"))
    assert other.admit(_entry(1)) is True
    _refused(one, _entry(1))
