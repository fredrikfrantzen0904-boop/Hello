"""Guarded access to execution adapters (routing lock, layer 3).

An execution engine never holds a raw adapter.  It holds a
:class:`GuardedExecutionAdapter`, which authorises the adapter with the
:class:`~gie.core.routing_lock.RoutingGuard` before *every* call and checks that
each request and each returned event is a simulated-venue :class:`OrderEvent` of
the guard's run mode.  Forgetting the guard is therefore not possible for code
that follows the interface: there is no other way to reach the adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, final

from gie.contracts.interfaces import ExecutionAdapter
from gie.contracts.order_event import OrderEvent, OrderEventType
from gie.contracts.position import Position
from gie.contracts.tick import Tick
from gie.core.errors import ContractValidationError, IllegalTransitionError, LiveRoutingLockedError
from gie.core.routing_lock import ExecutionVenue, RoutingGuard, RunMode


@final
class GuardedExecutionAdapter:
    """The only object through which an execution engine may reach an execution adapter."""

    __slots__ = ("_adapter", "_guard")

    def __init__(self, adapter: ExecutionAdapter, guard: RoutingGuard) -> None:
        if not isinstance(guard, RoutingGuard):
            raise LiveRoutingLockedError("a RoutingGuard is required to reach an execution adapter")
        guard.authorize(adapter)
        self._adapter: Final[ExecutionAdapter] = adapter
        self._guard: Final[RoutingGuard] = guard

    @property
    def venue(self) -> ExecutionVenue:
        return ExecutionVenue.SIMULATED

    @property
    def mode(self) -> RunMode:
        return self._guard.mode

    def _check_event(self, event: object, what: str) -> OrderEvent:
        if not isinstance(event, OrderEvent):
            raise ContractValidationError(f"{what} must be an OrderEvent", actual_type=type(event).__name__)
        if event.venue is not ExecutionVenue.SIMULATED:
            raise LiveRoutingLockedError(f"{what} is not for the simulated venue")
        if event.mode is not self._guard.mode:
            raise LiveRoutingLockedError(
                f"{what} belongs to another run mode",
                event_mode=event.mode.value,
                guard_mode=self._guard.mode.value,
            )
        return event

    def _request(self, request: object, expected: OrderEventType) -> OrderEvent:
        self._guard.authorize(self._adapter)
        event = self._check_event(request, "request")
        if event.event_type is not expected:
            raise IllegalTransitionError(
                "wrong request type for this adapter call",
                expected=expected.value,
                actual=event.event_type.value,
            )
        return event

    def submit(self, request: OrderEvent) -> None:
        self._adapter.submit(self._request(request, OrderEventType.SUBMIT_REQUESTED))

    def cancel(self, request: OrderEvent) -> None:
        self._adapter.cancel(self._request(request, OrderEventType.CANCEL_REQUESTED))

    def replace(self, request: OrderEvent) -> None:
        self._adapter.replace(self._request(request, OrderEventType.REPLACE_REQUESTED))

    def on_tick(self, tick: Tick) -> None:
        self._guard.authorize(self._adapter)
        if not isinstance(tick, Tick):
            raise ContractValidationError("on_tick expects a Tick", actual_type=type(tick).__name__)
        self._adapter.on_tick(tick)

    def poll(self) -> tuple[OrderEvent, ...]:
        self._guard.authorize(self._adapter)
        return tuple(self._check_event(event, "adapter event") for event in self._adapter.poll())

    def open_positions(self) -> Sequence[Position]:
        self._guard.authorize(self._adapter)
        positions = tuple(self._adapter.open_positions())
        for position in positions:
            if not isinstance(position, Position):
                raise ContractValidationError(
                    "adapter returned a non-Position", actual_type=type(position).__name__
                )
        return positions
