"""Event ordering and sequence integrity (requirement S00-ASG-25, "event ordering").

Two orders exist and they answer different questions.

**Input order.**  A run is driven by *exogenous inputs*: timers, ticks and
other observations, and (in replay of recorded sessions) broker reports.
Inputs are delivered in ascending :class:`EventKey` ``(time_ns, rank, stream,
seq)``, where ``time_ns`` is the *availability* instant - the moment the
information could first be known to GIE (receive time), never the source event
time.  Ordering by availability is what prevents look-ahead in replay.
``rank`` breaks ties between inputs that became available at the same instant
(:class:`StreamRank`); ``stream`` and ``seq`` make the order total.

**Journal order.**  Each input is processed to completion before the next one
is delivered ("run-to-completion step").  Every record produced in that step is
published to the run's journal, which numbers records in publication order.
That number - not the :class:`EventKey` - is the total order of a run: it is
causal by construction (a record is always published after the records it was
derived from) and it is reproducible because processing is deterministic.

:class:`EventKey` and :func:`merge_ordered` are therefore used to *merge input
streams* and, as a fallback, to order records of different journals; the ranks
of GIE-produced records follow the data flow so that this fallback is causal
for a chain that shares one instant.

**Per-stream sequence.**  Every stream numbers its records with consecutive
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
    """Tie-break for records with the same availability instant.

    Inputs first (run manifest, clock, market data, broker reports and the
    position state they imply), then GIE's own records in data-flow order:
    evidence, candidate, plan, risk decision, order request.  A new shared
    contract obtains a rank here when the coordinator adds it.
    """

    RUN_MANIFEST = 0
    CLOCK = 5
    TICK = 10
    ORDER_REPORT = 20
    POSITION = 30
    EVIDENCE = 40
    CANDIDATE = 50
    TRADE_PLAN = 60
    RISK_DECISION = 70
    ORDER_REQUEST = 80
    MODEL_VERSION = 90
    OTHER = 99


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
    first event of a stream      accepted if it equals ``first_seq`` (default 0;
                                 ``None`` accepts any start, for a stream that is
                                 joined in the middle)
    ``seq == last + 1``          accepted
    ``seq > last + 1``           :class:`SequenceGapError`, or
                                 ``ACCEPTED_AFTER_GAP`` if ``allow_gaps``
    ``seq <= last``, same hash   ``DUPLICATE`` - the caller must drop it
    ``seq <= last``, other hash  :class:`SequenceConflictError`
    ``seq <= last``, not kept    :class:`OutOfOrderError` (cannot be proven a
                                 duplicate, so it is refused)
    ordering time decreases      :class:`OutOfOrderError` (when ``time_ns`` is given)
    ===========================  =================================================

    A refused arrival never changes tracker state.  After a restart,
    :meth:`restore` re-establishes the position of a stream from the journal so
    that re-sent records are recognised as duplicates.

    Tick streams are tracked with ``allow_gaps=True`` (a feed can lose data; the
    gap is reported and no new risk may be created on it); order-event streams
    and all streams of GIE's own records are gap-free.
    """

    def __init__(self, *, allow_gaps: bool = False, first_seq: int | None = 0, window: int = 1024) -> None:
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise ContractValidationError("window must be an int >= 1", value=repr(window)[:32])
        if first_seq is not None and (
            isinstance(first_seq, bool) or not isinstance(first_seq, int) or first_seq < 0
        ):
            raise ContractValidationError("first_seq must be None or an int >= 0", value=repr(first_seq)[:32])
        if not isinstance(allow_gaps, bool):
            raise ContractValidationError("allow_gaps must be a bool")
        self._allow_gaps: Final[bool] = allow_gaps
        self._first_seq: Final[int | None] = first_seq
        self._window: Final[int] = window
        self._last: dict[str, int] = {}
        self._last_time: dict[str, int] = {}
        self._recent: dict[str, OrderedDict[int, str]] = {}

    def last_seq(self, stream: str) -> int | None:
        return self._last.get(stream)

    @staticmethod
    def _check_arguments(stream: str, seq: int, fingerprint: str, time_ns: int | None) -> None:
        if not isinstance(stream, str) or not stream:
            raise ContractValidationError("stream must be a non-empty string")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ContractValidationError("seq must be a non-negative int", stream=stream)
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ContractValidationError("fingerprint must be a non-empty string", stream=stream)
        if time_ns is not None and (isinstance(time_ns, bool) or not isinstance(time_ns, int) or time_ns < 0):
            raise ContractValidationError("time_ns must be None or a non-negative int", stream=stream)

    def restore(self, stream: str, last_seq: int, recent: dict[int, str] | None = None) -> None:
        """Re-establish a stream after a restart: its last journaled ``seq`` and recent fingerprints."""
        self._check_arguments(stream, last_seq, "restore", None)
        if stream in self._last:
            raise SequenceConflictError("stream is already tracked and cannot be restored", stream=stream)
        kept: OrderedDict[int, str] = OrderedDict()
        for seq in sorted(recent or {}):
            fingerprint = (recent or {})[seq]
            self._check_arguments(stream, seq, fingerprint, None)
            if seq > last_seq:
                raise ContractValidationError("recent fingerprints lie beyond last_seq", stream=stream)
            kept[seq] = fingerprint
        while len(kept) > self._window:
            kept.popitem(last=False)
        self._last[stream] = last_seq
        self._recent[stream] = kept

    def observe(self, stream: str, seq: int, fingerprint: str, time_ns: int | None = None) -> SeqResult:
        """Classify one arrival.  ``time_ns`` is the record's ordering time, if the caller has it."""
        self._check_arguments(stream, seq, fingerprint, time_ns)
        last = self._last.get(stream)
        if last is not None and seq <= last:
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
        if time_ns is not None and stream in self._last_time and time_ns < self._last_time[stream]:
            raise OutOfOrderError(
                "ordering time of a stream may not decrease",
                stream=stream,
                seq=seq,
                time_ns=time_ns,
                last_time_ns=self._last_time[stream],
            )
        if last is None:
            if self._first_seq is not None and seq != self._first_seq:
                raise SequenceGapError(
                    "stream does not start at the required first sequence number",
                    stream=stream,
                    seq=seq,
                    expected=self._first_seq,
                )
            self._accept(stream, seq, fingerprint, time_ns)
            return SeqResult(SeqOutcome.ACCEPTED)
        if seq == last + 1:
            self._accept(stream, seq, fingerprint, time_ns)
            return SeqResult(SeqOutcome.ACCEPTED)
        missing = seq - last - 1
        if not self._allow_gaps:
            raise SequenceGapError("sequence gap", stream=stream, seq=seq, expected=last + 1, missing=missing)
        self._accept(stream, seq, fingerprint, time_ns)
        return SeqResult(SeqOutcome.ACCEPTED_AFTER_GAP, missing=missing)

    def _accept(self, stream: str, seq: int, fingerprint: str, time_ns: int | None) -> None:
        self._last[stream] = seq
        if time_ns is not None:
            self._last_time[stream] = time_ns
        recent = self._recent.setdefault(stream, OrderedDict())
        recent[seq] = fingerprint
        while len(recent) > self._window:
            recent.popitem(last=False)
