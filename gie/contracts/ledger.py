"""Lifecycle ledger: the sequence rules of the contracts, enforced at one place.

Record invariants make a single invalid record impossible.  Rules that relate a
record to its predecessor - an order may not grow, a stop may not widen, a
model may not skip shadow validation - need the predecessor.  The ledger keeps
the newest record of every lifecycle chain and checks each new record against
it.  Every journal and the order gateway admit records through a ledger, so a
record that breaks a sequence rule never becomes part of a run's history.
"""

from __future__ import annotations

from typing import Final

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate, validate_candidate_transition
from gie.contracts.evidence import Evidence, validate_evidence_transition
from gie.contracts.model_version import ModelVersion, validate_model_transition
from gie.contracts.order_event import (
    TERMINAL_ORDER_STATES,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    validate_order_transition,
)
from gie.contracts.position import Position, validate_position_transition
from gie.contracts.risk_decision import RiskDecision, validate_daily_state_sequence
from gie.contracts.trade_plan import TradePlan, validate_plan_transition
from gie.core.errors import IllegalTransitionError

#: Terminal states in which an entry order has left its alternative group free again.
_RELEASED: Final[frozenset[OrderState]] = frozenset(
    {OrderState.REJECTED, OrderState.CANCELED, OrderState.EXPIRED}
)


class LifecycleLedger:
    """Newest record per lifecycle chain of one run, with the transition checks.

    ``check`` validates a record against the chain without recording it;
    ``admit`` validates and records it.  A refused record never changes the
    ledger.  Admitting the newest record of a chain again is a no-op (idempotent
    replays), reported by the return value ``False``.
    """

    def __init__(self) -> None:
        self._orders: dict[str, OrderEvent] = {}
        self._positions: dict[str, Position] = {}
        self._models: dict[str, ModelVersion] = {}
        self._evidence: dict[tuple[str, str], Evidence] = {}
        self._candidates: dict[tuple[str, str | None], Candidate] = {}
        self._plans: dict[tuple[str, str, int], TradePlan] = {}
        self._risk: dict[str, RiskDecision] = {}
        self._group_entry: dict[str, str] = {}

    # ------------------------------------------------------------------ accessors
    def order(self, client_order_id: str) -> OrderEvent | None:
        return self._orders.get(client_order_id)

    def position(self, position_id: str) -> Position | None:
        return self._positions.get(position_id)

    def model(self, model_version_id: str) -> ModelVersion | None:
        return self._models.get(model_version_id)

    def group_entry_order(self, alternative_group_id: str) -> OrderEvent | None:
        """Newest event of the entry order that currently occupies an alternative group, if any."""
        client_order_id = self._group_entry.get(alternative_group_id)
        return None if client_order_id is None else self._orders.get(client_order_id)

    # ------------------------------------------------------------------ core
    def _previous(self, record: Record) -> Record | None:
        if isinstance(record, OrderEvent):
            return self._orders.get(record.client_order_id)
        if isinstance(record, Position):
            return self._positions.get(record.position_id)
        if isinstance(record, ModelVersion):
            return self._models.get(record.model_version_id)
        if isinstance(record, Evidence):
            return self._evidence.get((record.engine, record.market_event_id))
        if isinstance(record, Candidate):
            return self._candidates.get((record.opportunity_id, record.model_version_id))
        if isinstance(record, TradePlan):
            return self._plans.get((record.candidate_id, record.entry_style.value, record.variant))
        if isinstance(record, RiskDecision):
            return self._risk.get(record.producer)
        return None

    def _check_alternative_group(self, event: OrderEvent) -> None:
        if event.intent is not OrderIntent.ENTRY or event.event_type is not OrderEventType.SUBMIT_REQUESTED:
            return
        assert event.alternative_group_id is not None
        holder = self.group_entry_order(event.alternative_group_id)
        if holder is None or holder.client_order_id == event.client_order_id:
            return
        if holder.order_state in _RELEASED and holder.cumulative_filled_lots == 0:
            return
        raise IllegalTransitionError(
            "another plan of this alternative group is already working or was executed",
            client_order_id=event.client_order_id,
            alternative_group_id=event.alternative_group_id,
            holder=holder.client_order_id,
            rule="at most one plan of an alternative group is ever executed",
        )

    def check(self, record: Record) -> bool:
        """Validate *record* against its chain.  Returns False when it is the chain's newest record again."""
        previous = self._previous(record)
        if previous is not None and previous == record:
            return False
        if isinstance(record, OrderEvent):
            assert previous is None or isinstance(previous, OrderEvent)
            validate_order_transition(previous, record)
            self._check_alternative_group(record)
        elif isinstance(record, Position):
            assert previous is None or isinstance(previous, Position)
            validate_position_transition(previous, record)
        elif isinstance(record, ModelVersion):
            assert previous is None or isinstance(previous, ModelVersion)
            validate_model_transition(previous, record)
        elif isinstance(record, Evidence):
            assert previous is None or isinstance(previous, Evidence)
            validate_evidence_transition(previous, record)
        elif isinstance(record, Candidate):
            assert previous is None or isinstance(previous, Candidate)
            validate_candidate_transition(previous, record)
        elif isinstance(record, TradePlan):
            assert previous is None or isinstance(previous, TradePlan)
            validate_plan_transition(previous, record)
        elif isinstance(record, RiskDecision):
            assert previous is None or isinstance(previous, RiskDecision)
            validate_daily_state_sequence(previous, record)
        return True

    def admit(self, record: Record) -> bool:
        """Validate *record* and make it the newest record of its chain.  Returns False for a repeat."""
        if not self.check(record):
            return False
        if isinstance(record, OrderEvent):
            self._orders[record.client_order_id] = record
            if record.intent is OrderIntent.ENTRY and record.event_type is OrderEventType.SUBMIT_REQUESTED:
                assert record.alternative_group_id is not None
                self._group_entry[record.alternative_group_id] = record.client_order_id
        elif isinstance(record, Position):
            self._positions[record.position_id] = record
        elif isinstance(record, ModelVersion):
            self._models[record.model_version_id] = record
        elif isinstance(record, Evidence):
            self._evidence[(record.engine, record.market_event_id)] = record
        elif isinstance(record, Candidate):
            self._candidates[(record.opportunity_id, record.model_version_id)] = record
        elif isinstance(record, TradePlan):
            self._plans[(record.candidate_id, record.entry_style.value, record.variant)] = record
        elif isinstance(record, RiskDecision):
            self._risk[record.producer] = record
        return True

    def open_orders(self) -> tuple[OrderEvent, ...]:
        """Newest event of every order that is not in a terminal state."""
        return tuple(
            event for event in self._orders.values() if event.order_state not in TERMINAL_ORDER_STATES
        )
