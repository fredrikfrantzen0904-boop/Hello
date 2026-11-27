"""Acceptance test S00-ACC-04 "paper-only routing lock": the order gateway (layer 3 of the lock).

Requirements: G08-03 (live lock), G08-10 (never enable real execution during this project phase),
M-AUTH-19 (keep real order routing disabled), OWN-14 (live execution remains disabled) and G08-07
(idempotent orders).

Expected behaviour is taken from the module docstrings of ``gie/contracts/guarded.py`` and
``gie/core/routing_lock.py``, from ``gie.contracts.interfaces`` (``ExecutionAdapter``,
``OrderGateway``) and from ``reviews/session_00/rework_2_change_notice.md`` sections 5 and 6 - not
from the implementation.  An execution engine reaches an adapter only through
``GuardedExecutionAdapter(adapter, guard, *, clock, journal)``, which for every call authorises
the adapter (registered simulated class, exact type, unchanged since registration), checks that the
request is a genuine ``OrderEvent`` of the guard's run mode, takes plan, approval and position
**from the run journal's ledger** (never from the caller), checks that the request is authorised,
publishes it to the journal **before** the adapter sees it (write-ahead) and then forwards it.
Reports coming back from the adapter are checked and journaled one by one (``PollResult``).

SYNTHETIC FIXTURE: the adapter classes defined in this module are inert in-memory fakes.  They
execute nothing, connect to nothing, hold no credentials and only remember how they were called.
All records are built with the labelled builders of ``tests/gie/samples.py``
(``DataOrigin.SYNTHETIC_FIXTURE``); none is market data, an owner decision or evidence of trading
performance.  Every test works on a private copy of the adapter registry, so no registration
leaks into other tests.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gc
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gie.contracts import (
    ExecutionVenue,
    FileJournal,
    GuardedExecutionAdapter,
    LifecycleLedger,
    MemoryJournal,
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
    ReplayClock,
    RiskDecision,
    RunManifest,
    RunMode,
    RunRole,
    Side,
    Tick,
    TradePlan,
    identity,
)
from gie.contracts.guarded import PollResult
from gie.contracts.ordering import ordering_time_ns
from gie.core import routing_lock
from gie.core.errors import (
    ConfigError,
    ContractValidationError,
    FutureDataError,
    GieError,
    IllegalTransitionError,
    LiveRoutingLockedError,
    StaleDataError,
)
from gie.core.routing_lock import RoutingGuard, register_simulated_adapter, registered_adapter_types
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

pytestmark = [
    pytest.mark.acceptance("S00-ACC-04"),
    pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-19", "OWN-14"),
]

E = OrderEventType
S = OrderState
T0 = samples.T0
#: After the sample request (T0 + 1 ms) and before the sample approval expires (T0 + 30 s).
NOW = T0 + 2 * NS_PER_MS
APPROVAL_EXPIRY = T0 + 30 * NS_PER_SECOND
_NEEDS_REASON = (E.REJECTED, E.CANCEL_REJECTED, E.REPLACE_REJECTED, E.RECONCILED)
_REQUEST_METHODS = ("submit", "cancel", "replace")
#: Index of the records of ``samples.run_story()`` (a coherent run of 28 records).
_STORY_ENTRY_INPUTS = 6  # manifest, tick, evidence, candidate, plan, approval
_STORY_HOLDING = 19  # ... up to the valued position after the entry filled (``valued``)


# --------------------------------------------------------------------------- fixture adapters
class _SimulatedAdapter:
    """SYNTHETIC FIXTURE: in-memory adapter that only records its calls and hands out queued reports."""

    __slots__ = ("calls", "positions", "reports")

    venue = ExecutionVenue.SIMULATED
    sends_real_orders = False

    def __init__(self, reports: tuple[Any, ...] = (), positions: tuple[Any, ...] = ()) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.reports: list[Any] = list(reports)
        self.positions: list[Any] = list(positions)

    def submit(self, request: OrderEvent) -> None:
        self.calls.append(("submit", request))

    def cancel(self, request: OrderEvent) -> None:
        self.calls.append(("cancel", request))

    def replace(self, request: OrderEvent) -> None:
        self.calls.append(("replace", request))

    def on_tick(self, tick: Tick) -> None:
        self.calls.append(("on_tick", tick))

    def poll(self) -> tuple[Any, ...]:
        self.calls.append(("poll", None))
        reports = tuple(self.reports)
        self.reports.clear()
        return reports

    def open_positions(self) -> tuple[Any, ...]:
        self.calls.append(("open_positions", None))
        return tuple(self.positions)


class _UnregisteredAdapter:
    """SYNTHETIC FIXTURE: declares everything a simulated adapter declares, but is never registered."""

    __slots__ = ("calls",)

    venue = ExecutionVenue.SIMULATED
    sends_real_orders = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def submit(self, request: OrderEvent) -> None:
        self.calls.append(("submit", request))

    def cancel(self, request: OrderEvent) -> None:
        self.calls.append(("cancel", request))

    def replace(self, request: OrderEvent) -> None:
        self.calls.append(("replace", request))

    def on_tick(self, tick: Tick) -> None:
        self.calls.append(("on_tick", tick))

    def poll(self) -> tuple[Any, ...]:
        self.calls.append(("poll", None))
        return ()

    def open_positions(self) -> tuple[Any, ...]:
        self.calls.append(("open_positions", None))
        return ()


class _SubclassedAdapter(_SimulatedAdapter):
    """SYNTHETIC FIXTURE: subclass of the registered adapter (its own class is not registered)."""

    __slots__ = ()

    def submit(self, request: OrderEvent) -> None:
        self.calls.append(("submit-overridden", request))


class _PermissiveGuard:
    """SYNTHETIC FIXTURE: looks like a RoutingGuard and approves every adapter."""

    mode = RunMode.REPLAY

    def authorized_methods(self, adapter: object) -> dict[str, Any]:
        return {name: getattr(type(adapter), name) for name in routing_lock.ADAPTER_METHODS}

    def authorize(self, adapter: object) -> None:
        return None


class _UnvalidatedOrderEvent(OrderEvent):
    """SYNTHETIC FIXTURE: an ``OrderEvent`` subclass that skips validation (so it can claim anything)."""

    __slots__ = ()

    def __post_init__(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _private_adapter_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fixture adapter in a private copy of the registry (restored after the test)."""
    private = dict(routing_lock._SIMULATED_ADAPTER_TYPES)
    monkeypatch.setattr(routing_lock, "_SIMULATED_ADAPTER_TYPES", private)
    register_simulated_adapter(_SimulatedAdapter)
    assert _SimulatedAdapter in registered_adapter_types()
    assert _UnregisteredAdapter not in registered_adapter_types()
    assert _SubclassedAdapter not in registered_adapter_types()


# --------------------------------------------------------------------------- run records
def _story() -> list[Record]:
    return samples.run_story()


def _paper_inputs() -> list[Record]:
    """The entry inputs of the sample story for a PAPER run (same content, the paper run's id)."""
    manifest = samples.run_manifest(
        mode=RunMode.PAPER,
        data_start_ns=None,
        data_end_ns=None,
        created_time_ns=T0 - 3600 * NS_PER_SECOND,
    )
    run = {"run_id": manifest.run_id, "mode": RunMode.PAPER}
    quote = samples.tick()
    evidence = samples.evidence(**run)
    candidate = samples.candidate(**run)
    plan = samples.trade_plan(**run)
    decision = samples.risk_decision(plan, **run)
    return [manifest, quote, evidence, candidate, plan, decision]


def _paper_order() -> OrderEvent:
    *_, plan, decision = _paper_inputs()
    assert isinstance(plan, TradePlan)
    assert isinstance(decision, RiskDecision)
    return samples.order_event(run_id=plan.run_id, mode=RunMode.PAPER, risk_decision_id=decision.decision_id)


def _journal(records: Sequence[Record]) -> MemoryJournal:
    journal = MemoryJournal()
    for record in records:
        journal.publish(record)
    return journal


