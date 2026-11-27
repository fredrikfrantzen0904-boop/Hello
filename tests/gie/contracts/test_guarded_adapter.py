"""Acceptance test S00-ACC-04 "paper-only routing lock": the order gateway (layer 3 of the lock).

Requirements: G08-03 (live lock), G08-10 (never enable real execution during this project phase),
M-AUTH-19 (keep real order routing disabled), OWN-14 (live execution remains disabled) and G08-07
(idempotent orders).

Expected behaviour is taken from the module docstrings of ``gie/contracts/guarded.py`` and
``gie/core/routing_lock.py``, from ``gie.contracts.interfaces`` (``ExecutionAdapter``,
``OrderGateway``) and from ``specifications/SHARED_CONTRACTS.md`` section 9.3 - not from the
implementation.  An execution engine reaches an adapter only through
``GuardedExecutionAdapter``, which before every call authorises the adapter (registered simulated
class, exact type, unchanged since registration), checks that the request is a genuine
``OrderEvent`` of the guard's run mode, checks that it is authorised (entry: selected plan and
unexpired approval; everything else: the open GIE position it may only reduce) and checks the
sequence rules.  Events coming back from the adapter pass the same checks.

SYNTHETIC FIXTURE: the adapter classes defined in this module are inert in-memory fakes.  They
execute nothing, connect to nothing, hold no credentials and only remember how they were called.
All records are built with the labelled builders of ``tests/gie/samples.py``
(``DataOrigin.SYNTHETIC_FIXTURE``); none is market data, an owner decision or evidence of trading
performance.  Every test works on a private copy of the adapter registry, so no registration
leaks into other tests.
"""

from __future__ import annotations

import contextlib
import gc
from dataclasses import dataclass
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest

