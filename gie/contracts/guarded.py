"""Order gateway: guarded access to execution adapters (routing lock, layer 3).

An execution engine never holds a raw adapter.  It holds a
:class:`GuardedExecutionAdapter`, which before *every* call

1. authorises the adapter with the :class:`~gie.core.routing_lock.RoutingGuard`
   (registered simulated class, exact type, no instance attributes, methods
   unchanged since registration) and then calls the adapter only through the
   functions that were registered;
2. checks that the request is a genuine :class:`OrderEvent` (exact type) for the
   simulated venue and the guard's run mode;
3. checks that the request is authorised - an entry against its selected plan
   and its unexpired risk approval, every other order against the open position
   it may only reduce (``gie.contracts.authorization``);
4. checks the sequence rules of the order (``gie.contracts.ledger``).

Events that come back from the adapter pass the same type, venue and sequence
checks before the engine sees them.  Forgetting a check is therefore not
possible for code that follows the interface: there is no other way to reach
the adapter.
"""

from __future__ import annotations

from typing import Any, Final, final

from gie.contracts.authorization import (
    validate_entry_authorization,
    validate_protective_cancel,
    validate_reducing_order,
)
from gie.contracts.ledger import LifecycleLedger
from gie.contracts.order_event import OrderEvent, OrderEventType, OrderIntent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.clock import Clock
from gie.core.errors import ContractValidationError, IllegalTransitionError, LiveRoutingLockedError
from gie.core.routing_lock import ExecutionVenue, RoutingGuard, RunMode, is_simulated_venue


@final
class GuardedExecutionAdapter:
    """The only object through which an execution engine may reach an execution adapter."""

    __slots__ = ("_adapter", "_clock", "_guard", "_ledger")

    def __init__(
        self, adapter: object, guard: RoutingGuard, *, clock: Clock, ledger: LifecycleLedger
    ) -> None:
        if type(guard) is not RoutingGuard:
            raise LiveRoutingLockedError("a genuine RoutingGuard is required to reach an execution adapter")
        if type(ledger) is not LifecycleLedger:
            raise LiveRoutingLockedError("the order gateway needs the run's LifecycleLedger")
        RoutingGuard.authorized_methods(guard, adapter)
        self._adapter: Final[object] = adapter
        self._guard: Final[RoutingGuard] = guard
        self._clock: Final[Clock] = clock
        self._ledger: Final[LifecycleLedger] = ledger

    @classmethod
    def create(
        cls,
        adapter_type: type,
        guard: RoutingGuard,
        *,
        clock: Clock,
        ledger: LifecycleLedger,
        **adapter_arguments: Any,
    ) -> GuardedExecutionAdapter:
        """Instantiate the adapter inside the gateway, so no unguarded reference to it exists."""
        return cls(adapter_type(**adapter_arguments), guard, clock=clock, ledger=ledger)

    @property
    def venue(self) -> ExecutionVenue:
        return ExecutionVenue.SIMULATED

    @property
    def mode(self) -> RunMode:
        return self._guard.mode

    # ------------------------------------------------------------------ checks
    def _call(self, method: str, *arguments: object) -> Any:
        """Authorise the adapter, then call the function that was registered for *method*."""
        methods = RoutingGuard.authorized_methods(self._guard, self._adapter)
        return methods[method](self._adapter, *arguments)

    def _check_event(self, event: object, what: str) -> OrderEvent:
        if type(event) is not OrderEvent:
            raise ContractValidationError(f"{what} must be an OrderEvent", actual_type=type(event).__name__)
        if not is_simulated_venue(event.venue):
            raise LiveRoutingLockedError(f"{what} is not for the simulated venue")
        if event.mode is not self.mode:
            raise LiveRoutingLockedError(
                f"{what} belongs to another run mode",
                event_mode=str(getattr(event.mode, "value", event.mode)),
                guard_mode=self.mode.value,
            )
        return event

    def _request(self, request: object, expected: OrderEventType) -> OrderEvent:
        event = self._check_event(request, "request")
        if event.event_type is not expected:
            raise IllegalTransitionError(
                "wrong request type for this gateway call",
                expected=expected.value,
                actual=event.event_type.value,
            )
        return event

    @staticmethod
    def _position_for(event: OrderEvent, position: Position | None) -> Position:
        if type(position) is not Position:
            raise IllegalTransitionError(
                "a non-entry order needs the position it belongs to",
                client_order_id=event.client_order_id,
                rule="position is required for PROTECTIVE_STOP, TAKE_PROFIT and EXIT orders",
            )
        return position

    # ------------------------------------------------------------------ requests
    def submit(
        self,
        request: OrderEvent,
        *,
        plan: TradePlan | None = None,
        decision: RiskDecision | None = None,
        position: Position | None = None,
    ) -> None:
        event = self._request(request, OrderEventType.SUBMIT_REQUESTED)
        if event.intent is OrderIntent.ENTRY:
            if type(plan) is not TradePlan or type(decision) is not RiskDecision:
                raise IllegalTransitionError(
                    "an entry order needs its plan and its risk decision",
                    client_order_id=event.client_order_id,
                    rule="plan and decision are required for ENTRY orders",
                )
            validate_entry_authorization(plan, decision, event, now_ns=self._clock.now_ns())
        else:
            validate_reducing_order(self._position_for(event, position), event)
        if not self._ledger.check(event):
            return  # identical repeat of a request that is already known: idempotent
        self._call("submit", event)
        self._ledger.admit(event)

    def cancel(self, request: OrderEvent, *, position: Position | None = None) -> None:
        event = self._request(request, OrderEventType.CANCEL_REQUESTED)
        if event.intent is OrderIntent.PROTECTIVE_STOP:
            validate_protective_cancel(self._position_for(event, position), event)
        if not self._ledger.check(event):
            return
        self._call("cancel", event)
        self._ledger.admit(event)

    def replace(self, request: OrderEvent, *, position: Position | None = None) -> None:
        event = self._request(request, OrderEventType.REPLACE_REQUESTED)
        if event.intent is not OrderIntent.ENTRY:
            validate_reducing_order(self._position_for(event, position), event)
        if not self._ledger.check(event):
            return
        self._call("replace", event)
        self._ledger.admit(event)

    # ------------------------------------------------------------------ market data and reports
    def on_tick(self, tick: Tick) -> None:
        if type(tick) is not Tick:
            raise ContractValidationError("on_tick expects a Tick", actual_type=type(tick).__name__)
        self._call("on_tick", tick)

    def poll(self) -> tuple[OrderEvent, ...]:
        """Reports of the simulated broker, each checked and admitted to the ledger in order.

        If a report breaks a sequence rule the error propagates (fail closed);
        the reports before it have been admitted, the faulty one and its
        successors have not.
        """
        events = tuple(self._check_event(event, "adapter event") for event in self._call("poll"))
        for event in events:
            if event.is_request:
                raise IllegalTransitionError(
                    "an adapter reports broker events, it does not issue requests",
                    client_order_id=event.client_order_id,
                )
            self._ledger.admit(event)
        return events

    def open_positions(self) -> tuple[Position, ...]:
        positions = tuple(self._call("open_positions"))
        for position in positions:
            if type(position) is not Position:
                raise ContractValidationError(
                    "adapter returned a non-Position", actual_type=type(position).__name__
                )
        return positions