# --------------------------------------------------------------------------- rig and builders
@dataclass
class _Rig:
    adapter: _SimulatedAdapter
    journal: MemoryJournal
    clock: ReplayClock
    gateway: GuardedExecutionAdapter

    @property
    def ledger(self) -> LifecycleLedger:
        return self.journal.ledger

    def requests(self) -> list[tuple[str, Any]]:
        """Order requests that reached the adapter, in order."""
        return [call for call in self.adapter.calls if call[0] in _REQUEST_METHODS]

    def journaled(self) -> list[Record]:
        return [entry.record for entry in self.journal.read()]

    def next_order_seq(self) -> int:
        """Next free position of the order event stream of the sample producer."""
        return sum(
            1
            for record in self.journaled()
            if isinstance(record, OrderEvent) and record.producer == "execution.sim"
        )

    def step(self, delta_ns: int = NS_PER_MS) -> int:
        """Move the run clock on (one step) and return the new instant."""
        return self.clock.advance_to(self.clock.now_ns() + delta_ns)

    def next(
        self, previous: OrderEvent, event_type: OrderEventType, order_state: OrderState, **overrides: Any
    ) -> OrderEvent:
        """The following event of the same order, numbered in the stream and stamped one step later."""
        moment = self.step()
        fields: dict[str, Any] = {
            "seq": self.next_order_seq(),
            "event_time_ns": moment,
            "receive_time_ns": moment,
        }
        fields.update(overrides)
        return _next(previous, event_type, order_state, **fields)

    def report(self, *reports: object) -> PollResult:
        self.adapter.reports.extend(reports)
        result = self.gateway.poll()
        assert type(result) is PollResult
        return result

    def drive(self, records: Sequence[Record]) -> None:
        """Feed story records through the run: requests through the gateway, reports through poll()."""
        for record in records:
            moment = ordering_time_ns(record)
            if moment > self.clock.now_ns():
                self.clock.advance_to(moment)
            if isinstance(record, OrderEvent):
                if record.is_request and not _created_by_broker(record):
                    getattr(self.gateway, _METHOD_OF[record.event_type])(record)
                else:
                    result = self.report(record)
                    assert result.clean, result.errors
                    assert result.events == (record,)
                continue
            self.journal.publish(record)
            if isinstance(record, Tick):
                self.gateway.on_tick(record)


def _created_by_broker(event: OrderEvent) -> bool:
    """The first event of an order the simulated broker creates itself (it enters through poll())."""
    return event.originator is OrderOriginator.BROKER and event.event_type is E.SUBMIT_REQUESTED


_METHOD_OF = {E.SUBMIT_REQUESTED: "submit", E.CANCEL_REQUESTED: "cancel", E.REPLACE_REQUESTED: "replace"}


def _rig(mode: object = "REPLAY", *, now_ns: int = NOW, records: Sequence[Record] | None = None) -> _Rig:
    """A gateway over a journal that holds the entry inputs (manifest .. risk approval) of the story."""
    if records is None:
        records = (
            _paper_inputs()
            if str(getattr(mode, "value", mode)) == "PAPER"
            else _story()[:_STORY_ENTRY_INPUTS]
        )
    adapter = _SimulatedAdapter()
    journal = _journal(records)
    clock = ReplayClock(now_ns)
    gateway = GuardedExecutionAdapter(adapter, RoutingGuard(mode), clock=clock, journal=journal)
    return _Rig(adapter, journal, clock, gateway)


def _holding_rig() -> tuple[_Rig, Position, OrderEvent]:
    """The story driven through the gateway until the entry has filled and the position is valued.

    Returns the rig, the newest position snapshot and the newest event of its working protective
    stop (created by the simulated broker and grown with the second fill).
    """
    story = _story()
    rig = _rig(records=story[:1], now_ns=ordering_time_ns(story[1]))
    rig.drive(story[1:_STORY_HOLDING])
    held = story[_STORY_HOLDING - 1]
    assert isinstance(held, Position)
    assert rig.ledger.position(held.position_id) is held
    stop = rig.ledger.protective_order(held.position_id)
    assert stop is not None
    assert (stop.originator, stop.order_state, stop.volume_lots) == (
        OrderOriginator.BROKER,
        S.WORKING,
        D("0.50"),
    )
    rig.adapter.calls.clear()
    rig.step(NS_PER_SECOND)
    return rig, held, stop


def _triple(**order_overrides: Any) -> tuple[TradePlan, RiskDecision, OrderEvent]:
    plan = samples.trade_plan()
    return plan, samples.risk_decision(plan), samples.order_event(**order_overrides)


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
        reason_code="FIXTURE_REASON"
        if event_type in _NEEDS_REASON
        else (previous.reason_code if previous.originator is OrderOriginator.BROKER else None),
    )
    if order_state not in (S.PENDING_REPLACE, S.PENDING_CANCEL):
        for name in ("requested_volume_lots", "requested_limit_price", "requested_trigger_price"):
            fields[name] = None
        fields["requested_valid_until_ns"] = None
    if previous.broker_order_id is None and not event_type.value.endswith("_REQUESTED"):
        fields["broker_order_id"] = identity.broker_order_id(previous.client_order_id)
    fields.update(overrides)
    fields["order_event_id"] = identity.order_event_id(fields["client_order_id"], fields["order_seq"])
    return OrderEvent(**fields)


def _child(
    rig: _Rig, position: Position, intent: OrderIntent, order_type: OrderType, **overrides: Any
) -> OrderEvent:
    """SUBMIT_REQUESTED of a reduce-only order of the execution engine for *position*, at the step time."""
    moment = rig.clock.now_ns()
    fields: dict[str, Any] = {
        "seq": rig.next_order_seq(),
        "event_time_ns": moment,
        "receive_time_ns": moment,
        "originator": OrderOriginator.EXECUTION,
        "attempt": 1,
        "side": Side.SELL if position.side is Side.BUY else Side.BUY,
        "intent": intent,
        "order_type": order_type,
        "volume_lots": position.volume_lots,
        "limit_price": D("2652.80") if order_type is OrderType.LIMIT else None,
        "trigger_price": position.stop_price if order_type is OrderType.STOP else None,
        "reference_price": position.mark_price if order_type is OrderType.MARKET else None,
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": position.position_id,
        "reason_code": "FIXTURE_REASON",
    }
    fields.update(overrides)
    return samples.order_event(**fields)


def _entry_rig() -> tuple[_Rig, OrderEvent]:
    """A rig whose sample entry order has been submitted (and journaled)."""
    rig = _rig()
    order = samples.order_event()
    rig.gateway.submit(order)
    return rig, order


class _Unchanged:
    """Fail closed: neither the adapter nor the journal learnt anything during the block."""

    def __init__(self, rig: _Rig) -> None:
        self.rig = rig

    def __enter__(self) -> _Unchanged:
        self.calls = len(self.rig.adapter.calls)
        self.length = len(self.rig.journal)
        self.orders = self.rig.ledger.open_orders()
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            assert len(self.rig.adapter.calls) == self.calls
            assert len(self.rig.journal) == self.length
            assert self.rig.ledger.open_orders() == self.orders


def _refused(
    rig: _Rig, call: Callable[[], object], error: type[GieError] = IllegalTransitionError
) -> GieError:
    """The call raises *error* and changes nothing: the adapter is not called, nothing is journaled."""
    with _Unchanged(rig), pytest.raises(error) as caught:
        call()
    return caught.value


# =========================================================================== the lock itself
def test_no_live_mode_and_no_real_venue_exist_and_the_constant_is_false() -> None:
    assert routing_lock.LIVE_ROUTING_PERMITTED is False
    assert [mode.value for mode in RunMode] == ["REPLAY", "PAPER"]
    assert [venue.value for venue in ExecutionVenue] == ["SIMULATED"]
    for word in ("LIVE", "REAL", "DEMO", "live", None):
        with pytest.raises(LiveRoutingLockedError) as caught:
            RoutingGuard(word)
        assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"


