"""Event ordering and sequence integrity (requirement S00-ASG-25, "event ordering").

**Total order.**  All events of a run are processed in ascending
:class:`EventKey` order ``(time_ns, rank, stream, seq)``:

* ``time_ns`` is the *availability* instant - the moment the information could
  first be known to GIE (receive time for market data and broker events,
  decision time for records GIE produces itself).  Ordering by availability,
  not by source event time, is what prevents look-ahead in replay.
* ``rank`` breaks ties between different kinds of information that became
  available at the same instant (:class:`StreamRank`): the clock first, then
  market data, then execution facts, then derived intelligence.  Protective
  logic therefore always sees a fill before any decision derived at the same
  instant.
* ``stream`` (a stable name) and ``seq`` (the producer's own counter) make the
  order total and deterministic.

**Per-stream sequence.**  Every stream numbers its events with consecutive
integers.  :class:`SequenceTracker` classifies each arrival as accepted, an
idempotent duplicate, a gap, an out-of-order arrival or a conflict.
"""

from __future__ import annotations

import heapq
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Final, TypeVar

from gie.core.errors import (
    ContractValidationError,
    OutOfOrderError,
    SequenceConflictError,
    SequenceGapError,
)

T = TypeVar("T")


class StreamRank(IntEnum):
    """Tie-break order for events that became available at the same instant."""

    CLOCK = 0
    TICK = 10
    ORDER_EVENT = 20
    POSITION = 30
    RISK_DECISION = 40
    EVIDENCE = 50
    CANDIDATE = 60
    TRADE_PLAN = 70
    MODEL_VERSION = 80
    OTHER = 90


@dataclass(frozen=True, slots=True, order=True)
class EventKey:
    """Position of one event in the total order of a run."""

    time_ns: int
    rank: int
    stream: str
    seq: int

    def __post_init__(self) -> None:
        for name in ("time_ns", "rank", "seq"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(
                    "event key component must be a non-negative int", component=name
                )
        if not isinstance(self.stream, str) or not self.stream:
            raise ContractValidationError("event key stream must be a non-empty string")


def merge_ordered(*streams: Iterable[tuple[EventKey, T]]) -> Iterator[tuple[EventKey, T]]:
    """Deterministic k-way merge of streams that are each sorted by :class:`EventKey`.

    Raises :class:`OutOfOrderError` if an input is not strictly ascending and
    :class:`SequenceConflictError` if two inputs yield the same key (the order
    would then depend on the argument order, i.e. not be deterministic).
    """

    def checked(index: int, source: Iterable[tuple[EventKey, T]]) -> Iterator[tuple[EventKey, int, T]]:
        previous: EventKey | None = None
        for key, item in source:
            if previous is not None and not previous < key:
                raise OutOfOrderError(
                    "input stream is not strictly ascending",
                    input_index=index,
                    stream=key.stream,
                    seq=key.seq,
                )
            previous = key
            yield key, index, item

    last: EventKey | None = None
    for key, _index, item in heapq.merge(
        *(checked(i, s) for i, s in enumerate(streams)), key=lambda entry: entry[0]
    ):
        if last is not None and key == last:
            raise SequenceConflictError(
                "two inputs produced the same event key", stream=key.stream, seq=key.seq
            )
        last = key
        yield key, item


class SeqOutcome(str, Enum):
    """Classification of one arrival by :class:`SequenceTracker`."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    ACCEPTED_AFTER_GAP = "ACCEPTED_AFTER_GAP"


@dataclass(frozen=True, slots=True)
class SeqResult:
    outcome: SeqOutcome
    #: Number of sequence numbers skipped immediately before this arrival.
    missing: int = 0


class SequenceTracker:
    """Per-stream sequence validation with idempotent duplicate detection.

    For every stream the tracker remembers the last accepted ``seq`` and the
    fingerprints (content hashes) of the most recent ``window`` arrivals.

    ===========================  =================================================
    arrival                      result
    ===========================  =================================================
    first event of a stream      accepted (must equal ``first_seq`` if given)
    ``seq == last + 1``          accepted
    ``seq > last + 1``           :class:`SequenceGapError`, or
                                 ``ACCEPTED_AFTER_GAP`` if ``allow_gaps``
    ``seq <= last``, same hash   ``DUPLICATE`` - the caller must drop it
    ``seq <= last``, other hash  :class:`SequenceConflictError`
    ``seq <= last``, not kept    :class:`OutOfOrderError` (cannot be proven a
                                 duplicate, so it is refused)
    ===========================  =================================================

    A refused arrival never changes tracker state.
    """

    def __init__(self, *, allow_gaps: bool = False, first_seq: int | None = None, window: int = 1024) -> None:
        if window < 1:
            raise ContractValidationError("window must be >= 1", value=window)
        self._allow_gaps: Final[bool] = allow_gaps
        self._first_seq: Final[int | None] = first_seq
        self._window: Final[int] = window
        self._last: dict[str, int] = {}
        self._recent: dict[str, OrderedDict[int, str]] = {}

    def last_seq(self, stream: str) -> int | None:
        return self._last.get(stream)

    def observe(self, stream: str, seq: int, fingerprint: str) -> SeqResult:
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ContractValidationError("seq must be a non-negative int", stream=stream)
        last = self._last.get(stream)
        if last is None:
            if self._first_seq is not None and seq != self._first_seq:
                raise SequenceGapError(
                    "stream does not start at the required first sequence number",
                    stream=stream,
                    seq=seq,
                    expected=self._first_seq,
                )
            self._accept(stream, seq, fingerprint)
            return SeqResult(SeqOutcome.ACCEPTED)
        if seq == last + 1:
            self._accept(stream, seq, fingerprint)
            return SeqResult(SeqOutcome.ACCEPTED)
        if seq > last + 1:
            missing = seq - last - 1
            if not self._allow_gaps:
                raise SequenceGapError(
                    "sequence gap", stream=stream, seq=seq, expected=last + 1, missing=missing
                )
            self._accept(stream, seq, fingerprint)
            return SeqResult(SeqOutcome.ACCEPTED_AFTER_GAP, missing=missing)
        known = self._recent[stream].get(seq)
        if known is None:
            raise OutOfOrderError(
                "late arrival that cannot be verified as a duplicate",
                stream=stream,
                seq=seq,
                last_seq=last,
            )
        if known != fingerprint:
            raise SequenceConflictError(
                "different content for an already accepted sequence number",
                stream=stream,
                seq=seq,
            )
        return SeqResult(SeqOutcome.DUPLICATE)

    def _accept(self, stream: str, seq: int, fingerprint: str) -> None:
        self._last[stream] = seq
        recent = self._recent.setdefault(stream, OrderedDict())
        recent[seq] = fingerprint
        while len(recent) > self._window:
            recent.popitem(last=False)
