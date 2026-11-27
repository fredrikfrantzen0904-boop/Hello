"""Service interfaces between subsystems (behavioural contracts).

These ``Protocol`` classes are the only way one subsystem may call another.
They are deliberately small: data flows as contract records, and each interface
names who implements it and who may call it.  Implementations are delivered by
the owning session; nothing here contains behaviour.

All calls are synchronous and must not block on the network, on a model that is
being trained or on the dashboard (G03).  Anything slow happens in a background
task that publishes records.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from gie.contracts._spec import Record
from gie.contracts.model_version import ModelKind, ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.routing_lock import ExecutionVenue


@runtime_checkable
class TickSink(Protocol):
    """Anything that consumes ticks in stream order (implemented by 02, 03, 06, 07, 10)."""

    def on_tick(self, tick: Tick) -> None: ...


@runtime_checkable
class RecordSink(Protocol):
    """Append-only destination for contract records (journal, bus, dashboard feed).

    ``publish`` must be idempotent per ``(stream, seq)``: publishing the same
    record twice has no further effect, publishing different content at the same
    position raises :class:`~gie.core.errors.SequenceConflictError`.
    """

    def publish(self, record: Record) -> None: ...


@runtime_checkable
class RiskGate(Protocol):
    """Risk engine as seen by decision and execution (implemented by 08).

    ``evaluate`` atomically reserves the granted risk; concurrent evaluations
    can never jointly exceed the available budget.  ``release`` returns an
    unused reservation; it is idempotent.
    """

    def evaluate(self, plan: TradePlan) -> RiskDecision: ...

    def release(self, reservation_id: str) -> None: ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Order endpoint as seen by the execution engine (implemented by 07).

    Only simulated adapters exist in this project phase: ``venue`` is
    ``ExecutionVenue.SIMULATED`` and ``sends_real_orders`` is ``False``, and the
    class must be admitted with
    :func:`gie.core.routing_lock.register_simulated_adapter`.  An execution
    engine never calls an adapter directly: it wraps it in
    :class:`gie.contracts.guarded.GuardedExecutionAdapter`, which authorises the
    adapter with the routing guard before every call.

    ``submit`` takes the ``SUBMIT_REQUESTED`` event of a new order, ``cancel``
    and ``replace`` the corresponding ``*_REQUESTED`` event.  All three are
    idempotent per ``order_event_id``.  Resulting events are returned by ``poll``.
    """

    venue: ExecutionVenue
    sends_real_orders: bool

    def submit(self, request: OrderEvent) -> None: ...

    def cancel(self, request: OrderEvent) -> None: ...

    def replace(self, request: OrderEvent) -> None: ...

    def on_tick(self, tick: Tick) -> None: ...

    def poll(self) -> Sequence[OrderEvent]: ...

    def open_positions(self) -> Sequence[Position]: ...


@runtime_checkable
class ModelRegistry(Protocol):
    """Read access to model versions (implemented by 09; read by 04, 05, 08, 11).

    ``get`` raises :class:`KeyError` for an unknown id; callers must then treat
    the model as *not validated* (fail closed).
    """

    def get(self, model_version_id: str) -> ModelVersion: ...

    def active(self, kind: ModelKind) -> ModelVersion | None: ...