def test_the_gateway_is_always_the_simulated_venue_in_the_mode_of_its_guard() -> None:
    for mode in (RunMode.REPLAY, RunMode.PAPER):
        rig = _rig(mode)
        assert rig.gateway.venue is ExecutionVenue.SIMULATED
        assert rig.gateway.mode is mode


# =========================================================================== construction
def test_the_gateway_needs_the_journal_and_the_clock_of_its_run() -> None:
    adapter = _SimulatedAdapter()
    journal = _journal(_story()[:_STORY_ENTRY_INPUTS])
    guard = RoutingGuard("REPLAY")
    for fake in (None, object(), {}, LifecycleLedger(), journal.ledger, [journal]):
        with pytest.raises(ConfigError):
            GuardedExecutionAdapter(adapter, guard, clock=ReplayClock(NOW), journal=fake)  # type: ignore[arg-type]
    for fake_clock in (None, NOW, object(), SimpleNamespace(now_ns=lambda: NOW)):
        with pytest.raises(ConfigError):
            GuardedExecutionAdapter(adapter, guard, clock=fake_clock, journal=journal)  # type: ignore[arg-type]
    # A journal that does not know its run yet, or belongs to a run of another mode.
    with pytest.raises(ConfigError):
        GuardedExecutionAdapter(adapter, guard, clock=ReplayClock(NOW), journal=MemoryJournal())
    with pytest.raises(ConfigError):
        GuardedExecutionAdapter(adapter, RoutingGuard("PAPER"), clock=ReplayClock(NOW), journal=journal)
    with pytest.raises(ConfigError):
        GuardedExecutionAdapter(adapter, guard, clock=ReplayClock(NOW), journal=_journal(_paper_inputs()[:1]))
    # The journal and the clock are keyword-only.
    with pytest.raises(TypeError):
        GuardedExecutionAdapter(adapter, guard, ReplayClock(NOW), journal)  # type: ignore[misc]
    assert adapter.calls == []


def test_a_file_journal_is_the_journal_of_a_run_as_well(tmp_path: Path) -> None:
    with FileJournal(tmp_path / "run.journal", fsync=False) as journal:
        for record in _story()[:_STORY_ENTRY_INPUTS]:
            journal.publish(record)
        adapter = _SimulatedAdapter()
        gateway = GuardedExecutionAdapter(
            adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), journal=journal
        )
        order = samples.order_event()
        gateway.submit(order)
        assert adapter.calls == [("submit", order)]
        assert journal.ledger.order(order.client_order_id) is order


# =========================================================================== authorised traffic
def test_an_authorised_entry_is_journaled_and_then_reaches_the_adapter() -> None:
    rig = _rig()
    order = samples.order_event()
    before = len(rig.journal)
    assert rig.gateway.submit(order) is None
    assert rig.adapter.calls == [("submit", order)]
    assert rig.adapter.calls[0][1] is order
    assert len(rig.journal) == before + 1
    assert rig.journaled()[-1] is order
    assert rig.ledger.order(order.client_order_id) is order
    assert rig.ledger.open_orders() == (order,)


def test_the_plan_and_approval_are_found_in_the_journal_not_taken_from_the_caller() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    for supplied in ({"plan": plan, "decision": decision}, {"position": samples.position()}):
        with _Unchanged(rig), pytest.raises(TypeError):
            rig.gateway.submit(order, **supplied)  # type: ignore[call-arg]
    with _Unchanged(rig), pytest.raises(TypeError):
        rig.gateway.cancel(_next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL), position=samples.position())  # type: ignore[call-arg]
    # The very same plan and approval, when they are part of the run, authorise the entry.
    rig.gateway.submit(order)
    assert rig.requests() == [("submit", order)]


def test_an_authorised_entry_reaches_the_adapter_in_paper_mode_too() -> None:
    rig = _rig("PAPER")
    order = _paper_order()
    rig.gateway.submit(order)
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(order.client_order_id) is order


def test_the_run_story_runs_through_the_gateway() -> None:
    # tests/gie/samples.py run_story(): one coherent run of 28 records.  Requests of the execution
    # engine go through submit / replace, broker reports and broker-created orders through poll(),
    # everything else straight into the journal.  The journal holds the run clock, so every record is
    # also held to its step instant.
    story = _story()
    clock = ReplayClock(ordering_time_ns(story[0]))
    journal = MemoryJournal(clock=clock)
    journal.publish(story[0])
    adapter = _SimulatedAdapter()
    gateway = GuardedExecutionAdapter(adapter, RoutingGuard("REPLAY"), clock=clock, journal=journal)
    rig = _Rig(adapter, journal, clock, gateway)
    rig.drive(story[1:])
    assert [entry.record for entry in journal.read()] == story
    requests = [request for _, request in rig.requests()]
    assert [(request.event_type, request.intent) for request in requests] == [
        (E.SUBMIT_REQUESTED, OrderIntent.ENTRY),
        (E.SUBMIT_REQUESTED, OrderIntent.EXIT),
        (E.REPLACE_REQUESTED, OrderIntent.PROTECTIVE_STOP),
    ]
    # The replacement of the broker's stop is asked for by the engine; the order keeps its originator.
    assert [request.originator for request in requests] == [
        OrderOriginator.EXECUTION,
        OrderOriginator.EXECUTION,
        OrderOriginator.BROKER,
    ]
    ticks = [call[1] for call in adapter.calls if call[0] == "on_tick"]
    assert ticks == [record for record in story if isinstance(record, Tick)]
    assert journal.ledger.open_orders() == ()
    assert journal.ledger.open_positions() == ()


@pytest.mark.requirement("G04-12")
def test_a_broker_created_protective_stop_enters_through_poll_after_the_entry_filled() -> None:
    story = _story()
    rig = _rig(records=story[:1], now_ns=ordering_time_ns(story[1]))
    rig.drive(story[1:9])  # ... entry submitted and accepted, quote of the first fill
    partial, stop_submit, stop_accepted = story[9], story[10], story[11]
    assert isinstance(stop_submit, OrderEvent)
    assert (stop_submit.originator, stop_submit.event_type) == (OrderOriginator.BROKER, E.SUBMIT_REQUESTED)
    # Before the entry has filled there is no position the stop could protect.
    rig.clock.advance_to(ordering_time_ns(stop_submit))
    early = rig.report(dataclasses.replace(stop_submit, seq=rig.next_order_seq()))
    assert early.events == ()
    assert len(early.errors) == 1
    assert isinstance(early.errors[0], IllegalTransitionError)
    assert rig.ledger.order(stop_submit.client_order_id) is None
    # After the first fill it is admitted, and its acknowledgement follows.
    rig.clock.advance_to(ordering_time_ns(stop_accepted))
    result = rig.report(partial, stop_submit, stop_accepted)
    assert result.clean
    assert result.events == (partial, stop_submit, stop_accepted)
    assert result.duplicates == 0
    assert rig.ledger.protective_order(stop_submit.position_id or "") is stop_accepted
    assert rig.requests() == [("submit", story[6])]  # the broker's order was never "submitted" by GIE


@pytest.mark.requirement("G04-12")
def test_the_gateway_never_submits_an_order_of_the_broker() -> None:
    rig, held, stop = _holding_rig()
    broker_exit = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET, originator=OrderOriginator.BROKER)
    error = _refused(rig, lambda: rig.gateway.submit(broker_exit))
    assert error.details.get("client_order_id") == broker_exit.client_order_id
    assert "execution engine" in str(error)
    # The broker's own stop, re-sent as a submit by the engine, is refused just the same.
    first_stop_event = next(
        record
        for record in rig.journaled()
        if isinstance(record, OrderEvent) and record.client_order_id == stop.client_order_id
    )
    _refused(rig, lambda: rig.gateway.submit(first_stop_event))
    # The same exit as an order of the execution engine is authorised.
    own_exit = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET)
    rig.gateway.submit(own_exit)
    assert rig.requests() == [("submit", own_exit)]


