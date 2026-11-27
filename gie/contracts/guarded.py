"""Order gateway: guarded access to execution adapters (routing lock, layer 3).

The execution engine never holds a raw adapter.  It holds a
:class:`GuardedExecutionAdapter`, which for *every* call

1. authorises the adapter with the :class:`~gie.core.routing_lock.RoutingGuard`
   (registered simulated class, exact type, no instance attributes, methods
   unchanged since registration) **before anything else** - nothing is checked,
   journaled or sent for an adapter the guard refuses - and later calls the
   adapter only through the functions that were registered, after authorising
   it once more;
2. checks that the request is a genuine :class:`OrderEvent` (exact type) for the
   simulated venue and the guard's run mode;
3. takes the plan, the risk approval and the position the request refers to
   **from the run ledger** - never from the caller - and checks that the request
   is authorised by them (``gie.contracts.authorization``);
4. publishes the request to the run journal **before** the adapter sees it
   (write-ahead).  The journal applies the sequence, reference and aggregate
   rules of the run, so a request that breaks one never reaches the adapter;
5. forwards the request.  A request that is already journaled is forwarded
   again: after a crash the engine re-sends what it is unsure about, and the
   adapter is idempotent per ``order_event_id``.

Reports that come back from the adapter are checked the same way and published
to the journal one by one, so that one faulty or repeated report costs nothing
but itself (:class:`PollResult`).

One run has one journal with one ledger; the gateway has no state of its own.
Because every request is journaled before it is sent, an order of GIE that the
journal does not know cannot exist at the simulated broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, final

from gie.contracts.authorization import (
    validate_entry_authorization,
    validate_protective_cancel,
    validate_reducing_order,
)
from gie.contracts.journal import MemoryJournal
from gie.contracts.order_event import OrderEvent, OrderEventType, OrderIntent, OrderOriginator
from gie.contracts.position import Position
from gie.contracts.run import RunManifest
from gie.contracts.tick import Tick
from gie.core.clock import Clock
from gie.core.errors import (
    ConfigError,
    ContractValidationError,
    FutureDataError,
    GieError,
    IllegalTransitionError,
    LiveRoutingLockedError,
)
from gie.core.routing_lock import ExecutionVenue, RoutingGuard, RunMode, is_simulated_venue


@final
@dataclass(frozen=True, slots=True)
class PollResult:
    """What one poll of the simulated broker yielded.

    ``events``: the reports that were new, valid and are now part of the journal,
    in order.  ``duplicates``: reports that were already journaled (delivered
    again) and were dropped.  ``errors``: one error per report that was refused;
    a refused report is not journaled and is not in ``events``.  Reports behind
    a refused one are still processed, so nothing that is valid is lost and
    nothing that was journaled is withheld.  An engine that sees errors must
    treat the affected orders as unknown and reconcile them.
    """

    events: tuple[OrderEvent, ...]
    duplicates: int
    errors: tuple[GieError, ...]

    @property
    def clean(self) -> bool:
        return not self.errors


@final
class GuardedExecutionAdapter:
    """The only object through which an execution engine may reach an execution adapter."""

    __slots__ = ("_adapter", "_clock", "_guard", "_journal")

    def __init__(self, adapter: object, guard: RoutingGuard, *, clock: Clock, journal: MemoryJournal) -> None:
        if type(guard) is not RoutingGuard:
            raise LiveRoutingLockedError("a genuine RoutingGuard is required to reach an execution adapter")
        if not isinstance(journal, MemoryJournal):
            raise ConfigError("the order gateway needs the journal of its run")
        if not isinstance(clock, Clock):
            raise ConfigError("the order gateway needs the clock of its run")
        manifest = journal.manifest
        if manifest is None or manifest.mode is not guard.mode:
            raise ConfigError("the journal of the gateway belongs to a run of the guard's mode")
        RoutingGuard.authorized_methods(guard, adapter)
        self._adapter: Final[object] = adapter
        self._guard: Final[RoutingGuard] = guard
        self._clock: Final[Clock] = clock
        self._journal: Final[MemoryJournal] = journal

    @classmethod
    def create(
        cls,
        adapter_type: type,
        guard: RoutingGuard,
        *,
        clock: Clock,
        journal: MemoryJournal,
        **adapter_arguments: Any,
    ) -> GuardedExecutionAdapter:
        """Instantiate the adapter inside the gateway, so no unguarded reference to it exists."""
        return cls(adapter_type(**adapter_arguments), guard, clock=clock, journal=journal)

    @property
    def venue(self) -> ExecutionVenue:
        return ExecutionVenue.SIMULATED

    @property
    def mode(self) -> RunMode:
        return self._guard.mode

    # ------------------------------------------------------------------ checks
    def _authorise_adapter(self) -> None:
        """Routing lock first: nothing is checked, journaled or sent for an adapter the guard refuses."""
        RoutingGuard.authorized_methods(self._guard, self._adapter)

    def _call(self, method: str, *arguments: object) -> Any:
        """Authorise the adapter (again), then call the function that was registered for *method*."""
        methods = RoutingGuard.authorized_methods(self._guard, self._adapter)
        return methods[method](self._adapter, *arguments)

    def _manifest(self) -> RunManifest:
        manifest = self._journal.manifest
        assert manifest is not None  # checked at construction; a journal never loses its manifest
        return manifest

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

    def _position(self, event: OrderEvent) -> Position:
        position = None if event.position_id is None else self._journal.ledger.position(event.position_id)
        if position is None:
            raise IllegalTransitionError(
                "a non-entry order needs its position in the run journal",
                client_order_id=event.client_order_id,
                rule="the position of a PROTECTIVE_STOP, TAKE_PROFIT or EXIT order is journaled first",
            )
        return position

    def _authorise_reducing(self, event: OrderEvent) -> None:
        ledger = self._journal.ledger
        position = self._position(event)
        validate_reducing_order(
            position,
            event,
            protective=ledger.protective_order(position.position_id),
            other_reducing_lots=ledger.reducing_volume(position.position_id, excluding=event.client_order_id),
        )

    # ------------------------------------------------------------------ requests
    def submit(self, request: OrderEvent) -> None:
        """Authorise, journal and forward the first event of an order of the execution engine."""
        self._authorise_adapter()
        event = self._request(request, OrderEventType.SUBMIT_REQUESTED)
        if event.originator is not OrderOriginator.EXECUTION:
            raise IllegalTransitionError(
                "only orders of the execution engine are submitted through the gateway",
                client_order_id=event.client_order_id,
            )
        ledger = self._journal.ledger
        if event.intent is OrderIntent.ENTRY:
            assert event.plan_id is not None
            assert event.risk_decision_id is not None
            plan = ledger.plan(event.plan_id)
            decision = ledger.decision(event.risk_decision_id)
            if plan is None or decision is None:
                raise IllegalTransitionError(
                    "an entry order needs its plan and its risk decision in the run journal",
                    client_order_id=event.client_order_id,
                    rule="plan and risk decision of an ENTRY order are journaled first",
                )
            if ledger.newest_plan(plan.plan_id) is not plan:
                raise IllegalTransitionError(
                    "a newer revision of the plan exists; its approval is void",
                    client_order_id=event.client_order_id,
                    rule="an entry is built from the newest revision of its plan",
                )
            validate_entry_authorization(
                plan, decision, event, now_ns=self._clock.now_ns(), role=self._manifest().role
            )
        else:
            self._authorise_reducing(event)
        self._journal.publish(event)
        self._call("submit", event)

    def cancel(self, request: OrderEvent) -> None:
        self._authorise_adapter()
        event = self._request(request, OrderEventType.CANCEL_REQUESTED)
        if event.intent is OrderIntent.PROTECTIVE_STOP:
            validate_protective_cancel(self._position(event), event)
        self._journal.publish(event)
        self._call("cancel", event)

    def replace(self, request: OrderEvent) -> None:
        self._authorise_adapter()
        event = self._request(request, OrderEventType.REPLACE_REQUESTED)
        if event.intent is not OrderIntent.ENTRY:
            self._authorise_reducing(event)
        self._journal.publish(event)
        self._call("replace", event)

    # ------------------------------------------------------------------ market data and reports
    def on_tick(self, tick: Tick) -> None:
        if type(tick) is not Tick:
            raise ContractValidationError("on_tick expects a Tick", actual_type=type(tick).__name__)
        self._call("on_tick", tick)

    def _admit_report(self, item: object) -> OrderEvent | None:
        """Journal one event that came out of the adapter; ``None`` if it was already journaled."""
        event = self._check_event(item, "adapter event")
        if event.is_request and not (
            event.originator is OrderOriginator.BROKER and event.event_type is OrderEventType.SUBMIT_REQUESTED
        ):
            raise IllegalTransitionError(
                "an adapter reports broker events; the only request it issues is the first event of "
                "an order it creates itself (originator BROKER)",
                client_order_id=event.client_order_id,
            )
        now_ns = self._clock.now_ns()
        if event.receive_time_ns > now_ns:
            raise FutureDataError(
                "a report is delivered at the instant it is observed, not before",
                client_order_id=event.client_order_id,
                receive_time_ns=event.receive_time_ns,
                now_ns=now_ns,
            )
        before = len(self._journal)
        self._journal.publish(event)
        return event if len(self._journal) > before else None

    def poll(self) -> PollResult:
        """Reports of the simulated broker, each checked and journaled on its own.

        An adapter that models latency holds a report back until its receive
        instant and schedules its own wake-up through the run's scheduler; the
        gateway refuses a report that lies in the future.  A report for another
        run mode or venue is refused like any other faulty report: it is not
        journaled, its lock error is returned in ``errors``, and the reports
        behind it are still processed.  (An adapter the routing guard refuses is
        never polled at all: that error is raised.)
        """
        events: list[OrderEvent] = []
        errors: list[GieError] = []
        duplicates = 0
        for item in tuple(self._call("poll")):
            try:
                admitted = self._admit_report(item)
            except GieError as error:
                errors.append(error)
                continue
            if admitted is None:
                duplicates += 1
            else:
                events.append(admitted)
        return PollResult(tuple(events), duplicates, tuple(errors))

    def open_positions(self) -> tuple[Position, ...]:
        positions = tuple(self._call("open_positions"))
        for position in positions:
            if type(position) is not Position:
                raise ContractValidationError(
                    "adapter returned a non-Position", actual_type=type(position).__name__
                )
        return positions
