"""Vocabulary, value objects and headers shared by all GIE contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Final

from gie.contracts._spec import (
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
from gie.core.routing_lock import ExecutionVenue, RunMode

__all__ = [
    "CODE_PATTERN",
    "ENGINE_PATTERN",
    "GOLD_SYMBOL",
    "PRODUCER_PATTERN",
    "REASON_CODE_PATTERN",
    "STRATEGY_ENGINES",
    "SYMBOL_PATTERN",
    "Contribution",
    "DataOrigin",
    "ExecutionVenue",
    "Horizon",
    "MarketSession",
    "NamedValue",
    "OrderType",
    "PriceBasis",
    "PriceCondition",
    "PriceTrigger",
    "Produced",
    "RunMode",
    "RunScoped",
    "Sequenced",
    "Side",
    "Timeframe",
    "derive_origin",
    "reason_codes_field",
    "symbol_field",
]

# --------------------------------------------------------------------------- constants
#: The only instrument GIE may trade (G01-01, G06-07).
GOLD_SYMBOL: Final[str] = "XAUUSD"

#: The six existing strategy engines (G02-01), by canonical identifier.
STRATEGY_ENGINES: Final[tuple[str, ...]] = (
    "cavemen",
    "silver_bullet",
    "liquidity_hunter",
    "orb_vwap",
    "vwap_trend_rider",
    "sweep_reversal",
)

SYMBOL_PATTERN: Final[str] = r"^[A-Z0-9][A-Z0-9._-]{0,23}$"
#: Component instance that produced a record, e.g. ``strategy_intelligence.cavemen``.
PRODUCER_PATTERN: Final[str] = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,4}$"
ENGINE_PATTERN: Final[str] = r"^[a-z][a-z0-9_]{1,31}$"
#: Lower-case vocabulary term (evidence kind, setup type, model name, feature name).
CODE_PATTERN: Final[str] = r"^[a-z][a-z0-9_]{1,47}$"
#: Upper-case machine reason code, e.g. ``DAILY_FLOOR_REACHED``.
REASON_CODE_PATTERN: Final[str] = r"^[A-Z][A-Z0-9_]{1,47}$"
SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
CURRENCY_PATTERN: Final[str] = r"^[A-Z]{3}$"
#: External reference (broker ticket, account alias): no free text, no control characters.
REFERENCE_PATTERN: Final[str] = r"^[A-Za-z0-9._:-]{1,64}$"


# --------------------------------------------------------------------------- enums
class Side(str, Enum):
    """Direction.  For a position, ``BUY`` means long and ``SELL`` means short."""

    BUY = "BUY"
    SELL = "SELL"


class DataOrigin(str, Enum):
    """Where the market data behind a record came from.

    Ordered from most to least trustworthy; a derived record carries the *least*
    trustworthy origin among its inputs (:func:`derive_origin`).
    ``BROKER_FEED`` is a real-time observation of a broker or vendor feed,
    ``HISTORICAL`` is imported recorded history, ``SYNTHETIC_FIXTURE`` is
    generated test data that is never evidence of anything.
    """

    BROKER_FEED = "BROKER_FEED"
    HISTORICAL = "HISTORICAL"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


_ORIGIN_ORDER: Final[dict[DataOrigin, int]] = {
    DataOrigin.BROKER_FEED: 0,
    DataOrigin.HISTORICAL: 1,
    DataOrigin.SYNTHETIC_FIXTURE: 2,
}


def derive_origin(*origins: DataOrigin) -> DataOrigin:
    """Origin of a record derived from inputs with the given origins (least trustworthy wins)."""
    if not origins:
        raise ValueError("at least one origin is required")
    return max(origins, key=lambda origin: _ORIGIN_ORDER[origin])


def origin_not_better_than(child: DataOrigin, *inputs: DataOrigin) -> bool:
    """True when *child* does not claim a more trustworthy origin than its inputs allow."""
    return not inputs or _ORIGIN_ORDER[child] >= _ORIGIN_ORDER[derive_origin(*inputs)]


class Timeframe(str, Enum):
    """Candle timeframe.  ``NONE`` is for observations that are not tied to a candle."""

    NONE = "NONE"
    TICK = "TICK"
    S1 = "S1"
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class Horizon(str, Enum):
    """Intended holding style of an opportunity (G01-02 hybrid scalping and intraday)."""

    SCALP = "SCALP"
    INTRADAY = "INTRADAY"


class MarketSession(str, Enum):
    """Broker-session bucket.  Boundaries are defined by the broker calendar (session 01)."""

    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OFF_SESSION = "OFF_SESSION"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class PriceBasis(str, Enum):
    """Which observable price a price condition is evaluated on."""

    BID = "BID"
    ASK = "ASK"
    MID = "MID"
    CANDLE_CLOSE = "CANDLE_CLOSE"


class PriceTrigger(str, Enum):
    """Comparison of the basis price with the condition price."""

    BELOW = "BELOW"
    AT_OR_BELOW = "AT_OR_BELOW"
    AT_OR_ABOVE = "AT_OR_ABOVE"
    ABOVE = "ABOVE"


# --------------------------------------------------------------------------- field helpers
def symbol_field(doc: str, *, gold_only: bool = False) -> Any:
    if gold_only:
        return fld(Str(choices=(GOLD_SYMBOL,), max_len=24, label="symbol"), doc)
    return fld(Str(pattern=SYMBOL_PATTERN, max_len=24, label="symbol"), doc)


def reason_codes_field(doc: str) -> Any:
    return fld(
        ListOf(
            Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code"),
            max_items=32,
            unique_key=lambda code: code,
            canonical_sort=lambda code: code,
            sorted_label="sorted ascending",
        ),
        doc,
    )


PRICE: Final[Dec] = Dec(gt="0", label="price")
ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)


# --------------------------------------------------------------------------- value objects
@record
@dataclass(frozen=True, slots=True, kw_only=True)
class NamedValue(Record):
    """A named number (feature value or metric).  ``null`` means unknown, never 0."""

    name: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Name from the owning dictionary.")
    value: float | None = fld(Opt(Float()), "Finite numeric value; null when the value is unknown.")


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class PriceCondition(Record):
    """Observable price condition: the basis price compared with a level.

    Used as the invalidation of evidence and as entry trigger / invalidation of a
    trade plan.  The condition is evaluated by the component that acts on it
    (the producer emits the resulting state change); it is true when
    ``basis price <trigger> price`` holds.
    """

    price: Decimal = fld(Dec(label="price"), "Level of the condition.", unit="quote currency per unit")
    trigger: PriceTrigger = fld(EnumOf(PriceTrigger), "Comparison that makes the condition true.")
    basis: PriceBasis = fld(EnumOf(PriceBasis), "Which price is compared.")
    timeframe: Timeframe | None = fld(
        Opt(EnumOf(Timeframe)), "Candle timeframe when basis is CANDLE_CLOSE, otherwise null."
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "COND-1",
            "timeframe is a candle timeframe if and only if basis is CANDLE_CLOSE",
            lambda r: (
                (r.timeframe is not None and r.timeframe not in (Timeframe.NONE, Timeframe.TICK))
                if r.basis is PriceBasis.CANDLE_CLOSE
                else r.timeframe is None
            ),
        ),
    )


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Contribution(Record):
    """Share of an estimate attributed to one driver (explainability).

    The driver is either one evidence record or one named feature (setup prior,
    session, cost, intermarket feature ...), never both.
    """

    evidence_id: str | None = fld(Opt(Id("evd")), "Contributing evidence, or null for a named feature.")
    feature: str | None = fld(
        Opt(Str(pattern=CODE_PATTERN, max_len=48, label="code")),
        "Contributing feature from the feature dictionary, or null for evidence.",
    )
    weight_r: float = fld(Float(label="R multiple"), "Signed contribution to the expected net R.", unit="R")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "CONTRIB-1",
            "exactly one of evidence_id and feature is set",
            lambda r: (r.evidence_id is None) != (r.feature is None),
        ),
    )

    @property
    def key(self) -> str:
        return self.evidence_id if self.evidence_id is not None else f"feature:{self.feature}"


def contributions_field(doc: str) -> Any:
    return fld(
        ListOf(
            Obj(Contribution),
            max_items=64,
            unique_key=lambda item: item.key,
            canonical_sort=lambda item: item.key,
            sorted_label="sorted by driver",
        ),
        doc,
    )


# --------------------------------------------------------------------------- headers
@dataclass(frozen=True, slots=True, kw_only=True)
class Sequenced(Record):
    """Header of every record that belongs to a producer stream."""

    producer: str = fld(
        Str(pattern=PRODUCER_PATTERN, max_len=96, label="producer"),
        "Component instance that emitted the record; together with the contract name it names the stream.",
    )
    seq: int = fld(
        Int(ge=0, label="sequence number"),
        "Position of the record in its stream: starts at 0 and increases by exactly 1.",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunScoped(Sequenced):
    """Header of records that belong to one replay or paper run.

    The stream of a run-scoped record is identified by ``(run_id, stream name)``;
    its ``seq`` continues across restarts of the same run.
    """

    run_id: str = fld(Id("run"), "Run that produced the record (see the RunManifest of that run).")
    mode: RunMode = fld(EnumOf(RunMode), "REPLAY or PAPER.  No live mode exists (routing lock).")
    data_origin: DataOrigin = fld(
        EnumOf(DataOrigin),
        "Least trustworthy origin among the market data the record is derived from.",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Produced(RunScoped):
    """Header of records that GIE derives from market data (decisions and state)."""

    decision_time_ns: int = fld(
        TimeNs(), "Clock reading at which the producer created the record.", unit="ns since epoch, UTC"
    )
    as_of_time_ns: int = fld(
        TimeNs(),
        "Market-data watermark: receive time of the newest market data the record depends on, "
        "directly or through its inputs.",
        unit="ns since epoch, UTC",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "CAUSAL-1",
            "as_of_time_ns <= decision_time_ns "
            "(a record cannot depend on data received after it was created)",
            lambda r: r.as_of_time_ns <= r.decision_time_ns,
        ),
    )