def test_authorised_reducing_orders_are_journaled_and_reach_the_adapter() -> None:
    rig, held, _stop = _holding_rig()
    take_profit = _child(rig, held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT, volume_lots=D("0.25"))
    rig.gateway.submit(take_profit)
    exit_order = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.25"), attempt=2)
    rig.gateway.submit(exit_order)
    for request in (take_profit, exit_order):
        assert rig.ledger.order(request.client_order_id) is request
    assert rig.requests() == [("submit", take_profit), ("submit", exit_order)]
    # Together they reach the open volume; one lot fraction more is refused.
    third = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.01"), attempt=3)
    _refused(rig, lambda: rig.gateway.submit(third))


@pytest.mark.requirement("G04-11")
def test_a_tightening_replacement_of_the_broker_stop_reaches_the_adapter() -> None:
    rig, held, stop = _holding_rig()
    tighten = rig.next(
        stop,
        E.REPLACE_REQUESTED,
        S.PENDING_REPLACE,
        requested_trigger_price=D("2649.00"),
        reason_code="RATCHET",
    )
    assert tighten.originator is OrderOriginator.BROKER  # the originator of an order never changes
    rig.gateway.replace(tighten)
    assert rig.requests() == [("replace", tighten)]
    replaced = rig.next(tighten, E.REPLACED, S.WORKING, trigger_price=D("2649.00"), reason_code="RATCHET")
    result = rig.report(replaced)
    assert result.events == (replaced,)
    newest = rig.ledger.order(stop.client_order_id)
    assert newest is replaced
    assert newest.trigger_price == D("2649.00")
    assert held.position_id == stop.position_id


def test_reports_of_the_adapter_are_journaled_one_by_one_and_returned_in_order() -> None:
    rig = _rig()
    submit, accepted, partial, filled = samples.order_lifecycle()
    rig.gateway.submit(submit)
    empty = rig.gateway.poll()
    assert empty == PollResult((), 0, ())
    assert empty.clean
    rig.clock.advance_to(partial.receive_time_ns)
    result = rig.report(accepted, partial)
    assert result.events == (accepted, partial)
    assert result.clean
    assert rig.ledger.order(submit.client_order_id) is partial
    rig.clock.advance_to(filled.receive_time_ns)
    assert rig.report(filled).events == (filled,)
    assert rig.ledger.order(submit.client_order_id) is filled
    assert rig.ledger.open_orders() == ()


def test_cancelling_an_entry_only_reduces_risk_and_needs_no_position() -> None:
    rig, order = _entry_rig()
    cancel = rig.next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    rig.gateway.cancel(cancel)
    assert rig.requests() == [("submit", order), ("cancel", cancel)]
    assert rig.ledger.order(order.client_order_id) is cancel


@pytest.mark.requirement("G04-09")
def test_an_entry_may_be_shrunk_but_not_grown_through_the_gateway() -> None:
    rig, order = _entry_rig()
    accepted = rig.next(order, E.ACCEPTED, S.WORKING)
    assert rig.report(accepted).events == (accepted,)
    grow = rig.next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.60"))
    _refused(rig, lambda: rig.gateway.replace(grow))
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(order.client_order_id) is accepted
    shrink = rig.next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.30"))
    rig.gateway.replace(shrink)
    assert rig.requests() == [("submit", order), ("replace", shrink)]


def test_ticks_and_position_queries_pass_through_the_guard() -> None:
    held = samples.position()
    rig = _rig()
    rig.adapter.positions.append(held)
    tick = samples.tick()
    assert rig.gateway.on_tick(tick) is None
    assert rig.gateway.open_positions() == (held,)
    assert rig.adapter.calls == [("on_tick", tick), ("open_positions", None)]
    for not_a_tick in (None, "tick", SimpleNamespace(bid=D("1"), ask=D("2")), samples.order_event()):
        with pytest.raises(ContractValidationError):
            rig.gateway.on_tick(not_a_tick)  # type: ignore[arg-type]
    assert rig.adapter.calls == [("on_tick", tick), ("open_positions", None)]
    rig.adapter.positions.append("not a position")
    with pytest.raises(ContractValidationError):
        rig.gateway.open_positions()


def test_create_builds_the_adapter_inside_the_gateway() -> None:
    order = samples.order_event()
    held = samples.position()
    accepted = _next(order, E.ACCEPTED, S.WORKING)
    journal = _journal(_story()[:_STORY_ENTRY_INPUTS])
    gateway = GuardedExecutionAdapter.create(
        _SimulatedAdapter,
        RoutingGuard("REPLAY"),
        clock=ReplayClock(accepted.receive_time_ns),
        journal=journal,
        reports=(accepted,),
        positions=(held,),
    )
    assert type(gateway) is GuardedExecutionAdapter
    assert gateway.open_positions() == (held,)
    gateway.submit(order)
    assert journal.ledger.order(order.client_order_id) is order
    assert gateway.poll() == PollResult((accepted,), 0, ())
    assert journal.ledger.order(order.client_order_id) is accepted


# =========================================================================== adapter refused
def test_an_unregistered_adapter_class_is_refused() -> None:
    adapter = _UnregisteredAdapter()
    for mode in ("REPLAY", "PAPER"):
        records = _paper_inputs()[:1] if mode == "PAPER" else _story()[:1]
        with pytest.raises(LiveRoutingLockedError) as caught:
            GuardedExecutionAdapter(
                adapter, RoutingGuard(mode), clock=ReplayClock(NOW), journal=_journal(records)
            )
        assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter.create(
            _UnregisteredAdapter,
            RoutingGuard("REPLAY"),
            clock=ReplayClock(NOW),
            journal=_journal(_story()[:1]),
        )
    assert adapter.calls == []


@pytest.mark.parametrize("not_an_adapter", [None, object(), "adapter", _SimulatedAdapter, 42])
def test_objects_that_are_no_adapter_instances_are_refused(not_an_adapter: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            not_an_adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), journal=_journal(_story()[:1])
        )


def test_a_subclass_of_a_registered_adapter_is_refused() -> None:
    adapter = _SubclassedAdapter()
    assert isinstance(adapter, _SimulatedAdapter)
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), journal=_journal(_story()[:1])
        )
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter.create(
            _SubclassedAdapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), journal=_journal(_story()[:1])
        )
    assert adapter.calls == []


def test_a_routing_guard_look_alike_is_refused() -> None:
    adapter = _SimulatedAdapter()
    journal = _journal(_story()[:1])
    for fake in (_PermissiveGuard(), None, "REPLAY", RunMode.REPLAY, RoutingGuard):
        with pytest.raises(LiveRoutingLockedError):
            GuardedExecutionAdapter(adapter, fake, clock=ReplayClock(NOW), journal=journal)  # type: ignore[arg-type]
        with pytest.raises(LiveRoutingLockedError):
            GuardedExecutionAdapter.create(
                _SimulatedAdapter,
                fake,  # type: ignore[arg-type]
                clock=ReplayClock(NOW),
                journal=journal,
            )
    # A permissive guard must not even help an unregistered adapter.
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            _UnregisteredAdapter(),
            _PermissiveGuard(),  # type: ignore[arg-type]
            clock=ReplayClock(NOW),
            journal=journal,
        )
    assert adapter.calls == []


def test_the_routing_guard_cannot_be_subclassed_or_changed() -> None:
    with pytest.raises(LiveRoutingLockedError):
        type("_SubclassedGuard", (RoutingGuard,), {"authorized_methods": _PermissiveGuard.authorized_methods})
    guard = RoutingGuard("REPLAY")
    with pytest.raises(LiveRoutingLockedError):
        guard._mode = RunMode.PAPER  # type: ignore[misc]
    with pytest.raises(LiveRoutingLockedError):
        guard.authorized_methods = _PermissiveGuard().authorized_methods  # type: ignore[method-assign]
    assert guard.mode is RunMode.REPLAY


