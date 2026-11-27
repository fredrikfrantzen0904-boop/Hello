"""Tick contract - one top-of-book quote of one instrument from one feed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, final

from gie.contracts._spec import Dec, EnumOf, Int, Invariant, ListOf, Opt, Record, Str, TimeNs, fld, inv
from gie.contracts.common import PRICE, DataOrigin, symbol_field
from gie.contracts.registry import contract
from gie.core.canonical import EXACT
from gie.core.errors import FutureDataError

SOURCE_PATTERN = r"^[a-z0-9][a-z0-9_.:-]{0,63}$"


class TickFlag(str, Enum):
    """Data-quality flags.  A flag never removes a tick; it tells consumers what is wrong.

    The definitions below are the published meaning.  Thresholds, look-back
    lengths and the source clock-offset estimate are owned by session 01
    (market data) and are research parameters.
    """

    #: ``ask < bid``.  Mandatory whenever the quote is crossed.
    CROSSED = "CROSSED"
    #: Source timestamp is later than the local receive time (source clock ahead).
    EVENT_AFTER_RECEIVE = "EVENT_AFTER_RECEIVE"
    #: Source timestamp is lower than the highest source timestamp of any earlier tick
    #: of the stream (high-water mark).
    OUT_OF_ORDER = "OUT_OF_ORDER"
    #: All source fields (event time, bid, ask, sizes) equal those of a tick already
    #: ingested within the look-back window.
    DUPLICATE = "DUPLICATE"
    #: The feed lost data immediately before this tick.
    GAP_BEFORE = "GAP_BEFORE"
    #: Source delay (receive time minus event time, corrected for the estimated source
    #: clock offset) exceeds the limit for this feed.
    DELAYED = "DELAYED"
    #: Price or spread outside the plausibility bounds of the data-quality filter.
    OUTLIER = "OUTLIER"
    #: Quote observed outside the broker's trading session.
    OFF_SESSION = "OFF_SESSION"
    #: The receive time was not recorded but modelled.  Mandatory for imported history
    #: without a recorded receive time; such ticks are never evidence about latency.
    RECEIVE_TIME_MODELLED = "RECEIVE_TIME_MODELLED"


@final
@contract("Tick", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Tick(Record):
    """Raw bid/ask quote exactly as observed, plus ingest metadata.

    Stream: ``tick/<source>/<symbol>``.  Ordering time: ``receive_time_ns``,
    which never decreases within a stream.  ``seq`` is assigned once, when the
    tick is ingested into the tick store, in arrival order; it never restarts and
    replay preserves it.  Ticks are never modified after ingest: problems are
    expressed as flags, and a backfill, re-import or re-flagging creates a new,
    versioned ``source``.

    Imported history without a recorded receive time carries
    ``receive_time_ns = max(receive time of the previous tick of the stream,
    event_time_ns + modelled feed delay)`` and the flag ``RECEIVE_TIME_MODELLED``.
    """

    symbol: str = symbol_field("Canonical GIE symbol (XAUUSD is the only tradable one).")
    source: str = fld(
        Str(pattern=SOURCE_PATTERN, max_len=64, label="feed id"),
        "Identifier of the feed (and dataset version) that delivered the quote.",
    )
    origin: DataOrigin = fld(
        EnumOf(DataOrigin),
        "Whether the quote is a real-time feed observation, imported history or a fixture.",
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
        "Local clock reading when GIE received the quote (the availability instant), "
        "or the modelled value for imported history.",
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

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
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
        return EXACT.divide(EXACT.add(self.bid, self.ask), Decimal(2))

    @property
    def spread(self) -> Decimal:
        return EXACT.subtract(self.ask, self.bid)

    @property
    def stream(self) -> str:
        return f"tick/{self.source}/{self.symbol}"


def feed_silence_ns(last_tick: Tick, now_ns: int) -> int:
    """How long the stream of *last_tick* has been silent: ``now_ns - receive_time_ns``.

    This is the freshness reference for market data (section 3.3): the receive
    time of the newest accepted tick, never its source event time.  A receive
    time later than *now_ns* is look-ahead or clock skew and raises
    :class:`~gie.core.errors.FutureDataError`.
    """
    if now_ns < last_tick.receive_time_ns:
        raise FutureDataError(
            "tick was received after the current instant",
            now_ns=now_ns,
            receive_time_ns=last_tick.receive_time_ns,
            stream=last_tick.stream,
        )
    return now_ns - last_tick.receive_time_ns
