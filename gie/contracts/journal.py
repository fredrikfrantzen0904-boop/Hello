"""Run journal: the append-only record of a run and the definition of its total order.

A journal belongs to exactly one run.  Its first record is the run's
:class:`~gie.contracts.run.RunManifest`.  Records are numbered in publication
order (``journal_seq`` 0, 1, 2, ...); that number is the total order of the
run.  Because every input is processed to completion before the next one is
delivered, publication order is causal, and because processing is
deterministic, replaying the same inputs reproduces the journal byte for byte.

Before a record is appended it must pass, in this order:

1. it is an instance of a registered contract (exact type) and belongs to the
   journal's run and mode;
2. its stream sequence (:class:`~gie.core.ordering.SequenceTracker`): the next
   ``seq`` of its stream, with a non-decreasing ordering time - an identical
   record at an already journaled position is an idempotent duplicate and is
   not appended again;
3. the sequence rules of its lifecycle chain
   (:class:`~gie.contracts.ledger.LifecycleLedger`);
4. the look-ahead guard: a derived record's market-data watermark may not lie
   beyond the newest tick that was journaled.

A refused record leaves the journal unchanged.

:class:`MemoryJournal` keeps the entries in memory.  :class:`FileJournal`
additionally writes each entry as one line of canonical JSON to an append-only
file and rebuilds its state from that file when it is opened again (restart).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from gie.contracts._spec import Record
from gie.contracts.codec import fingerprint, from_wire, to_wire
from gie.contracts.common import Produced, RunMode
from gie.contracts.ledger import LifecycleLedger
from gie.contracts.model_version import ModelVersion
from gie.contracts.ordering import ordering_time_ns, stream_name
from gie.contracts.run import RunManifest
from gie.contracts.tick import Tick
from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import (
    CanonicalEncodingError,
    ContractValidationError,
    FutureDataError,
    GieError,
    JournalCorruptError,
    SequenceConflictError,
)
from gie.core.ordering import SeqOutcome, SequenceTracker


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One journaled record with its position in the total order of the run."""

    journal_seq: int
    stream: str
    fingerprint: str
    record: Record

    def to_wire(self) -> dict[str, Any]:
        return {
            "journal_seq": self.journal_seq,
            "stream": self.stream,
            "fingerprint": self.fingerprint,
            "record": to_wire(self.record),
        }


class MemoryJournal:
    """In-memory journal of one run (implements ``RecordSink`` and ``RecordSource``)."""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._positions: dict[tuple[str, int], int] = {}
        self._manifest: RunManifest | None = None
        self._ledger: Final[LifecycleLedger] = LifecycleLedger()
        # Streams of GIE's own records start at 0 and are gap-free.  Tick and model-registry
        # streams are numbered outside the run (tick store, registry), so a run joins them in the
        # middle; a tick feed may also lose data, which is reported, not refused.
        self._run_streams: Final[SequenceTracker] = SequenceTracker(first_seq=0)
        self._registry_streams: Final[SequenceTracker] = SequenceTracker(first_seq=None)
        self._tick_streams: Final[SequenceTracker] = SequenceTracker(first_seq=None, allow_gaps=True)
        self._tick_watermark_ns: int | None = None
        self.tick_gaps: int = 0

    # ------------------------------------------------------------------ reading
    @property
    def manifest(self) -> RunManifest | None:
        return self._manifest

    @property
    def run_id(self) -> str | None:
        return None if self._manifest is None else self._manifest.run_id

    @property
    def ledger(self) -> LifecycleLedger:
        return self._ledger

    def __len__(self) -> int:
        return len(self._entries)

    def read(self, from_journal_seq: int = 0) -> Iterator[JournalEntry]:
        """Entries in journal order, starting at *from_journal_seq*."""
        if (
            isinstance(from_journal_seq, bool)
            or not isinstance(from_journal_seq, int)
            or from_journal_seq < 0
        ):
            raise ContractValidationError("from_journal_seq must be an int >= 0")
        yield from self._entries[from_journal_seq:]

    def streams(self) -> tuple[str, ...]:
        return tuple(sorted({entry.stream for entry in self._entries}))

    def canonical_lines(self) -> list[str]:
        """The journal as canonical JSON lines (what :class:`FileJournal` writes)."""
        return [canonical_dumps(entry.to_wire()) for entry in self._entries]

    # ------------------------------------------------------------------ writing
    def _tracker(self, record: Record) -> SequenceTracker:
        if isinstance(record, Tick):
            return self._tick_streams
        if isinstance(record, ModelVersion):
            return self._registry_streams
        return self._run_streams

    def _check_run(self, record: Record) -> None:
        if self._manifest is None:
            if not isinstance(record, RunManifest):
                raise ContractValidationError(
                    "the first record of a journal is the RunManifest of its run",
                    contract=type(record).__name__,
                )
            return
        if isinstance(record, RunManifest):
            if record != self._manifest:
                raise SequenceConflictError("a journal belongs to exactly one run", run_id=record.run_id)
            return
        run_id: str | None = getattr(record, "run_id", None)
        mode: RunMode | None = getattr(record, "mode", None)
        if run_id is not None and run_id != self._manifest.run_id:
            raise ContractValidationError(
                "record belongs to another run", contract=type(record).__name__, run_id=run_id
            )
        if mode is not None and mode is not self._manifest.mode:
            raise ContractValidationError(
                "record has another run mode than its run", contract=type(record).__name__
            )

    def _check_watermark(self, record: Record) -> None:
        if not isinstance(record, Produced) or self._tick_watermark_ns is None:
            return
        if record.as_of_time_ns > self._tick_watermark_ns:
            raise FutureDataError(
                "record depends on market data that has not been delivered yet (look-ahead)",
                contract=type(record).__name__,
                as_of_time_ns=record.as_of_time_ns,
                tick_watermark_ns=self._tick_watermark_ns,
            )

    def publish(self, record: Record) -> int:
        """Append *record* and return its ``journal_seq`` (idempotent for an identical repeat)."""
        wire = to_wire(record)  # refuses anything that is not a registered contract (exact type)
        stream = stream_name(record)
        digest = fingerprint(record)
        self._check_run(record)
        known = self._positions.get((stream, record.seq))  # type: ignore[attr-defined]
        if known is not None and self._entries[known].fingerprint == digest:
            return known
        self._check_watermark(record)
        self._ledger.check(record)
        result = self._tracker(record).observe(
            stream,
            record.seq,  # type: ignore[attr-defined]
            digest,
            ordering_time_ns(record),
        )
        if result.outcome is SeqOutcome.DUPLICATE:  # pragma: no cover - handled by the lookup above
            return self._positions[(stream, record.seq)]  # type: ignore[attr-defined]
        if result.outcome is SeqOutcome.ACCEPTED_AFTER_GAP:
            self.tick_gaps += 1
        self._ledger.admit(record)
        if isinstance(record, RunManifest):
            self._manifest = record
        if isinstance(record, Tick):
            self._tick_watermark_ns = max(self._tick_watermark_ns or 0, record.receive_time_ns)
        entry = JournalEntry(len(self._entries), stream, digest, record)
        self._entries.append(entry)
        self._positions[(stream, record.seq)] = entry.journal_seq  # type: ignore[attr-defined]
        self._persist(entry, wire)
        return entry.journal_seq

    def _persist(self, entry: JournalEntry, wire: dict[str, Any]) -> None:
        """Hook for durable journals; the in-memory journal has nothing to do."""


