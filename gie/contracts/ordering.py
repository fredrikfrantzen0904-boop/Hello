"""Stream names and ordering keys of contract records (see ``gie.core.ordering``).

The key orders the *inputs* of a run (ticks, clock readings) and gives records
of different journals a deterministic order.  Within one run the order of
record is the journal order; sorting derived records by this key is a
deterministic tie-break, not a statement about cause and effect.
"""

from __future__ import annotations

from typing import Final

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.clock import ClockReading
from gie.contracts.evidence import Evidence
from gie.contracts.model_version import ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.position import Position
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.run import RunManifest
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.errors import UnknownContractError
from gie.core.ordering import EventKey, StreamRank

#: contract -> (stream prefix, name of the ordering-time field, rank).
#: OrderEvent has two ranks: requests and reports.  A report is always observed strictly later
#: than the request it follows, so the key never contradicts the order of events of one order.
#: RunManifest: the start of the data period for a replay, the creation time for a paper run.
STREAMS: Final[dict[type[Record], tuple[str, str, StreamRank]]] = {
    RunManifest: ("run_manifest", "data_start_ns | created_time_ns", StreamRank.RUN_MANIFEST),
    ClockReading: ("clock", "wall_time_ns", StreamRank.CLOCK),
    Tick: ("tick", "receive_time_ns", StreamRank.TICK),
    OrderEvent: ("order_event", "receive_time_ns", StreamRank.ORDER_REPORT),
    Position: ("position", "decision_time_ns", StreamRank.POSITION),
    Evidence: ("evidence", "decision_time_ns", StreamRank.EVIDENCE),
    Candidate: ("candidate", "decision_time_ns", StreamRank.CANDIDATE),
    TradePlan: ("trade_plan", "decision_time_ns", StreamRank.TRADE_PLAN),
    RiskDecision: ("risk_decision", "decision_time_ns", StreamRank.RISK_DECISION),
    ModelVersion: ("model_version", "status_time_ns", StreamRank.MODEL_VERSION),
}


def _entry(value: Record) -> tuple[str, str, StreamRank]:
    entry = STREAMS.get(type(value))
    if entry is None:
        raise UnknownContractError("record has no stream", contract=type(value).__name__)
    return entry


def stream_name(value: Record) -> str:
    """Stable name of the stream a record belongs to (``<prefix>/<producer>``; ticks by feed and symbol)."""
    prefix, _, _ = _entry(value)
    if isinstance(value, Tick):
        return value.stream
    return f"{prefix}/{value.producer}"  # type: ignore[attr-defined]


def ordering_time_ns(value: Record) -> int:
    """Availability instant of a record: when its information could first be known to GIE."""
    _, time_field, _ = _entry(value)
    if isinstance(value, RunManifest):
        start = value.data_start_ns if value.created_time_ns is None else value.created_time_ns
        assert start is not None  # RUN-2: a replay has a data period, a paper run a creation time
        return start
    time_ns: int = getattr(value, time_field)
    return time_ns


def ordering_key(value: Record) -> EventKey:
    """Input-order key ``(availability time, rank, stream, seq)`` of a record."""
    _, _, rank = _entry(value)
    if isinstance(value, OrderEvent) and value.is_request:
        rank = StreamRank.ORDER_REQUEST
    return EventKey(ordering_time_ns(value), rank, stream_name(value), value.seq)  # type: ignore[attr-defined]