def test_a_guard_subclass_that_survived_its_refused_creation_cannot_open_the_gateway() -> None:
    # Refusing a subclass at class creation does not necessarily destroy the class object: it could
    # still be found through ``RoutingGuard.__subclasses__()``.  The gateway must not accept it.
    overrides = {
        "__slots__": (),
        "authorized_methods": _PermissiveGuard.authorized_methods,
        "authorize": _PermissiveGuard.authorize,
    }
    rogue = _UnregisteredAdapter()
    survivors: list[type] = []
    gc.disable()
    try:
        with pytest.raises(LiveRoutingLockedError):
            type("_SurvivingGuard", (RoutingGuard,), overrides)
        survivors = [k for k in RoutingGuard.__subclasses__() if k.__name__ == "_SurvivingGuard"]
        for survivor in survivors:
            forged = object.__new__(survivor)
            with contextlib.suppress(GieError, AttributeError, TypeError):
                RoutingGuard.__init__(forged, "REPLAY")
            for adapter in (rogue, _SimulatedAdapter()):
                with pytest.raises(LiveRoutingLockedError):
                    GuardedExecutionAdapter(
                        adapter, forged, clock=ReplayClock(NOW), journal=_journal(_story()[:1])
                    )
            with pytest.raises(LiveRoutingLockedError):
                GuardedExecutionAdapter.create(
                    _UnregisteredAdapter, forged, clock=ReplayClock(NOW), journal=_journal(_story()[:1])
                )
    finally:
        survivors.clear()
        gc.enable()
        gc.collect()
    assert rogue.calls == []


def test_swapping_the_private_references_of_a_gateway_does_not_reach_an_unregistered_adapter() -> None:
    rig = _rig()
    order = samples.order_event()
    rogue = _UnregisteredAdapter()
    swapped = True
    try:
        rig.gateway._adapter = rogue  # type: ignore[misc]
    except (AttributeError, TypeError, GieError):
        swapped = False  # an implementation that forbids the assignment is fine as well
    if swapped:
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.submit(order)
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.poll()
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.on_tick(samples.tick())
    with contextlib.suppress(AttributeError, TypeError, GieError):
        rig.gateway._guard = _PermissiveGuard()  # type: ignore[misc]
    with contextlib.suppress(GieError):
        rig.gateway.submit(order)
    with contextlib.suppress(AttributeError, TypeError, GieError):
        rig.gateway._journal = MemoryJournal()  # type: ignore[misc]
    assert rogue.calls == []


def test_an_adapter_class_modified_after_registration_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    order = samples.order_event()
    rerouted: list[Any] = []

    def routed_elsewhere(self: Any, request: OrderEvent) -> None:
        rerouted.append(request)

    monkeypatch.setattr(_SimulatedAdapter, "submit", routed_elsewhere)
    # Module docstring of guarded.py: for every call the gateway first authorises the adapter, and
    # only then checks, journals and forwards the request.  A refused adapter learns nothing and the
    # journal records no request that was never sent.
    error = _refused(rig, lambda: rig.gateway.submit(order), LiveRoutingLockedError)
    assert error.code == "GIE.ROUTING.LIVE_LOCKED"
    assert rerouted == []
    assert rig.ledger.order(order.client_order_id) is None
    # Every other operation is refused as well: the class is no longer the registered one.
    for operation in (
        rig.gateway.poll,
        rig.gateway.open_positions,
        lambda: rig.gateway.on_tick(samples.tick()),
    ):
        with pytest.raises(LiveRoutingLockedError):
            operation()
    assert rig.adapter.calls == []
    # Neither a new gateway nor a second registration brings the modified class back.
    register_simulated_adapter(_SimulatedAdapter)
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            _SimulatedAdapter(),
            RoutingGuard("REPLAY"),
            clock=ReplayClock(NOW),
            journal=_journal(_story()[:1]),
        )
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.submit(order)
    assert rerouted == []


@pytest.mark.parametrize("method", ["cancel", "replace", "on_tick", "poll", "open_positions"])
def test_modifying_any_adapter_method_locks_every_operation(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    rig = _rig()
    order = samples.order_event()
    monkeypatch.setattr(_SimulatedAdapter, method, lambda self, *arguments: ())
    _refused(rig, lambda: rig.gateway.submit(order), LiveRoutingLockedError)
    assert rig.ledger.order(order.client_order_id) is None


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("sends_real_orders", True),
        ("sends_real_orders", None),
        ("sends_real_orders", 0),
        ("venue", "SIMULATED"),
        ("venue", "LIVE"),
        ("venue", None),
    ],
)
def test_an_adapter_class_that_no_longer_declares_the_simulated_venue_is_refused(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
) -> None:
    rig = _rig()
    order = samples.order_event()
    monkeypatch.setattr(_SimulatedAdapter, attribute, value)
    _refused(rig, lambda: rig.gateway.submit(order), LiveRoutingLockedError)
    _refused(rig, rig.gateway.poll, LiveRoutingLockedError)
    assert rig.ledger.order(order.client_order_id) is None


def test_removing_an_adapter_method_after_registration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig()
    order = samples.order_event()
    monkeypatch.delattr(_SimulatedAdapter, "poll")
    _refused(rig, lambda: rig.gateway.submit(order), LiveRoutingLockedError)
    assert rig.ledger.order(order.client_order_id) is None


def test_the_unmodified_adapter_works_again_once_the_modification_is_gone() -> None:
    # Control for the tests above: without a modification the very same calls go through.
    rig = _rig()
    order = samples.order_event()
    rig.gateway.submit(order)
    assert rig.requests() == [("submit", order)]
    assert rig.gateway.poll() == PollResult((), 0, ())


# =========================================================================== entry refused
@pytest.mark.parametrize("missing", ["plan", "decision", "both"])
def test_an_entry_whose_plan_or_risk_decision_is_not_journaled_is_refused(missing: str) -> None:
    story = _story()
    keep = {"plan": [0, 1, 2, 3], "decision": [0, 1, 2, 3, 4], "both": [0, 1, 2, 3]}[missing]
    records = [story[index] for index in keep]
    if missing == "plan":
        # A decision without its plan cannot be journaled either (referential ledger): nothing cites it.
        with pytest.raises(GieError):
            _journal([*records, story[5]])
    rig = _rig(records=records)
    order = samples.order_event()
    error = _refused(rig, lambda: rig.gateway.submit(order))
    assert error.details.get("client_order_id") == order.client_order_id
    assert "journaled first" in str(error.details.get("rule"))


@pytest.mark.requirement("G04-09", "G04-12", "S08-ACC-06")
@pytest.mark.parametrize(
    "deviation",
    [
        "volume_above_grant",
        "other_stop_attached",
        "other_price",
        "order_outlives_approval",
        "other_slippage_limit",
        "no_slippage_limit",
    ],
)
def test_an_entry_that_fails_authorisation_never_reaches_the_adapter(deviation: str) -> None:
    rig = _rig()
    order = {
        "volume_above_grant": lambda: samples.order_event(volume_lots=D("0.51")),
        "other_stop_attached": lambda: samples.order_event(attached_stop_price=D("2647.00")),
        "other_price": lambda: samples.order_event(limit_price=D("2649.70")),
        "order_outlives_approval": lambda: samples.order_event(valid_until_ns=APPROVAL_EXPIRY + 1),
        "other_slippage_limit": lambda: samples.order_event(max_slippage=D("0.20")),
        "no_slippage_limit": lambda: samples.order_event(max_slippage=None),
    }[deviation]()
    error = _refused(rig, lambda: rig.gateway.submit(order))
    assert error.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert rig.ledger.order(order.client_order_id) is None


