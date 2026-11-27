"""Service interfaces between subsystems (behavioural contracts).

These ``Protocol`` classes are the only way one subsystem may call another.
Data flows as contract records; each interface names who implements it and who
may call it.  Implementations are delivered by the owning session; nothing here
contains behaviour.

**Run loop (owned by session 10 for replay, by the composition root for
paper).**  One step is: (1) fire the timers that are due at or before the
availability instant of the next input (``Scheduler``); (2) advance the clock to
that instant (replay); (3) deliver the input to the sinks in the fixed order
*protective* (reflex, simulated broker) -> *tactical* (risk, decision,
execution) -> *strategic* (evidence, prediction); (4) publish every record
produced in the step to the journal before the next input is taken.  A sink that
raises is marked failed and may create no risk until it has recovered; the
other sinks still receive the input.

**Blocking.**  All calls are synchronous and must not wait for the network, a
model that is being trained or the dashboard (G03).  In replay, strategic
consumers run inside the step.  In paper they are fed through a bounded queue
and publish their results as records at a later instant; a late result can make
evidence stale (which freshness rules then reject) but can never delay a
protective decision.  In replay, background results and modelled decision
latency are delivered through the scheduler at a virtual instant, never "when a
thread finishes"; measured latency (``gie.core.latency``) is evidence only and
never enters a record.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.journal import JournalEntry
from gie.contracts.model_version import ModelKind, ModelStatus, ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import PlanEstimate, Target, TradePlan
from gie.core.routing_lock import ExecutionVenue


@runtime_checkable
class TickSink(Protocol):
    """Anything that consumes ticks in stream order (02, 03, 04, 05, 06, 07, 08, 10)."""

    def on_tick(self, tick: Tick) -> None: ...


@runtime_checkable
class TickSource(Protocol):
    """Ordered market-data input of a run (implemented by 01; driven by 10 in replay).

    ``ticks`` yields the ticks of the given streams whose receive time lies in
    ``[start_ns, end_ns)`` in ascending ``ordering_key`` order, deterministically.
    Queries for derived data (candles) are added together with their contracts.
    """

    def ticks(self, streams: Sequence[str], start_ns: int, end_ns: int) -> Iterator[Tick]: ...


@runtime_checkable
class RecordSink(Protocol):
    """Append-only destination for contract records (the run journal).

    ``publish`` returns the record's position in the total order of the run and
    is idempotent per ``(stream, seq)``: publishing the same record twice has no
    further effect; different content at the same position raises
    :class:`~gie.core.errors.SequenceConflictError`.  A sink belongs to one run.
    """

    def publish(self, record: Record) -> int: ...


@runtime_checkable
class RecordSource(Protocol):
    """Read access to a run's journal in total order (09, 10, 11, 12)."""

    @property
    def run_id(self) -> str | None: ...

    def read(self, from_journal_seq: int = 0) -> Iterator[JournalEntry]: ...

    def streams(self) -> tuple[str, ...]: ...


@runtime_checkable
class PlanScorer(Protocol):
    """Plan-level estimates (implemented by 04, called by 05).

    Returns the estimate for one concrete geometry of a candidate.  Inference
    only: no training, no network, no blocking (G03-05, G03-06).  Returns
    ``None`` when no model can score the plan; such a plan cannot be selected.
    """

    def score_plan(
        self,
        candidate: Candidate,
        *,
        entry_price: Decimal,
        stop_price: Decimal,
        targets: Sequence[Target],
    ) -> PlanEstimate | None: ...


@runtime_checkable
class RiskGate(Protocol):
    """Risk engine as seen by decision and execution (implemented by 08).

    ``evaluate`` takes a SELECTED plan and atomically reserves the granted risk:
    concurrent evaluations can never jointly exceed the available budget, and a
    second plan of an alternative group is not approved while the first holds a
    reservation.  ``release`` returns an unused reservation; it is idempotent.
    """

    def evaluate(self, plan: TradePlan) -> RiskDecision: ...

    def release(self, reservation_id: str) -> None: ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Simulated order endpoint (implemented by 07).

    Only simulated adapters exist in this project phase.  The class declares
    ``venue = ExecutionVenue.SIMULATED`` and ``sends_real_orders = False``,
    defines ``__slots__`` and is admitted with
    :func:`gie.core.routing_lock.register_simulated_adapter`.  No component calls
    an adapter directly: the execution engine holds an :class:`OrderGateway`
    (``gie.contracts.guarded.GuardedExecutionAdapter``), which authorises the
    adapter and the request before every call.

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
class OrderGateway(Protocol):
    """What the execution and reflex engines depend on to place orders.

    An entry needs its plan and its risk approval; every other order needs the
    position it belongs to.  The gateway refuses anything that is not authorised
    (``gie.contracts.authorization``) or that breaks a sequence rule
    (``gie.contracts.ledger``) before the adapter is called.
    """

    def submit(
        self,
        request: OrderEvent,
        *,
        plan: TradePlan | None = None,
        decision: RiskDecision | None = None,
        position: Position | None = None,
    ) -> None: ...

    def cancel(self, request: OrderEvent, *, position: Position | None = None) -> None: ...

    def replace(self, request: OrderEvent, *, position: Position | None = None) -> None: ...

    def on_tick(self, tick: Tick) -> None: ...

    def poll(self) -> tuple[OrderEvent, ...]: ...

    def open_positions(self) -> tuple[Position, ...]: ...


@runtime_checkable
class ModelRegistry(Protocol):
    """Model versions and their promotion history (implemented by 09).

    Reads (04, 05, 08, 11): ``get`` returns the newest registry entry of a
    version and raises :class:`KeyError` for an unknown id - callers must then
    treat the model as *not validated* (fail closed).  ``active`` is keyed by
    model family ``(kind, model_name)``.  ``status_at`` answers which status a
    version had at a past instant, so a decision can be re-checked later.

    Writes (04 registers artifacts; 09 changes status): ``register`` adds a
    RESEARCH entry, ``transition`` appends a status change.  Both validate with
    ``validate_registry_history`` and refuse an activation whose cited evidence
    (report, shadow runs, approved decision-log entry) does not exist.
    """

    def get(self, model_version_id: str) -> ModelVersion: ...

    def active(self, kind: ModelKind, model_name: str) -> ModelVersion | None: ...

    def find(
        self,
        *,
        kind: ModelKind | None = None,
        status: ModelStatus | None = None,
        model_name: str | None = None,
    ) -> Sequence[ModelVersion]: ...

    def history(self, model_version_id: str) -> Sequence[ModelVersion]: ...

    def status_at(self, model_version_id: str, time_ns: int) -> ModelStatus | None: ...

    def register(self, entry: ModelVersion) -> None: ...

    def transition(self, entry: ModelVersion) -> None: ...
