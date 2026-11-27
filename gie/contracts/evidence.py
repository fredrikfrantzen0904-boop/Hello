"""Evidence contract - one causal, directional observation by one producer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from gie.contracts import identity
from gie.contracts._spec import (
    EnumOf,
    Float,
    Id,
    Int,
    ListOf,
    Obj,
    Opt,
    Record,
    Str,
    TimeNs,
    fld,
    inv,
    record,
)
from gie.contracts.common import (
    CODE_PATTERN,
    ENGINE_PATTERN,
    GOLD_SYMBOL,
    PRICE,
    REASON_CODE_PATTERN,
    STRATEGY_ENGINES,
    NamedValue,
    PriceBasis,
    PriceTrigger,
    Produced,
    Timeframe,
    symbol_field,
)
from gie.contracts.registry import contract

LEVEL_ROLE_PATTERN: Final[str] = r"^[A-Z][A-Z0-9_]{1,31}$"

#: Evidence kinds that several engines can observe and that must de-duplicate
#: exactly (brief 02).  Further kinds are defined in the session-02 feature dictionary.
SHARED_EVIDENCE_KINDS: Final[tuple[str, ...]] = ("liquidity_sweep", "fair_value_gap", "breakout")
#: Kind used to expose a resting entry level such as the Cavemen limit at breakout close.
PENDING_LIMIT_KIND: Final[str] = "pending_limit"
#: Level role carrying the price of such a resting entry.
PENDING_LIMIT_ROLE: Final[str] = "PENDING_LIMIT"


class ProducerClass(str, Enum):
    """What kind of component produced the evidence."""

    STRATEGY_ENGINE = "STRATEGY_ENGINE"
    INTERMARKET = "INTERMARKET"
    VOLATILITY = "VOLATILITY"
    PATTERN = "PATTERN"


class EvidenceState(str, Enum):
    """Whether, and how, the producer's condition holds.

    ``INACTIVE`` and ``MISSING`` are different facts and must never be merged:
    *inactive* means "evaluated, condition absent" (a known no); *missing* means
    "could not be evaluated" (unknown).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    MISSING = "MISSING"
    CONTRA = "CONTRA"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class EvidenceStage(str, Enum):
    """Progress of the setup the evidence belongs to (anticipate -> prepare -> react)."""

    NONE = "NONE"
    ANTICIPATED = "ANTICIPATED"
    FORMING = "FORMING"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class PriceLevel(Record):
    """A price with a role, e.g. the swept level or the bounds of a fair value gap."""

    role: str = fld(
        Str(pattern=LEVEL_ROLE_PATTERN, max_len=32, label="level role"),
        "Role of the level from the feature dictionary (e.g. SWEPT_LEVEL, FVG_HIGH, PENDING_LIMIT).",
    )
    price: Decimal = fld(PRICE, "Price of the level.", unit="USD per troy ounce")


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Invalidation(Record):
    """Observable price condition that invalidates the evidence."""

    price: Decimal = fld(PRICE, "Invalidation price.", unit="USD per troy ounce")
    trigger: PriceTrigger = fld(
        EnumOf(PriceTrigger), "Invalidated when the basis price is at or below / at or above the price."
    )
    basis: PriceBasis = fld(EnumOf(PriceBasis), "Which price the condition is evaluated on.")
    timeframe: Timeframe | None = fld(
        Opt(EnumOf(Timeframe)), "Candle timeframe when basis is CANDLE_CLOSE, otherwise null."
    )

    INVARIANTS = (
        inv(
            "INVAL-1",
            "timeframe is set if and only if basis is CANDLE_CLOSE",
            lambda r: (r.timeframe is not None) == (r.basis is PriceBasis.CANDLE_CLOSE),
        ),
    )


_ZERO_SCORE_STATES = (EvidenceState.INACTIVE, EvidenceState.INVALIDATED, EvidenceState.EXPIRED)
_LIVE_STATES = (EvidenceState.ACTIVE, EvidenceState.CONTRA)


