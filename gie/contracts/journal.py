"""Run journal: the append-only record of a run and the definition of its total order.

A journal belongs to exactly one run.  Its first record is the run's
:class:`~gie.contracts.run.RunManifest`.  Records are numbered in publication
order (``journal_seq`` 0, 1, 2, ...); that number is the total order of the
run.  Because every input is processed to completion before the next one is
delivered, publication order is causal, and because processing is
deterministic, replaying the same inputs reproduces the journal byte for byte.

Before a record is appended it must pass, in this order:

1. **type and run** - it is an instance of a registered contract (exact type)
   and belongs to the journal's run and mode;
2. **stream position** - an identical record at an already journaled position
   is an idempotent duplicate and is not appended again; different content at
   a journaled position is a conflict; otherwise it is the next ``seq`` of its
   stream with a non-decreasing ordering time
   (:class:`~gie.core.ordering.SequenceTracker`);
3. **provenance** - what the run manifest declares: a tick comes from a declared
   source, lies inside the replayed period and claims no better origin than the
   run; no record claims a better data origin than the run; a scoring model is
   one the run pinned, with the pinned status, and is valid for the record's
   market-data watermark;
4. **time** - when the journal was given the run clock, a record is stamped
   with the instant of the step in which it is published (never later, never
   earlier); tick receive times never decrease across streams; the look-ahead
   guard: the market-data watermark of a derived record does not lie beyond
   the newest input that was journaled, and a derived record cannot precede
   the first input;
5. **history** - the chain, reference and aggregate rules of the run ledger
   (:class:`~gie.contracts.ledger.LifecycleLedger`).

A refused record leaves the journal unchanged.  The checks judge what a record
*declares*; a component that lies about its watermark is found by the
prefix-invariance test of the backtesting session, not here.

:class:`MemoryJournal` keeps the entries in memory.  :class:`FileJournal`
writes each entry as one line of canonical JSON to an append-only file -
before the entry becomes visible - and rebuilds its state from that file when
it is opened again (restart).  :class:`JournalReader` is the read-only way to
follow a journal file that another process writes (dashboard, QA).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from gie.contracts._spec import Record
from gie.contracts.candidate import Candidate
from gie.contracts.clock import ClockReading
from gie.contracts.codec import fingerprint, from_wire, to_wire
from gie.contracts.common import DataOrigin, Produced, RunMode, origin_not_better_than
from gie.contracts.ledger import LifecycleLedger
from gie.contracts.model_version import ModelVersion
from gie.contracts.order_event import OrderEvent
from gie.contracts.ordering import ordering_time_ns, stream_name
from gie.contracts.risk_decision import RiskDecision
from gie.contracts.run import RunManifest
from gie.contracts.tick import Tick
from gie.contracts.trade_plan import TradePlan
from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.clock import Clock
from gie.core.errors import (
    CanonicalEncodingError,
    ConfigError,
    ContractValidationError,
    FutureDataError,
    GieError,
    JournalCorruptError,
    OutOfOrderError,
    SequenceConflictError,
    UnknownContractError,
)
from gie.core.ordering import SeqOutcome, SequenceTracker

_LINE_KEYS: Final[frozenset[str]] = frozenset({"journal_seq", "stream", "fingerprint", "record"})


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


def _refuse(rule: str, record: Record, **details: str | int | None) -> ContractValidationError:
    return ContractValidationError(
        f"record refused by the run journal: {rule}", contract=type(record).__name__, rule=rule, **details
    )


class MemoryJournal:
    """In-memory journal of one run (implements ``RecordSink`` and ``RecordSource``).

    *clock* is the run clock.  A journal that has it holds every record to the
    step in which it is published; a journal without it (tools, tests, a
    journal file being re-read) can only apply the checks that need no clock.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        if clock is not None and not isinstance(clock, Clock):
            raise ConfigError("the journal needs the clock of its run (or none)")
        self._clock: Clock | None = clock
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
        self._input_watermark_ns: int | None = None
        self._tick_time_ns: int | None = None
        self._journaled_tick_streams: Final[set[str]] = set()
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
        """The run ledger: what the journaled records have established so far (read it, never write)."""
        return self._ledger

    @property
    def input_watermark_ns(self) -> int | None:
        """Newest instant the run is known to have reached (newest journaled tick or clock reading)."""
        return self._input_watermark_ns

    def __len__(self) -> int:
        return len(self._entries)

    def read(self, from_journal_seq: int = 0) -> Iterator[JournalEntry]:
        """Entries in journal order, starting at *from_journal_seq*."""
        _require_position(from_journal_seq)
        yield from self._entries[from_journal_seq:]

    def streams(self) -> tuple[str, ...]:
        return tuple(sorted({entry.stream for entry in self._entries}))

    def canonical_lines(self) -> list[str]:
        """The journal as canonical JSON lines (what :class:`FileJournal` writes)."""
        return [canonical_dumps(entry.to_wire()) for entry in self._entries]

    def commit(self) -> None:
        """End of a step: make everything published so far durable (nothing to do in memory)."""

    # ------------------------------------------------------------------ checks
    def _tracker(self, record: Record) -> SequenceTracker:
        if isinstance(record, Tick):
            return self._tick_streams
        if isinstance(record, ModelVersion):
            return self._registry_streams
        return self._run_streams

    def _check_run(self, record: Record) -> None:
        if self._manifest is None:
            if not isinstance(record, RunManifest):
                raise _refuse("the first record of a journal is the RunManifest of its run", record)
            return
        if isinstance(record, RunManifest):
            if record != self._manifest:
                raise SequenceConflictError("a journal belongs to exactly one run", run_id=record.run_id)
            return
        run_id: str | None = getattr(record, "run_id", None)
        mode: RunMode | None = getattr(record, "mode", None)
        if run_id is not None and run_id != self._manifest.run_id:
            raise _refuse("record belongs to another run", record, run_id=run_id)
        if mode is not None and mode is not self._manifest.mode:
            raise _refuse("record has another run mode than its run", record)

    def _check_provenance(self, record: Record) -> None:
        manifest = self._manifest
        if manifest is None or isinstance(record, RunManifest | ModelVersion):
            return
        if isinstance(record, Tick):
            if record.source not in manifest.data_sources:
                raise _refuse("a tick comes from a data source the run manifest declares", record)
            if not origin_not_better_than(manifest.data_origin, record.origin):
                raise _refuse("the run does not claim a better data origin than a tick it uses", record)
            if manifest.mode is RunMode.REPLAY:
                assert manifest.data_start_ns is not None
                assert manifest.data_end_ns is not None
                if not manifest.data_start_ns <= record.receive_time_ns < manifest.data_end_ns:
                    raise _refuse("a tick lies inside the period the replay declares", record)
            return
        origin: DataOrigin | None = getattr(record, "data_origin", None)
        if origin is not None and not origin_not_better_than(origin, manifest.data_origin):
            raise _refuse("no record claims a better data origin than its run", record)
        if isinstance(record, Candidate | TradePlan) and record.model_version_id is not None:
            pinned = manifest.model(record.model_version_id, record.as_of_time_ns)
            if pinned is None:
                raise _refuse(
                    "a scoring model is one the run manifest pins and is valid for the record's watermark",
                    record,
                    model_version_id=record.model_version_id,
                )
            if record.model_status is not pinned.status:
                raise _refuse(
                    "model_status is the status the run manifest pins for the model",
                    record,
                    model_version_id=record.model_version_id,
                )
        if isinstance(record, OrderEvent) and record.fill_tick_source is not None:
            # A fill names the quote it was matched against.  Where the run journals that tick
            # stream, the quote must be part of the journal; a run that keeps its ticks elsewhere
            # (tick store, extension CX-19) cannot be checked here.
            assert record.fill_tick_seq is not None
            quotes = f"tick/{record.fill_tick_source}/{record.symbol}"
            if (
                quotes in self._journaled_tick_streams
                and (quotes, record.fill_tick_seq) not in self._positions
            ):
                raise _refuse(
                    "a fill names a quote of a journaled tick stream that is part of the journal",
                    record,
                    stream=quotes,
                    fill_tick_seq=record.fill_tick_seq,
                )
        if isinstance(record, RiskDecision) and record.quality_model_version_id is not None:
            pinned = manifest.model(record.quality_model_version_id, record.as_of_time_ns)
            if pinned is None or not pinned.validated:
                raise _refuse(
                    "quality used for sizing comes from a model the run manifest pins as validated",
                    record,
                    model_version_id=record.quality_model_version_id,
                )

    def _check_time(self, record: Record) -> None:
        if isinstance(record, RunManifest | ModelVersion):
            return  # registry and manifest times are not run time
        time_ns = ordering_time_ns(record)
        if self._clock is not None:
            now_ns = self._clock.now_ns()
            if time_ns > now_ns:
                raise FutureDataError(
                    "record is stamped later than the step in which it is published",
                    contract=type(record).__name__,
                    time_ns=time_ns,
                    now_ns=now_ns,
                )
            if time_ns < now_ns:
                raise OutOfOrderError(
                    "record is stamped earlier than the step in which it is published",
                    contract=type(record).__name__,
                    time_ns=time_ns,
                    now_ns=now_ns,
                )
        if isinstance(record, Tick):
            if self._tick_time_ns is not None and record.receive_time_ns < self._tick_time_ns:
                raise OutOfOrderError(
                    "tick receive times never decrease across the streams of a run (input order)",
                    stream=record.stream,
                    time_ns=record.receive_time_ns,
                    last_time_ns=self._tick_time_ns,
                )
            return
        if isinstance(record, Produced):
            watermark = self._input_watermark_ns
            if watermark is None or record.as_of_time_ns > watermark:
                raise FutureDataError(
                    "record depends on market data that has not been delivered yet (look-ahead)",
                    contract=type(record).__name__,
                    as_of_time_ns=record.as_of_time_ns,
                    input_watermark_ns=watermark,
                )

    # ------------------------------------------------------------------ writing
    def publish(self, record: Record) -> int:
        """Append *record* and return its ``journal_seq`` (idempotent for an identical repeat)."""
        if not isinstance(record, Record) or type(record).CONTRACT is None:
            raise UnknownContractError(
                "only records of registered contracts can be journaled", actual_type=type(record).__name__
            )
        wire = to_wire(record)  # refuses anything that is not a registered contract (exact type)
        stream = stream_name(record)
        seq: int = record.seq  # type: ignore[attr-defined]
        digest = fingerprint(record)
        self._check_run(record)
        known = self._positions.get((stream, seq))
        if known is not None:
            if self._entries[known].fingerprint == digest:
                return known
            raise SequenceConflictError(
                "different content for an already journaled stream position", stream=stream, seq=seq
            )
        tracker = self._tracker(record)
        result = tracker.check(stream, seq, digest, ordering_time_ns(record))
        self._check_provenance(record)
        self._check_time(record)
        self._ledger.check(record)

        entry = JournalEntry(len(self._entries), stream, digest, record)
        # Durable first: only a record that has been written becomes part of the journal.
        self._persist(entry, wire)
        tracker.observe(stream, seq, digest, ordering_time_ns(record))
        self._ledger.admit(record)
        if result.outcome is SeqOutcome.ACCEPTED_AFTER_GAP:
            self.tick_gaps += 1
        if isinstance(record, RunManifest):
            self._manifest = record
        if isinstance(record, Tick):
            self._tick_time_ns = record.receive_time_ns
            self._journaled_tick_streams.add(stream)
        if isinstance(record, Tick | ClockReading):
            self._input_watermark_ns = max(self._input_watermark_ns or 0, ordering_time_ns(record))
        self._entries.append(entry)
        self._positions[(stream, seq)] = entry.journal_seq
        return entry.journal_seq

    def _persist(self, entry: JournalEntry, wire: dict[str, Any]) -> None:
        """Hook for durable journals; the in-memory journal has nothing to do."""


