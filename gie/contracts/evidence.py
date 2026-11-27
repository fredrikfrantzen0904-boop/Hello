"""Evidence contract - one causal, directional observation by one producer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    Bool,
    Dec,
    EnumOf,
    Float,
    Id,
    Int,
    Invariant,
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
    REASON_CODE_PATTERN,
    STRATEGY_ENGINES,
    ZERO,
    NamedValue,
    PriceCondition,
    Produced,
    Timeframe,
    symbol_field,
)
from gie.contracts.registry import contract
from gie.core.errors import IllegalTransitionError

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

    * ``ACTIVE`` - the condition is present.  The scores give its direction and
      strength; both scores 0 means "present, without directional information"
      (a forming range, a volatility regime).
    * ``INACTIVE`` - evaluated, condition absent: a known *no*.
    * ``MISSING`` - could not be evaluated: *unknown*.  Never to be treated as no.
    * ``CONTRA`` - the producer observes a condition that speaks against the
      market events named in ``contradicts_market_event_ids``; the scores give
      the direction and strength of the contradicting observation.
    * ``INVALIDATED`` - a previously reported observation was invalidated by price.
    * ``EXPIRED`` - a previously reported observation aged out.
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
    price: Decimal = fld(Dec(label="price"), "Value of the level.", unit="quote currency per unit")
    identity: bool = fld(
        Bool(),
        "True when the level is part of the market event's identity (it then never changes "
        "between revisions and all engines must report the same value).",
    )


_NO_EVENT_STATES: Final[tuple[EvidenceState, ...]] = (EvidenceState.INACTIVE, EvidenceState.MISSING)
_ZERO_SCORE_STATES: Final[tuple[EvidenceState, ...]] = (
    EvidenceState.INACTIVE,
    EvidenceState.INVALIDATED,
    EvidenceState.EXPIRED,
)
_LIVE_STATES: Final[tuple[EvidenceState, ...]] = (EvidenceState.ACTIVE, EvidenceState.CONTRA)
_FINAL_STATES: Final[tuple[EvidenceState, ...]] = (EvidenceState.INVALIDATED, EvidenceState.EXPIRED)


def _expected_event_id(evidence: Evidence) -> str:
    if evidence.state in _NO_EVENT_STATES:
        return identity.evaluation_slot_id(
            evidence.engine,
            evidence.kind,
            evidence.symbol,
            evidence.timeframe.value,
            evidence.anchor_time_ns,
        )
    return identity.market_event_id(
        evidence.kind,
        evidence.symbol,
        evidence.timeframe.value,
        evidence.anchor_time_ns,
        [level.price for level in evidence.levels if level.identity],
    )


def _pending_limit_shape(evidence: Evidence) -> bool:
    if evidence.kind != PENDING_LIMIT_KIND or evidence.state is not EvidenceState.ACTIVE:
        return True
    limits = [level for level in evidence.levels if level.role == PENDING_LIMIT_ROLE]
    directional = [score for score in (evidence.bullish_score, evidence.bearish_score) if score and score > 0]
    return len(limits) == 1 and len(directional) == 1 and evidence.related_market_event_id is not None


def _gold_prices_positive(evidence: Evidence) -> bool:
    if evidence.symbol != GOLD_SYMBOL:
        return True
    if any(level.price <= ZERO for level in evidence.levels):
        return False
    return evidence.invalidation is None or evidence.invalidation.price > ZERO


@final
@contract("Evidence", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence(Produced):
    """Directional observation with scores, levels, stage, freshness and invalidation.

    Stream: ``evidence/<producer>``.  Ordering time: ``decision_time_ns``.

    * Scores, levels and invalidation always describe the instrument named in
      ``symbol``; they are never pre-translated into a gold direction.  How an
      observation on another instrument relates to gold is learned downstream
      (correlations are never assumed fixed).
    * An evolving observation is a chain of revisions linked by
      ``supersedes_evidence_id``; consumers use the newest revision only.  The
      identity of the market event never changes along the chain.
    * ``anchor_time_ns`` is the source event time that pins the market event in
      time.  The convention per kind (for candle-defined events: the open time of
      the defining candle) is fixed by the feature dictionary and must be the
      same for every engine, otherwise events cannot de-duplicate.
    * Producers must be causal: the record may only depend on market data
      received at or before ``as_of_time_ns`` (prefix invariance).
    """

    evidence_id: str = fld(Id("evd"), "identity.evidence_id(engine, market_event_id, revision).")
    market_event_id: str = fld(
        Id("mev"),
        "Identity of the observed market event, shared by all engines that observe it (exact "
        "de-duplication).  INACTIVE and MISSING records carry the engine's evaluation-slot id instead.",
    )
    related_market_event_id: str | None = fld(
        Opt(Id("mev")),
        "Market event this observation follows from (for a pending limit: the breakout it belongs to).",
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
    symbol: str = symbol_field("Instrument the observation is about (scores and levels refer to it).")
    timeframe: Timeframe = fld(
        EnumOf(Timeframe), "Timeframe the observation was made on; NONE when it is not tied to a candle."
    )
    anchor_time_ns: int = fld(
        TimeNs(),
        "Source event time that pins the market event (part of its identity).",
        unit="ns since epoch, UTC",
    )
    observable_time_ns: int = fld(
        TimeNs(),
        "Receive time of the input from which this revision's state could first be determined. "
        "decision_time_ns - observable_time_ns is the notification delay.",
        unit="ns since epoch, UTC",
    )
    state: EvidenceState = fld(EnumOf(EvidenceState), "Evaluation state of the producer's condition.")
    stage: EvidenceStage = fld(EnumOf(EvidenceStage), "Progress of the setup.")
    bullish_score: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="score")),
        "Strength of evidence that the instrument rises; null only when MISSING.",
        unit="0..1",
    )
    bearish_score: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="score")),
        "Strength of evidence that the instrument falls; null only when MISSING.",
        unit="0..1",
    )
    levels: tuple[PriceLevel, ...] = fld(
        ListOf(
            Obj(PriceLevel),
            max_items=32,
            unique_key=lambda level: (level.role, level.price),
            canonical_sort=lambda level: (level.role, level.price),
            sorted_label="sorted by (role, price)",
        ),
        "Levels that describe the event.",
    )
    invalidation: PriceCondition | None = fld(
        Opt(Obj(PriceCondition)),
        "Price condition that invalidates the observation, if one exists.  The producer evaluates "
        "it and emits an INVALIDATED revision; consumers do not re-evaluate it.",
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
    contradicts_market_event_ids: tuple[str, ...] = fld(
        ListOf(
            Id("mev"),
            max_items=16,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Market events this observation speaks against; non-empty if and only if state is CONTRA.",
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
        "Engine-specific features from the feature dictionary.  A feature that is absent or null "
        "is unknown, never 0.",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "EVD-1",
            "evidence_id equals identity.evidence_id(engine, market_event_id, revision)",
            lambda r: r.evidence_id == identity.evidence_id(r.engine, r.market_event_id, r.revision),
        ),
        inv(
            "EVD-2",
            "market_event_id equals identity.market_event_id(kind, symbol, timeframe, anchor_time_ns, "
            "prices of identity levels); for INACTIVE and MISSING it equals "
            "identity.evaluation_slot_id(engine, kind, symbol, timeframe, anchor_time_ns)",
            lambda r: r.market_event_id == _expected_event_id(r),
        ),
        inv(
            "EVD-3",
            "state is MISSING if and only if both scores are null; otherwise both scores are numbers",
            lambda r: (
                (r.bullish_score is None and r.bearish_score is None)
                if r.state is EvidenceState.MISSING
                else (r.bullish_score is not None and r.bearish_score is not None)
            ),
        ),
        inv(
            "EVD-4",
            "missing_reason is set if and only if state is MISSING",
            lambda r: (r.missing_reason is not None) == (r.state is EvidenceState.MISSING),
        ),
        inv(
            "EVD-5",
            "when state is INACTIVE, INVALIDATED or EXPIRED both scores are exactly 0",
            lambda r: r.state not in _ZERO_SCORE_STATES or (r.bullish_score == 0 and r.bearish_score == 0),
        ),
        inv(
            "EVD-6",
            "contradicts_market_event_ids is non-empty if and only if state is CONTRA, and never "
            "contains the record's own market_event_id",
            lambda r: (
                bool(r.contradicts_market_event_ids) == (r.state is EvidenceState.CONTRA)
                and r.market_event_id not in r.contradicts_market_event_ids
            ),
        ),
        inv(
            "EVD-7",
            "supersedes_evidence_id is null for revision 0 and otherwise equals the id of the previous "
            "revision, identity.evidence_id(engine, market_event_id, revision - 1)",
            lambda r: (
                r.supersedes_evidence_id is None
                if r.revision == 0
                else r.supersedes_evidence_id
                == identity.evidence_id(r.engine, r.market_event_id, r.revision - 1)
            ),
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
            "INACTIVE and MISSING records describe no market event: stage is NONE, there are no "
            "identity levels and no related market event",
            lambda r: (
                r.state not in _NO_EVENT_STATES
                or (
                    r.stage is EvidenceStage.NONE
                    and not any(level.identity for level in r.levels)
                    and r.related_market_event_id is None
                )
            ),
        ),
        inv(
            "EVD-11",
            "observable_time_ns <= as_of_time_ns (the state cannot have been observable later than "
            "the newest data the record uses)",
            lambda r: r.observable_time_ns <= r.as_of_time_ns,
        ),
        inv(
            "EVD-12",
            "an ACTIVE record of kind pending_limit has exactly one PENDING_LIMIT level, exactly one "
            "score greater than 0 (bullish = buy limit) and names the breakout in related_market_event_id",
            _pending_limit_shape,
        ),
        inv(
            "EVD-13",
            "levels and invalidation of an XAUUSD observation are prices greater than 0",
            _gold_prices_positive,
        ),
    )


#: Progress order of a setup (anticipate -> prepare -> react): the order of the enumeration.
_STAGE_ORDER: Final[dict[EvidenceStage, int]] = {stage: index for index, stage in enumerate(EvidenceStage)}

_EVIDENCE_IMMUTABLE: Final[tuple[str, ...]] = (
    "run_id",
    "mode",
    "producer",
    "producer_class",
    "engine",
    "kind",
    "symbol",
    "timeframe",
    "anchor_time_ns",
    "market_event_id",
    "related_market_event_id",
)


def validate_evidence_transition(previous: Evidence | None, revision: Evidence) -> None:
    """Check that *revision* may follow *previous* in the chain of one observation.

    The identity of the market event (and with it every identity level) is fixed
    at revision 0; INVALIDATED and EXPIRED are final; the stage never goes back
    (NONE < ANTICIPATED < FORMING < ARMED < TRIGGERED < CONFIRMED < COMPLETED).
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal evidence transition: {rule}",
            evidence_id=revision.evidence_id,
            revision=revision.revision,
            rule=rule,
        )

    if previous is None:
        if revision.revision != 0:
            raise fail("the first record of an observation has revision 0")
        return
    if previous.state in _FINAL_STATES:
        raise fail(f"no revision may follow state {previous.state.value}")
    if revision.revision != previous.revision + 1:
        raise fail("revision must increase by exactly 1")
    if revision.supersedes_evidence_id != previous.evidence_id:
        raise fail("supersedes_evidence_id must name the previous revision")
    for name in _EVIDENCE_IMMUTABLE:
        if getattr(previous, name) != getattr(revision, name):
            raise fail(f"{name} may not change between revisions")
    if (previous.state in _NO_EVENT_STATES) != (revision.state in _NO_EVENT_STATES):
        raise fail("a record of a market event and a record of an evaluation slot are different chains")
    identity_before = sorted((level.role, level.price) for level in previous.levels if level.identity)
    identity_after = sorted((level.role, level.price) for level in revision.levels if level.identity)
    if identity_before != identity_after:
        raise fail("identity levels may not change between revisions")
    for name in ("decision_time_ns", "as_of_time_ns", "seq"):
        if getattr(revision, name) < getattr(previous, name):
            raise fail(f"{name} may not decrease")
    if _STAGE_ORDER[revision.stage] < _STAGE_ORDER[previous.stage]:
        raise fail(
            f"stage may not go back ({previous.stage.value} -> {revision.stage.value}); a setup that "
            "falls back is INVALIDATED and a new observation starts"
        )


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    """All current observations of one market event, with a deterministic representative."""

    market_event_id: str
    representative: Evidence
    members: tuple[Evidence, ...]


