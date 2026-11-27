"""Independent tests for the service interfaces (``gie.contracts.interfaces``).

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/SHARED_CONTRACTS.md`` section 10 (the ``Protocol`` classes ``TickSink``,
  ``TickSource``, ``RecordSink``, ``RecordListener``, ``RecordSource``, ``PlanScorer``, ``RiskGate``,
  ``ExecutionAdapter``, ``OrderGateway``, ``ProtectionPort`` and ``ModelRegistry`` are the shared
  interface surface),
* ``reviews/session_00/rework_2_change_notice.md`` section 5 (the gateway takes the request only and
  ``poll()`` returns a ``PollResult``; the reflex engine uses ``ProtectionPort``) and section 3
  (``commit()`` ends a step),
  section 6.2 (``MemoryJournal`` and ``FileJournal`` are the reference journals; history is read
  through ``RecordSource``) and section 9.3 (``GuardedExecutionAdapter`` is the ``OrderGateway``);
* the docstrings of ``gie/contracts/interfaces.py`` (which methods each interface has; "nothing
  here contains behaviour").

SYNTHETIC FIXTURE: the classes defined in this file are empty structural stand-ins that exist only
to check the shape of the interfaces.  They implement no subsystem, are never registered as
execution adapters, place no orders and return no data; the records used come from
``tests/gie/samples.py``.  No execution adapter is constructed here.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import pytest

from gie.contracts import interfaces
from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.guarded import GuardedExecutionAdapter, PollResult
from gie.contracts.interfaces import (
    ExecutionAdapter,
    ModelRegistry,
    OrderGateway,
    PlanScorer,
    ProtectionPort,
    RecordListener,
    RecordSink,
    RecordSource,
    RiskGate,
    TickSink,
    TickSource,
)
from gie.contracts.journal import FileJournal, JournalEntry, MemoryJournal
from gie.contracts.model_version import ModelKind, ModelStatus, ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import PlanEstimate, Target, TradePlan
from gie.core.errors import SequenceConflictError
from tests.gie import samples

#: The members of each interface as documented in ``gie/contracts/interfaces.py``.
DOCUMENTED_MEMBERS: dict[type, set[str]] = {
    TickSink: {"on_tick"},
    TickSource: {"ticks"},
    RecordSink: {"publish", "commit"},
    RecordListener: {"accepts", "on_record"},
    RecordSource: {"run_id", "read", "streams"},
    PlanScorer: {"score_plan"},
    RiskGate: {"evaluate", "release"},
    ExecutionAdapter: {
        "venue",
        "sends_real_orders",
        "submit",
        "cancel",
        "replace",
        "on_tick",
        "poll",
        "open_positions",
    },
    OrderGateway: {"submit", "cancel", "replace", "on_tick", "poll", "open_positions"},
    ProtectionPort: {"tighten_stop", "exit"},
    ModelRegistry: {"get", "active", "find", "history", "status_at", "register", "transition"},
}


def _members(protocol: type) -> set[str]:
    """Public members a protocol class declares (methods, properties and annotated attributes)."""
    declared = {name for name in vars(protocol) if not name.startswith("_")}
    return declared | set(vars(protocol).get("__annotations__", {}))


def _parameters(function: Any) -> list[tuple[str, Any, bool]]:
    """(name, kind, has default) of every parameter except ``self``."""
    signature = inspect.signature(function)
    return [
        (parameter.name, parameter.kind, parameter.default is not inspect.Parameter.empty)
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    ]


# =========================================================================== structural stand-ins
class _TickSinkShape:
    def on_tick(self, tick: Tick) -> None:
        return None


class _TickSourceShape:
    def ticks(self, streams: Sequence[str], start_ns: int, end_ns: int) -> Iterator[Tick]:
        return iter(())


class _RecordSinkShape:
    def publish(self, record: Record) -> int:
        return 0

    def commit(self) -> None:
        return None


class _RecordListenerShape:
    def accepts(self) -> tuple[type[Record], ...]:
        return (OrderEvent, Position)

    def on_record(self, record: Record) -> None:
        return None


class _RecordSourceShape:
    @property
    def run_id(self) -> str | None:
        return None

    def read(self, from_journal_seq: int = 0) -> Iterator[JournalEntry]:
        return iter(())

    def streams(self) -> tuple[str, ...]:
        return ()


class _PlanScorerShape:
    def score_plan(
        self,
        candidate: Candidate,
        *,
        entry_price: Decimal,
        stop_price: Decimal,
        targets: Sequence[Target],
    ) -> PlanEstimate | None:
        return None  # "no model can score the plan"


class _RiskGateShape:
    def evaluate(self, plan: TradePlan) -> RiskDecision:
        raise NotImplementedError("SYNTHETIC FIXTURE: structural stand-in only")

    def release(self, reservation_id: str) -> None:
        return None


class _OrderGatewayShape:
    def submit(self, request: OrderEvent) -> None:
        return None

    def cancel(self, request: OrderEvent) -> None:
        return None

    def replace(self, request: OrderEvent) -> None:
        return None

    def on_tick(self, tick: Tick) -> None:
        return None

    def poll(self) -> PollResult:
        return PollResult((), 0, ())

    def open_positions(self) -> tuple[Position, ...]:
        return ()


class _ProtectionPortShape:
    def tighten_stop(self, position_id: str, trigger_price: Decimal, reason_code: str) -> str:
        return "ord-fixture"  # SYNTHETIC FIXTURE: structural stand-in, no order is built

    def exit(self, position_id: str, volume_lots: Decimal, reason_code: str) -> str:
        return "ord-fixture"


class _ModelRegistryShape:
    def get(self, model_version_id: str) -> ModelVersion:
        raise KeyError(model_version_id)  # unknown id: the caller treats the model as not validated

    def active(self, kind: ModelKind, model_name: str) -> ModelVersion | None:
        return None

    def find(
        self,
        *,
        kind: ModelKind | None = None,
        status: ModelStatus | None = None,
        model_name: str | None = None,
    ) -> Sequence[ModelVersion]:
        return ()

    def history(self, model_version_id: str) -> Sequence[ModelVersion]:
        return ()

    def status_at(self, model_version_id: str, time_ns: int) -> ModelStatus | None:
        return None

    def register(self, entry: ModelVersion) -> None:
        return None

    def transition(self, entry: ModelVersion) -> None:
        return None


SHAPES: dict[type, type] = {
    TickSink: _TickSinkShape,
    TickSource: _TickSourceShape,
    RecordSink: _RecordSinkShape,
    RecordListener: _RecordListenerShape,
    RecordSource: _RecordSourceShape,
    PlanScorer: _PlanScorerShape,
    RiskGate: _RiskGateShape,
    OrderGateway: _OrderGatewayShape,
    ProtectionPort: _ProtectionPortShape,
    ModelRegistry: _ModelRegistryShape,
}

_BY_NAME = sorted(DOCUMENTED_MEMBERS, key=lambda protocol: protocol.__name__)


def _name(protocol: type) -> str:
    return protocol.__name__


# =========================================================================== the interface surface
@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18")
def test_the_eleven_documented_interfaces_exist() -> None:
    names = {
        "TickSink",
        "TickSource",
        "RecordSink",
        "RecordListener",
        "RecordSource",
        "PlanScorer",
        "RiskGate",
        "ExecutionAdapter",
        "OrderGateway",
        "ProtectionPort",
        "ModelRegistry",
    }
    assert {protocol.__name__ for protocol in DOCUMENTED_MEMBERS} == names
    for name in names:
        assert getattr(interfaces, name) in DOCUMENTED_MEMBERS
    defined_here = {
        name
        for name, value in vars(interfaces).items()
        if isinstance(value, type) and value.__module__ == interfaces.__name__
    }
    assert defined_here == names  # nothing else is part of the shared interface surface


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18")
@pytest.mark.parametrize("protocol", _BY_NAME, ids=_name)
def test_each_interface_is_a_runtime_checkable_protocol_with_the_documented_members(protocol: type) -> None:
    assert issubclass(protocol, Protocol)  # type: ignore[arg-type]
    assert getattr(protocol, "_is_protocol", False) is True
    assert _members(protocol) == DOCUMENTED_MEMBERS[protocol]
    # runtime_checkable: isinstance() works and an arbitrary object does not satisfy the interface.
    assert not isinstance(object(), protocol)
    assert not isinstance(samples.tick(), protocol)


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18")
@pytest.mark.parametrize("protocol", _BY_NAME, ids=_name)
def test_an_interface_contains_no_behaviour_and_cannot_be_instantiated(protocol: type) -> None:
    with pytest.raises(TypeError):
        protocol()
    assert protocol.__doc__  # every interface documents who implements it and who may call it


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18", "COM-07")
@pytest.mark.parametrize("protocol", sorted(SHAPES, key=_name), ids=_name)
def test_a_class_with_the_documented_methods_satisfies_the_interface(protocol: type) -> None:
    shape = SHAPES[protocol]
    assert isinstance(shape(), protocol)
    # ... and the stand-in declares exactly the documented members with the documented parameters.
    assert {name for name in vars(shape) if not name.startswith("_")} == DOCUMENTED_MEMBERS[protocol]
    for member in DOCUMENTED_MEMBERS[protocol]:
        declared = inspect.getattr_static(protocol, member)
        if isinstance(declared, property):
            assert isinstance(inspect.getattr_static(shape, member), property)
        else:
            assert _parameters(getattr(shape, member)) == _parameters(getattr(protocol, member))


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18", "COM-07")
@pytest.mark.parametrize(
    ("protocol", "missing"),
    [
        (protocol, member)
        for protocol in sorted(SHAPES, key=_name)
        for member in sorted(DOCUMENTED_MEMBERS[protocol])
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_a_class_that_lacks_one_documented_member_does_not_satisfy_the_interface(
    protocol: type, missing: str
) -> None:
    shape = SHAPES[protocol]
    members = {name: value for name, value in vars(shape).items() if not name.startswith("_")}
    del members[missing]
    incomplete = type(f"Incomplete{protocol.__name__}", (), members)
    assert not isinstance(incomplete(), protocol)
    complete = type(f"Complete{protocol.__name__}", (), {**members, missing: vars(shape)[missing]})
    assert isinstance(complete(), protocol)


@pytest.mark.requirement("S00-ASG-25")
def test_interfaces_are_not_interchangeable() -> None:
    assert not isinstance(_TickSinkShape(), RecordSink)
    assert not isinstance(_RecordSinkShape(), RecordSource)
    assert not isinstance(_RecordSourceShape(), RecordSink)
    assert not isinstance(_RiskGateShape(), OrderGateway)
    assert not isinstance(_ModelRegistryShape(), RecordSource)
    # An order gateway also consumes ticks (it forwards them to the simulated broker).
    assert isinstance(_OrderGatewayShape(), TickSink)
    assert not isinstance(_TickSinkShape(), OrderGateway)


# =========================================================================== journals
@pytest.mark.requirement("S00-ASG-25", "G08-04")
def test_memory_journal_is_a_record_sink_and_a_record_source() -> None:
    journal = MemoryJournal()
    assert isinstance(journal, RecordSink)
    assert isinstance(journal, RecordSource)
    assert issubclass(MemoryJournal, RecordSink)
    assert not isinstance(journal, TickSink)
    assert not isinstance(journal, ModelRegistry)
    assert _parameters(MemoryJournal.publish) == _parameters(RecordSink.publish)
    assert _parameters(MemoryJournal.commit) == _parameters(RecordSink.commit) == []
    assert _parameters(MemoryJournal.read) == _parameters(RecordSource.read)
    assert _parameters(MemoryJournal.streams) == _parameters(RecordSource.streams)
    assert isinstance(inspect.getattr_static(MemoryJournal, "run_id"), property)


@pytest.mark.requirement("S00-ASG-25", "G08-04")
def test_file_journal_is_a_record_sink_and_a_record_source(tmp_path: Path) -> None:
    with FileJournal(tmp_path / "run.journal") as journal:
        assert isinstance(journal, RecordSink)
        assert isinstance(journal, RecordSource)
        assert journal.run_id is None
        assert journal.publish(samples.run_manifest()) == 0
        assert journal.run_id == samples.RUN_ID


@pytest.mark.requirement("S00-ASG-25")
def test_a_journal_used_through_the_interfaces_behaves_as_documented() -> None:
    def write(sink: RecordSink, record: Record) -> int:
        return sink.publish(record)

    def history(source: RecordSource, start: int = 0) -> list[Record]:
        return [entry.record for entry in source.read(start)]

    journal = MemoryJournal()
    source: RecordSource = journal
    assert source.run_id is None
    assert source.streams() == ()
    assert history(source) == []
    manifest, tick, evidence = samples.run_manifest(), samples.tick(), samples.evidence()
    # "publish returns the record's position in the total order of the run ..."
    assert [write(journal, record) for record in (manifest, tick, evidence)] == [0, 1, 2]
    # "... and is idempotent per (stream, seq): publishing the same record twice has no further effect"
    assert write(journal, tick) == 1
    assert history(source) == [manifest, tick, evidence]
    # "different content at the same position raises SequenceConflictError"
    with pytest.raises(SequenceConflictError):
        write(journal, samples.tick(bid=Decimal("2650.11")))
    assert source.run_id == samples.RUN_ID
    assert history(source, 1) == [tick, evidence]
    assert all(type(entry) is JournalEntry for entry in source.read())
    assert source.streams() == tuple(sorted(source.streams()))
    assert len(source.streams()) == 3


# =========================================================================== order gateway
@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_guarded_execution_adapter_has_the_shape_of_an_order_gateway() -> None:
    # Checked on the class only: no adapter and no gateway is constructed in this test.
    assert issubclass(GuardedExecutionAdapter, OrderGateway)
    for member in sorted(DOCUMENTED_MEMBERS[OrderGateway]):
        implementation = inspect.getattr_static(GuardedExecutionAdapter, member)
        assert inspect.isfunction(implementation), member
        assert _parameters(implementation) == _parameters(getattr(OrderGateway, member)), member
    # The gateway takes the request only: plan, approval and position come from the run ledger,
    # never from the caller (change notice section 5).
    for call in (
        GuardedExecutionAdapter.submit,
        GuardedExecutionAdapter.cancel,
        GuardedExecutionAdapter.replace,
    ):
        assert _parameters(call) == [("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, False)]
    # Constructor: (adapter, guard, *, clock, journal), all required.
    assert _parameters(GuardedExecutionAdapter.__init__) == [
        ("adapter", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("guard", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("clock", inspect.Parameter.KEYWORD_ONLY, False),
        ("journal", inspect.Parameter.KEYWORD_ONLY, False),
    ]
    create = _parameters(inspect.getattr_static(GuardedExecutionAdapter, "create").__func__)
    assert create == [
        ("cls", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("adapter_type", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("guard", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("clock", inspect.Parameter.KEYWORD_ONLY, False),
        ("journal", inspect.Parameter.KEYWORD_ONLY, False),
        ("adapter_arguments", inspect.Parameter.VAR_KEYWORD, False),
    ]


@pytest.mark.requirement("S00-ASG-25", "M-AUTH-07")
def test_the_gateway_is_not_a_raw_execution_adapter_and_exposes_nothing_else() -> None:
    # The gateway declares no ``sends_real_orders`` and hides the adapter it guards.
    assert not hasattr(GuardedExecutionAdapter, "sends_real_orders")
    public = {name for name in vars(GuardedExecutionAdapter) if not name.startswith("_")}
    assert public == DOCUMENTED_MEMBERS[OrderGateway] | {"create", "venue", "mode"}
    assert all(name.startswith("_") for name in GuardedExecutionAdapter.__slots__)
    # The adapter interface has the same calls plus the two declarations of the routing lock.
    assert DOCUMENTED_MEMBERS[ExecutionAdapter] - DOCUMENTED_MEMBERS[OrderGateway] == {
        "venue",
        "sends_real_orders",
    }


@pytest.mark.requirement("S00-ASG-25")
def test_gateway_and_adapter_calls_take_the_documented_arguments() -> None:
    request_only = [("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, False)]
    for call in (
        OrderGateway.submit,
        OrderGateway.cancel,
        OrderGateway.replace,
        ExecutionAdapter.submit,
        ExecutionAdapter.cancel,
        ExecutionAdapter.replace,
    ):
        assert _parameters(call) == request_only
    assert _parameters(OrderGateway.poll) == _parameters(ExecutionAdapter.poll) == []
    assert _parameters(OrderGateway.on_tick) == _parameters(TickSink.on_tick)
    # The gateway's poll yields a PollResult; the adapter's poll the raw reports.
    hints = inspect.get_annotations(OrderGateway.poll, eval_str=True)
    assert hints["return"] is PollResult
    assert inspect.get_annotations(GuardedExecutionAdapter.poll, eval_str=True)["return"] is PollResult
    assert inspect.get_annotations(ExecutionAdapter.poll, eval_str=True)["return"] == Sequence[OrderEvent]


@pytest.mark.requirement("S00-ASG-25")
def test_poll_result_is_clean_exactly_when_nothing_was_refused() -> None:
    event = samples.order_event()
    clean = PollResult((event,), 2, ())
    assert clean.clean
    assert clean.events == (event,)
    assert clean.duplicates == 2
    refused = PollResult((), 0, (SequenceConflictError("SYNTHETIC FIXTURE: refused report"),))
    assert not refused.clean
    with pytest.raises(AttributeError):
        clean.duplicates = 0  # type: ignore[misc]


# =========================================================================== protection port, listener
@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18")
def test_protection_port_takes_the_documented_arguments_and_returns_the_order_id() -> None:
    expected = {
        "tighten_stop": ["position_id", "trigger_price", "reason_code"],
        "exit": ["position_id", "volume_lots", "reason_code"],
    }
    for member, names in expected.items():
        parameters = _parameters(getattr(ProtectionPort, member))
        assert parameters == [(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, False) for name in names]
        hints = inspect.get_annotations(getattr(ProtectionPort, member), eval_str=True)
        assert hints["return"] is str  # the client_order_id of the order that carries the intent
    assert inspect.get_annotations(ProtectionPort.tighten_stop, eval_str=True)["trigger_price"] is Decimal
    assert inspect.get_annotations(ProtectionPort.exit, eval_str=True)["volume_lots"] is Decimal
    # The reflex engine holds a protection port, never an order gateway or an adapter.
    assert not isinstance(_ProtectionPortShape(), OrderGateway)
    assert not isinstance(_OrderGatewayShape(), ProtectionPort)
    assert not isinstance(GuardedExecutionAdapter, ProtectionPort)
    assert not issubclass(GuardedExecutionAdapter, ProtectionPort)


@pytest.mark.requirement("S00-ASG-25", "S00-ASG-18")
def test_record_listener_names_the_contracts_it_wants_and_receives_records() -> None:
    assert _parameters(RecordListener.accepts) == []
    assert _parameters(RecordListener.on_record) == [
        ("record", inspect.Parameter.POSITIONAL_OR_KEYWORD, False)
    ]
    listener: RecordListener = _RecordListenerShape()
    assert all(issubclass(contract, Record) for contract in listener.accepts())
    # A record sink is not a listener and vice versa; a tick sink is not a listener.
    assert not isinstance(_RecordSinkShape(), RecordListener)
    assert not isinstance(_RecordListenerShape(), RecordSink)
    assert not isinstance(_TickSinkShape(), RecordListener)
    assert not isinstance(MemoryJournal(), RecordListener)


# =========================================================================== other interfaces
@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_model_registry_reads_and_writes_take_the_documented_arguments() -> None:
    assert _parameters(ModelRegistry.get) == [
        ("model_version_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, False)
    ]
    assert [name for name, _, _ in _parameters(ModelRegistry.active)] == ["kind", "model_name"]
    assert _parameters(ModelRegistry.find) == [
        ("kind", inspect.Parameter.KEYWORD_ONLY, True),
        ("status", inspect.Parameter.KEYWORD_ONLY, True),
        ("model_name", inspect.Parameter.KEYWORD_ONLY, True),
    ]
    assert [name for name, _, _ in _parameters(ModelRegistry.status_at)] == ["model_version_id", "time_ns"]
    assert [name for name, _, _ in _parameters(ModelRegistry.register)] == ["entry"]
    assert [name for name, _, _ in _parameters(ModelRegistry.transition)] == ["entry"]
    # Fail closed: an unknown id is a KeyError, which the caller treats as "not validated".
    registry: ModelRegistry = _ModelRegistryShape()
    with pytest.raises(KeyError):
        registry.get(samples.MODEL_ID)
    assert registry.active(ModelKind.NET_R_MODEL, samples.MODEL_NAME) is None


@pytest.mark.requirement("S00-ASG-25")
def test_plan_scorer_risk_gate_and_tick_source_take_the_documented_arguments() -> None:
    assert _parameters(PlanScorer.score_plan) == [
        ("candidate", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
        ("entry_price", inspect.Parameter.KEYWORD_ONLY, False),
        ("stop_price", inspect.Parameter.KEYWORD_ONLY, False),
        ("targets", inspect.Parameter.KEYWORD_ONLY, False),
    ]
    assert [name for name, _, _ in _parameters(RiskGate.evaluate)] == ["plan"]
    assert [name for name, _, _ in _parameters(RiskGate.release)] == ["reservation_id"]
    assert [name for name, _, _ in _parameters(TickSource.ticks)] == ["streams", "start_ns", "end_ns"]
    assert _parameters(RecordSource.read) == [
        ("from_journal_seq", inspect.Parameter.POSITIONAL_OR_KEYWORD, True)
    ]
    scorer: PlanScorer = _PlanScorerShape()
    plan = samples.trade_plan()
    assert plan.entry_price is not None
    estimate = scorer.score_plan(
        samples.candidate(), entry_price=plan.entry_price, stop_price=plan.stop_price, targets=plan.targets
    )
    assert estimate is None  # "None when no model can score the plan"
