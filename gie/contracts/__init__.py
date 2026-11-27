"""Shared, versioned contracts of the Gold Intelligence Engine (owned by session 00).

Importing this package registers every contract.  Subsystems import from here
(or from the individual modules) and never redefine a contract themselves;
changes go through a change request to the coordinator.
"""

from gie.contracts import identity
from gie.contracts._spec import Record
from gie.contracts.authorization import (
    validate_entry_authorization,
    validate_protective_cancel,
    validate_reducing_order,
)
from gie.contracts.candidate import (
    CANDIDATE_STATUS_TRANSITIONS,
    Candidate,
    CandidateStatus,
    Destination,
    QuantilePoint,
    validate_candidate_transition,
)
from gie.contracts.causality import check_derivation
from gie.contracts.clock import (
    Clock,
    ClockMode,
    ClockReading,
    PollingScheduler,
    ReplayClock,
    Scheduler,
    SystemClock,
    read_clock,
)
from gie.contracts.codec import fingerprint, from_json, from_wire, to_bytes, to_json, to_wire
from gie.contracts.common import (
    GOLD_SYMBOL,
    STRATEGY_ENGINES,
    Contribution,
    DataOrigin,
    ExecutionVenue,
    Horizon,
    MarketSession,
    NamedValue,
    OrderType,
    PriceBasis,
    PriceCondition,
    PriceTrigger,
    RunMode,
    Side,
    Timeframe,
    derive_origin,
)
from gie.contracts.evidence import (
    PENDING_LIMIT_KIND,
    PENDING_LIMIT_ROLE,
    SHARED_EVIDENCE_KINDS,
    Evidence,
    EvidenceGroup,
    EvidenceStage,
    EvidenceState,
    PriceLevel,
    ProducerClass,
    group_by_market_event,
    validate_evidence_transition,
)
from gie.contracts.guarded import GuardedExecutionAdapter
from gie.contracts.journal import FileJournal, JournalEntry, MemoryJournal, repair_torn_tail
from gie.contracts.ledger import LifecycleLedger
from gie.contracts.model_version import (
    MODEL_STATUS_TRANSITIONS,
    ContractRef,
    ModelKind,
    ModelStatus,
    ModelVersion,
    is_out_of_sample,
    validate_model_transition,
    validate_registry_history,
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
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.contracts.position import (
    Position,
    PositionOwner,
    PositionState,
    validate_position_transition,
)
from gie.contracts.registry import all_contracts, get_contract
from gie.contracts.risk_decision import (
    DailyState,
    RiskDecision,
    RiskOutcome,
    validate_daily_state_sequence,
)
from gie.contracts.run import RunContext, RunManifest, RunRole
from gie.contracts.tick import Tick, TickFlag, feed_silence_ns
from gie.contracts.trade_plan import (
    PLAN_STATUS_TRANSITIONS,
    EntryStyle,
    PlanEstimate,
    PlanStatus,
    Target,
    TradePlan,
    validate_plan_transition,
)

#: Version of the contract *set* (bumped whenever any contract version changes).
CONTRACT_SET_VERSION = "1.0.0"

__all__ = [
    "CANDIDATE_STATUS_TRANSITIONS",
    "CONTRACT_SET_VERSION",
    "GOLD_SYMBOL",
    "MODEL_STATUS_TRANSITIONS",
    "ORDER_TRANSITIONS",
    "PENDING_LIMIT_KIND",
    "PENDING_LIMIT_ROLE",
    "PLAN_STATUS_TRANSITIONS",
    "SHARED_EVIDENCE_KINDS",
    "STRATEGY_ENGINES",
    "TERMINAL_ORDER_STATES",
    "Candidate",
    "CandidateStatus",
    "Clock",
    "ClockMode",
    "ClockReading",
    "ContractRef",
    "Contribution",
    "DailyState",
    "DataOrigin",
    "Destination",
    "EntryStyle",
    "Evidence",
    "EvidenceGroup",
    "EvidenceStage",
    "EvidenceState",
    "ExecutionVenue",
    "FileJournal",
    "GuardedExecutionAdapter",
    "Horizon",
    "JournalEntry",
    "LifecycleLedger",
    "MarketSession",
    "MemoryJournal",
    "ModelKind",
    "ModelStatus",
    "ModelVersion",
    "NamedValue",
    "OrderEvent",
    "OrderEventType",
    "OrderIntent",
    "OrderState",
    "OrderType",
    "PlanEstimate",
    "PlanStatus",
    "PollingScheduler",
    "Position",
    "PositionOwner",
    "PositionState",
    "PriceBasis",
    "PriceCondition",
    "PriceLevel",
    "PriceTrigger",
    "ProducerClass",
    "QuantilePoint",
    "Record",
    "ReplayClock",
    "RiskDecision",
    "RiskOutcome",
    "RunContext",
    "RunManifest",
    "RunMode",
    "RunRole",
    "Scheduler",
    "Side",
    "SystemClock",
    "Target",
    "Tick",
    "TickFlag",
    "Timeframe",
    "TradePlan",
    "all_contracts",
    "check_derivation",
    "derive_origin",
    "feed_silence_ns",
    "fingerprint",
    "from_json",
    "from_wire",
    "get_contract",
    "group_by_market_event",
    "identity",
    "is_out_of_sample",
    "ordering_key",
    "ordering_time_ns",
    "read_clock",
    "repair_torn_tail",
    "stream_name",
    "to_bytes",
    "to_json",
    "to_wire",
    "validate_candidate_transition",
    "validate_daily_state_sequence",
    "validate_entry_authorization",
    "validate_evidence_transition",
    "validate_model_transition",
    "validate_order_transition",
    "validate_plan_transition",
    "validate_position_transition",
    "validate_protective_cancel",
    "validate_reducing_order",
    "validate_registry_history",
]