class FileJournal(MemoryJournal):
    """Durable journal: one canonical JSON line per record in an append-only file.

    Opening an existing file replays it through the same checks as live
    publication, so a journal that does not reproduce its own history is refused
    (:class:`JournalCorruptError`).  A crash can leave an incomplete last line;
    that is detected and refused as well - :func:`repair_torn_tail` removes it
    explicitly, because the record on it was never durably published.

    ``fsync=True`` (the default) forces every record to stable storage before
    ``publish`` returns.
    """

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        super().__init__()
        self._path: Final[Path] = Path(path)
        self._fsync: Final[bool] = fsync
        self._handle: IO[bytes] | None = None
        self._replaying = True
        if self._path.exists():
            self._replay()
        self._replaying = False
        self._handle = self._path.open("ab")

    @property
    def path(self) -> Path:
        return self._path

    def _replay(self) -> None:
        data = self._path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise JournalCorruptError(
                "journal ends with an incomplete line (torn write); see repair_torn_tail",
                path=str(self._path),
            )
        for number, line in enumerate(data.splitlines()):
            try:
                wire = strict_loads(line)
                if not isinstance(wire, dict) or set(wire) != {
                    "journal_seq",
                    "stream",
                    "fingerprint",
                    "record",
                }:
                    raise JournalCorruptError("journal line has the wrong shape", line=number)
                record: Record = from_wire(wire["record"])
                journal_seq = self.publish(record)
            except JournalCorruptError:
                raise
            except (GieError, CanonicalEncodingError) as exc:
                raise JournalCorruptError(
                    "journal line cannot be replayed", path=str(self._path), line=number, cause=exc.code
                ) from exc
            entry = self._entries[journal_seq]
            if (
                journal_seq != number
                or wire["journal_seq"] != number
                or wire["stream"] != entry.stream
                or wire["fingerprint"] != entry.fingerprint
                or canonical_dumps(entry.to_wire()) != line.decode("utf-8")
            ):
                raise JournalCorruptError(
                    "journal line does not match the record it contains", path=str(self._path), line=number
                )

    def _persist(self, entry: JournalEntry, wire: dict[str, Any]) -> None:
        if self._replaying:
            return
        if self._handle is None:
            raise JournalCorruptError("journal is closed", path=str(self._path))
        self._handle.write(canonical_dumps(entry.to_wire()).encode("utf-8") + b"\n")
        self._handle.flush()
        if self._fsync:
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> FileJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def repair_torn_tail(path: Path) -> int:
    """Remove an incomplete last line from a journal file; returns the number of bytes removed.

    Only the bytes after the last newline are removed.  A complete line is never
    touched, so nothing that was durably published is lost.  This is an explicit
    operator action after a crash, never done implicitly.
    """
    data = Path(path).read_bytes()
    if not data or data.endswith(b"\n"):
        return 0
    keep = data.rfind(b"\n") + 1
    with Path(path).open("r+b") as handle:
        handle.truncate(keep)
        handle.flush()
        os.fsync(handle.fileno())
    return len(data) - keep