def _require_position(from_journal_seq: int) -> None:
    if isinstance(from_journal_seq, bool) or not isinstance(from_journal_seq, int) or from_journal_seq < 0:
        raise ContractValidationError("from_journal_seq must be an int >= 0")


def _line(journal_seq: int, stream: str, digest: str, wire: dict[str, Any]) -> bytes:
    text = canonical_dumps(
        {"journal_seq": journal_seq, "stream": stream, "fingerprint": digest, "record": wire}
    )
    return text.encode("utf-8") + b"\n"


def _parse_line(line: bytes, number: int, where: str) -> tuple[dict[str, Any], Record]:
    """One journal line -> (its parsed form, its record); raises :class:`JournalCorruptError`."""
    try:
        parsed = strict_loads(line)
        if not isinstance(parsed, dict) or set(parsed) != _LINE_KEYS:
            raise JournalCorruptError("journal line has the wrong shape", path=where, line=number)
        # The line itself must be canonical: this detects every change of its bytes, including
        # rewritten line ends, without depending on the contract version that wrote it.
        if canonical_dumps(parsed).encode("utf-8") != line:
            raise JournalCorruptError("journal line is not in canonical form", path=where, line=number)
        if parsed["journal_seq"] != number:
            raise JournalCorruptError("journal line is not at its position", path=where, line=number)
        record: Record = from_wire(parsed["record"])
        if fingerprint(record) != parsed["fingerprint"] or stream_name(record) != parsed["stream"]:
            raise JournalCorruptError(
                "journal line does not match the record it contains", path=where, line=number
            )
    except JournalCorruptError:
        raise
    except (GieError, CanonicalEncodingError) as exc:
        raise JournalCorruptError(
            "journal line cannot be read", path=where, line=number, cause=exc.code
        ) from exc
    return parsed, record


