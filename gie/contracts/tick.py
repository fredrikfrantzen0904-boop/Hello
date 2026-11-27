"""Tick contract - one top-of-book quote of one instrument from one feed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from gie.contracts._spec import Dec, EnumOf, Int, ListOf, Opt, Record, Str, TimeNs, fld, inv
from gie.contracts.common import PRICE, DataOrigin, symbol_field
from gie.contracts.registry import contract

SOURCE_PATTERN = r"^[a-z0-9][a-z0-9_.:-]{0,63}$"


class TickFlag(str, Enum):
    """Data-quality flags.  A flag never removes a tick; it tells consumers what is wrong.

    Detection thresholds are owned by session 01 (market data).
    """

    #: ``ask < bid``.  Mandatory whenever the quote is crossed.
    CROSSED = "CROSSED"
    #: Source timestamp is later than the local receive time (clock offset).
    EVENT_AFTER_RECEIVE = "EVENT_AFTER_RECEIVE"
    #: Source timestamp is earlier than that of the previous tick of the stream.
    OUT_OF_ORDER = "OUT_OF_ORDER"
    #: Same source timestamp and prices as the previous tick of the stream.
    DUPLICATE = "DUPLICATE"
    #: The feed lost data immediately before this tick.
    GAP_BEFORE = "GAP_BEFORE"
    #: Received later after its source timestamp than the feed-freshness limit.
    STALE = "STALE"
    #: Price or spread outside the plausibility bounds of the data-quality filter.
    OUTLIER = "OUTLIER"
    #: Quote observed outside the broker's trading session.
    OFF_SESSION = "OFF_SESSION"


@contract("Tick", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Tick(Record):
    """Raw bid/ask quote exactly as observed, plus ingest metadata.

    Stream: ``tick/<source>/<symbol>``; ``seq`` is assigned by ingest in arrival
    order.  Ordering time: ``receive_time_ns``.  Ticks are never modified after
    ingest: problems are expressed as flags.
    """

    symbol: str = symbol_field("Canonical GIE symbol (XAUUSD is the only tradable one).")
    source: str = fld(
        Str(pattern=SOURCE_PATTERN, max_len=64, label="feed id"),
        "Identifier of the feed that delivered the quote.",
    )
    origin: DataOrigin = fld(
        EnumOf(DataOrigin), "Whether the quote is a live broker observation, imported history or a fixture."
    )
    seq: int = fld(
        Int(ge=0, label="sequence number"),
        "Ingest sequence of the (source, symbol) stream: starts at 0 and increases by exactly 1.",
    )
    event_time_ns: int = fld(
        TimeNs(), "Timestamp given by the source, converted to UTC.", unit="ns since epoch, UTC"
    )
    receive_time_ns: int = fld(
        TimeNs(),
        "Local clock reading when GIE received the quote.  For imported history without a recorded "
        "receive time it equals event_time_ns plus the modelled feed delay.",
        unit="ns since epoch, UTC",
    )
    bid: Decimal = fld(PRICE, "Best bid.", unit="quote currency per unit (USD per troy ounce for XAUUSD)")
    ask: Decimal = fld(PRICE, "Best ask.", unit="quote currency per unit (USD per troy ounce for XAUUSD)")
    bid_volume: Decimal | None = fld(
        Opt(Dec(ge="0", label="volume")),
        "Size at the best bid; null when the feed does not provide it.",
        unit="lots",
    )
    ask_volume: Decimal | None = fld(
        Opt(Dec(ge="0", label="volume")),
        "Size at the best ask; null when the feed does not provide it.",
        unit="lots",
    )
    flags: tuple[TickFlag, ...] = fld(
        ListOf(
            EnumOf(TickFlag),
            max_items=16,
            unique_key=lambda flag: flag,
            canonical_sort=lambda flag: flag.value,
            sorted_label="sorted by value",
        ),
        "Data-quality flags (empty when nothing is wrong).",
    )

    INVARIANTS = (
        inv(
            "TICK-1",
            "the CROSSED flag is present if and only if ask < bid",
            lambda t: (t.ask < t.bid) == (TickFlag.CROSSED in t.flags),
        ),
        inv(
            "TICK-2",
            "the EVENT_AFTER_RECEIVE flag is present if and only if event_time_ns > receive_time_ns",
            lambda t: (t.event_time_ns > t.receive_time_ns) == (TickFlag.EVENT_AFTER_RECEIVE in t.flags),
        ),
    )

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def stream(self) -> str:
        return f"tick/{self.source}/{self.symbol}"