@pytest.mark.requirement("G04-09", "G04-12", "S08-ACC-06")
@pytest.mark.parametrize("journaled", ["rejected_decision", "plan_not_selected", "plan_of_a_retired_model"])
def test_an_entry_resting_on_a_journaled_record_that_does_not_authorise_it_is_refused(journaled: str) -> None:
    story = _story()
    inputs = story[:4]
    if journaled == "rejected_decision":
        plan = samples.trade_plan()
        decision = samples.rejected_risk_decision(plan)
    elif journaled == "plan_not_selected":
        plan = samples.trade_plan(status=PlanStatus.ARMED)
        decision = samples.risk_decision(plan)
    else:
        # The run pins its models with their status; a plan claiming another status is not
        # journaled at all (the run manifest decides which statuses a run's plans rest on).
        with pytest.raises(ContractValidationError) as caught:
            _journal([*inputs, samples.trade_plan(model_status=ModelStatus.RETIRED)])
        assert "pins" in str(caught.value.details.get("rule"))
        return
    rig = _rig(records=[*inputs, plan, decision])
    order = samples.order_event(risk_decision_id=decision.decision_id)
    _refused(rig, lambda: rig.gateway.submit(order))


@pytest.mark.requirement("S08-ACC-06")
def test_the_executable_model_statuses_of_the_run_role_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    # Change notice section 5 (D-026): the gateway asks validate_entry_authorization with the role of
    # the run (RunManifest.role).  The sample run is a RESEARCH run whose plan rests on a RESEARCH
    # model: authorised.  The same records judged as a PRIMARY run are refused.
    calls: list[Any] = []
    from gie.contracts import guarded

    original = guarded.validate_entry_authorization

    def spy(*arguments: Any, **keywords: Any) -> None:
        calls.append(keywords["role"])
        original(*arguments, **keywords)

    monkeypatch.setattr(guarded, "validate_entry_authorization", spy)
    rig = _rig()
    manifest = rig.journal.manifest
    assert isinstance(manifest, RunManifest)
    rig.gateway.submit(samples.order_event())
    assert calls == [manifest.role]
    with pytest.raises(IllegalTransitionError):
        original(
            samples.trade_plan(),
            samples.risk_decision(),
            samples.order_event(),
            now_ns=NOW,
            role=RunRole.PRIMARY,
        )


def test_the_gateway_checks_expiry_against_its_own_clock_and_fails_closed_at_the_boundary() -> None:
    order = samples.order_event()
    for now_ns in (APPROVAL_EXPIRY, APPROVAL_EXPIRY + 1, APPROVAL_EXPIRY + 3600 * NS_PER_SECOND):
        late = _rig(now_ns=now_ns)
        error = _refused(late, lambda late=late: late.gateway.submit(order), StaleDataError)
        assert isinstance(error, GieError)
    in_time = _rig(now_ns=APPROVAL_EXPIRY - 1)
    in_time.gateway.submit(order)
    assert in_time.requests() == [("submit", order)]


def test_time_passing_on_the_injected_clock_expires_an_approval_that_was_still_valid() -> None:
    rig = _rig()
    order = samples.order_event()
    rig.clock.advance_to(APPROVAL_EXPIRY)
    _refused(rig, lambda: rig.gateway.submit(order), StaleDataError)


def test_an_entry_request_from_the_future_of_the_run_clock_is_refused() -> None:
    order = samples.order_event()
    early = _rig(now_ns=order.event_time_ns - 1)
    _refused(early, lambda: early.gateway.submit(order))
    at_the_instant = _rig(now_ns=order.event_time_ns)
    at_the_instant.gateway.submit(order)
    assert at_the_instant.requests() == [("submit", order)]


@pytest.mark.requirement("S08-ACC-06")
def test_a_second_plan_of_the_same_alternative_group_never_reaches_the_adapter() -> None:
    other_plan = samples.trade_plan(variant=1, seq=1)
    other_decision = samples.risk_decision(other_plan, seq=1)
    rig = _rig(records=[*_story()[:_STORY_ENTRY_INPUTS], other_plan, other_decision])
    order = samples.order_event()
    rig.gateway.submit(order)
    other_order = samples.order_event(
        seq=1, plan_id=other_plan.plan_id, risk_decision_id=other_decision.decision_id
    )
    assert other_order.alternative_group_id == order.alternative_group_id
    assert other_order.client_order_id != order.client_order_id
    error = _refused(rig, lambda: rig.gateway.submit(other_order))
    assert "alternative group" in str(error.details.get("rule"))
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(other_order.client_order_id) is None


def test_an_entry_for_a_plan_the_ledger_knows_to_be_withdrawn_never_reaches_the_adapter() -> None:
    # The run's ledger keeps the newest revision of every plan; once that revision is WITHDRAWN the
    # earlier SELECTED revision is no longer the plan's state, even though its approval has not
    # expired yet: "an entry is built from the newest revision of its plan".
    moment = T0 + NS_PER_MS
    withdrawn = samples.trade_plan(
        revision=1,
        seq=1,
        status=PlanStatus.WITHDRAWN,
        status_reasons=("FIXTURE_INVALIDATED",),
        decision_time_ns=moment,
        as_of_time_ns=samples.T0 - NS_PER_MS,
    )
    rig = _rig(records=[*_story()[:_STORY_ENTRY_INPUTS], withdrawn])
    order = samples.order_event()
    error = _refused(rig, lambda: rig.gateway.submit(order))
    assert "newest revision" in str(error.details.get("rule"))


# =========================================================================== other orders refused
def test_a_non_entry_order_without_its_position_in_the_journal_is_refused() -> None:
    rig = _rig()
    held = samples.position()
    for request in (
        _child(rig, held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP),
        _child(rig, held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT),
        _child(rig, held, OrderIntent.EXIT, OrderType.MARKET),
    ):
        error = _refused(rig, lambda request=request: rig.gateway.submit(request))
        assert error.details.get("client_order_id") == request.client_order_id
        assert "journaled first" in str(error.details.get("rule"))