from gie.contracts import (
    ExecutionVenue,
    GuardedExecutionAdapter,
    LifecycleLedger,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    OrderType,
    PlanStatus,
    Position,
    ReplayClock,
    RiskDecision,
    RunMode,
    Side,
    Tick,
    TradePlan,
    identity,
)
from gie.core import routing_lock
from gie.core.errors import (
    ContractValidationError,
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


class _PermissiveLedger(LifecycleLedger):
    """SYNTHETIC FIXTURE: a ledger subclass that would accept every sequence."""

    def check(self, record: Any) -> bool:
        return True


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


# --------------------------------------------------------------------------- rig and builders
@dataclass
class _Rig:
    adapter: _SimulatedAdapter
    ledger: LifecycleLedger
    clock: ReplayClock
    gateway: GuardedExecutionAdapter

    def requests(self) -> list[tuple[str, Any]]:
        """Order requests that reached the adapter, in order."""
        return [call for call in self.adapter.calls if call[0] in _REQUEST_METHODS]


def _rig(mode: object = "REPLAY", *, now_ns: int = NOW) -> _Rig:
    adapter = _SimulatedAdapter()
    ledger = LifecycleLedger()
    clock = ReplayClock(now_ns)
    gateway = GuardedExecutionAdapter(adapter, RoutingGuard(mode), clock=clock, ledger=ledger)
    return _Rig(adapter, ledger, clock, gateway)


def _triple(**order_overrides: Any) -> tuple[TradePlan, RiskDecision, OrderEvent]:
    plan = samples.trade_plan()
    return plan, samples.risk_decision(plan), samples.order_event(**order_overrides)


def _paper_triple() -> tuple[TradePlan, RiskDecision, OrderEvent]:
    plan = samples.trade_plan(mode=RunMode.PAPER)
    return plan, samples.risk_decision(plan, mode=RunMode.PAPER), samples.order_event(mode=RunMode.PAPER)


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


def _child(position: Position, intent: OrderIntent, order_type: OrderType, **overrides: Any) -> OrderEvent:
    """SUBMIT_REQUESTED of a non-entry order of *position* on the reducing side."""
    fields: dict[str, Any] = {
        "side": Side.SELL if position.side is Side.BUY else Side.BUY,
        "intent": intent,
        "order_type": order_type,
        "volume_lots": position.volume_lots,
        "limit_price": D("2652.80") if order_type is OrderType.LIMIT else None,
        "trigger_price": position.stop_price if order_type is OrderType.STOP else None,
        "reference_price": None,
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


def _nothing_reached(rig: _Rig, *requests: OrderEvent) -> None:
    """Fail closed: the adapter was not called and the ledger did not learn about the request."""
    assert rig.adapter.calls == []
    assert rig.ledger.open_orders() == ()
    for request in requests:
        assert rig.ledger.order(request.client_order_id) is None


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


# =========================================================================== authorised traffic
def test_an_authorised_entry_reaches_the_adapter_and_the_ledger() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    assert rig.gateway.submit(order, plan=plan, decision=decision) is None
    assert rig.adapter.calls == [("submit", order)]
    assert rig.adapter.calls[0][1] is order
    assert rig.ledger.order(order.client_order_id) is order
    assert rig.ledger.open_orders() == (order,)


def test_an_authorised_entry_reaches_the_adapter_in_paper_mode_too() -> None:
    rig = _rig("PAPER")
    plan, decision, order = _paper_triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    assert rig.requests() == [("submit", order)]


def test_authorised_reducing_orders_reach_the_adapter() -> None:
    rig = _rig()
    held = samples.position()
    stop = samples.protective_stop_lifecycle()[0]
    take_profit = _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT)
    exit_order = _child(held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.25"))
    for request in (stop, take_profit, exit_order):
        rig.gateway.submit(request, position=held)
        assert rig.ledger.order(request.client_order_id) is request
    assert rig.requests() == [("submit", stop), ("submit", take_profit), ("submit", exit_order)]


def test_reports_of_the_adapter_are_checked_admitted_and_returned_in_order() -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    submit, accepted, partial, filled = samples.order_lifecycle()
    rig.gateway.submit(submit, plan=plan, decision=decision)
    assert rig.gateway.poll() == ()
    rig.adapter.reports.extend([accepted, partial])
    reports = rig.gateway.poll()
    assert isinstance(reports, tuple)
    assert reports == (accepted, partial)
    assert rig.ledger.order(submit.client_order_id) is partial
    rig.adapter.reports.append(filled)
    assert rig.gateway.poll() == (filled,)
    assert rig.ledger.order(submit.client_order_id) is filled
    assert rig.ledger.open_orders() == ()


@pytest.mark.requirement("G04-11")
def test_a_tightening_stop_replacement_reaches_the_adapter() -> None:
    rig = _rig()
    held = samples.position()
    submit, working, tighten, replaced = samples.protective_stop_lifecycle()
    rig.gateway.submit(submit, position=held)
    rig.adapter.reports.append(working)
    assert rig.gateway.poll() == (working,)
    rig.gateway.replace(tighten, position=held)
    assert rig.requests() == [("submit", submit), ("replace", tighten)]
    rig.adapter.reports.append(replaced)
    assert rig.gateway.poll() == (replaced,)
    newest = rig.ledger.order(submit.client_order_id)
    assert newest is replaced
    assert newest.trigger_price == D("2649.90")


def test_cancelling_an_entry_only_reduces_risk_and_needs_no_position() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    cancel = _next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    rig.gateway.cancel(cancel)
    assert rig.requests() == [("submit", order), ("cancel", cancel)]
    assert rig.ledger.order(order.client_order_id) is cancel


def test_an_entry_may_be_shrunk_but_not_grown_through_the_gateway() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    accepted = _next(order, E.ACCEPTED, S.WORKING)
    rig.adapter.reports.append(accepted)
    rig.gateway.poll()
    grow = _next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.60"))
    with pytest.raises(IllegalTransitionError):
        rig.gateway.replace(grow)
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(order.client_order_id) is accepted
    shrink = _next(accepted, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_volume_lots=D("0.30"))
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
    plan, decision, order = _triple()
    held = samples.position()
    accepted = _next(order, E.ACCEPTED, S.WORKING)
    ledger = LifecycleLedger()
    gateway = GuardedExecutionAdapter.create(
        _SimulatedAdapter,
        RoutingGuard("REPLAY"),
        clock=ReplayClock(NOW),
        ledger=ledger,
        reports=(accepted,),
        positions=(held,),
    )
    assert type(gateway) is GuardedExecutionAdapter
    assert gateway.open_positions() == (held,)
    gateway.submit(order, plan=plan, decision=decision)
    assert ledger.order(order.client_order_id) is order
    assert gateway.poll() == (accepted,)
    assert ledger.order(order.client_order_id) is accepted


# =========================================================================== adapter refused
def test_an_unregistered_adapter_class_is_refused() -> None:
    adapter = _UnregisteredAdapter()
    for mode in ("REPLAY", "PAPER"):
        with pytest.raises(LiveRoutingLockedError) as caught:
            GuardedExecutionAdapter(
                adapter, RoutingGuard(mode), clock=ReplayClock(NOW), ledger=LifecycleLedger()
            )
        assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter.create(
            _UnregisteredAdapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=LifecycleLedger()
        )
    assert adapter.calls == []


@pytest.mark.parametrize("not_an_adapter", [None, object(), "adapter", _SimulatedAdapter, 42])
def test_objects_that_are_no_adapter_instances_are_refused(not_an_adapter: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            not_an_adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=LifecycleLedger()
        )


def test_a_subclass_of_a_registered_adapter_is_refused() -> None:
    adapter = _SubclassedAdapter()
    assert isinstance(adapter, _SimulatedAdapter)
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=LifecycleLedger()
        )
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter.create(
            _SubclassedAdapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=LifecycleLedger()
        )
    assert adapter.calls == []


def test_a_routing_guard_look_alike_is_refused() -> None:
    adapter = _SimulatedAdapter()
    for fake in (_PermissiveGuard(), None, "REPLAY", RunMode.REPLAY, RoutingGuard):
        with pytest.raises(LiveRoutingLockedError):
            GuardedExecutionAdapter(adapter, fake, clock=ReplayClock(NOW), ledger=LifecycleLedger())  # type: ignore[arg-type]
        with pytest.raises(LiveRoutingLockedError):
            GuardedExecutionAdapter.create(
                _SimulatedAdapter,
                fake,  # type: ignore[arg-type]
                clock=ReplayClock(NOW),
                ledger=LifecycleLedger(),
            )
    # A permissive guard must not even help an unregistered adapter.
    with pytest.raises(LiveRoutingLockedError):
        GuardedExecutionAdapter(
            _UnregisteredAdapter(),
            _PermissiveGuard(),  # type: ignore[arg-type]
            clock=ReplayClock(NOW),
            ledger=LifecycleLedger(),
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
    # Refusing a subclass in ``__init_subclass__`` does not destroy the class object: it can still
    # be found through ``RoutingGuard.__subclasses__()``.  The gateway must not accept it as a guard.
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
                    GuardedExecutionAdapter(adapter, forged, clock=ReplayClock(NOW), ledger=LifecycleLedger())
            with pytest.raises(LiveRoutingLockedError):
                GuardedExecutionAdapter.create(
                    _UnregisteredAdapter, forged, clock=ReplayClock(NOW), ledger=LifecycleLedger()
                )
    finally:
        survivors.clear()
        gc.enable()
        gc.collect()
    assert rogue.calls == []


def test_swapping_the_private_references_of_a_gateway_does_not_reach_an_unregistered_adapter() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rogue = _UnregisteredAdapter()
    swapped = True
    try:
        rig.gateway._adapter = rogue  # type: ignore[misc]
    except (AttributeError, TypeError, GieError):
        swapped = False  # an implementation that forbids the assignment is fine as well
    if swapped:
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.submit(order, plan=plan, decision=decision)
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.poll()
        with pytest.raises(LiveRoutingLockedError):
            rig.gateway.on_tick(samples.tick())
        assert rig.ledger.order(order.client_order_id) is None
    with contextlib.suppress(AttributeError, TypeError, GieError):
        rig.gateway._guard = _PermissiveGuard()  # type: ignore[misc]
    with contextlib.suppress(GieError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    assert rogue.calls == []


def test_the_gateway_needs_the_genuine_lifecycle_ledger() -> None:
    adapter = _SimulatedAdapter()
    for fake in (_PermissiveLedger(), None, object(), {}):
        with pytest.raises(LiveRoutingLockedError):
            GuardedExecutionAdapter(adapter, RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=fake)  # type: ignore[arg-type]
    assert adapter.calls == []


def test_an_adapter_class_modified_after_registration_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rerouted: list[Any] = []

    def routed_elsewhere(self: Any, request: OrderEvent) -> None:
        rerouted.append(request)

    monkeypatch.setattr(_SimulatedAdapter, "submit", routed_elsewhere)
    with pytest.raises(LiveRoutingLockedError) as caught:
        rig.gateway.submit(order, plan=plan, decision=decision)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    assert rerouted == []
    _nothing_reached(rig, order)
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
            _SimulatedAdapter(), RoutingGuard("REPLAY"), clock=ReplayClock(NOW), ledger=LifecycleLedger()
        )
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    assert rerouted == []


@pytest.mark.parametrize("method", ["cancel", "replace", "on_tick", "poll", "open_positions"])
def test_modifying_any_adapter_method_locks_every_operation(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    monkeypatch.setattr(_SimulatedAdapter, method, lambda self, *arguments: ())
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    _nothing_reached(rig, order)


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
    plan, decision, order = _triple()
    monkeypatch.setattr(_SimulatedAdapter, attribute, value)
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.poll()
    _nothing_reached(rig, order)


def test_removing_an_adapter_method_after_registration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    monkeypatch.delattr(_SimulatedAdapter, "poll")
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    _nothing_reached(rig, order)


def test_the_unmodified_adapter_works_again_once_the_modification_is_gone() -> None:
    # Control for the tests above: without a modification the very same calls go through.
    rig = _rig()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    assert rig.requests() == [("submit", order)]
    assert rig.gateway.poll() == ()


# =========================================================================== entry refused
@pytest.mark.parametrize("given", ["nothing", "plan_only", "decision_only", "position_instead"])
def test_an_entry_without_its_plan_and_its_risk_decision_is_refused(given: str) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    arguments: dict[str, Any] = {
        "nothing": {},
        "plan_only": {"plan": plan},
        "decision_only": {"decision": decision},
        "position_instead": {"position": samples.position()},
    }[given]
    with pytest.raises(IllegalTransitionError) as caught:
        rig.gateway.submit(order, **arguments)
    assert caught.value.details.get("client_order_id") == order.client_order_id
    _nothing_reached(rig, order)


def test_look_alikes_of_plan_and_decision_do_not_authorise_an_entry() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    fake_plan = SimpleNamespace(**{spec.name: getattr(plan, spec.name) for spec in TradePlan.FIELD_SPECS})
    fake_decision = SimpleNamespace(
        approved=True, **{spec.name: getattr(decision, spec.name) for spec in RiskDecision.FIELD_SPECS}
    )
    for arguments in ({"plan": fake_plan, "decision": decision}, {"plan": plan, "decision": fake_decision}):
        with pytest.raises(IllegalTransitionError):
            rig.gateway.submit(order, **arguments)
    _nothing_reached(rig, order)


@pytest.mark.requirement("G04-09", "G04-12", "S08-ACC-06")
@pytest.mark.parametrize(
    "deviation",
    [
        "volume_above_grant",
        "rejected_decision",
        "plan_not_selected",
        "plan_changed_after_approval",
        "other_stop_attached",
        "other_price",
        "order_outlives_approval",
        "approval_of_another_plan",
    ],
)
def test_an_entry_that_fails_authorisation_never_reaches_the_adapter(deviation: str) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    if deviation == "volume_above_grant":
        order = samples.order_event(volume_lots=D("0.51"))
    elif deviation == "rejected_decision":
        decision = samples.rejected_risk_decision(plan)
    elif deviation == "plan_not_selected":
        plan = samples.trade_plan(status=PlanStatus.ARMED)
        decision = samples.risk_decision(plan)
    elif deviation == "plan_changed_after_approval":
        plan = samples.trade_plan(max_spread=D("0.50"))
    elif deviation == "other_stop_attached":
        order = samples.order_event(attached_stop_price=D("2647.00"))
    elif deviation == "other_price":
        order = samples.order_event(limit_price=D("2649.90"))
    elif deviation == "order_outlives_approval":
        order = samples.order_event(valid_until_ns=APPROVAL_EXPIRY + 1)
    else:
        decision = samples.risk_decision(samples.trade_plan(variant=1))
    with pytest.raises(IllegalTransitionError) as caught:
        rig.gateway.submit(order, plan=plan, decision=decision)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    _nothing_reached(rig, order)


def test_the_gateway_checks_expiry_against_its_own_clock_and_fails_closed_at_the_boundary() -> None:
    plan, decision, order = _triple()
    for now_ns in (APPROVAL_EXPIRY, APPROVAL_EXPIRY + 1, APPROVAL_EXPIRY + 3600 * NS_PER_SECOND):
        late = _rig(now_ns=now_ns)
        with pytest.raises(StaleDataError) as caught:
            late.gateway.submit(order, plan=plan, decision=decision)
        assert isinstance(caught.value, GieError)
        _nothing_reached(late, order)
    in_time = _rig(now_ns=APPROVAL_EXPIRY - 1)
    in_time.gateway.submit(order, plan=plan, decision=decision)
    assert in_time.requests() == [("submit", order)]


def test_time_passing_on_the_injected_clock_expires_an_approval_that_was_still_valid() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rig.clock.advance_to(APPROVAL_EXPIRY)
    with pytest.raises(StaleDataError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    _nothing_reached(rig, order)


@pytest.mark.requirement("S08-ACC-06")
def test_a_second_plan_of_the_same_alternative_group_never_reaches_the_adapter() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    other_plan = samples.trade_plan(variant=1)
    other_decision = samples.risk_decision(other_plan, seq=1)
    other_order = samples.order_event(plan_id=other_plan.plan_id, risk_decision_id=other_decision.decision_id)
    assert other_order.alternative_group_id == order.alternative_group_id
    assert other_order.client_order_id != order.client_order_id
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(other_order, plan=other_plan, decision=other_decision)
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(other_order.client_order_id) is None


# =========================================================================== other orders refused
@pytest.mark.parametrize("position", [None, "position", 0])
def test_a_non_entry_order_without_its_position_is_refused(position: object) -> None:
    rig = _rig()
    held = samples.position()
    look_alike = SimpleNamespace(**{spec.name: getattr(held, spec.name) for spec in Position.FIELD_SPECS})
    for request in (
        samples.protective_stop_lifecycle()[0],
        _child(held, OrderIntent.TAKE_PROFIT, OrderType.LIMIT),
        _child(held, OrderIntent.EXIT, OrderType.MARKET),
    ):
        for candidate in (position, look_alike):
            with pytest.raises(IllegalTransitionError) as caught:
                rig.gateway.submit(request, position=candidate)  # type: ignore[arg-type]
            assert caught.value.details.get("client_order_id") == request.client_order_id
        _nothing_reached(rig, request)


def test_plan_and_decision_do_not_replace_the_position_of_a_non_entry_order() -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    stop = samples.protective_stop_lifecycle()[0]
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(stop, plan=plan, decision=decision)
    _nothing_reached(rig, stop)


@pytest.mark.requirement("G04-09", "G04-10", "G04-11")
@pytest.mark.parametrize(
    "deviation",
    ["adds_to_the_position", "above_open_volume", "looser_stop", "partial_cover", "external", "closed"],
)
def test_a_non_entry_order_that_does_not_reduce_its_position_never_reaches_the_adapter(
    deviation: str,
) -> None:
    rig = _rig()
    held = samples.position(stop_price=D("2649.90"))
    if deviation == "adds_to_the_position":
        request = _child(held, OrderIntent.EXIT, OrderType.MARKET, side=Side.BUY)
    elif deviation == "above_open_volume":
        request = _child(held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.51"))
    elif deviation == "looser_stop":
        request = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, trigger_price=D("2647.80"))
    elif deviation == "partial_cover":
        request = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, volume_lots=D("0.25"))
    elif deviation == "external":
        held = samples.external_position(symbol="XAUUSD")
        request = _child(held, OrderIntent.EXIT, OrderType.MARKET)
    else:
        held = samples.position_lifecycle()[2]
        request = _child(held, OrderIntent.EXIT, OrderType.MARKET, volume_lots=D("0.25"))
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(request, position=held)
    _nothing_reached(rig, request)


@pytest.mark.requirement("G04-11", "G04-12")
def test_a_widening_stop_replacement_never_reaches_the_adapter() -> None:
    rig = _rig()
    held = samples.position()
    submit, working, _tighten, _replaced = samples.protective_stop_lifecycle()
    rig.gateway.submit(submit, position=held)
    rig.adapter.reports.append(working)
    rig.gateway.poll()
    widen = _next(working, E.REPLACE_REQUESTED, S.PENDING_REPLACE, requested_trigger_price=D("2640.00"))
    with pytest.raises(IllegalTransitionError):
        rig.gateway.replace(widen, position=held)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.replace(widen)
    assert rig.requests() == [("submit", submit)]
    assert rig.ledger.order(submit.client_order_id) is working


@pytest.mark.requirement("G04-11", "G04-12")
def test_the_protective_stop_of_an_open_position_cannot_be_cancelled_through_the_gateway() -> None:
    rig = _rig()
    held = samples.position()
    submit, working, *_ = samples.protective_stop_lifecycle()
    rig.gateway.submit(submit, position=held)
    rig.adapter.reports.append(working)
    rig.gateway.poll()
    cancel = _next(working, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.cancel(cancel, position=held)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.cancel(cancel)
    assert rig.requests() == [("submit", submit)]
    assert rig.ledger.order(submit.client_order_id) is working
    # Once the position is closed the stop is only a leftover and may be withdrawn.
    closed = samples.position_lifecycle()[2]
    rig.gateway.cancel(cancel, position=closed)
    assert rig.requests() == [("submit", submit), ("cancel", cancel)]


@pytest.mark.requirement("G04-09", "G04-10")
@pytest.mark.parametrize("newest", ["partially_closed", "closed"])
def test_an_outdated_position_snapshot_does_not_authorise_more_than_the_open_volume(newest: str) -> None:
    # SHARED_CONTRACTS 9.3: every other order "needs the open GIE position it belongs to and may only
    # reduce it ... volume not above the open volume".  The gateway holds the run's ledger, which
    # keeps the newest snapshot of every position.  An older snapshot than the one the ledger knows
    # states an open volume that no longer exists; an exit of that size would open a position on
    # the opposite side without plan or approval.
    rig = _rig()
    opened, reduced, closed = samples.position_lifecycle()
    history = [opened, reduced] if newest == "partially_closed" else [opened, reduced, closed]
    for snapshot in history:
        assert rig.ledger.admit(snapshot) is True
    current = rig.ledger.position(opened.position_id)
    assert current is history[-1]
    assert current.volume_lots < opened.volume_lots
    exit_of_the_old_volume = _child(opened, OrderIntent.EXIT, OrderType.MARKET)
    assert exit_of_the_old_volume.volume_lots == D("0.50") > current.volume_lots
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(exit_of_the_old_volume, position=opened)
    _nothing_reached(rig, exit_of_the_old_volume)


def test_an_entry_for_a_plan_the_ledger_knows_to_be_withdrawn_never_reaches_the_adapter() -> None:
    # SHARED_CONTRACTS 9.3: "an entry needs its SELECTED plan".  The run's ledger keeps the newest
    # revision of every plan; once that revision is WITHDRAWN the earlier SELECTED revision is no
    # longer the plan's state, even though its approval has not expired yet.
    rig = _rig()
    plan, decision, order = _triple()
    moment = T0 + NS_PER_MS
    withdrawn = samples.trade_plan(
        revision=1,
        seq=1,
        status=PlanStatus.WITHDRAWN,
        status_reasons=("FIXTURE_INVALIDATED",),
        decision_time_ns=moment,
        as_of_time_ns=moment,
    )
    assert rig.ledger.admit(plan) is True
    assert rig.ledger.admit(withdrawn) is True
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    _nothing_reached(rig, order)


# =========================================================================== request shape refused
@pytest.mark.parametrize(
    ("method", "event_index"),
    [("submit", 1), ("submit", 3), ("cancel", 0), ("cancel", 1), ("replace", 0), ("replace", 2)],
)
def test_a_request_of_the_wrong_event_type_for_the_method_is_refused(method: str, event_index: int) -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    event = samples.order_lifecycle()[event_index]
    arguments: dict[str, Any] = {"plan": plan, "decision": decision} if method == "submit" else {}
    with pytest.raises(IllegalTransitionError) as caught:
        getattr(rig.gateway, method)(event, **arguments)
    assert caught.value.details.get("actual") == event.event_type.value
    _nothing_reached(rig, event)


def test_requests_of_the_other_kinds_are_refused_by_each_method() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    cancel = _next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    stop_working = samples.protective_stop_lifecycle()[1]
    replace = samples.protective_stop_lifecycle()[2]
    held = samples.position()
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(cancel, plan=plan, decision=decision)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.submit(replace, position=held)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.replace(cancel)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.cancel(replace, position=held)
    with pytest.raises(IllegalTransitionError):
        rig.gateway.replace(stop_working, position=held)
    _nothing_reached(rig, order, replace)


@pytest.mark.parametrize("method", ["submit", "cancel", "replace"])
def test_an_order_event_look_alike_is_refused(method: str) -> None:
    rig = _rig()
    plan, decision, order = _triple()
    fields = {spec.name: getattr(order, spec.name) for spec in OrderEvent.FIELD_SPECS}
    arguments: dict[str, Any] = {"plan": plan, "decision": decision} if method == "submit" else {}
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
        with pytest.raises(ContractValidationError) as caught:
            getattr(rig.gateway, method)(look_alike, **arguments)
        assert caught.value.code == "GIE.CONTRACT.INVALID"
    _nothing_reached(rig, order)


def test_requests_of_another_run_mode_than_the_guards_are_refused() -> None:
    replay_plan, replay_decision, replay_order = _triple()
    paper_plan, paper_decision, paper_order = _paper_triple()
    paper_rig = _rig("PAPER")
    with pytest.raises(LiveRoutingLockedError) as caught:
        paper_rig.gateway.submit(replay_order, plan=replay_plan, decision=replay_decision)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    _nothing_reached(paper_rig, replay_order)
    replay_rig = _rig("REPLAY")
    with pytest.raises(LiveRoutingLockedError):
        replay_rig.gateway.submit(paper_order, plan=paper_plan, decision=paper_decision)
    held = samples.position()
    paper_stop = _child(held, OrderIntent.PROTECTIVE_STOP, OrderType.STOP, mode=RunMode.PAPER)
    with pytest.raises(LiveRoutingLockedError):
        replay_rig.gateway.submit(paper_stop, position=held)
    with pytest.raises(LiveRoutingLockedError):
        replay_rig.gateway.cancel(_next(paper_order, E.CANCEL_REQUESTED, S.PENDING_CANCEL))
    _nothing_reached(replay_rig, paper_order, paper_stop)
    # Control: each order is accepted by the gateway of its own mode.
    paper_rig.gateway.submit(paper_order, plan=paper_plan, decision=paper_decision)
    replay_rig.gateway.submit(replay_order, plan=replay_plan, decision=replay_decision)
    assert paper_rig.requests() == [("submit", paper_order)]
    assert replay_rig.requests() == [("submit", replay_order)]


# =========================================================================== reports refused
@pytest.mark.parametrize("junk", [None, "FILLED", 42, {"event_type": "FILLED"}, ("FILLED",)])
def test_an_adapter_that_returns_non_order_events_from_poll_is_refused(junk: object) -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    submit, accepted, *_ = samples.order_lifecycle()
    rig.gateway.submit(submit, plan=plan, decision=decision)
    rig.adapter.reports.extend([accepted, junk])
    with pytest.raises(ContractValidationError) as caught:
        rig.gateway.poll()
    assert caught.value.code == "GIE.CONTRACT.INVALID"
    # Nothing of a batch that contains a non-event is taken over.
    assert rig.ledger.order(submit.client_order_id) is submit


def test_an_unvalidated_order_event_subclass_from_poll_is_refused() -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    submit, accepted, *_ = samples.order_lifecycle()
    rig.gateway.submit(submit, plan=plan, decision=decision)
    fields = {spec.name: getattr(accepted, spec.name) for spec in OrderEvent.FIELD_SPECS}
    for forged in (
        _UnvalidatedOrderEvent(**fields),
        _UnvalidatedOrderEvent(**{**fields, "venue": "LIVE"}),
        SimpleNamespace(is_request=False, **fields),
    ):
        rig.adapter.reports.append(forged)
        with pytest.raises(ContractValidationError):
            rig.gateway.poll()
        assert rig.ledger.order(submit.client_order_id) is submit


def test_an_adapter_reports_broker_events_and_cannot_issue_requests() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    stop_submit = samples.protective_stop_lifecycle()[0]
    other_plan = samples.trade_plan(variant=2)
    foreign_entry = samples.order_event(
        plan_id=other_plan.plan_id,
        risk_decision_id=samples.risk_decision(other_plan).decision_id,
        alternative_group_id=identity.alternative_group_id(samples.unscored_candidate().opportunity_id),
    )
    cancel = _next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    for request in (stop_submit, foreign_entry, cancel):
        assert request.is_request is True
        rig.adapter.reports.append(request)
        with pytest.raises(IllegalTransitionError) as caught:
            rig.gateway.poll()
        assert caught.value.details.get("client_order_id") == request.client_order_id
    assert rig.ledger.order(order.client_order_id) is order
    assert rig.ledger.order(stop_submit.client_order_id) is None
    assert rig.ledger.order(foreign_entry.client_order_id) is None
    assert rig.ledger.open_orders() == (order,)


def test_reports_of_another_run_mode_are_refused() -> None:
    rig = _rig("REPLAY")
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    paper_report = _next(order, E.ACCEPTED, S.WORKING, mode=RunMode.PAPER)
    rig.adapter.reports.append(paper_report)
    with pytest.raises(LiveRoutingLockedError):
        rig.gateway.poll()
    assert rig.ledger.order(order.client_order_id) is order


@pytest.mark.parametrize(
    "fault",
    ["skips_a_sequence_number", "unknown_order", "illegal_transition", "after_terminal", "grows_the_order"],
)
def test_a_report_that_breaks_the_order_state_machine_is_not_admitted(fault: str) -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    submit, accepted, partial, filled = samples.order_lifecycle()
    rig.gateway.submit(submit, plan=plan, decision=decision)
    admitted_before: list[OrderEvent] = [accepted]
    if fault == "skips_a_sequence_number":
        faulty = filled
    elif fault == "unknown_order":
        faulty = samples.protective_stop_lifecycle()[1]
    elif fault == "illegal_transition":
        faulty = _next(accepted, E.REPLACED, S.WORKING)
    elif fault == "after_terminal":
        admitted_before = [accepted, partial, filled]
        faulty = _next(filled, E.CANCELED, S.CANCELED, cumulative_filled_lots=D("0"), average_fill_price=None)
    else:
        faulty = _next(accepted, E.RECONCILED, S.WORKING, volume_lots=D("5.00"))
    follower = _next(accepted, E.EXPIRED, S.EXPIRED)
    rig.adapter.reports.extend([*admitted_before, faulty, follower])
    with pytest.raises(IllegalTransitionError) as caught:
        rig.gateway.poll()
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    # Reports before the faulty one were admitted; the faulty one and its successors were not.
    assert rig.ledger.order(submit.client_order_id) is admitted_before[-1]
    assert rig.ledger.order(faulty.client_order_id) is not faulty
    assert rig.ledger.order(submit.client_order_id) is not follower


# =========================================================================== idempotency
@pytest.mark.requirement("G08-07")
def test_a_repeated_identical_submit_reaches_the_adapter_once() -> None:
    rig = _rig()
    plan, decision, order = _triple()
    for _ in range(3):
        assert rig.gateway.submit(order, plan=plan, decision=decision) is None
    # An equal request built again (as after a restart) is the same request.
    again_plan, again_decision, again_order = _triple()
    assert again_order == order
    assert again_order is not order
    rig.gateway.submit(again_order, plan=again_plan, decision=again_decision)
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(order.client_order_id) is order


@pytest.mark.requirement("G08-07")
def test_repeated_identical_cancel_and_replace_requests_reach_the_adapter_once() -> None:
    rig = _rig()
    held = samples.position()
    submit, working, tighten, replaced = samples.protective_stop_lifecycle()
    rig.gateway.submit(submit, position=held)
    rig.gateway.submit(submit, position=held)
    rig.adapter.reports.append(working)
    rig.gateway.poll()
    rig.gateway.replace(tighten, position=held)
    rig.gateway.replace(tighten, position=held)
    assert rig.requests() == [("submit", submit), ("replace", tighten)]
    rig.adapter.reports.append(replaced)
    rig.gateway.poll()
    plan, decision, order = _triple()
    rig.gateway.submit(order, plan=plan, decision=decision)
    cancel = _next(order, E.CANCEL_REQUESTED, S.PENDING_CANCEL)
    rig.gateway.cancel(cancel)
    rig.gateway.cancel(cancel)
    assert rig.requests() == [
        ("submit", submit),
        ("replace", tighten),
        ("submit", order),
        ("cancel", cancel),
    ]


@pytest.mark.requirement("G08-07", "S08-ACC-06")
def test_a_request_re_sent_after_the_order_progressed_never_creates_a_second_order() -> None:
    rig = _rig()
    plan, decision, _order = _triple()
    submit, accepted, partial, filled = samples.order_lifecycle()
    rig.gateway.submit(submit, plan=plan, decision=decision)
    for report in (accepted, partial, filled):
        rig.adapter.reports.append(report)
        rig.gateway.poll()
        # Whether the gateway drops the duplicate silently or refuses it: it never reaches the adapter.
        with contextlib.suppress(IllegalTransitionError):
            rig.gateway.submit(submit, plan=plan, decision=decision)
        assert rig.requests() == [("submit", submit)]
        assert rig.ledger.order(submit.client_order_id) is report


def test_a_request_the_adapter_failed_on_is_not_recorded_as_sent() -> None:
    plan, decision, order = _triple()
    rig = _rig()
    rig.adapter.calls = None  # type: ignore[assignment]  # makes the fixture adapter raise on use
    with pytest.raises(AttributeError):
        rig.gateway.submit(order, plan=plan, decision=decision)
    assert rig.ledger.order(order.client_order_id) is None
    rig.adapter.calls = []
    rig.gateway.submit(order, plan=plan, decision=decision)
    assert rig.requests() == [("submit", order)]
    assert rig.ledger.order(order.client_order_id) is order