@contract("Evidence", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence(Produced):
    """Directional observation with scores, levels, stage, freshness and invalidation.

    Stream: ``evidence/<producer>``.  Ordering time: ``decision_time_ns``.
    An evolving observation is a chain of revisions linked by
    ``supersedes_evidence_id``; consumers use the newest revision only.
    Producers must be causal: the record may only depend on data received at or
    before ``as_of_time_ns`` (prefix invariance).
    """

    evidence_id: str = fld(Id("evd"), "identity.evidence_id(engine, market_event_id, revision).")
    market_event_id: str = fld(
        Id("mev"),
        "Shared identity of the observed market event; equal ids are the same event (exact de-duplication).",
    )
    producer_class: ProducerClass = fld(EnumOf(ProducerClass), "Kind of producing component.")
    engine: str = fld(
        Str(pattern=ENGINE_PATTERN, max_len=32, label="engine id"),
        "Producing engine.  For STRATEGY_ENGINE one of the six canonical engine ids.",
    )
    kind: str = fld(
        Str(pattern=CODE_PATTERN, max_len=48, label="code"),
        "Evidence kind from the feature dictionary (e.g. liquidity_sweep, fair_value_gap, breakout).",
    )
    symbol: str = symbol_field("Instrument the observation is about.")
    timeframe: Timeframe = fld(EnumOf(Timeframe), "Timeframe the observation was made on.")
    anchor_time_ns: int = fld(
        TimeNs(),
        "Source event time of the market structure that defines the event (part of its identity).",
        unit="ns since epoch, UTC",
    )
    state: EvidenceState = fld(EnumOf(EvidenceState), "Evaluation state of the producer's condition.")
    stage: EvidenceStage = fld(EnumOf(EvidenceStage), "Progress of the setup.")
    bullish_score: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="score")),
        "Strength of bullish evidence; null only when MISSING.",
        unit="0..1",
    )
    bearish_score: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="score")),
        "Strength of bearish evidence; null only when MISSING.",
        unit="0..1",
    )
    levels: tuple[PriceLevel, ...] = fld(
        ListOf(Obj(PriceLevel), max_items=32), "Price levels that describe the event."
    )
    invalidation: Invalidation | None = fld(
        Opt(Obj(Invalidation)), "Price condition that invalidates the evidence, if one exists."
    )
    valid_until_ns: int = fld(
        TimeNs(), "Expiry instant: the evidence must not be used at or after it.", unit="ns since epoch, UTC"
    )
    revision: int = fld(
        Int(ge=0, label="revision"), "0 for the first record of an observation, +1 per update."
    )
    supersedes_evidence_id: str | None = fld(
        Opt(Id("evd")), "Previous revision replaced by this record; null for revision 0."
    )
    contradicts_evidence_id: str | None = fld(
        Opt(Id("evd")), "Evidence that this record contradicts; set if and only if state is CONTRA."
    )
    missing_reason: str | None = fld(
        Opt(Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code")),
        "Why the producer could not evaluate; set if and only if state is MISSING.",
    )
    features: tuple[NamedValue, ...] = fld(
        ListOf(
            Obj(NamedValue),
            max_items=64,
            unique_key=lambda item: item.name,
            canonical_sort=lambda item: item.name,
            sorted_label="sorted by name",
        ),
        "Engine-specific numeric features from the feature dictionary.",
    )

    INVARIANTS = (
        inv(
            "EVD-1",
            "evidence_id equals identity.evidence_id(engine, market_event_id, revision)",
            lambda r: r.evidence_id == identity.evidence_id(r.engine, r.market_event_id, r.revision),
        ),
        inv(
            "EVD-2",
            "state is MISSING if and only if both scores are null; otherwise both scores are numbers",
            lambda r: (
                (r.bullish_score is None and r.bearish_score is None)
                if r.state is EvidenceState.MISSING
                else (r.bullish_score is not None and r.bearish_score is not None)
            ),
        ),
        inv(
            "EVD-3",
            "missing_reason is set if and only if state is MISSING",
            lambda r: (r.missing_reason is not None) == (r.state is EvidenceState.MISSING),
        ),
        inv(
            "EVD-4",
            "when state is ACTIVE at least one score is greater than 0",
            lambda r: r.state is not EvidenceState.ACTIVE or max(r.bullish_score, r.bearish_score) > 0,
        ),
        inv(
            "EVD-5",
            "when state is INACTIVE, INVALIDATED or EXPIRED both scores are exactly 0",
            lambda r: r.state not in _ZERO_SCORE_STATES or (r.bullish_score == 0 and r.bearish_score == 0),
        ),
        inv(
            "EVD-6",
            "contradicts_evidence_id is set if and only if state is CONTRA",
            lambda r: (r.contradicts_evidence_id is not None) == (r.state is EvidenceState.CONTRA),
        ),
        inv(
            "EVD-7",
            "supersedes_evidence_id is set if and only if revision > 0",
            lambda r: (r.supersedes_evidence_id is not None) == (r.revision > 0),
        ),
        inv(
            "EVD-8",
            "when state is ACTIVE or CONTRA, valid_until_ns is later than decision_time_ns",
            lambda r: r.state not in _LIVE_STATES or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "EVD-9",
            "a STRATEGY_ENGINE producer is one of the six canonical engines and observes XAUUSD",
            lambda r: (
                r.producer_class is not ProducerClass.STRATEGY_ENGINE
                or (r.engine in STRATEGY_ENGINES and r.symbol == GOLD_SYMBOL)
            ),
        ),
        inv(
            "EVD-10",
            "stage is NONE when state is INACTIVE or MISSING",
            lambda r: (
                r.state not in (EvidenceState.INACTIVE, EvidenceState.MISSING)
                or r.stage is EvidenceStage.NONE
            ),
        ),
    )
