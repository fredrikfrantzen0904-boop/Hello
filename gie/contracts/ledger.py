"""Lifecycle ledger: the sequence rules of the contracts, enforced at one place.

Record invariants make a single invalid record impossible.  Rules that relate a
record to other records need those records:

* **chains** - an order may not grow, a stop may not widen, a model may not
  skip shadow validation: each new record is checked against its predecessor;
* **references** - a record may only refer to records that are already part of
  the run: a candidate to its evidence, a plan to its candidate, a risk decision
  to its plan, an entry order to its plan and approval, every other order and
  every position to the entry order or position it belongs to;
* **aggregates** - at most one alternative plan of a group is executed; a
  position holds at most what its entry order has filled; one protective stop
  per position; the daily profit floor and an emergency halt hold for the whole
  trading day whoever writes the decision; one ACTIVE version per model family.

A run has **one** ledger: it belongs to the run's journal, every record is
admitted to it when it is published, and the order gateway reads it.  A record
that breaks a rule therefore never becomes part of a run's history.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from gie.contracts import identity
from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate, validate_candidate_transition
from gie.contracts.evidence import Evidence, validate_evidence_transition
from gie.contracts.model_version import ModelVersion, RegistryHistory
from gie.contracts.order_event import (
    TERMINAL_ORDER_STATES,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderOriginator,
    OrderState,
    validate_order_transition,
)
from gie.contracts.position import Position, PositionOwner, PositionState, validate_position_transition
from gie.contracts.risk_decision import DailyRiskSummary, RiskDecision, advance_daily_risk
from gie.contracts.trade_plan import TradePlan, validate_plan_transition
from gie.core.canonical import EXACT
from gie.core.errors import IllegalTransitionError

#: Terminal states in which an entry order has left its alternative group free again.
_RELEASED: Final[frozenset[OrderState]] = frozenset(
    {OrderState.REJECTED, OrderState.CANCELED, OrderState.EXPIRED}
)
_ZERO: Final[Decimal] = Decimal(0)
_PlanChain = tuple[str, str, int]


def _fail(rule: str, **details: str | int | None) -> IllegalTransitionError:
    return IllegalTransitionError(f"record refused by the run ledger: {rule}", rule=rule, **details)


class LifecycleLedger:
    """What a run has established so far, with the checks every new record must pass.

    ``check`` validates a record without recording it; ``admit`` validates and
    records it.  A refused record never changes the ledger.  Presenting the
    newest record of a chain again is a no-op (idempotent repeats), reported by
    the return value ``False``.

    ``referential=False`` switches the reference rules off; it exists for tools
    that examine a fragment of a history (a single order chain, for example).
    A journal always uses the default.
    """

    def __init__(self, *, referential: bool = True) -> None:
        self._referential: Final[bool] = referential
        self._orders: dict[str, OrderEvent] = {}
        self._positions: dict[str, Position] = {}
        self._registry: Final[RegistryHistory] = RegistryHistory()
        self._evidence: dict[tuple[str, str], Evidence] = {}
        self._evidence_ids: set[str] = set()
        self._candidates: dict[tuple[str, str | None], Candidate] = {}
        self._candidate_ids: dict[str, Candidate] = {}
        self._plans: dict[_PlanChain, TradePlan] = {}
        self._plan_ids: dict[str, TradePlan] = {}
        self._decisions: dict[str, RiskDecision] = {}
        self._risk_days: dict[str, DailyRiskSummary] = {}
        self._group_entry: dict[str, str] = {}
        self._entry_by_position: dict[str, str] = {}
        self._position_orders: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ accessors
    def order(self, client_order_id: str) -> OrderEvent | None:
        """Newest event of an order."""
        return self._orders.get(client_order_id)

    def position(self, position_id: str) -> Position | None:
        """Newest snapshot of a position."""
        return self._positions.get(position_id)

    def model(self, model_version_id: str) -> ModelVersion | None:
        """Newest registry entry of a model version."""
        return self._registry.model(model_version_id)

    def candidate(self, candidate_id: str) -> Candidate | None:
        return self._candidate_ids.get(candidate_id)

    def plan(self, plan_id: str) -> TradePlan | None:
        """The plan revision with this id (every revision has its own id)."""
        return self._plan_ids.get(plan_id)

    def newest_plan(self, plan_id: str) -> TradePlan | None:
        """Newest revision of the plan that the revision *plan_id* belongs to."""
        plan = self._plan_ids.get(plan_id)
        return None if plan is None else self._plans[_plan_chain(plan)]

    def decision(self, decision_id: str) -> RiskDecision | None:
        return self._decisions.get(decision_id)

    def daily_risk(self, run_id: str) -> DailyRiskSummary | None:
        """What the risk decisions of the run have established for its current trading day."""
        return self._risk_days.get(run_id)

    def group_entry_order(self, alternative_group_id: str) -> OrderEvent | None:
        """Newest event of the entry order that currently occupies an alternative group, if any."""
        client_order_id = self._group_entry.get(alternative_group_id)
        return None if client_order_id is None else self._orders.get(client_order_id)

    def open_orders(self) -> tuple[OrderEvent, ...]:
        """Newest event of every order that is not in a terminal state."""
        return tuple(
            event for event in self._orders.values() if event.order_state not in TERMINAL_ORDER_STATES
        )

    def open_positions(self) -> tuple[Position, ...]:
        """Newest snapshot of every position that is OPEN."""
        return tuple(
            position for position in self._positions.values() if position.state is PositionState.OPEN
        )

    def position_orders(self, position_id: str, *, working_only: bool = True) -> tuple[OrderEvent, ...]:
        """Newest event of the reduce-only orders of a position (by default the non-terminal ones)."""
        events = (self._orders[order_id] for order_id in self._position_orders.get(position_id, ()))
        return tuple(
            event for event in events if not working_only or event.order_state not in TERMINAL_ORDER_STATES
        )

    def protective_order(self, position_id: str) -> OrderEvent | None:
        """The non-terminal PROTECTIVE_STOP order of a position, if one exists."""
        for event in self.position_orders(position_id):
            if event.intent is OrderIntent.PROTECTIVE_STOP:
                return event
        return None

    # ------------------------------------------------------------------ chains
    def _previous(self, record: Record) -> Record | None:
        if isinstance(record, OrderEvent):
            return self._orders.get(record.client_order_id)
        if isinstance(record, Position):
            return self._positions.get(record.position_id)
        if isinstance(record, ModelVersion):
            return self._registry.model(record.model_version_id)
        if isinstance(record, Evidence):
            return self._evidence.get((record.engine, record.market_event_id))
        if isinstance(record, Candidate):
            return self._candidates.get((record.opportunity_id, record.model_version_id))
        if isinstance(record, TradePlan):
            return self._plans.get(_plan_chain(record))
        if isinstance(record, RiskDecision):
            return self._decisions.get(record.decision_id)
        return None

    # ------------------------------------------------------------------ order rules
    def _check_alternative_group(self, event: OrderEvent) -> None:
        if event.intent is not OrderIntent.ENTRY or event.event_type is not OrderEventType.SUBMIT_REQUESTED:
            return
        assert event.alternative_group_id is not None
        holder = self.group_entry_order(event.alternative_group_id)
        if holder is None or holder.client_order_id == event.client_order_id:
            return
        if holder.order_state in _RELEASED and holder.cumulative_filled_lots == _ZERO:
            return
        raise IllegalTransitionError(
            "another plan of this alternative group is already working or was executed",
            client_order_id=event.client_order_id,
            alternative_group_id=event.alternative_group_id,
            holder=holder.client_order_id,
            rule="at most one plan of an alternative group is ever executed",
        )

    def entry_order_of(self, event: OrderEvent) -> OrderEvent | None:
        """The entry order whose position a reduce-only order names, as far as the ledger knows it."""
        if event.position_id is None:
            return None
        entry_id = self._entry_by_position.get(event.position_id)
        return None if entry_id is None else self._orders.get(entry_id)

    def _check_new_order(self, event: OrderEvent) -> None:
        """Reference and aggregate rules for the first event of an order."""
        order_id = event.client_order_id
        if event.intent is OrderIntent.ENTRY:
            assert event.plan_id is not None
            assert event.risk_decision_id is not None
            if not self._referential:
                return
            plan = self._plan_ids.get(event.plan_id)
            decision = self._decisions.get(event.risk_decision_id)
            if plan is None or decision is None:
                raise _fail(
                    "an entry order refers to a plan and a risk decision that are part of the run",
                    client_order_id=order_id,
                )
            if decision.plan_id != plan.plan_id or not decision.approved:
                raise _fail(
                    "the risk decision cited by an entry order approves the plan it cites",
                    client_order_id=order_id,
                )
            return
        assert event.position_id is not None
        entry = self.entry_order_of(event)
        if self._referential and (entry is None or entry.cumulative_filled_lots == _ZERO):
            raise _fail(
                "a reduce-only order belongs to a position whose entry order has filled",
                client_order_id=order_id,
                position_id=event.position_id,
            )
        if entry is not None:
            if event.side is entry.side:
                raise _fail(
                    "a reduce-only order is on the opposite side of its entry", client_order_id=order_id
                )
            if event.volume_lots > entry.cumulative_filled_lots:
                raise _fail(
                    "a reduce-only order does not exceed what the entry order has filled",
                    client_order_id=order_id,
                )
            if event.originator is OrderOriginator.BROKER:
                attached = {
                    OrderIntent.PROTECTIVE_STOP: (entry.attached_stop_price, event.trigger_price),
                    OrderIntent.TAKE_PROFIT: (entry.attached_take_profit_price, event.limit_price),
                }.get(event.intent)
                if attached is not None and (attached[0] is None or attached[0] != attached[1]):
                    raise _fail(
                        "protection created by the simulated broker is the protection attached to the entry",
                        client_order_id=order_id,
                    )
        if (
            event.intent is OrderIntent.PROTECTIVE_STOP
            and self.protective_order(event.position_id) is not None
        ):
            raise _fail(
                "a position has at most one working protective stop",
                client_order_id=order_id,
                position_id=event.position_id,
            )

    # ------------------------------------------------------------------ position rules
    def _check_position(self, snapshot: Position) -> None:
        if snapshot.owner is not PositionOwner.GIE:
            return
        assert snapshot.opening_client_order_id is not None
        entry = self._orders.get(snapshot.opening_client_order_id)
        if entry is None:
            if self._referential:
                raise _fail(
                    "a GIE position refers to its entry order, which is part of the run",
                    position_id=snapshot.position_id,
                )
        else:
            if entry.intent is not OrderIntent.ENTRY or entry.side is not snapshot.side:
                raise _fail(
                    "the opening order of a position is an ENTRY order on the position's side",
                    position_id=snapshot.position_id,
                )
            if entry.plan_id != snapshot.plan_id:
                raise _fail(
                    "a position carries the plan of its entry order", position_id=snapshot.position_id
                )
            if snapshot.initial_volume_lots > entry.cumulative_filled_lots:
                raise _fail(
                    "a position holds at most what its entry order has filled "
                    "(no averaging down, no adding to a position)",
                    position_id=snapshot.position_id,
                )
        protective = snapshot.protective_client_order_id
        if protective is not None:
            order = self._orders.get(protective)
            if order is None:
                if self._referential:
                    raise _fail(
                        "the protective order named by a position is part of the run",
                        position_id=snapshot.position_id,
                    )
            elif (
                order.intent is not OrderIntent.PROTECTIVE_STOP
                or order.position_id != snapshot.position_id
                or order.order_state in TERMINAL_ORDER_STATES
            ):
                raise _fail(
                    "the protective order named by a position is a working PROTECTIVE_STOP of that position",
                    position_id=snapshot.position_id,
                )

    # ------------------------------------------------------------------ references of decisions
    def _check_references(self, record: Record) -> None:
        if not self._referential:
            return
        if isinstance(record, Candidate):
            missing = [item for item in record.evidence_ids if item not in self._evidence_ids]
            if missing:
                raise _fail(
                    "a candidate refers to evidence that is part of the run",
                    candidate_id=record.candidate_id,
                    evidence_id=missing[0],
                )
        elif isinstance(record, TradePlan):
            candidate = self._candidate_ids.get(record.candidate_id)
            if candidate is None:
                raise _fail("a plan refers to a candidate that is part of the run", plan_id=record.plan_id)
            predicted = {(item.scenario, item.label): item.price for item in candidate.destinations}
            for target in record.targets:
                if (
                    target.scenario is not None
                    and predicted.get((target.scenario, target.label)) != target.price
                ):
                    raise _fail(
                        "a target that names a scenario is that destination of the plan's candidate, "
                        "with the same price",
                        plan_id=record.plan_id,
                        target=f"{target.scenario}/{target.label}",
                    )
        elif isinstance(record, RiskDecision) and record.plan_id not in self._plan_ids:
            raise _fail(
                "a risk decision refers to a plan that is part of the run", decision_id=record.decision_id
            )

    # ------------------------------------------------------------------ core
    def check(self, record: Record) -> bool:
        """Validate *record*.  Returns False when it is the newest record of its chain again."""
        if isinstance(record, ModelVersion):
            return self._registry.check(record)
        previous = self._previous(record)
        if previous is not None and previous == record:
            return False
        if isinstance(record, OrderEvent):
            assert previous is None or isinstance(previous, OrderEvent)
            validate_order_transition(previous, record)
            if previous is None:
                self._check_new_order(record)
            self._check_alternative_group(record)
        elif isinstance(record, Position):
            assert previous is None or isinstance(previous, Position)
            validate_position_transition(previous, record)
            self._check_position(record)
        elif isinstance(record, Evidence):
            assert previous is None or isinstance(previous, Evidence)
            validate_evidence_transition(previous, record)
        elif isinstance(record, Candidate):
            assert previous is None or isinstance(previous, Candidate)
            validate_candidate_transition(previous, record)
            self._check_references(record)
        elif isinstance(record, TradePlan):
            assert previous is None or isinstance(previous, TradePlan)
            validate_plan_transition(previous, record)
            self._check_references(record)
        elif isinstance(record, RiskDecision):
            if previous is not None:
                raise _fail(
                    "a risk decision is written once; a re-evaluation is a new decision (attempt + 1)",
                    decision_id=record.decision_id,
                )
            self._check_references(record)
            advance_daily_risk(self._risk_days.get(record.run_id), record)
        return True

    def admit(self, record: Record) -> bool:
        """Validate *record* and make it part of the run.  Returns False for a repeat."""
        if isinstance(record, ModelVersion):
            return self._registry.admit(record)
        if not self.check(record):
            return False
        if isinstance(record, OrderEvent):
            first = record.client_order_id not in self._orders
            self._orders[record.client_order_id] = record
            if first and record.intent is OrderIntent.ENTRY:
                assert record.alternative_group_id is not None
                self._group_entry[record.alternative_group_id] = record.client_order_id
                self._entry_by_position[_position_id_of(record)] = record.client_order_id
            elif first:
                assert record.position_id is not None
                self._position_orders.setdefault(record.position_id, []).append(record.client_order_id)
        elif isinstance(record, Position):
            self._positions[record.position_id] = record
        elif isinstance(record, Evidence):
            self._evidence[(record.engine, record.market_event_id)] = record
            self._evidence_ids.add(record.evidence_id)
        elif isinstance(record, Candidate):
            self._candidates[(record.opportunity_id, record.model_version_id)] = record
            self._candidate_ids[record.candidate_id] = record
        elif isinstance(record, TradePlan):
            self._plans[_plan_chain(record)] = record
            self._plan_ids[record.plan_id] = record
        elif isinstance(record, RiskDecision):
            self._decisions[record.decision_id] = record
            self._risk_days[record.run_id] = advance_daily_risk(self._risk_days.get(record.run_id), record)
        return True

    # ------------------------------------------------------------------ aggregates used by the gateway
    def reducing_volume(self, position_id: str, *, excluding: str | None = None) -> Decimal:
        """Unfilled volume of the working EXIT and TAKE_PROFIT orders of a position."""
        total = _ZERO
        for event in self.position_orders(position_id):
            if event.client_order_id == excluding or event.intent is OrderIntent.PROTECTIVE_STOP:
                continue
            total = EXACT.add(total, EXACT.subtract(event.volume_lots, event.cumulative_filled_lots))
        return total


def _plan_chain(plan: TradePlan) -> _PlanChain:
    return (plan.candidate_id, plan.entry_style.value, plan.variant)


def _position_id_of(entry: OrderEvent) -> str:
    return identity.position_id(entry.run_id, entry.client_order_id)
