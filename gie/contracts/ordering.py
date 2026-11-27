"""Stream names and total-order keys of contract records (see ``gie.core.ordering``)."""

from __future__ import annotations

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.clock import ClockReading
from gie.contracts.evidence import Evidence
from gie.contracts.model_version import ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.errors import UnknownContractError
from gie.core.ordering import EventKey, StreamRank

_PRODUCED: dict[type[Record], tuple[str, StreamRank]] = {
    Evidence: ("evidence", StreamRank.EVIDENCE),
    Candidate: ("candidate", StreamRank.CANDIDATE),
    TradePlan: ("trade_plan", StreamRank.TRADE_PLAN),
    RiskDecision: ("risk_decision", StreamRank.RISK_DECISION),
    Position: ("position", StreamRank.POSITION),
}


def stream_name(value: Record) -> str:
    """Stable name of the stream a record belongs to."""
    if isinstance(value, Tick):
        return value.stream
    if isinstance(value, OrderEvent):
        return f"order_event/{value.producer}"
    if isinstance(value, ClockReading):
        return f"clock/{value.producer}"
    if isinstance(value, ModelVersion):
        return f"model_version/{value.producer}"
    entry = _PRODUCED.get(type(value))
    if entry is None:
        raise UnknownContractError("record has no stream", contract=type(value).__name__)
    return f"{entry[0]}/{value.producer}"  # type: ignore[attr-defined]


def ordering_key(value: Record) -> EventKey:
    """Position of a record in the total order of a run.

    The time component is the instant at which the information became available
    to GIE: receive time for ticks and order events, decision time for records
    GIE produces, wall time for clock readings and status time for model versions.
    """
    if isinstance(value, Tick):
        return EventKey(value.receive_time_ns, StreamRank.TICK, value.stream, value.seq)
    if isinstance(value, OrderEvent):
        return EventKey(value.receive_time_ns, StreamRank.ORDER_EVENT, stream_name(value), value.seq)
    if isinstance(value, ClockReading):
        return EventKey(value.wall_time_ns, StreamRank.CLOCK, stream_name(value), value.seq)
    if isinstance(value, ModelVersion):
        return EventKey(value.status_time_ns, StreamRank.MODEL_VERSION, stream_name(value), value.seq)
    entry = _PRODUCED.get(type(value))
    if entry is None:
        raise UnknownContractError("record has no ordering key", contract=type(value).__name__)
    return EventKey(value.decision_time_ns, entry[1], stream_name(value), value.seq)  # type: ignore[attr-defined]
