"""Service interfaces between subsystems (behavioural contracts).

These ``Protocol`` classes are the only way one subsystem may call another.
Data flows as contract records; each interface names who implements it and who
may call it.  Implementations are delivered by the owning session; nothing here
contains behaviour.

**Run loop (owned by session 10 for replay, by the composition root for paper;
both use the same step).**  One step processes one input - a tick, or a timer
that is due:

1. the loop sets step time to the instant at which the input became available
   (``ReplayClock.advance_to`` / ``PaperClock.begin_step``); timers that are due
   at or before that instant fire first, each as a step of its own;
2. the input is journaled and delivered to the tick sinks in the fixed order
   *protective* - simulated broker first, then position keeping, then the
   reflex engine - *tactical* (risk, decision, execution) - *strategic*
   (evidence, prediction);
3. every record a component publishes during the step is journaled at once
   and then delivered to the record listeners (:class:`RecordListener`) in the
   same tier order, before the component that published it continues with its
   next record; records published by listeners are handled the same way until
   nothing new is published (the loop bounds the number of rounds and fails the
   step if the bound is hit);
4. the step ends with ``journal.commit()``; only then is the next input taken.

Components see **step time**: the clock they are handed does not move during a
step, in paper exactly as in replay, so the records of a step carry the same
instant in both modes.  A sink or listener that raises is marked failed and may
create no risk until it has recovered; the others still receive the input.

**Blocking.**  All calls are synchronous and must not wait for the network, a
model that is being trained or the dashboard (G03).  Work that takes long -
model inference in the background, anything strategic in paper - runs outside
the loop; its result is handed to the loop, which publishes it as a record in a
later step, stamped with that step's instant and with the market-data watermark
the computation really used.  A late result can make evidence stale (which the
freshness rules then reject); it can never delay a protective decision.  In
replay the same result is delivered through the scheduler at a virtual instant,
never "when a thread finishes".  Measured latency (``gie.core.latency``) is
evidence only and never enters a record.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.guarded import PollResult
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

    def commit(self) -> None:
        """End of a step: make everything published so far durable."""
        ...


@runtime_checkable
class RecordListener(Protocol):
    """Anything that consumes records of other subsystems during a step.

    The run loop delivers every journaled record to the listeners that
    subscribed to its contract, in tier order (protective, tactical, strategic),
    within the step in which it was published.  This is how evidence reaches
    prediction, candidates reach decision, and order events and positions reach
    the reflex and risk engines - no subsystem calls another to hand over a
    record.  ``accepts`` names the contracts a listener wants.
    """

    def accepts(self) -> tuple[type[Record], ...]: ...

    def on_record(self, record: Record) -> None: ...


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
    evaluations within one step can never jointly exceed the available budget
    (the ``pending_risk_amount`` of the second decision includes the first
    reservation), and a second plan of an alternative group is not approved
    while the first holds a reservation.  ``release`` returns an unused
    reservation; it is idempotent.  The risk engine learns what became of a
    reservation from the order events of the entry order that cites the
    decision.  A record of the life of a reservation (reserved, consumed,
    released, expired) is a contract extension proposed by session 08.
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
    idempotent per ``order_event_id``: a request that is sent again (after a
    restart, for example) has no further effect.  Resulting events are returned
    by ``poll``, each once its receive instant has been reached: an adapter that
    models latency holds reports back and schedules its own wake-up through the
    run's scheduler; it never returns a report at the instant of the request it
    answers.  ``poll`` also returns the first event of the orders the broker
    creates itself (originator BROKER): the protective stop and take profit
    attached to an entry, at the entry's first fill, and broker-side closes.
    A reduce-only order never fills beyond the open volume of its position;
    the remainder is cancelled.
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
    """What the execution engine depends on to place orders (``GuardedExecutionAdapter``).

    The gateway finds the plan and risk approval of an entry, and the position
    of every other order, in the run ledger; nothing is taken from the caller.
    It refuses anything that is not authorised (``gie.contracts.authorization``)
    or that the run journal refuses, journals the request and only then calls
    the adapter.
    """

    def submit(self, request: OrderEvent) -> None: ...

    def cancel(self, request: OrderEvent) -> None: ...

    def replace(self, request: OrderEvent) -> None: ...

    def on_tick(self, tick: Tick) -> None: ...

    def poll(self) -> PollResult: ...

    def open_positions(self) -> tuple[Position, ...]: ...


@runtime_checkable
class ProtectionPort(Protocol):
    """What the reflex engine asks of the execution engine (implemented by 07, called by 06).

    Order events have one writer, the execution engine.  The reflex engine
    decides per tick and states its intent here; execution builds, numbers,
    journals and sends the order events.  Both calls are synchronous and
    in-process and return without waiting for the simulated broker.

    ``tighten_stop`` asks for a tighter protective stop.  While a replacement is
    outstanding the execution engine keeps only the newest target and sends it
    when the outstanding one has been answered.  ``exit`` closes volume at once
    (a MARKET EXIT order) when protection cannot wait.  The return value is the
    ``client_order_id`` of the order that carries the intent.
    """

    def tighten_stop(self, position_id: str, trigger_price: Decimal, reason_code: str) -> str: ...

    def exit(self, position_id: str, volume_lots: Decimal, reason_code: str) -> str: ...


@runtime_checkable
class ModelRegistry(Protocol):
    """Model versions and their promotion history (implemented by 09).

    Reads (04, 05, 08, 11): ``get`` returns the newest registry entry of a
    version and raises :class:`KeyError` for an unknown id - callers must then
    treat the model as *not validated* (fail closed).  ``active`` is keyed by
    model family ``(kind, model_name)``.  ``status_at`` answers which status a
    version had at a past instant, so a decision can be re-checked later.
    A run does not follow the registry while it runs: it uses the statuses its
    manifest pinned, so a promotion or rollback takes effect in a new run.

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