@pytest.mark.requirement("G04-09", "G04-10", "G04-11")
@pytest.mark.parametrize(
    "deviation",
    [
        "adds_to_the_position",
        "above_open_volume",
        "second_protective_stop",
        "looser_replacement",
        "partial_cover",
    ],
)
def test_a_non_entry_order_that_does_not_reduce_its_position_never_reaches_the_adapter(
    deviation: str,
) -> None:
    rig, held, stop = _holding_rig()
    if deviation == "adds_to_the_position":
        request = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET, side=Side.BUY)
        call: Callable[[], object] = lambda: rig.gateway.submit(request)  # noqa: E731
    elif deviation == "above_open_volume":
        request = _child(rig, held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.51"))
        call = lambda: rig.gateway.submit(request)  # noqa: E731
    elif deviation == "second_protective_stop":
        request = _child(rig, held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, trigger_price=D("2649.00"))
        call = lambda: rig.gateway.submit(request)  # noqa: E731
    elif deviation == "looser_replacement":
        request = rig.next(stop, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2640.00"))
        call = lambda: rig.gateway.replace(request)  # noqa: E731
    else:
        request = rig.next(stop, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.25"))
        call = lambda: rig.gateway.replace(request)  # noqa: E731
    _refused(rig, call)
    assert rig.ledger.order(request.client_order_id) is not request


@pytest.mark.requirement("G04-11", "G04-12")
def test_the_protective_stop_of_an_open_position_cannot_be_cancelled_through_the_gateway() -> None:
    rig, held, stop = _holding_rig()
    cancel = rig.next(stop, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    error = _refused(rig, lambda: rig.gateway.cancel(cancel))
    assert "open position" in str(error.details.get("rule"))
    assert rig.ledger.order(stop.client_order_id) is stop
    assert rig.ledger.position(held.position_id) is held


@pytest.mark.requirement("G04-09", "G04-10")
def test_the_newest_position_in_the_journal_limits_the_volume_of_a_reducing_order() -> None:
    # The gateway reads the position from the run ledger, which keeps the newest snapshot: after half
    # of the position was closed, an exit of the old volume would open a position on the opposite side
    # without plan or approval.
    story = _story()
    rig = _rig(records=story[:1], now_ns=ordering_time_ns(story[1]))
    rig.drive(story[1:23])  # ... exit filled, position reduced to 0.25 lots
    reduced = story[22]
    assert isinstance(reduced, Position)
    assert rig.ledger.position(reduced.position_id) is reduced
    assert reduced.volume_lots == D("0.25") < reduced.initial_volume_lots
    rig.step()
    old_volume = _child(rig, reduced, OrderIntent.EXIT, OrderType.MARKET, attempt=2, volume_lots=D("0.50"))
    _refused(rig, lambda: rig.gateway.submit(old_volume))
    current_volume = _child(
        rig, reduced, OrderIntent.EXIT, OrderType.MARKET, attempt=2, volume_lots=D("0.25")
    )
    rig.gateway.submit(current_volume)
    assert rig.requests()[-1] == ("submit", current_volume)


# =========================================================================== request shape refused
@pytest.mark.parametrize(
    ("method", "event_index"),
    [("submit", 1), ("submit", 3), ("cancel", 0), ("cancel", 1), ("replace", 0), ("replace", 2)],
)
def test_a_request_of_the_wrong_event_type_for_the_method_is_refused(method: str, event_index: int) -> None:
    rig = _rig()
    event = samples.order_lifecycle()[event_index]
    error = _refused(rig, lambda: getattr(rig.gateway, method)(event))
    assert error.details.get("actual") == event.event_type.value
    assert (
        error.details.get("expected")
        == {"submit": "SUBMIT_REQUESTED", "cancel": "CANCEL_REQUESTED", "replace": "REPLACE_REQUESTED"}[
            method
        ]
    )


def test_requests_of_the_other_kinds_are_refused_by_each_method() -> None:
    rig, order = _entry_rig()
    cancel = rig.next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    replace = rig.next(order, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.30"))
    _refused(rig, lambda: rig.gateway.submit(cancel))
    _refused(rig, lambda: rig.gateway.submit(replace))
    _refused(rig, lambda: rig.gateway.replace(cancel))
    _refused(rig, lambda: rig.gateway.cancel(replace))
    _refused(rig, lambda: rig.gateway.replace(order))
    _refused(rig, lambda: rig.gateway.cancel(order))


@pytest.mark.parametrize("method", ["submit", "cancel", "replace"])
def test_an_order_event_look_alike_is_refused(method: str) -> None:
    rig = _rig()
    order = samples.order_event()
    fields = {spec.name: getattr(order, spec.name) for spec in OrderEvent.FIELD_SPECS}
    look_alikes = [
        SimpleNamespace(is_request=True, is_terminal=False, **fields),
        dict(fields),
        _UnvalidatedOrderEvent(**fields),
        _UnvalidatedOrderEvent(**{**fields, "venue": "LIVE", "mode": "LIVE"}),
        None,
        "SUBMIT_REQUESTED",
    ]
    assert isinstance(look_alikes[2], OrderEvent)
    for look_alike in look_alikes:
        error = _refused(
            rig,
            lambda look_alike=look_alike: getattr(rig.gateway, method)(look_alike),
            ContractValidationError,
        )
        assert error.code == "GIE.CONTRACT.INVALID"


def test_requests_of_another_run_mode_than_the_guards_are_refused() -> None:
    replay_order = samples.order_event()
    paper_order = _paper_order()
    paper_rig = _rig("PAPER")
    error = _refused(paper_rig, lambda: paper_rig.gateway.submit(replay_order), LiveRoutingLockedError)
    assert error.code == "GIE.ROUTING.LIVE_LOCKED"
    replay_rig = _rig("REPLAY")
    _refused(replay_rig, lambda: replay_rig.gateway.submit(paper_order), LiveRoutingLockedError)
    paper_cancel = _next(paper_order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    _refused(replay_rig, lambda: replay_rig.gateway.cancel(paper_cancel), LiveRoutingLockedError)
    # Control: each order is accepted by the gateway of its own mode.
    paper_rig.gateway.submit(paper_order)
    replay_rig.gateway.submit(replay_order)
    assert paper_rig.requests() == [("submit", paper_order)]
    assert replay_rig.requests() == [("submit", replay_order)]


# =========================================================================== reports
@pytest.mark.parametrize("junk", [None, "FILLED", 42, {"event_type": "FILLED"}, ("FILLED",)])
def test_a_non_order_event_from_poll_lands_in_errors_and_hides_nothing(junk: object) -> None:
    rig, submit = _entry_rig()
    _submit, accepted, partial, _filled = samples.order_lifecycle()
    rig.clock.advance_to(partial.receive_time_ns)
    result = rig.report(accepted, junk, partial)
    assert result.events == (accepted, partial)
    assert len(result.errors) == 1
    assert isinstance(result.errors[0], ContractValidationError)
    assert result.errors[0].code == "GIE.CONTRACT.INVALID"
    assert not result.clean
    assert rig.ledger.order(submit.client_order_id) is partial


def test_an_unvalidated_order_event_subclass_from_poll_is_refused() -> None:
    rig, submit = _entry_rig()
    accepted = samples.order_lifecycle()[1]
    rig.clock.advance_to(accepted.receive_time_ns)
    fields = {spec.name: getattr(accepted, spec.name) for spec in OrderEvent.FIELD_SPECS}
    for forged in (
        _UnvalidatedOrderEvent(**fields),
        _UnvalidatedOrderEvent(**{**fields, "venue": "LIVE"}),
        SimpleNamespace(is_request=False, **fields),
    ):
        result = rig.report(forged)
        assert result.events == ()
        assert [type(error) for error in result.errors] == [ContractValidationError]
        assert rig.ledger.order(submit.client_order_id) is submit


def test_an_adapter_cannot_issue_requests_of_the_execution_engine() -> None:
    rig, order = _entry_rig()
    other_plan = samples.trade_plan(variant=2)
    foreign_entry = samples.order_event(
        seq=1,
        plan_id=other_plan.plan_id,
        risk_decision_id=samples.risk_decision(other_plan).decision_id,
        alternative_group_id=identity.alternative_group_id(samples.unscored_candidate().opportunity_id),
    )
    cancel = rig.next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    replace = rig.next(order, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.30"))
    for request in (foreign_entry, cancel, replace):
        assert request.is_request is True
        assert request.originator is OrderOriginator.EXECUTION
        result = rig.report(request)
        assert result.events == ()
        assert len(result.errors) == 1
        assert isinstance(result.errors[0], IllegalTransitionError)
        assert result.errors[0].details.get("client_order_id") == request.client_order_id
    assert rig.ledger.order(order.client_order_id) is order
    assert rig.ledger.order(foreign_entry.client_order_id) is None
    assert rig.ledger.open_orders() == (order,)
    assert rig.requests() == [("submit", order)]


def test_reports_of_another_run_mode_are_refused_and_hide_no_valid_report() -> None:
    rig, order = _entry_rig()
    paper_report = rig.next(order, E.ACCEPTED, S.WORKING, mode=RunMode.PAPER)
    accepted = dataclasses.replace(paper_report, mode=RunMode.REPLAY)
    # The routing lock is never weakened: a report of another mode is not journaled ...
    surfaced: list[GieError] = []
    result: PollResult | None = None
    try:
        result = rig.report(paper_report, accepted)
    except LiveRoutingLockedError as error:
        surfaced.append(error)
    if result is not None:
        surfaced.extend(result.errors)
    assert [type(error) for error in surfaced] == [LiveRoutingLockedError]
    # ... and, PollResult: "reports behind a refused one are still processed, so nothing that is
    # valid is lost" (change notice section 5: a refused report does not hide the others).
    assert rig.ledger.order(order.client_order_id) == accepted


def test_a_report_from_the_future_of_the_run_clock_is_refused() -> None:
    order = samples.order_event()
    rig = _rig(now_ns=order.receive_time_ns)
    rig.gateway.submit(order)
    accepted = _next(order, E.ACCEPTED, S.WORKING)
    rig.clock.advance_to(accepted.receive_time_ns - 1)
    result = rig.report(accepted)
    assert result.events == ()
    assert [type(error) for error in result.errors] == [FutureDataError]
    assert rig.ledger.order(order.client_order_id) is order
    rig.clock.advance_to(accepted.receive_time_ns)
    assert rig.report(accepted).events == (accepted,)


@pytest.mark.parametrize(
    "fault",
    ["skips_a_sequence_number", "unknown_order", "illegal_transition", "after_terminal", "grows_the_order"],
)
def test_a_report_that_breaks_the_order_rules_is_refused_and_does_not_hide_the_following_ones(
    fault: str,
) -> None:
    rig, _order = _entry_rig()
    _submit, accepted, partial, filled = samples.order_lifecycle()
    rig.clock.advance_to(filled.receive_time_ns + NS_PER_SECOND)
    admitted: list[OrderEvent] = [accepted]
    if fault == "skips_a_sequence_number":
        faulty = dataclasses.replace(filled, seq=2)  # order_seq 3 directly after order_seq 1
    elif fault == "unknown_order":
        faulty = samples.protective_stop_lifecycle()[1]
    elif fault == "illegal_transition":
        faulty = _next(accepted, E.REPLACED, S.WORKING, seq=2)
    elif fault == "after_terminal":
        admitted = [accepted, partial, filled]
        faulty = _next(
            filled, E.CANCELED, S.CANCELED, cumulative_filled_lots=D("0"), average_fill_price=None, seq=4
        )
    else:
        faulty = _next(accepted, E.RECONCILED, S.WORKING, volume_lots=D("5.00"), seq=2)
    follower_seq = admitted[-1].seq + 1
    follower = (
        _next(filled, E.RECONCILED, S.FILLED, seq=follower_seq)
        if fault == "after_terminal"
        else dataclasses.replace(partial, seq=follower_seq)
    )
    result = rig.report(*admitted, faulty, follower)
    assert len(result.errors) == (2 if fault == "after_terminal" else 1)
    assert isinstance(result.errors[0], GieError)
    assert faulty not in result.events
    if fault == "after_terminal":
        # Nothing can follow the terminal FILLED: the follower is refused on its own merits.
        assert result.events == tuple(admitted)
    else:
        assert result.events == (*admitted, follower)
    assert rig.ledger.order(faulty.client_order_id) is not faulty


def test_a_report_delivered_again_is_counted_as_a_duplicate() -> None:
    rig, submit = _entry_rig()
    _submit, accepted, partial, _filled = samples.order_lifecycle()
    rig.clock.advance_to(partial.receive_time_ns)
    assert rig.report(accepted).events == (accepted,)
    result = rig.report(accepted, partial, partial, accepted)
    assert result.events == (partial,)
    assert result.duplicates == 3
    assert result.clean
    assert rig.ledger.order(submit.client_order_id) is partial
    # A different report at a journaled position is no duplicate: it is refused.
    conflicting = dataclasses.replace(partial, last_fill_cost_amount=D("0.90"))
    refused = rig.report(conflicting)
    assert refused.events == ()
    assert refused.duplicates == 0
    assert len(refused.errors) == 1


# =========================================================================== write-ahead and idempotency
class _JournalProbe(list[tuple[str, Any]]):
    """SYNTHETIC FIXTURE: a call log that notes, at the moment of each call, what the journal knows."""

    def __init__(self, rig: _Rig) -> None:
        super().__init__()
        self.rig = rig
        self.journaled_at_call: list[bool] = []

    def append(self, call: tuple[str, Any]) -> None:
        name, request = call
        if name in _REQUEST_METHODS:
            self.journaled_at_call.append(self.rig.ledger.order(request.client_order_id) == request)
        super().append(call)


@pytest.mark.requirement("G08-07")
def test_the_request_is_in_the_journal_before_the_adapter_sees_it() -> None:
    rig = _rig()
    probe = _JournalProbe(rig)
    rig.adapter.calls = probe
    order = samples.order_event()
    rig.gateway.submit(order)
    cancel = rig.next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    rig.gateway.cancel(cancel)
    assert [name for name, _ in probe] == ["submit", "cancel"]
    assert probe.journaled_at_call == [True, True]


@pytest.mark.requirement("G08-07")
def test_an_adapter_that_raises_leaves_the_request_journaled_and_a_re_send_forwards_it_again() -> None:
    rig = _rig()
    order = samples.order_event()
    rig.adapter.calls = None  # type: ignore[assignment]  # makes the fixture adapter raise on use
    with pytest.raises(AttributeError):
        rig.gateway.submit(order)
    # Write-ahead: the request was journaled before the adapter was called.
    assert rig.ledger.order(order.client_order_id) is order
    length = len(rig.journal)
    rig.adapter.calls = []
    rig.gateway.submit(order)
    # Forwarded again (the adapter is idempotent per order_event_id), journaled once.
    assert rig.requests() == [("submit", order)]
    assert len(rig.journal) == length
    assert rig.ledger.order(order.client_order_id) is order


@pytest.mark.requirement("G08-07")
def test_a_repeated_identical_submit_is_journaled_once_and_forwarded_with_the_same_identity() -> None:
    rig = _rig()
    order = samples.order_event()
    for _ in range(3):
        assert rig.gateway.submit(order) is None
    length = len(rig.journal)
    # An equal request built again (as after a restart) is the same request.
    again = samples.order_event()
    assert again == order
    assert again is not order
    rig.gateway.submit(again)
    assert len(rig.journal) == length
    assert rig.ledger.order(order.client_order_id) is order
    forwarded = [request for _, request in rig.requests()]
    assert len(forwarded) == 4
    assert {request.order_event_id for request in forwarded} == {order.order_event_id}
    assert [entry.record for entry in rig.journal.read()].count(order) == 1


@pytest.mark.requirement("G08-07")
def test_repeated_cancel_and_replace_requests_are_journaled_once() -> None:
    rig, held, stop = _holding_rig()
    tighten = rig.next(stop, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2649.00"))
    rig.gateway.replace(tighten)
    length = len(rig.journal)
    rig.gateway.replace(tighten)
    assert len(rig.journal) == length
    assert rig.requests() == [("replace", tighten), ("replace", tighten)]
    exit_order = _child(rig, held, OrderIntent.EXIT, OrderType.LIMIT, volume_lots=D("0.25"))
    rig.gateway.submit(exit_order)
    cancel = rig.next(exit_order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    rig.gateway.cancel(cancel)
    length = len(rig.journal)
    rig.gateway.cancel(cancel)
    assert len(rig.journal) == length
    assert rig.ledger.order(exit_order.client_order_id) is cancel
    assert [name for name, _ in rig.requests()] == ["replace", "replace", "submit", "cancel", "cancel"]


@pytest.mark.requirement("G08-07", "S08-ACC-06")
def test_a_request_re_sent_after_the_order_progressed_never_creates_a_second_order() -> None:
    rig, submit = _entry_rig()
    for report in samples.order_lifecycle()[1:]:
        rig.clock.advance_to(report.receive_time_ns)
        assert rig.report(report).events == (report,)
        length = len(rig.journal)
        # Whether the gateway forwards the already journaled request again or refuses it: the journal
        # never gets a second order and the adapter only ever sees the same order_event_id.
        with contextlib.suppress(GieError):
            rig.gateway.submit(submit)
        assert len(rig.journal) == length
        assert rig.ledger.order(submit.client_order_id) is report
        assert {request.order_event_id for _, request in rig.requests()} == {submit.order_event_id}


def test_a_changed_request_under_a_journaled_identity_is_refused() -> None:
    rig, order = _entry_rig()
    changed = dataclasses.replace(order, volume_lots=D("0.40"))
    assert changed.order_event_id == order.order_event_id
    with _Unchanged(rig), pytest.raises(GieError):
        rig.gateway.submit(changed)
    assert rig.requests() == [("submit", order)]