def _complete_lines(data: bytes) -> tuple[list[bytes], int]:
    """Split *data* at line feeds only; returns the complete lines and the length they cover."""
    if not data:
        return [], 0
    end = data.rfind(b"\n") + 1
    return (data[:end].split(b"\n")[:-1] if end else []), end


def _lock_exclusive(handle: IO[bytes]) -> bool:
    """Take a non-blocking exclusive lock that the operating system releases when the process dies."""
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415 - platform-specific standard library module

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 - platform-specific standard library module

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


Opener = Callable[[Path], IO[bytes]]


def _append_opener(path: Path) -> IO[bytes]:
    return path.open("ab")


class FileJournal(MemoryJournal):
    """Durable journal: one canonical JSON line per record in an append-only file.

    * **Durable before visible.**  A record is written (and, with ``fsync=True``,
      forced to stable storage) before it becomes part of the journal.  If the
      write fails the record is not published, the journal marks itself failed
      and refuses every further publication until it has been reopened - memory
      can never run ahead of the file.
    * **One writer.**  Opening takes an exclusive lock (on ``<path>.lock``, held
      by the operating system for the life of the process); a second writer is
      refused with :class:`~gie.core.errors.ConfigError`.  Readers use
      :class:`JournalReader`.
    * **Restart.**  Opening an existing file replays it through the same checks
      as live publication (except the clock rule: the instants of an earlier
      process are history), so a journal that does not reproduce its own history
      is refused (:class:`JournalCorruptError`).  A crash can leave an incomplete
      last line; that is refused as well - :func:`repair_torn_tail`, or
      :meth:`open_for_restart`, removes it, because the record on it was never
      published.  A line written by an older MINOR or PATCH version of a
      contract is read like any other message of that version.
    * ``fsync=True`` (the default) forces every record to stable storage before
      ``publish`` returns.  A run loop that prefers one synchronisation per step
      opens the journal with ``fsync=False`` and calls :meth:`commit` at the end
      of each step.
    * *opener* replaces the function that opens the file for appending; quality
      assurance uses it to inject write failures.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        fsync: bool = True,
        opener: Opener | None = None,
    ) -> None:
        super().__init__(None)
        self._path: Final[Path] = Path(path)
        self._fsync: Final[bool] = fsync
        self._handle: IO[bytes] | None = None
        self._failed: bool = False
        self._dirty: bool = False
        self.repaired_bytes: int = 0
        self._lock: IO[bytes] | None = Path(f"{self._path}.lock").open("a+b")  # noqa: SIM115 - held open
        if not _lock_exclusive(self._lock):
            self._lock.close()
            self._lock = None
            raise ConfigError("journal file is already open for writing", path=str(self._path))
        try:
            self._replaying = True
            if self._path.exists():
                self._replay()
            self._replaying = False
            self._handle = (opener or _append_opener)(self._path)
        except BaseException:
            self.close()
            raise
        self._clock = clock

    @classmethod
    def open_for_restart(
        cls, path: Path, *, clock: Clock | None = None, fsync: bool = True, opener: Opener | None = None
    ) -> FileJournal:
        """Open a journal after a crash: an incomplete last line is removed first.

        The torn line was never published (its ``publish`` did not return), so
        removing it loses nothing; ``repaired_bytes`` tells the run loop that it
        happened so that it can report it.  Every other kind of damage is still
        refused.
        """
        removed = repair_torn_tail(path) if Path(path).exists() else 0
        journal = cls(path, clock=clock, fsync=fsync, opener=opener)
        journal.repaired_bytes = removed
        return journal

    @property
    def path(self) -> Path:
        return self._path

    def _replay(self) -> None:
        data = self._path.read_bytes()
        lines, covered = _complete_lines(data)
        if covered != len(data):
            raise JournalCorruptError(
                "journal ends with an incomplete line (torn write); see repair_torn_tail",
                path=str(self._path),
            )
        for number, line in enumerate(lines):
            _, record = _parse_line(line, number, str(self._path))
            try:
                journal_seq = self.publish(record)
            except GieError as exc:
                raise JournalCorruptError(
                    "journal line cannot be replayed", path=str(self._path), line=number, cause=exc.code
                ) from exc
            if journal_seq != number:
                raise JournalCorruptError(
                    "journal line repeats an earlier record", path=str(self._path), line=number
                )

    def _persist(self, entry: JournalEntry, wire: dict[str, Any]) -> None:
        if self._replaying:
            return
        if self._failed:
            raise JournalCorruptError(
                "journal is in a failed state after a write error; reopen it", path=str(self._path)
            )
        if self._handle is None:
            raise JournalCorruptError("journal is closed", path=str(self._path))
        try:
            self._handle.write(_line(entry.journal_seq, entry.stream, entry.fingerprint, wire))
            self._handle.flush()
            if self._fsync:
                os.fsync(self._handle.fileno())
            else:
                self._dirty = True
        except Exception as exc:
            # Part of the line may be on disk.  Nothing was published; whatever is there is a torn
            # tail that the next opening removes or refuses.
            self._failed = True
            raise JournalCorruptError(
                "journal write failed; the record was not published", path=str(self._path)
            ) from exc

    def commit(self) -> None:
        """Force everything published so far to stable storage (one synchronisation per step)."""
        if self._handle is None or self._failed or not self._dirty:
            return
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except Exception as exc:
            self._failed = True
            raise JournalCorruptError("journal synchronisation failed", path=str(self._path)) from exc
        self._dirty = False

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def __enter__(self) -> FileJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def repair_torn_tail(path: Path) -> int:
    """Remove an incomplete last line from a journal file; returns the number of bytes removed.

    Only the bytes after the last line feed are removed.  A complete line is
    never touched, so nothing that was durably published is lost.
    """
    data = Path(path).read_bytes()
    _, covered = _complete_lines(data)
    if covered == len(data):
        return 0
    with Path(path).open("r+b") as handle:
        handle.truncate(covered)
        handle.flush()
        os.fsync(handle.fileno())
    return len(data) - covered


class JournalReader:
    """Read-only follower of a journal file (implements ``RecordSource``).

    For the dashboard, quality assurance and any process other than the run:
    it opens the file for reading only, never takes the writer's lock, can
    neither publish nor reach an order gateway, and does not keep the records
    in memory.  A last line that is still being written is simply not there
    yet.  Every complete line is verified: canonical form, position,
    fingerprint and contract validity of its record.
    """

    def __init__(self, path: Path) -> None:
        self._path: Final[Path] = Path(path)
        self._offsets: list[int] = []  # byte offset of each verified line
        self._verified_bytes: int = 0
        self._streams: set[str] = set()
        self._run_id: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str | None:
        self.poll()
        return self._run_id

    def __len__(self) -> int:
        """Number of complete entries seen so far (call :meth:`poll` to look for more)."""
        return len(self._offsets)

    def streams(self) -> tuple[str, ...]:
        self.poll()
        return tuple(sorted(self._streams))

    def _read_from(self, offset: int) -> bytes:
        if not self._path.exists():
            return b""
        with self._path.open("rb") as handle:
            handle.seek(offset)
            return handle.read()

    def poll(self) -> list[JournalEntry]:
        """Verify and return the entries that were completed since the last call."""
        lines, covered = _complete_lines(self._read_from(self._verified_bytes))
        fresh: list[JournalEntry] = []
        offset = self._verified_bytes
        for line in lines:
            number = len(self._offsets)
            parsed, record = _parse_line(line, number, str(self._path))
            if number == 0:
                if not isinstance(record, RunManifest):
                    raise JournalCorruptError(
                        "the first record of a journal is the RunManifest of its run", path=str(self._path)
                    )
                self._run_id = record.run_id
            self._offsets.append(offset)
            offset += len(line) + 1
            self._streams.add(parsed["stream"])
            fresh.append(JournalEntry(number, parsed["stream"], parsed["fingerprint"], record))
        self._verified_bytes += covered
        return fresh

    def read(self, from_journal_seq: int = 0) -> Iterator[JournalEntry]:
        """Entries in journal order, starting at *from_journal_seq*, up to the last complete line."""
        _require_position(from_journal_seq)
        self.poll()
        if from_journal_seq >= len(self._offsets):
            return
        data = self._read_from(self._offsets[from_journal_seq])
        lines, _ = _complete_lines(data)
        for index, line in enumerate(lines[: len(self._offsets) - from_journal_seq]):
            number = from_journal_seq + index
            parsed, record = _parse_line(line, number, str(self._path))
            yield JournalEntry(number, parsed["stream"], parsed["fingerprint"], record)
