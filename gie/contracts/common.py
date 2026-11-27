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
    "DataOrigin",
    "ExecutionVenue",
    "Horizon",
    "MarketSession",
    "NamedValue",
    "OrderType",
    "PriceBasis",
    "PriceTrigger",
    "Produced",
    "RunMode",
    "RunScoped",
    "Sequenced",
    "Side",
    "Timeframe",
    "combine_origins",
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


# --------------------------------------------------------------------------- enums
class Side(str, Enum):
    """Direction.  For a position, ``BUY`` means long and ``SELL`` means short."""

    BUY = "BUY"
    SELL = "SELL"


class DataOrigin(str, Enum):
    """Where the market data behind a record came from.

    Ordered from most to least trustworthy; a derived record carries the *least*
    trustworthy origin among its inputs (:func:`combine_origins`).
    """

    BROKER_FEED = "BROKER_FEED"
    HISTORICAL = "HISTORICAL"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


_ORIGIN_ORDER: Final[dict[DataOrigin, int]] = {
    DataOrigin.BROKER_FEED: 0,
    DataOrigin.HISTORICAL: 1,
    DataOrigin.SYNTHETIC_FIXTURE: 2,
}


def combine_origins(*origins: DataOrigin) -> DataOrigin:
    """Origin of a record derived from inputs with the given origins (least trustworthy wins)."""
    if not origins:
        raise ValueError("at least one origin is required")
    return max(origins, key=lambda origin: _ORIGIN_ORDER[origin])


class Timeframe(str, Enum):
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
    AT_OR_BELOW = "AT_OR_BELOW"
    AT_OR_ABOVE = "AT_OR_ABOVE"


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


# --------------------------------------------------------------------------- value objects
@record
@dataclass(frozen=True, slots=True, kw_only=True)
class NamedValue(Record):
    """A named finite number (feature value or metric)."""

    name: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Name from the owning dictionary.")
    value: float = fld(Float(), "Finite numeric value.")


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
    """Header of records that belong to one replay or paper run."""

    run_id: str = fld(Id("run"), "Run that produced the record.")
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
        "Receive time of the newest input the record depends on (causality watermark).",
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


ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)

PRICE = Dec(gt="0", label="price")
