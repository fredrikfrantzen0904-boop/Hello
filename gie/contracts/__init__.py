"""Shared, versioned contracts of the Gold Intelligence Engine (owned by session 00).

Importing this package registers every contract.  Subsystems import from here
(or from the individual modules) and never redefine a contract themselves;
changes go through a change request to the coordinator.
"""

from gie.contracts import identity
from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate, CandidateStatus, Destination, QuantilePoint
from gie.contracts.clock import Clock, ClockMode, ClockReading, ReplayClock, SystemClock, read_clock
from gie.contracts.codec import fingerprint, from_json, from_wire, to_bytes, to_json, to_wire
from gie.contracts.common import (
    GOLD_SYMBOL,
    STRATEGY_ENGINES,
    DataOrigin,
    ExecutionVenue,
    Horizon,
    MarketSession,
    NamedValue,
    OrderType,
    PriceBasis,
    PriceTrigger,
    RunMode,
    Side,
    Timeframe,
    combine_origins,
)
from gie.contracts.evidence import (
    PENDING_LIMIT_KIND,
    PENDING_LIMIT_ROLE,
    SHARED_EVIDENCE_KINDS,
    Evidence,
    EvidenceStage,
    EvidenceState,
    Invalidation,
    PriceLevel,
    ProducerClass,
)
from gie.contracts.model_version import (
    MODEL_STATUS_TRANSITIONS,
    ContractRef,
    ModelKind,
    ModelStatus,
    ModelVersion,
    validate_model_transition,
)
from gie.contracts.order_event import (
    ORDER_TRANSITIONS,
    TERMINAL_ORDER_STATES,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    validate_order_transition,
)
from gie.contracts.ordering import ordering_key, stream_name
from gie.contracts.position import (
    Position,
    PositionOwner,
    PositionState,
    validate_position_transition,
)
from gie.contracts.registry import all_contracts, get_contract
from gie.contracts.risk_decision import DailyState, RiskDecision, RiskOutcome
from gie.contracts.tick import Tick, TickFlag
from gie.contracts.trade_plan import EntryStyle, EvidenceContribution, PlanStatus, Target, TradePlan

#: Version of the contract *set* (bumped whenever any contract version changes).
CONTRACT_SET_VERSION = "1.0.0"

__all__ = [
    "CONTRACT_SET_VERSION",
    "GOLD_SYMBOL",
    "MODEL_STATUS_TRANSITIONS",
    "ORDER_TRANSITIONS",
    "PENDING_LIMIT_KIND",
    "PENDING_LIMIT_ROLE",
    "SHARED_EVIDENCE_KINDS",
    "STRATEGY_ENGINES",
    "TERMINAL_ORDER_STATES",
    "Candidate",
    "CandidateStatus",
    "Clock",
    "ClockMode",
    "ClockReading",
    "ContractRef",
    "DailyState",
    "DataOrigin",
    "Destination",
    "EntryStyle",
    "Evidence",
    "EvidenceContribution",
    "EvidenceStage",
    "EvidenceState",
    "ExecutionVenue",
    "Horizon",
    "Invalidation",
    "MarketSession",
    "ModelKind",
    "ModelStatus",
    "ModelVersion",
    "NamedValue",
    "OrderEvent",
    "OrderEventType",
    "OrderIntent",
    "OrderState",
    "OrderType",
    "PlanStatus",
    "Position",
    "PositionOwner",
    "PositionState",
    "PriceBasis",
    "PriceLevel",
    "PriceTrigger",
    "ProducerClass",
    "QuantilePoint",
    "Record",
    "ReplayClock",
    "RiskDecision",
    "RiskOutcome",
    "RunMode",
    "Side",
    "SystemClock",
    "Target",
    "Tick",
    "TickFlag",
    "Timeframe",
    "TradePlan",
    "all_contracts",
    "combine_origins",
    "fingerprint",
    "from_json",
    "from_wire",
    "get_contract",
    "identity",
    "ordering_key",
    "read_clock",
    "stream_name",
    "to_bytes",
    "to_json",
    "to_wire",
    "validate_model_transition",
    "validate_order_transition",
    "validate_position_transition",
]