def group_by_market_event(records: Iterable[Evidence]) -> tuple[EvidenceGroup, ...]:
    """Exact de-duplication of evidence (G02-02): one group per market event.

    Pass the newest revision of each observation.  Records with equal
    ``market_event_id`` are the same market event seen by different engines and
    form one group; INACTIVE and MISSING records carry engine-scoped slot ids and
    therefore never merge.  Members are ordered by ``(decision_time_ns, engine,
    producer, seq)`` and the first is the representative.  Within a group the
    event counts **once**: scores of members are never summed.
    """
    groups: dict[str, list[Evidence]] = {}
    for item in records:
        if type(item) is not Evidence:
            raise TypeError("group_by_market_event expects Evidence records")
        groups.setdefault(item.market_event_id, []).append(item)
    result = []
    for event_id in sorted(groups):
        members = sorted(
            groups[event_id], key=lambda e: (e.decision_time_ns, e.engine, e.producer, e.seq, e.revision)
        )
        engines = [member.engine for member in members]
        if len(set(engines)) != len(engines):
            raise IllegalTransitionError(
                "more than one record of the same engine for one market event (pass newest revisions only)",
                market_event_id=event_id,
            )
        result.append(EvidenceGroup(event_id, members[0], tuple(members)))
    return tuple(result)
