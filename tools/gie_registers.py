"""Persistent project registers: validate, render, sync test evidence, apply status updates, lock.

The project keeps four registers for its whole multi-session life (requirements OWN-08, OWN-16,
OWN-17, OWN-18, S00-ASG-27, S00-ASG-28):

* ``specifications/requirements_matrix.yaml`` - every requirement with its status (source of truth);
* ``specifications/acceptance_tests.yaml``    - the acceptance test register;
* ``docs/DECISION_LOG.md``    - decisions ``## D-001 - Title`` and open items ``## U-01 - Title``;
* ``docs/CHANGE_REQUESTS.md`` - blockers ``## B-001 - Title`` and change requests ``## CR-001 - Title``.

``specifications/requirements_lock.json`` (the requirement lock) pins the identity and the wording of
every requirement of the matrix, so that none can disappear or be reworded unnoticed.  It is written
by the ``lock`` command only and never edited by hand.

The two Markdown logs are written by hand.  Each entry is a level-2 heading followed by bullet lines
such as ``- Status: OPEN`` and ``- Requirements: S00-ASG-08, COM-03``.  Three Markdown files are
generated from the registers and must never be edited: ``specifications/REQUIREMENTS_MATRIX.md``,
``docs/ACCEPTANCE_TEST_REGISTER.md`` and ``docs/IMPLEMENTATION_STATUS.md``.

Usage: ``python -m tools.gie_registers <command> [--root PATH]``

``validate``
    Print every problem found in the registers; exit 1 if there is any.  Lines prefixed ``NOTE:``
    are warnings that do not fail.
``render [--check]``
    Write the three generated Markdown files, or with ``--check`` verify byte for byte that the
    committed files equal what would be written (exit 1 if one is missing or stale).
``sync-evidence --evidence FILE [--check]``
    Derive the status of every acceptance test from a pytest evidence file written by
    ``pytest --gie-evidence=FILE`` (see ``tests/conftest.py``) and record the counts.  With
    ``--check`` nothing is written: exit 0 only when the register already holds exactly what this
    evidence file gives (the file may then be outside the repository, e.g. fresh CI evidence).
``apply --patch FILE``
    Apply status updates to the requirements matrix from a YAML patch; all or nothing.
``lock [--reword ID [ID ...] --reason TEXT]``
    Add the requirements that are not locked yet to the requirement lock (the file is created when
    it does not exist).  With ``--reword`` additionally record a deliberate correction of the named
    requirements.  Refuses, writing nothing, while a locked requirement is missing from the matrix
    or differs from its locked hash without being named.
``stats``
    Print totals as JSON.

Exit status: 0 success, 1 problems were reported, 2 command line error.

Rules worth knowing (the complete list is the code of :func:`check_registers`):

* a requirement is never deleted and never reworded unnoticed.  The requirement lock holds, for
  every requirement id, the SHA-256 of its ``id``, ``source``, ``section``, ``text`` and
  ``source_clause`` (:func:`atom_hash`).  ``validate`` reports a missing lock file, a locked id that
  is missing from the matrix, an entry whose hash differs from the locked one and an id that is not
  locked yet.  A deliberate correction is recorded with ``lock --reword ID --reason TEXT``, which
  appends ``{id, old, new, reason}`` to the append-only ``changes`` list of the lock; the last
  change entry of an id must give its locked hash.  No command removes an id from the lock, and
  ``apply`` and ``sync-evidence`` never touch it.  In addition the requirement count may not drop
  below ``minimum_requirement_count``;
* the ``source_clause`` of a requirement is verbatim: it must occur in the text of its ``source`` -
  ``specifications/extracted/<name>.txt`` for a source ``<name>.pdf``, otherwise the file named by
  the repository-relative path in ``source`` (in a Markdown file a leading ``>`` quote marker and
  one following space are dropped from every line).  Runs of whitespace compare equal to one
  space; `` ... `` (space, three dots, space) marks an omission between two verbatim parts, and the
  parts must occur in the order of the source;
* the ``text`` of a requirement holds the owner's words only: every word of it must occur in its
  ``source_clause`` (ignoring case and a plural ending) or be one of a few function words
  (:data:`TEXT_GLUE_WORDS`); explanations and interpretations belong in ``notes``;
* the SHA-256 of every extracted specification text must equal the recorded value - a changed
  specification is noticed and the matrix must be re-verified against it;
* ``verified`` needs more than a path: a contribution with status ``verified`` of *every* session
  listed in ``sessions``; every contribution with status ``verified`` lists at least one evidence
  path and every listed path exists; a requirement whose id contains ``-ACC-`` additionally needs
  its acceptance test register entry to be ``passing``.  ``blocked`` needs a blocker id that exists
  as a heading in the Markdown logs;
* a ``review_status`` other than ``unverified`` needs a ``review_reference``: the id ``D-nnn`` of a
  decision that exists in ``docs/DECISION_LOG.md`` with ``- Status: APPROVED``;
* an acceptance test is never ``passing`` without recorded evidence (``passed > 0``, nothing failed,
  nothing skipped), and an expected failure (``xfailed``) is not a pass;
* the acceptance test register is bound to its evidence file: for every entry with an ``evidence``
  mapping the named file must exist inside the repository and be usable evidence, and the status and
  the counts recorded in the register must equal what ``sync-evidence`` derives from that file (a
  register edited by hand, or an evidence file that changed, is reported).  Without an ``evidence``
  mapping an entry can only be ``planned``, or ``blocked`` with blocker ids;
* an evidence file is refused when its ``exit_status`` is not 0 or 1 (the test run did not complete)
  or when it lists ``collection_errors`` (a test module that could not be imported is not a pass).
  ``sync-evidence`` also reports every module listed in ``test_modules`` of an entry with marked
  tests that contributed no executed test for that entry: a module that silently lost its tests
  must not go unnoticed;
* once any requirement has a status, every requirement named by an OPEN blocker must be ``blocked``
  or ``partial`` (before that the blocker is only announced in a ``NOTE:`` line).

Out of scope, because it cannot be detected statically: a test that carries an acceptance marker but
whose body can never fail (for example ``assert True``).  Such a test still counts as passed; only
the independent review of the test code can find it.

Interpretations made where the task left a choice (each is covered by a test):

* ``sync-evidence`` records the ``evidence`` mapping only for entries that have marked tests in the
  evidence file and removes a stale one; an entry without tests is ``planned``, or ``blocked`` when
  it lists blockers.  A test that did not run (``not_run``) counts as skipped and is reported.
* ``sync-evidence --check`` reports the same problems as ``sync-evidence`` would report for the
  evidence file (unknown marker ids, tests that did not run, a listed module without a test, a
  missing blocker id) in addition to every entry that differs; only the recorded path
  ``evidence.file`` is not compared.
* A missing requirement lock is a problem but not a structural one: ``render`` and ``stats`` still
  work, ``apply`` refuses (it never writes a state that ``validate`` rejects).  A lock that cannot
  be read or has the wrong shape is structural, like a damaged register.
* A ``review_reference`` is checked whenever it is present, also while the review status is still
  ``unverified``; ``review_status_values`` must contain ``unverified``.
* A source text or an evidence file that cannot be used is reported once per file, naming the
  entries that depend on it; an extracted text that the specification hash check already reports
  as missing is not reported a second time.
* "Equal session" of an acceptance entry and its requirement is exact: the requirement must list
  that one session.
* ``apply`` validates the complete result, so it also refuses when the registers were already
  invalid for a reason the patch does not repair.
* Unknown keys, an extracted specification text without a recorded hash, a log entry that names an
  unknown requirement id and a second contribution of the same session are reported as problems.

Nothing here reads a clock, the network or git: the same files always give the same output.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

MATRIX_FILE = "specifications/requirements_matrix.yaml"
ACCEPTANCE_FILE = "specifications/acceptance_tests.yaml"
LOCK_FILE = "specifications/requirements_lock.json"
EXTRACTED_DIR = "specifications/extracted"
DECISION_LOG_FILE = "docs/DECISION_LOG.md"
CHANGE_REQUESTS_FILE = "docs/CHANGE_REQUESTS.md"

MATRIX_MD = "specifications/REQUIREMENTS_MATRIX.md"
ACCEPTANCE_MD = "docs/ACCEPTANCE_TEST_REGISTER.md"
STATUS_MD = "docs/IMPLEMENTATION_STATUS.md"
GENERATED_FILES = (MATRIX_MD, ACCEPTANCE_MD, STATUS_MD)
GENERATED_NOTICE = "GENERATED - do not edit"

#: The fields of a requirement that the requirement lock pins: its identity and the owner's words.
LOCKED_FIELDS = ("id", "source", "section", "text", "source_clause")
#: The fixed explanatory text written into a new requirement lock.
LOCK_DESCRIPTION = (
    "Requirement lock of specifications/requirements_matrix.yaml, written by "
    "'python -m tools.gie_registers lock' - never edit it by hand. 'atoms' maps every requirement id to "
    "the SHA-256 of its id, source, section, text and source_clause. 'validate' fails when a locked "
    "requirement is missing from the matrix (requirements are never deleted), when one of these fields "
    "differs from what was locked, and when a requirement is not locked yet. 'changes' is the append-only "
    "history of deliberate corrections recorded by 'lock --reword ID --reason TEXT'. Ids are never removed."
)
_LOCK_KEYS = ("schema_version", "description", "atoms", "changes")
_LOCK_CHANGE_KEYS = ("id", "old", "new", "reason")
#: How a correction of locked words is recorded (quoted by the problems that ask for it).
_LOCK_COMMAND = "python -m tools.gie_registers lock"

#: Marks an omission between two verbatim parts of a ``source_clause``.
CLAUSE_OMISSION = " ... "
#: Number of characters of a part of a source clause shown in a problem.
_CLAUSE_PREVIEW = 60
_MARKDOWN_QUOTE_RE = re.compile(r"(?m)^> ?")

SESSIONS = tuple(f"S{number:02d}" for number in range(13))
REQUIREMENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z]+)?-[0-9]{2,3}$")

#: Function words that may connect the owner's words in an atomised requirement ``text``.
TEXT_GLUE_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "of",
        "for",
        "and",
        "to",
        "in",
        "at",
        "with",
        "be",
        "not",
        "do",
        "no",
        "as",
        "by",
        "on",
        "or",
        "it",
        "its",
        "that",
        "this",
    ]
)
_WORD_RE = re.compile(r"[a-z0-9%+&-]+(?:\.[0-9]+%?)?")

#: Requirement statuses whose meaning the rules below depend on; the matrix must declare them.
REQUIRED_STATUSES = ("not_started", "in_progress", "partial", "implemented", "verified", "blocked")
#: Requirement statuses listed one by one in the implementation status (most urgent first).
ATTENTION_STATUSES = ("blocked", "partial", "in_progress", "implemented")
#: Acceptance statuses derived by ``sync-evidence``; the register must declare them.
ACCEPTANCE_STATUSES = ("planned", "passing", "failing", "blocked", "partial")

#: Outcomes written by ``tests/conftest.py``.  An expected failure is not a pass; a test that
#: did not run is not a pass either (it is counted with the skipped tests and reported).
PASSED_OUTCOMES = frozenset({"passed"})
FAILED_OUTCOMES = frozenset({"failed", "error", "xpassed", "xfailed"})
SKIPPED_OUTCOMES = frozenset({"skipped", "not_run"})
#: Outcomes of a test that was executed (or skipped on purpose): everything except ``not_run``.
EXECUTED_OUTCOMES = (PASSED_OUTCOMES | FAILED_OUTCOMES | SKIPPED_OUTCOMES) - {"not_run"}
#: Exit statuses of a pytest run that completed: 0 all tests passed, 1 some tests failed.
COMPLETED_EXIT_STATUSES = (0, 1)
#: The review status of a requirement that nobody has reviewed yet; any other needs a review reference.
UNREVIEWED = "unverified"

_MATRIX_KEYS = (
    "schema_version",
    "title",
    "description",
    "conventions",
    "specification",
    "status_values",
    "review_status_values",
    "requirements",
)
_REQUIREMENT_KEYS = (
    "id",
    "source",
    "section",
    "kind",
    "text",
    "source_clause",
    "sessions",
    "status",
    "review_status",
    "contributions",
    "notes",
)
_REQUIREMENT_OPTIONAL_KEYS = ("blockers", "review_reference")
_CONTRIBUTION_KEYS = ("session", "summary", "status", "evidence")
_ACCEPTANCE_KEYS = ("schema_version", "title", "description", "status_values", "acceptance_tests")
_ACCEPTANCE_ENTRY_KEYS = (
    "id",
    "requirement",
    "session",
    "title",
    "verification",
    "test_modules",
    "status",
    "blockers",
    "notes",
)
_EVIDENCE_COUNT_KEYS = ("tests", "passed", "failed", "skipped")
_PATCH_CHANGE_KEYS = ("status", "review_status", "review_reference", "notes", "contribution", "blockers")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOCKER_ID_RE = re.compile(r"^(B|CR)-[0-9]{3,}$")
_REFERENCE_ID_RE = re.compile(r"^(?:(B|CR|D)-[0-9]{3,}|(U)-[0-9]{2,})$")
_DECISION_ID_RE = re.compile(r"^D-[0-9]{3,}$")
_ACCEPTANCE_MARKER_RE = re.compile(r"acceptance\s*\(([^)]*)\)")
_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")


class RegisterError(Exception):
    """A register or an input file cannot be used; the message says which one and why."""


# --------------------------------------------------------------------------- YAML
class _StrictLoader(yaml.SafeLoader):
    """Safe loader that refuses duplicate mapping keys (a duplicate would silently hide a value)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: list[Any] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


class _FlowList(list[Any]):
    """A list written in YAML flow style, for example ``[S00, S01]``."""


class _Dumper(yaml.SafeDumper):
    """Dumper of the registers: no anchors or aliases, short id lists in flow style."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_flow_list(dumper: yaml.SafeDumper, data: _FlowList) -> yaml.SequenceNode:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


_Dumper.add_representer(_FlowList, _represent_flow_list)

#: Keys whose value is a list of short scalar ids; they are written as ``key: [a, b]``.
FLOW_STYLE_KEYS = frozenset({"sessions", "blockers"})


def _with_flow_lists(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _FlowList(value)
            if key in FLOW_STYLE_KEYS and isinstance(value, list) and all(isinstance(v, str) for v in value)
            else _with_flow_lists(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_with_flow_lists(item) for item in node]
    return node


def parse_yaml(text: str, label: str) -> Any:
    """Parse one YAML document; duplicate keys and syntax errors raise :class:`RegisterError`."""
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - _StrictLoader is a SafeLoader
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. an impossible date such as 2026-13-45
        raise RegisterError(f"{label}: not valid YAML: {' '.join(str(exc).split())}") from exc


def dump_yaml(document: Any) -> str:
    """Serialise a register in the repository's formatting conventions.

    Key order is preserved, lines wrap at 110 columns, Unicode is written as is, no anchors or
    aliases are produced, and the lists named in :data:`FLOW_STYLE_KEYS` use flow style.  Loading
    a register and dumping it again reproduces the file byte for byte, so that updates made by
    this tool show up as small diffs.  YAML comments cannot be kept (PyYAML drops them); the
    registers therefore hold their explanations in ``description`` / ``notes`` fields.
    """
    return yaml.dump(
        _with_flow_lists(document),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=110,
        default_flow_style=False,
    )


def _read_text(path: Path) -> str:
    """File content as UTF-8 text without newline translation (byte-exact round trips)."""
    return path.read_bytes().decode("utf-8")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object without duplicate keys (a duplicate would silently hide a value)."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_json(text: str, label: str) -> Any:
    """Parse one JSON document; duplicate keys and syntax errors raise :class:`RegisterError`."""
    try:
        return json.loads(text, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise RegisterError(f"{label}: not valid JSON: {' '.join(str(exc).split())}") from exc


def _collapse(text: str) -> str:
    """*text* with every run of whitespace replaced by one space (none at either end)."""
    return " ".join(text.split())


def _some(ids: Sequence[str], limit: int = 5) -> str:
    """The first ids of a list, and how many more there are."""
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown} and {len(ids) - limit} more"


def _write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically: a failure never leaves a half-written register behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)


# --------------------------------------------------------------------------- reports
@dataclass
class Report:
    """Outcome of a command: problems fail it, notes are warnings, messages say what was done."""

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    #: Number of problems that are schema or type errors, or a missing / unreadable register.
    structural_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def structure_ok(self) -> bool:
        """False when the content of a register cannot be trusted enough to render or update it."""
        return self.structural_count == 0

    def problem(self, message: str) -> None:
        self.problems.append(message)

    def structural(self, message: str) -> None:
        self.problems.append(message)
        self.structural_count += 1

    def note(self, message: str) -> None:
        self.notes.append(message)

    def lines(self) -> list[str]:
        """Problems followed by the warnings, each warning prefixed ``NOTE:``."""
        return [*self.problems, *(f"NOTE: {note}" for note in self.notes)]


# --------------------------------------------------------------------------- Markdown logs
@dataclass(frozen=True)
class LogKind:
    prefix: str
    name: str
    file: str
    statuses: tuple[str, ...]
    min_digits: int


LOG_KINDS: dict[str, LogKind] = {
    "D": LogKind("D", "decision", DECISION_LOG_FILE, ("PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED"), 3),
    "U": LogKind("U", "open item", DECISION_LOG_FILE, ("OPEN", "RESOLVED"), 2),
    "B": LogKind("B", "blocker", CHANGE_REQUESTS_FILE, ("OPEN", "RESOLVED", "APPROVED", "REJECTED"), 3),
    "CR": LogKind(
        "CR", "change request", CHANGE_REQUESTS_FILE, ("OPEN", "RESOLVED", "APPROVED", "REJECTED"), 3
    ),
}
LOG_FILES = (DECISION_LOG_FILE, CHANGE_REQUESTS_FILE)

_ENTRY_HEADING_RE = re.compile(r"^## (D|U|B|CR)-([0-9]+) - (\S.*)$")
_ENTRY_LIKE_RE = re.compile(r"^#{1,6}\s*(?:D|U|B|CR)-[0-9]")
_SECTION_HEADING_RE = re.compile(r"^#{1,2}\s")
_FIELD_RE = re.compile(r"^- ([A-Za-z][A-Za-z ]*?)\s*:\s*(.*)$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


@dataclass(frozen=True)
class LogEntry:
    """One decision, open item, blocker or change request of a Markdown log."""

    id: str
    kind: str
    title: str
    #: Value of the single ``- Status:`` line, or "" when there is none.
    status: str
    #: True when there is exactly one status line and its value is allowed for the kind.
    status_valid: bool
    requirements: tuple[str, ...]
    file: str
    line: int

    @property
    def is_open(self) -> bool:
        return self.status_valid and self.status == "OPEN"


@dataclass(frozen=True)
class Logs:
    """Entries of both Markdown logs (first occurrence of each id) and what is wrong with them."""

    entries: tuple[LogEntry, ...] = ()
    problems: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()

    def by_id(self) -> dict[str, LogEntry]:
        return {entry.id: entry for entry in self.entries}

    def of_kind(self, *kinds: str) -> list[LogEntry]:
        return [entry for entry in self.entries if entry.kind in kinds]


def parse_log(text: str, file: str) -> tuple[list[LogEntry], list[str]]:
    """Entries of one Markdown log and the format problems found in it.

    An entry starts at ``## <id> - <title>`` and ends at the next level-1 or level-2 heading.
    Bullet lines ``- Key: value`` inside it are its fields (indented continuation lines belong to
    the field).  Fenced code blocks are ignored.  A heading that starts with something that looks
    like an entry id but does not have exactly this form is reported, never skipped silently.
    """
    entries: list[LogEntry] = []
    problems: list[str] = []
    heading: tuple[str, str, str, int] | None = None
    fields: list[list[str]] = []

    def close() -> None:
        nonlocal heading, fields
        if heading is None:
            return
        kind, entry_id, title, line = heading
        spec = LOG_KINDS[kind]
        statuses = [value.strip() for key, value in fields if key == "status"]
        allowed = ", ".join(spec.statuses)
        where = f"{file}:{line}: {entry_id}"
        if not statuses:
            problems.append(f"{where} has no '- Status:' line (allowed {spec.name} statuses: {allowed})")
        elif len(statuses) > 1:
            problems.append(f"{where} has {len(statuses)} '- Status:' lines; exactly one is required")
        elif statuses[0] not in spec.statuses:
            problems.append(
                f"{where} has status {statuses[0]!r}; allowed {spec.name} statuses: {allowed} "
                "(the line must hold the status word only)"
            )
        requirements = tuple(
            token.strip()
            for key, value in fields
            if key == "requirements"
            for token in value.split(",")
            if token.strip()
        )
        entries.append(
            LogEntry(
                id=entry_id,
                kind=kind,
                title=title,
                status=statuses[0] if statuses else "",
                status_valid=len(statuses) == 1 and statuses[0] in spec.statuses,
                requirements=requirements,
                file=file,
                line=line,
            )
        )
        heading = None
        fields = []

    in_fence = False
    continuing = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continuing = False
            continue
        if in_fence:
            continue
        match = _ENTRY_HEADING_RE.match(line)
        if match and len(match[2]) >= LOG_KINDS[match[1]].min_digits:
            close()
            heading = (match[1], f"{match[1]}-{match[2]}", match[3].strip(), number)
            continuing = False
        elif _ENTRY_LIKE_RE.match(line):
            close()
            continuing = False
            problems.append(
                f"{file}:{number}: heading {line!r} looks like a register entry but is not of the form "
                "'## D-001 - Title' / '## U-01 - Title' / '## B-001 - Title' / '## CR-001 - Title'"
            )
        elif _SECTION_HEADING_RE.match(line):
            close()
            continuing = False
        elif heading is not None:
            field_match = _FIELD_RE.match(line)
            if field_match:
                fields.append([field_match[1].strip().lower(), field_match[2]])
                continuing = True
            elif continuing and line[:1] in (" ", "\t") and line.strip():
                fields[-1][1] += " " + line.strip()
            else:
                continuing = False
    close()
    return entries, problems


def load_logs(root: Path) -> Logs:
    """Parse both Markdown logs below ``root``; a missing file contributes no entries."""
    entries: list[LogEntry] = []
    problems: list[str] = []
    missing: list[str] = []
    first: dict[str, LogEntry] = {}
    for file in LOG_FILES:
        path = root / file
        if not path.is_file():
            missing.append(file)
            continue
        try:
            text = _read_text(path)
        except UnicodeDecodeError as exc:
            problems.append(f"{file}: not UTF-8 text ({exc.reason})")
            continue
        parsed, found = parse_log(text, file)
        problems.extend(found)
        for entry in parsed:
            spec = LOG_KINDS[entry.kind]
            if spec.file != file:
                problems.append(f"{file}:{entry.line}: {entry.id} ({spec.name}) belongs in {spec.file}")
            earlier = first.get(entry.id)
            if earlier is not None:
                problems.append(
                    f"{file}:{entry.line}: duplicate id {entry.id} "
                    f"(first defined at {earlier.file}:{earlier.line})"
                )
                continue
            first[entry.id] = entry
            entries.append(entry)
    return Logs(tuple(entries), tuple(problems), tuple(missing))


# --------------------------------------------------------------------------- loading
@dataclass(frozen=True)
class Registers:
    """Everything the checks and the renderers read, loaded once."""

    root: Path
    #: Parsed documents (whatever the YAML holds; an empty file parses as None).
    matrix: Any
    acceptance: Any
    logs: Logs
    #: File contents as read (None when missing), to detect whether the file is in the tool's format.
    matrix_text: str | None = None
    acceptance_text: str | None = None
    #: Register files that are missing or unreadable; the reason is in the report of the loader.
    unloaded: tuple[str, ...] = ()
    #: Parsed requirement lock (None when the file is missing or unreadable).
    lock: Any = None
    #: True when the lock file does not exist; whether that is a problem is decided by the checks.
    lock_missing: bool = True


def _load_document(root: Path, file: str, report: Report) -> tuple[Any, str | None, bool]:
    """Parsed document, raw text and whether the file could be loaded at all."""
    path = root / file
    if not path.is_file():
        report.structural(f"{file}: the register file does not exist")
        return None, None, False
    try:
        text = _read_text(path)
    except UnicodeDecodeError as exc:
        report.structural(f"{file}: not UTF-8 text ({exc.reason})")
        return None, None, False
    try:
        return parse_yaml(text, file), text, True
    except RegisterError as exc:
        report.structural(str(exc))
        return None, text, False


def _load_lock(root: Path, report: Report) -> tuple[Any, bool, bool]:
    """Parsed requirement lock, whether the file is missing, and whether it could be read."""
    path = root / LOCK_FILE
    if not path.is_file():
        if path.exists():
            report.structural(f"{LOCK_FILE}: the requirement lock is not a file")
            return None, False, False
        return None, True, True
    try:
        return parse_json(_read_text(path), LOCK_FILE), False, True
    except UnicodeDecodeError as exc:
        report.structural(f"{LOCK_FILE}: not UTF-8 text ({exc.reason})")
    except RegisterError as exc:
        report.structural(str(exc))
    return None, False, False


def load_registers(root: Path) -> tuple[Registers, Report]:
    """Load the registers below ``root``; the report holds what could not be loaded."""
    report = Report()
    matrix, matrix_text, matrix_loaded = _load_document(root, MATRIX_FILE, report)
    acceptance, acceptance_text, acceptance_loaded = _load_document(root, ACCEPTANCE_FILE, report)
    lock, lock_missing, lock_loaded = _load_lock(root, report)
    unloaded = [
        file
        for file, loaded in (
            (MATRIX_FILE, matrix_loaded),
            (ACCEPTANCE_FILE, acceptance_loaded),
            (LOCK_FILE, lock_loaded),
        )
        if not loaded
    ]
    registers = Registers(
        root=root,
        matrix=matrix,
        acceptance=acceptance,
        logs=load_logs(root),
        matrix_text=matrix_text,
        acceptance_text=acceptance_text,
        unloaded=tuple(unloaded),
        lock=lock,
        lock_missing=lock_missing,
    )
    return registers, report


# --------------------------------------------------------------------------- schema helpers
def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_keys(
    mapping: Mapping[Any, Any], where: str, required: Sequence[str], optional: Sequence[str], report: Report
) -> None:
    for key in required:
        if key not in mapping:
            report.structural(f"{where}: missing key '{key}'")
    for key in mapping:
        if key not in required and key not in optional:
            report.structural(f"{where}: unknown key {key!r}")


def _require_text(mapping: Mapping[Any, Any], key: str, where: str, report: Report) -> None:
    if key in mapping and not _is_text(mapping[key]):
        report.structural(f"{where}: '{key}' must be a non-empty string")


def _repo_path_problem(root: Path, value: str) -> str | None:
    """Why ``value`` is not an existing repository-relative path, or None when it is one."""
    parts = value.split("/")
    if not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return "is not a repository-relative path with forward slashes"
    if any(part in ("", ".", "..") for part in parts):
        return "is not a normalised repository-relative path"
    if not (root / value).exists():
        return "does not exist"
    return None


def _has_acceptance_marker(text: str, requirement_id: str) -> bool:
    """True when ``text`` contains ``acceptance(... "<requirement id>" ...)`` (textual check)."""
    return any(
        requirement_id in _QUOTED_RE.findall(match.group(1)) for match in _ACCEPTANCE_MARKER_RE.finditer(text)
    )


def _sessions_problem(value: object) -> str | None:
    if not _is_str_list(value) or not value:
        return "'sessions' must be a non-empty list of session ids"
    assert isinstance(value, list)
    unknown = [session for session in value if session not in SESSIONS]
    if unknown:
        return f"unknown session id(s) {', '.join(map(repr, unknown))} (valid: S00..S12)"
    if len(set(value)) != len(value):
        return "'sessions' lists a session more than once"
    return None


# --------------------------------------------------------------------------- matrix checks
def source_text_file(source: str) -> str:
    """Repository-relative path of the text that the ``source_clause`` of a requirement is taken from.

    A source ``<name>.pdf`` is compared with its extracted text ``specifications/extracted/<name>.txt``;
    any other source is itself the repository-relative path of a text file.
    """
    if source.endswith(".pdf"):
        return f"{EXTRACTED_DIR}/{source.removesuffix('.pdf')}.txt"
    return source


def source_clause_defect(source_clause: str, source_text: str) -> str | None:
    """Why *source_clause* is not verbatim in *source_text* (whitespace collapsed), or None when it is.

    The clause is split at :data:`CLAUSE_OMISSION`; every part must be non-empty and occur in the
    source text, each one after the end of the one before.
    """
    position = 0
    for part in _collapse(source_clause).split(CLAUSE_OMISSION):
        if not part:
            return f"it has an empty part next to an omission mark '{CLAUSE_OMISSION.strip()}'"
        found = source_text.find(part, position)
        if found < 0:
            preview = repr(part[:_CLAUSE_PREVIEW])
            if part in source_text:
                return f"the part {preview} does not occur after the part before it (order of the source)"
            return f"the part {preview} does not occur"
        position = found + len(part)
    return None


@dataclass
class _SourceTexts:
    """Whitespace-collapsed text of the source documents, read once per check."""

    root: Path
    texts: dict[str, str] = field(default_factory=dict)
    #: Files that cannot be used (repository-relative path -> reason) and the requirements naming them.
    unusable: dict[str, str] = field(default_factory=dict)
    users: dict[str, list[str]] = field(default_factory=dict)
    #: Files that another check already reported as missing.
    reported: set[str] = field(default_factory=set)

    def get(self, file: str, requirement_id: str) -> str | None:
        """Collapsed text of *file*, or None when it cannot be used (reported once by :meth:`report`)."""
        if file not in self.texts and file not in self.unusable:
            self._read(file)
        if file in self.unusable:
            self.users.setdefault(file, []).append(requirement_id)
            return None
        return self.texts[file]

    def _read(self, file: str) -> None:
        reason = _repo_path_problem(self.root, file)
        path = self.root / file
        if reason is None and not path.is_file():
            reason = "is not a file"
        text = ""
        if reason is None:
            try:
                text = _read_text(path)
            except UnicodeDecodeError:
                reason = "is not UTF-8 text"
            except OSError as exc:
                reason = f"cannot be read ({exc.strerror or exc})"
        if reason is not None:
            self.unusable[file] = reason
            return
        if file.lower().endswith(".md"):
            text = _MARKDOWN_QUOTE_RE.sub("", text)
        self.texts[file] = _collapse(text)

    def report(self, report: Report) -> None:
        for file, reason in self.unusable.items():
            if file in self.reported:
                continue
            users = self.users[file]
            report.problem(
                f"source text {file!r} {reason}: the source_clause of {len(users)} requirement(s) "
                f"({_some(users)}) cannot be compared with it"
            )


@dataclass
class _MatrixInfo:
    """What the cross-register checks need to know about the matrix."""

    #: The source documents that the source clauses are compared with.
    sources: _SourceTexts
    usable: bool = False
    status_values: tuple[str, ...] = ()
    review_values: tuple[str, ...] = ()
    #: Every string id that occurs, sound entry or not.
    all_ids: set[str] = field(default_factory=set)
    #: Entries without a schema or type error, by id (first occurrence).
    sound: dict[str, dict[str, Any]] = field(default_factory=dict)


def _check_contribution(
    root: Path, contribution: object, where: str, status_values: Sequence[str], report: Report
) -> bool:
    """Schema of one contribution; returns True when its fields have the right types."""
    if not isinstance(contribution, dict):
        report.structural(f"{where}: a contribution must be a mapping")
        return False
    before = report.structural_count
    _check_keys(contribution, where, _CONTRIBUTION_KEYS, ("blockers",), report)
    if "session" in contribution and contribution["session"] not in SESSIONS:
        report.structural(f"{where}: 'session' must be one of S00..S12, not {contribution['session']!r}")
    _require_text(contribution, "summary", where, report)
    if "status" in contribution and contribution["status"] not in status_values:
        report.structural(
            f"{where}: status {contribution['status']!r} is not one of {', '.join(status_values)}"
        )
    if "evidence" in contribution and not _is_str_list(contribution["evidence"]):
        report.structural(f"{where}: 'evidence' must be a list of repository-relative paths")
    blockers = contribution.get("blockers", [])
    if not _is_str_list(blockers) or any(not _BLOCKER_ID_RE.fullmatch(item) for item in blockers):
        report.structural(
            f"{where}: 'blockers' must be a list of blocker / change request ids (B-nnn, CR-nnn)"
        )
    if report.structural_count != before:
        return False
    for path in contribution["evidence"]:
        reason = _repo_path_problem(root, path)
        if reason:
            report.problem(f"{where}: evidence path {path!r} {reason}")
    return True


def _words(text: str) -> list[str]:
    """Lower-case words of *text*; ``/`` and apostrophes separate words (``bid/ask`` is two words)."""
    normalised = text.lower().replace("'", " ").replace("\u2019", " ").replace("/", " ")
    words = (word.strip(".-") for word in _WORD_RE.findall(normalised))
    return [word for word in words if any(character.isalnum() for character in word)]


def _stem(word: str) -> str:
    for ending in ("ies", "es", "s"):
        if word.endswith(ending) and len(word) > len(ending) + 2:
            return word[: -len(ending)]
    return word


def words_not_in_clause(text: str, source_clause: str) -> list[str]:
    """Words of *text* that do not come from *source_clause* (see :data:`TEXT_GLUE_WORDS`)."""
    clause_words = set(_words(source_clause))
    clause_stems = {_stem(word) for word in clause_words}
    return sorted(
        {
            word
            for word in _words(text)
            if word not in clause_words and _stem(word) not in clause_stems and word not in TEXT_GLUE_WORDS
        }
    )


def _check_requirement(
    registers: Registers, entry: object, index: int, info: _MatrixInfo, report: Report
) -> None:
    if not isinstance(entry, dict):
        report.structural(f"{MATRIX_FILE}: requirements[{index}] must be a mapping")
        return
    entry_id = entry.get("id")
    where = (
        f"requirement {entry_id}" if isinstance(entry_id, str) else f"{MATRIX_FILE}: requirements[{index}]"
    )
    before = report.structural_count
    _check_keys(entry, where, _REQUIREMENT_KEYS, _REQUIREMENT_OPTIONAL_KEYS, report)
    if "id" in entry:
        if not isinstance(entry_id, str):
            report.structural(f"{where}: 'id' must be a string")
        else:
            if not REQUIREMENT_ID_RE.fullmatch(entry_id):
                report.structural(f"{where}: id does not match {REQUIREMENT_ID_RE.pattern}")
            if entry_id in info.all_ids:
                report.structural(f"{where}: duplicate requirement id")
            info.all_ids.add(entry_id)
    for key in ("source", "section", "kind", "text"):
        _require_text(entry, key, where, report)
    if "source_clause" in entry:
        if not isinstance(entry["source_clause"], str):
            report.structural(f"{where}: 'source_clause' must be a string")
        elif not entry["source_clause"].strip():
            report.problem(
                f"{where}: source_clause is empty (it must hold the verbatim paragraph of the specification)"
            )
    if "sessions" in entry:
        reason = _sessions_problem(entry["sessions"])
        if reason:
            report.structural(f"{where}: {reason}")
    if "status" in entry and entry["status"] not in info.status_values:
        report.structural(
            f"{where}: status {entry['status']!r} is not one of {', '.join(info.status_values)}"
        )
    if "review_status" in entry and entry["review_status"] not in info.review_values:
        report.structural(
            f"{where}: review_status {entry['review_status']!r} is not one of {', '.join(info.review_values)}"
        )
    if "notes" in entry and not isinstance(entry["notes"], str):
        report.structural(f"{where}: 'notes' must be a string")
    if "review_reference" in entry and not _is_decision_id(entry["review_reference"]):
        report.structural(f"{where}: 'review_reference' must be the id of a decision (D-nnn)")
    blockers = entry.get("blockers", [])
    if not _is_str_list(blockers) or any(not _REFERENCE_ID_RE.fullmatch(item) for item in blockers):
        report.structural(f"{where}: 'blockers' must be a list of ids (B-nnn, CR-nnn, D-nnn, U-nn)")
    contributions = entry.get("contributions", [])
    if not isinstance(contributions, list):
        report.structural(f"{where}: 'contributions' must be a list")
    else:
        seen_sessions: set[str] = set()
        for number, contribution in enumerate(contributions):
            label = f"{where}: contributions[{number}]"
            if not _check_contribution(registers.root, contribution, label, info.status_values, report):
                continue
            if contribution["session"] in seen_sessions:
                report.structural(f"{where}: more than one contribution of session {contribution['session']}")
            seen_sessions.add(contribution["session"])
    if report.structural_count != before or not isinstance(entry_id, str):
        return

    # From here on the entry has the right shape; the remaining rules are about its content.
    info.sound.setdefault(entry_id, entry)
    added_words = words_not_in_clause(entry["text"], entry["source_clause"])
    if entry["source_clause"].strip() and added_words:
        report.problem(
            f"{where}: text contains words that are not in its source_clause: {', '.join(added_words)} "
            "(text holds the owner's words only; explanations belong in 'notes')"
        )
    if entry["source_clause"].strip():
        file = source_text_file(entry["source"])
        source_text = info.sources.get(file, entry_id)
        defect = None if source_text is None else source_clause_defect(entry["source_clause"], source_text)
        if defect:
            report.problem(
                f"{where}: source_clause is not verbatim: {defect} in {file} (runs of whitespace compare "
                f"equal; '{CLAUSE_OMISSION}' marks an omission between two verbatim parts)"
            )
    references = [*blockers, *(item for c in contributions for item in c.get("blockers", []))]
    for reference in references:
        _check_reference(reference, where, registers.logs, report)
    for number, contribution in enumerate(contributions):
        if contribution["status"] == "verified" and not contribution["evidence"]:
            report.problem(
                f"{where}: contributions[{number}] ({contribution['session']}) has status 'verified' but "
                "lists no evidence path"
            )
    if entry["status"] == "verified":
        if not any(
            _repo_path_problem(registers.root, path) is None for c in contributions for path in c["evidence"]
        ):
            report.problem(
                f"{where}: status 'verified' needs at least one contribution with an existing evidence path"
            )
        verified_sessions = {c["session"] for c in contributions if c["status"] == "verified"}
        lacking = [session for session in entry["sessions"] if session not in verified_sessions]
        if lacking:
            report.problem(
                f"{where}: status 'verified' needs a contribution with status 'verified' of every session "
                f"in 'sessions'; there is none of {', '.join(lacking)}"
            )
    if entry["status"] == "blocked" and not references:
        report.problem(
            f"{where}: status 'blocked' needs a blocker id (B-nnn / CR-nnn) in a contribution "
            "or in a 'blockers' key of the requirement"
        )
    _check_review_reference(entry, where, registers.logs, report)


def _is_decision_id(value: object) -> bool:
    return isinstance(value, str) and _DECISION_ID_RE.fullmatch(value) is not None


def _check_review_reference(entry: Mapping[str, Any], where: str, logs: Logs, report: Report) -> None:
    """A review status other than 'unverified' rests on an APPROVED decision of the decision log."""
    reference = entry.get("review_reference")
    if reference is None:
        if entry["review_status"] != UNREVIEWED:
            report.problem(
                f"{where}: review_status '{entry['review_status']}' needs a 'review_reference': the id "
                f"D-nnn of the decision in {DECISION_LOG_FILE} with '- Status: APPROVED' that records "
                "the review"
            )
        return
    decision = logs.by_id().get(reference)
    if decision is None:
        _check_reference(reference, where, logs, report)
    elif not (decision.status_valid and decision.status == "APPROVED"):
        actual = f"'- Status: {decision.status}'" if decision.status else "no valid '- Status:' line"
        report.problem(
            f"{where}: review_reference {reference} must be a decision with '- Status: APPROVED' in "
            f"{DECISION_LOG_FILE}; it has {actual}"
        )


def _check_reference(reference: str, where: str, logs: Logs, report: Report) -> None:
    """A referenced B-/CR-/D-/U- id must exist as a heading in the Markdown logs."""
    match = _REFERENCE_ID_RE.fullmatch(reference)
    if match is None:
        return
    if reference in logs.by_id():
        return
    kind = LOG_KINDS[match[1] or match[2]]
    if kind.file in logs.missing_files:
        report.problem(f"{where}: references {reference} but {kind.file} does not exist")
    else:
        report.problem(f"{where}: references {reference}, which is not a heading in {kind.file}")


def _check_specification(root: Path, specification: object, info: _MatrixInfo, report: Report) -> None:
    where = f"{MATRIX_FILE}: specification"
    if not isinstance(specification, dict):
        report.structural(f"{where} must be a mapping")
        return
    _check_keys(specification, where, ("version", "note", "extracted_text_sha256"), (), report)
    _require_text(specification, "version", where, report)
    if "note" in specification and not isinstance(specification["note"], str):
        report.structural(f"{where}: 'note' must be a string")
    hashes = specification.get("extracted_text_sha256")
    if "extracted_text_sha256" not in specification:
        return
    if not isinstance(hashes, dict) or not hashes:
        report.structural(
            f"{where}: 'extracted_text_sha256' must be a non-empty mapping of file name to SHA-256"
        )
        return
    directory = root / EXTRACTED_DIR
    for name, digest in hashes.items():
        if not isinstance(name, str) or not name or name != Path(name).name or "\\" in name or name == "..":
            report.structural(f"{where}: {name!r} is not a plain file name in {EXTRACTED_DIR}/")
            continue
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            report.structural(
                f"{where}: the value recorded for {name} is not a lower-case SHA-256 hex digest"
            )
            continue
        path = directory / name
        if not path.is_file():
            report.problem(
                f"{EXTRACTED_DIR}/{name}: listed in specification.extracted_text_sha256 "
                "but the file does not exist"
            )
            info.sources.reported.add(f"{EXTRACTED_DIR}/{name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            report.problem(
                f"{EXTRACTED_DIR}/{name}: the specification text changed - recorded sha256 {digest}, "
                f"actual {actual}; re-verify the matrix against the new text before recording the new hash"
            )
    if directory.is_dir():
        for path in sorted(directory.glob("*.txt")):
            if path.name not in hashes:
                report.problem(
                    f"{EXTRACTED_DIR}/{path.name}: specification text without a recorded hash in "
                    "specification.extracted_text_sha256 (a new specification file must be noticed)"
                )


def _check_matrix(registers: Registers, report: Report) -> _MatrixInfo:
    info = _MatrixInfo(sources=_SourceTexts(registers.root))
    matrix = registers.matrix
    if MATRIX_FILE in registers.unloaded:
        return info
    if not isinstance(matrix, dict):
        report.structural(f"{MATRIX_FILE}: the top level must be a mapping")
        return info
    _check_keys(matrix, MATRIX_FILE, _MATRIX_KEYS, ("minimum_requirement_count",), report)
    if "schema_version" in matrix and not (
        _is_int(matrix["schema_version"]) and matrix["schema_version"] == 1
    ):
        report.structural(f"{MATRIX_FILE}: schema_version must be 1")
    for key in ("title", "description", "conventions"):
        _require_text(matrix, key, MATRIX_FILE, report)
    if "specification" in matrix:
        _check_specification(registers.root, matrix["specification"], info, report)

    status_values = matrix.get("status_values")
    if (
        isinstance(status_values, dict)
        and status_values
        and all(_is_text(key) and isinstance(value, str) for key, value in status_values.items())
    ):
        info.status_values = tuple(status_values)
        for name in REQUIRED_STATUSES:
            if name not in status_values:
                report.structural(
                    f"{MATRIX_FILE}: status_values must define '{name}' (the register rules use it)"
                )
    elif "status_values" in matrix:
        report.structural(f"{MATRIX_FILE}: status_values must be a non-empty mapping of status to meaning")
    review_values = matrix.get("review_status_values")
    if _is_str_list(review_values) and review_values and len(set(review_values)) == len(review_values):
        info.review_values = tuple(review_values)
        if UNREVIEWED not in review_values:
            report.structural(
                f"{MATRIX_FILE}: review_status_values must contain '{UNREVIEWED}' (the register rules use it)"
            )
    elif "review_status_values" in matrix:
        report.structural(f"{MATRIX_FILE}: review_status_values must be a non-empty list of distinct strings")

    requirements = matrix.get("requirements")
    if not isinstance(requirements, list):
        if "requirements" in matrix:
            report.structural(f"{MATRIX_FILE}: requirements must be a list")
        return info
    info.usable = True
    for index, entry in enumerate(requirements):
        _check_requirement(registers, entry, index, info, report)
    info.sources.report(report)

    if "minimum_requirement_count" in matrix:
        minimum = matrix["minimum_requirement_count"]
        if not _is_int(minimum) or minimum < 0:
            report.structural(f"{MATRIX_FILE}: minimum_requirement_count must be a non-negative integer")
        elif len(requirements) < minimum:
            report.problem(
                f"{MATRIX_FILE}: {len(requirements)} requirements, but minimum_requirement_count is "
                f"{minimum} - requirements are never deleted"
            )
    return info


def _check_log_references(registers: Registers, info: _MatrixInfo, report: Report) -> None:
    """Requirement ids named by log entries exist; requirements of OPEN blockers are not open work."""
    if not info.usable:
        return
    statuses_set = any(entry["status"] != "not_started" for entry in info.sound.values())
    for entry in registers.logs.entries:
        waiting: list[str] = []
        for requirement_id in entry.requirements:
            if requirement_id not in info.all_ids:
                report.problem(
                    f"{entry.file}:{entry.line}: {entry.id} names requirement {requirement_id!r}, "
                    "which is not in the requirements matrix"
                )
                continue
            requirement = info.sound.get(requirement_id)
            if entry.kind != "B" or not entry.is_open or requirement is None:
                continue
            if requirement["status"] in ("blocked", "partial"):
                continue
            if statuses_set:
                report.problem(
                    f"requirement {requirement_id}: named by the OPEN blocker {entry.id} but its status is "
                    f"'{requirement['status']}'; it must be 'blocked' or 'partial' until the blocker "
                    "is resolved"
                )
            else:
                waiting.append(requirement_id)
        if waiting:
            report.note(
                f"{entry.id} is OPEN: when statuses are set, {', '.join(waiting)} must be 'blocked' or "
                "'partial' (no requirement has a status yet, so this does not fail)"
            )


# --------------------------------------------------------------------------- requirement lock
def atom_hash(requirement: Mapping[str, Any]) -> str:
    """SHA-256 of the locked fields of a requirement (:data:`LOCKED_FIELDS`), as lower-case hex.

    The hash is taken over the UTF-8 bytes of the compact JSON object with sorted keys and
    unescaped Unicode, so it changes with any character of the id, source, section, text or
    source clause and with nothing else.
    """
    atom = {key: requirement[key] for key in LOCKED_FIELDS}
    encoded = json.dumps(atom, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8", errors="surrogatepass")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def lock_shape_problems(lock: object) -> list[str]:
    """What is wrong with the shape of a parsed requirement lock (empty when it can be used)."""
    if not isinstance(lock, dict):
        return [f"{LOCK_FILE}: the top level must be a JSON object"]
    problems = [f"{LOCK_FILE}: missing key '{key}'" for key in _LOCK_KEYS if key not in lock]
    problems += [f"{LOCK_FILE}: unknown key {key!r}" for key in lock if key not in _LOCK_KEYS]
    if "schema_version" in lock and not (_is_int(lock["schema_version"]) and lock["schema_version"] == 1):
        problems.append(f"{LOCK_FILE}: schema_version must be 1")
    if "description" in lock and not _is_text(lock["description"]):
        problems.append(f"{LOCK_FILE}: 'description' must be a non-empty string")
    atoms = lock.get("atoms", {})
    if not isinstance(atoms, dict):
        problems.append(f"{LOCK_FILE}: 'atoms' must be an object of requirement id to SHA-256")
    else:
        problems += [
            f"{LOCK_FILE}: the hash locked for {requirement_id} is not a lower-case SHA-256 hex digest"
            for requirement_id, digest in atoms.items()
            if not _is_sha256(digest)
        ]
    changes = lock.get("changes", [])
    if not isinstance(changes, list):
        problems.append(f"{LOCK_FILE}: 'changes' must be a list")
        return problems
    for index, change in enumerate(changes):
        where = f"{LOCK_FILE}: changes[{index}]"
        if not isinstance(change, dict):
            problems.append(f"{where} must be an object with the keys {', '.join(_LOCK_CHANGE_KEYS)}")
            continue
        problems += [f"{where}: missing key '{key}'" for key in _LOCK_CHANGE_KEYS if key not in change]
        problems += [f"{where}: unknown key {key!r}" for key in change if key not in _LOCK_CHANGE_KEYS]
        for key in ("id", "reason"):
            if key in change and not _is_text(change[key]):
                problems.append(f"{where}: '{key}' must be a non-empty string")
        for key in ("old", "new"):
            if key in change and not _is_sha256(change[key]):
                problems.append(f"{where}: '{key}' must be a lower-case SHA-256 hex digest")
    return problems


def lock_history_problems(lock: Mapping[str, Any]) -> list[str]:
    """Contradictions between ``atoms`` and ``changes`` of a lock of the right shape.

    Ids are never removed and ``changes`` is append-only, so every change entry names a locked id
    and the last entry of an id gives the hash that is locked for it.
    """
    atoms: dict[str, str] = lock["atoms"]
    problems: list[str] = []
    last: dict[str, str] = {}
    for index, change in enumerate(lock["changes"]):
        if change["id"] not in atoms:
            problems.append(
                f"{LOCK_FILE}: changes[{index}] names {change['id']}, which is not a locked id - ids are "
                "never removed from the lock (the lock was edited by hand)"
            )
        last[change["id"]] = change["new"]
    for requirement_id, digest in last.items():
        if requirement_id in atoms and atoms[requirement_id] != digest:
            problems.append(
                f"{LOCK_FILE}: the hash locked for {requirement_id} is not the 'new' hash of its last entry "
                f"in 'changes' - the lock was edited by hand (a correction is recorded with: "
                f"{_LOCK_COMMAND} --reword {requirement_id} --reason ...)"
            )
    return problems


def _deleted_problem(requirement_id: str) -> str:
    return (
        f"requirement {requirement_id}: locked in {LOCK_FILE} but missing from the requirements matrix - "
        "requirements are never deleted"
    )


def _reworded_problem(requirement_id: str) -> str:
    return (
        f"requirement {requirement_id}: text, source, section or source_clause changed since it was locked "
        f"- if this is a deliberate correction run: {_LOCK_COMMAND} --reword {requirement_id} --reason ..."
    )


def _check_lock(registers: Registers, info: _MatrixInfo, report: Report) -> None:
    """The requirement lock: nothing locked is missing or reworded, and everything is locked."""
    if LOCK_FILE in registers.unloaded:
        return
    if registers.lock_missing:
        if info.usable and registers.matrix["requirements"]:
            report.problem(
                f"{LOCK_FILE}: the requirement lock does not exist although the matrix holds "
                f"{len(registers.matrix['requirements'])} requirement(s) - run: {_LOCK_COMMAND}"
            )
        return
    shape = lock_shape_problems(registers.lock)
    for problem in shape:
        report.structural(problem)
    if shape:
        return
    report.problems.extend(lock_history_problems(registers.lock))
    if not info.usable:
        return
    atoms: dict[str, str] = registers.lock["atoms"]
    for requirement_id in atoms:
        if requirement_id not in info.all_ids:
            report.problem(_deleted_problem(requirement_id))
    for requirement_id, requirement in info.sound.items():
        if requirement_id not in atoms:
            report.problem(
                f"requirement {requirement_id}: not in {LOCK_FILE} (a new requirement is locked before it "
                f"is used) - run: {_LOCK_COMMAND}"
            )
        elif atoms[requirement_id] != atom_hash(requirement):
            report.problem(_reworded_problem(requirement_id))


def lock_requirements(
    root: Path = REPO_ROOT, *, reword: Sequence[str] = (), reason: str | None = None
) -> Report:
    """Lock the requirements that are not locked yet; with ``reword`` record deliberate corrections.

    Without ``reword`` the ids missing from the lock are added and nothing else changes; the lock
    file is created with every requirement when it does not exist.  With ``reword`` the hash of
    exactly the named ids is replaced as well, and one entry ``{id, old, new, reason}`` per id is
    appended to ``changes``: each named id must be in the matrix, be locked and really differ, and
    ``reason`` must say why.  Whenever a locked id is missing from the matrix, or a locked hash
    differs without being named, nothing is written.  No id is ever removed.
    """
    registers, report = _load_structurally_valid(root, "lock the requirements")
    if registers is None:
        return report
    if registers.lock_missing:
        lock: dict[str, Any] = {
            "schema_version": 1,
            "description": LOCK_DESCRIPTION,
            "atoms": {},
            "changes": [],
        }
    else:
        lock = copy.deepcopy(registers.lock)
        report.problems.extend(lock_history_problems(lock))
    atoms: dict[str, str] = lock["atoms"]
    current = {entry["id"]: atom_hash(entry) for entry in registers.matrix["requirements"]}

    named = list(reword)
    if named and (reason is None or not reason.strip()):
        report.problem("lock --reword needs --reason TEXT: say why the locked words are corrected")
    if not named and reason is not None:
        report.problem("lock --reason is only used together with --reword ID [ID ...]")
    for requirement_id in dict.fromkeys(named):
        if named.count(requirement_id) > 1:
            report.problem(f"lock --reword: {requirement_id} is named more than once")
        if requirement_id not in current:
            report.problem(f"lock --reword: {requirement_id!r} is not in the requirements matrix")
        elif requirement_id not in atoms:
            report.problem(
                f"lock --reword: {requirement_id} is not locked yet (a new requirement is locked without "
                "--reword)"
            )
        elif atoms[requirement_id] == current[requirement_id]:
            report.problem(
                f"lock --reword: {requirement_id} has not changed since it was locked; there is nothing to "
                "reword"
            )
    report.problems.extend(_deleted_problem(item) for item in atoms if item not in current)
    report.problems.extend(
        _reworded_problem(item)
        for item, digest in current.items()
        if item in atoms and atoms[item] != digest and item not in named
    )
    if report.problems:
        report.problems.insert(0, f"{LOCK_FILE}: refused, nothing was written")
        return report

    added = [requirement_id for requirement_id in current if requirement_id not in atoms]
    for requirement_id in added:
        atoms[requirement_id] = current[requirement_id]
    for requirement_id in named:
        lock["changes"].append(
            {
                "id": requirement_id,
                "old": atoms[requirement_id],
                "new": current[requirement_id],
                "reason": _collapse(reason or ""),
            }
        )
        atoms[requirement_id] = current[requirement_id]
    if not added and not named:
        report.messages.append(f"{LOCK_FILE}: unchanged ({len(atoms)} requirement(s) locked)")
        return report
    _write_text(root / LOCK_FILE, json.dumps(lock, indent=2, sort_keys=True) + "\n")
    state = "created" if registers.lock_missing else "written"
    report.messages.append(
        f"{LOCK_FILE}: {state} - {len(added)} requirement(s) added, {len(named)} reworded, "
        f"{len(atoms)} locked"
    )
    report.messages.extend(
        f"reworded {requirement_id}: {_collapse(reason or '')}" for requirement_id in named
    )
    return report


# --------------------------------------------------------------------------- acceptance checks
def _check_evidence_record(evidence: object, where: str, report: Report) -> bool:
    if not isinstance(evidence, dict):
        report.structural(f"{where}: 'evidence' must be a mapping written by sync-evidence")
        return False
    before = report.structural_count
    _check_keys(evidence, f"{where}: evidence", ("file", *_EVIDENCE_COUNT_KEYS), (), report)
    _require_text(evidence, "file", f"{where}: evidence", report)
    for key in _EVIDENCE_COUNT_KEYS:
        if key in evidence and (not _is_int(evidence[key]) or evidence[key] < 0):
            report.structural(f"{where}: evidence '{key}' must be a non-negative integer")
    if report.structural_count != before:
        return False
    if evidence["tests"] != evidence["passed"] + evidence["failed"] + evidence["skipped"]:
        report.structural(f"{where}: evidence counts are inconsistent (tests != passed + failed + skipped)")
        return False
    return True


@dataclass
class _AcceptanceState:
    """What is known while the entries of the acceptance test register are checked one by one."""

    matrix: _MatrixInfo
    status_values: tuple[str, ...] = ()
    ids: set[str] = field(default_factory=set)
    requirements: set[str] = field(default_factory=set)
    #: Entries without a schema or type error, in file order.
    sound: list[dict[str, Any]] = field(default_factory=list)


def _check_acceptance_entry(
    registers: Registers, entry: object, index: int, state: _AcceptanceState, report: Report
) -> None:
    info = state.matrix
    if not isinstance(entry, dict):
        report.structural(f"{ACCEPTANCE_FILE}: acceptance_tests[{index}] must be a mapping")
        return
    entry_id = entry.get("id")
    where = (
        f"acceptance test {entry_id}"
        if isinstance(entry_id, str)
        else f"{ACCEPTANCE_FILE}: acceptance_tests[{index}]"
    )
    before = report.structural_count
    _check_keys(entry, where, _ACCEPTANCE_ENTRY_KEYS, ("evidence",), report)
    for key in ("id", "requirement", "title", "verification"):
        _require_text(entry, key, where, report)
    if "session" in entry and entry["session"] not in SESSIONS:
        report.structural(f"{where}: 'session' must be one of S00..S12, not {entry['session']!r}")
    if "test_modules" in entry and not _is_str_list(entry["test_modules"]):
        report.structural(f"{where}: 'test_modules' must be a list of repository-relative paths")
    if "status" in entry and entry["status"] not in state.status_values:
        report.structural(
            f"{where}: status {entry['status']!r} is not one of {', '.join(state.status_values)}"
        )
    blockers = entry.get("blockers", [])
    if not _is_str_list(blockers) or any(not _REFERENCE_ID_RE.fullmatch(item) for item in blockers):
        report.structural(f"{where}: 'blockers' must be a list of ids (B-nnn, CR-nnn, D-nnn, U-nn)")
    if "notes" in entry and not isinstance(entry["notes"], str):
        report.structural(f"{where}: 'notes' must be a string")
    if "evidence" in entry:
        _check_evidence_record(entry["evidence"], where, report)
    if report.structural_count != before:
        return

    # From here on the entry has the right shape; the remaining rules are about its content.
    state.sound.append(entry)
    requirement_id: str = entry["requirement"]
    if entry["id"] in state.ids:
        report.problem(f"{where}: duplicate acceptance test id")
    state.ids.add(entry["id"])
    if requirement_id in state.requirements:
        report.problem(f"{where}: requirement {requirement_id} already has an acceptance test entry")
    state.requirements.add(requirement_id)
    if entry_id != f"AT-{requirement_id}":
        report.problem(f"{where}: id must be 'AT-{requirement_id}' (AT- followed by the requirement id)")
    if info.usable:
        requirement = info.sound.get(requirement_id)
        if requirement_id not in info.all_ids:
            report.problem(f"{where}: requirement {requirement_id} is not in the requirements matrix")
        elif requirement is not None and requirement["kind"] != "acceptance":
            report.problem(
                f"{where}: requirement {requirement_id} has kind '{requirement['kind']}', not 'acceptance'"
            )
        elif requirement is not None and requirement["sessions"] != [entry["session"]]:
            report.problem(
                f"{where}: session {entry['session']} differs from the sessions "
                f"[{', '.join(requirement['sessions'])}] of requirement {requirement_id}"
            )
    for reference in entry["blockers"]:
        _check_reference(reference, where, registers.logs, report)

    status: str = entry["status"]
    if status in ("blocked", "partial") and not entry["blockers"]:
        report.problem(f"{where}: status '{status}' needs at least one id in 'blockers'")
    evidence = entry.get("evidence")
    if evidence is None and status not in ("planned", "blocked"):
        # Without evidence an entry is 'planned', or 'blocked' by its blocker ids alone.
        report.problem(f"{where}: status '{status}' without recorded evidence (run sync-evidence)")
    if (
        status == "passing"
        and evidence is not None
        and (evidence["passed"] <= 0 or evidence["failed"] != 0 or evidence["skipped"] != 0)
    ):
        report.problem(
            f"{where}: status 'passing' contradicts the recorded evidence "
            f"({evidence['passed']} passed, {evidence['failed']} failed, {evidence['skipped']} skipped); "
            "passing needs passed > 0 and nothing failed or skipped"
        )
    if status == "planned":
        return
    if status in ("passing", "failing", "partial") and not entry["test_modules"]:
        report.problem(f"{where}: status '{status}' needs at least one path in 'test_modules'")
    for module in entry["test_modules"]:
        reason = _repo_path_problem(registers.root, module)
        path = registers.root / module
        if reason:
            report.problem(f"{where}: test module {module!r} {reason} (status is '{status}')")
        elif not path.is_file():
            report.problem(f"{where}: test module {module!r} is not a file")
        else:
            try:
                text = _read_text(path)
            except UnicodeDecodeError:
                report.problem(f"{where}: test module {module!r} is not UTF-8 text")
                continue
            if not _has_acceptance_marker(text, requirement_id):
                report.problem(
                    f"{where}: test module {module!r} does not contain the marker "
                    f'acceptance("{requirement_id}")'
                )


def _check_recorded_evidence(
    registers: Registers, entries: Sequence[Mapping[str, Any]], report: Report
) -> None:
    """The status and the counts recorded with an evidence file equal what that file gives.

    The evidence file named by an entry is read again and the entry is derived from it exactly as
    ``sync-evidence`` does (:func:`derive_entry`), so a status or a count typed by hand, and an
    evidence file that was replaced after the sync, are both noticed.  A file that cannot be used
    is reported once, with the entries that record it.
    """
    hint = "the register was edited by hand or the evidence file changed"
    users: dict[str, list[str]] = {}
    for entry in entries:
        if "evidence" in entry:
            users.setdefault(entry["evidence"]["file"], []).append(entry["id"])
    loaded: dict[str, Evidence] = {}
    for file, entry_ids in users.items():
        reason = _repo_path_problem(registers.root, file)
        if reason is None and not (registers.root / file).is_file():
            reason = "is not a file"
        if reason is None:
            try:
                evidence = load_evidence(registers.root / file, label=file)
            except RegisterError as exc:
                reason = f"cannot be used ({str(exc).removeprefix(file + ': ')})"
            else:
                refusal = evidence.refusal()
                if refusal is None:
                    loaded[file] = evidence
                else:
                    reason = f"is not usable evidence ({refusal})"
        if reason is not None:
            report.problem(
                f"evidence file {file!r} {reason}: it is recorded as the evidence of {_some(entry_ids)} "
                f"- {hint}"
            )
    for entry in entries:
        recorded = entry.get("evidence")
        if recorded is None or recorded["file"] not in loaded:
            continue
        file = recorded["file"]
        derived = derive_entry(entry, loaded[file].tests)
        where = f"acceptance test {entry['id']}"
        if derived.tests == 0:
            report.problem(
                f"{where}: records evidence from {file}, but that file holds no test marked "
                f'acceptance("{entry["requirement"]}") - {hint} (run sync-evidence)'
            )
        elif entry["status"] != derived.status or any(
            recorded[key] != getattr(derived, key) for key in _EVIDENCE_COUNT_KEYS
        ):
            report.problem(
                f"{where}: the register records status '{entry['status']}' with {recorded['tests']} test(s) "
                f"({recorded['passed']} passed, {recorded['failed']} failed, {recorded['skipped']} skipped), "
                f"but {file} gives status '{derived.status}' with {derived.tests} test(s) "
                f"({derived.passed} passed, {derived.failed} failed, {derived.skipped} skipped) - {hint} "
                "(run sync-evidence)"
            )


def _check_acceptance(registers: Registers, info: _MatrixInfo, report: Report) -> dict[str, str]:
    """Check the acceptance test register; returns the status of every sound entry by requirement id."""
    document = registers.acceptance
    if ACCEPTANCE_FILE in registers.unloaded:
        return {}
    if not isinstance(document, dict):
        report.structural(f"{ACCEPTANCE_FILE}: the top level must be a mapping")
        return {}
    _check_keys(document, ACCEPTANCE_FILE, _ACCEPTANCE_KEYS, (), report)
    if "schema_version" in document and not (
        _is_int(document["schema_version"]) and document["schema_version"] == 1
    ):
        report.structural(f"{ACCEPTANCE_FILE}: schema_version must be 1")
    for key in ("title", "description"):
        _require_text(document, key, ACCEPTANCE_FILE, report)
    state = _AcceptanceState(matrix=info)
    declared = document.get("status_values")
    if _is_str_list(declared) and declared and len(set(declared)) == len(declared):
        state.status_values = tuple(declared)
        for name in ACCEPTANCE_STATUSES:
            if name not in state.status_values:
                report.structural(
                    f"{ACCEPTANCE_FILE}: status_values must contain '{name}' (sync-evidence uses it)"
                )
    elif "status_values" in document:
        report.structural(f"{ACCEPTANCE_FILE}: status_values must be a non-empty list of distinct strings")
    entries = document.get("acceptance_tests")
    if not isinstance(entries, list):
        if "acceptance_tests" in document:
            report.structural(f"{ACCEPTANCE_FILE}: acceptance_tests must be a list")
        return {}
    for index, entry in enumerate(entries):
        _check_acceptance_entry(registers, entry, index, state, report)
    _check_recorded_evidence(registers, state.sound, report)
    if info.usable:
        for requirement_id, requirement in info.sound.items():
            if requirement["kind"] == "acceptance" and requirement_id not in state.requirements:
                report.problem(
                    f"requirement {requirement_id}: kind 'acceptance' but {ACCEPTANCE_FILE} has no entry "
                    f"AT-{requirement_id}"
                )
    statuses: dict[str, str] = {}
    for entry in state.sound:
        statuses.setdefault(entry["requirement"], entry["status"])
    return statuses


def _check_verified_acceptance(info: _MatrixInfo, statuses: Mapping[str, str], report: Report) -> None:
    """An acceptance requirement is 'verified' only while its acceptance test is 'passing'."""
    for requirement_id, requirement in info.sound.items():
        if "-ACC-" not in requirement_id or requirement["status"] != "verified":
            continue
        actual = statuses.get(requirement_id)
        if actual == "passing":
            continue
        state = (
            f"its status is '{actual}'"
            if actual is not None
            else f"{ACCEPTANCE_FILE} has no usable entry for it"
        )
        report.problem(
            f"requirement {requirement_id}: status 'verified' needs the acceptance test AT-{requirement_id} "
            f"with status 'passing' ({state})"
        )


def check_registers(registers: Registers, loaded: Report | None = None) -> Report:
    """Every rule of the registers, applied to loaded documents (nothing is read twice)."""
    report = Report()
    if loaded is not None:
        report.problems.extend(loaded.problems)
        report.notes.extend(loaded.notes)
        report.structural_count = loaded.structural_count
    report.problems.extend(registers.logs.problems)
    info = _check_matrix(registers, report)
    _check_lock(registers, info, report)
    acceptance_statuses = _check_acceptance(registers, info, report)
    _check_verified_acceptance(info, acceptance_statuses, report)
    _check_log_references(registers, info, report)
    return report


def validate(root: Path = REPO_ROOT) -> Report:
    """Check the registers below ``root``; ``report.problems`` is empty when they are valid."""
    registers, loaded = load_registers(root)
    report = check_registers(registers, loaded)
    for file, document, text in (
        (MATRIX_FILE, registers.matrix, registers.matrix_text),
        (ACCEPTANCE_FILE, registers.acceptance, registers.acceptance_text),
    ):
        if file not in registers.unloaded and isinstance(document, dict) and dump_yaml(document) != text:
            report.note(
                f"{file} is not in the formatting written by this tool (edited by hand?): the next "
                "'apply' / 'sync-evidence' rewrites the whole file and drops YAML comments"
            )
    if report.ok:
        report.messages.append(_summary(registers))
    return report


def _summary(registers: Registers) -> str:
    log_counts = ", ".join(f"{kind} {len(registers.logs.of_kind(kind))}" for kind in LOG_KINDS)
    return (
        f"registers valid: {len(registers.matrix['requirements'])} requirements, "
        f"{len(registers.acceptance['acceptance_tests'])} acceptance tests (log entries: {log_counts})"
    )


def _load_structurally_valid(root: Path, action: str) -> tuple[Registers | None, Report]:
    """Registers that are safe to render or update (else None) and the report of the command.

    Rendering and updating need registers of the right shape, not necessarily registers that pass
    every rule: ``sync-evidence`` for instance is how a 'passing' entry without evidence is repaired.
    """
    registers, loaded = load_registers(root)
    checked = check_registers(registers, loaded)
    report = Report()
    if not checked.structure_ok:
        report.structural_count = checked.structural_count
        report.problems.append(
            f"cannot {action}: the registers are not structurally valid (nothing was written)"
        )
        report.problems.extend(checked.problems)
        return None, report
    return registers, report


# --------------------------------------------------------------------------- rendering
def _cell(text: str) -> str:
    """Text for a Markdown table cell: pipes escaped, line breaks kept as ``<br>``.

    A pipe becomes ``\\|``.  Backslashes directly in front of a pipe are doubled first, so that they
    cannot swallow the escape and split the cell.
    """
    lines = [" ".join(line.split()) for line in text.strip().splitlines()]
    joined = "<br>".join(line for line in lines if line)
    return re.sub(r"(\\*)\|", lambda match: match.group(1) * 2 + "\\|", joined)


def _inline(text: str) -> str:
    return " ".join(text.split())


def _code_list(paths: Iterable[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths)


def _sessions_text(sessions: Sequence[str]) -> str:
    if set(sessions) == set(SESSIONS):
        return f"all ({SESSIONS[0]}-{SESSIONS[-1]})"
    return ", ".join(sessions)


def _notice(*sources: str) -> list[str]:
    return [
        f"> {GENERATED_NOTICE}. Rendered by `python -m tools.gie_registers render` from",
        f"> {', '.join(f'`{source}`' for source in sources)}.",
        "> Change the source and render again; `render --check` (run in CI) rejects a stale copy.",
        "",
    ]


def _count_table(header: str, unit: str, values: Sequence[str], counts: Mapping[str, int]) -> list[str]:
    lines = [f"| {header} | {unit} |", "|---|---:|"]
    lines += [f"| {value} | {counts.get(value, 0)} |" for value in values]
    lines += [f"| **total** | {sum(counts.values())} |", ""]
    return lines


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _contribution_text(contribution: Mapping[str, Any]) -> str:
    text = f"{contribution['session']} [{contribution['status']}]: {_inline(contribution['summary'])}"
    extras: list[str] = []
    if contribution["evidence"]:
        extras.append(f"evidence: {_code_list(contribution['evidence'])}")
    if contribution.get("blockers"):
        extras.append(f"blockers: {', '.join(contribution['blockers'])}")
    return f"{text} ({'; '.join(extras)})" if extras else text


def _review_text(requirement: Mapping[str, Any]) -> str:
    """Review status of a requirement, followed by its review reference when it has one."""
    reference = requirement.get("review_reference")
    return f"{requirement['review_status']} ({reference})" if reference else str(requirement["review_status"])


def _requirement_details(requirement: Mapping[str, Any]) -> list[str]:
    """Contributions, blockers and notes of a requirement, one item per line."""
    details = [_contribution_text(contribution) for contribution in requirement["contributions"]]
    if requirement.get("blockers"):
        details.append(f"Blockers: {', '.join(requirement['blockers'])}.")
    if requirement["notes"].strip():
        details.append(f"Note: {requirement['notes'].strip()}")
    return details


def render_matrix_md(matrix: Mapping[str, Any]) -> str:
    requirements: list[dict[str, Any]] = matrix["requirements"]
    status_values = list(matrix["status_values"])
    lines = [f"# {_inline(matrix['title'])}", "", *_notice(MATRIX_FILE)]
    lines += [f"Specification: {_inline(matrix['specification']['version'])}.", ""]
    lines += ["## Totals by status", ""]
    counts = _counts(requirement["status"] for requirement in requirements)
    lines += ["| status | requirements | meaning |", "|---|---:|---|"]
    lines += [
        f"| {status} | {counts.get(status, 0)} | {_cell(matrix['status_values'][status])} |"
        for status in status_values
    ]
    lines += [f"| **total** | {len(requirements)} | |", ""]
    lines += ["## Totals by review status", ""]
    lines += _count_table(
        "review status",
        "requirements",
        matrix["review_status_values"],
        _counts(requirement["review_status"] for requirement in requirements),
    )
    sections: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for requirement in requirements:
        sections.setdefault((requirement["source"], requirement["section"]), []).append(requirement)
    for (source, section), members in sections.items():
        lines += [f"## {_inline(source)} - {_inline(section)}", ""]
        lines += [
            "| id | requirement | sessions | status | review | contributions/notes |",
            "|---|---|---|---|---|---|",
        ]
        for requirement in members:
            details = "<br>".join(_cell(item) for item in _requirement_details(requirement))
            lines.append(
                f"| {requirement['id']} | {_cell(requirement['text'])} "
                f"| {_sessions_text(requirement['sessions'])} | {requirement['status']} "
                f"| {_review_text(requirement)} | {details} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_acceptance_md(document: Mapping[str, Any]) -> str:
    entries: list[dict[str, Any]] = document["acceptance_tests"]
    lines = [f"# {_inline(document['title'])}", "", *_notice(ACCEPTANCE_FILE)]
    lines += [_inline(document["description"]), ""]
    lines += ["## Totals by status", ""]
    lines += _count_table(
        "status", "acceptance tests", document["status_values"], _counts(entry["status"] for entry in entries)
    )
    for session in SESSIONS:
        members = [entry for entry in entries if entry["session"] == session]
        if not members:
            continue
        lines += [f"## {session}", ""]
        lines += [
            "| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |",
            "|---|---|---|---|---|---|",
        ]
        for entry in members:
            title = _cell(entry["title"])
            if entry["notes"].strip():
                title += f"<br>Note: {_cell(entry['notes'])}"
            evidence = entry.get("evidence")
            results = (
                f"{evidence['passed']} / {evidence['failed']} / {evidence['skipped']}"
                f"<br>`{_cell(evidence['file'])}`"
                if evidence
                else "-"
            )
            modules = "<br>".join(f"`{_cell(module)}`" for module in entry["test_modules"])
            lines.append(
                f"| {entry['id']} | {title} | {entry['status']} | {results} | {modules} "
                f"| {', '.join(entry['blockers'])} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def requirements_by_session(requirements: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Requirements of each session: the session is listed in 'sessions' or has a contribution."""
    result: dict[str, list[Mapping[str, Any]]] = {session: [] for session in SESSIONS}
    for requirement in requirements:
        involved = set(requirement["sessions"]) | {c["session"] for c in requirement["contributions"]}
        for session in SESSIONS:
            if session in involved:
                result[session].append(requirement)
    return result


def _log_table(entries: Sequence[LogEntry], *, kind_column: bool) -> list[str]:
    header = (
        "| id | type | status | title | requirements |"
        if kind_column
        else "| id | status | title | requirements |"
    )
    lines = [header, "|---|---|---|---|---|" if kind_column else "|---|---|---|---|"]
    for entry in entries:
        status = entry.status if entry.status_valid else f"INVALID ({entry.status or 'no status line'})"
        kind = f" {LOG_KINDS[entry.kind].name} |" if kind_column else ""
        lines.append(
            f"| {entry.id} |{kind} {_cell(status)} | {_cell(entry.title)} "
            f"| {_cell(', '.join(entry.requirements))} |"
        )
    lines.append("")
    return lines


def render_status_md(registers: Registers) -> str:
    matrix: dict[str, Any] = registers.matrix
    requirements: list[dict[str, Any]] = matrix["requirements"]
    entries: list[dict[str, Any]] = registers.acceptance["acceptance_tests"]
    status_values = list(matrix["status_values"])
    acceptance_values: list[str] = list(registers.acceptance["status_values"])
    logs = registers.logs
    lines = ["# GIE implementation status", ""]
    lines += _notice(MATRIX_FILE, ACCEPTANCE_FILE, DECISION_LOG_FILE, CHANGE_REQUESTS_FILE)

    lines += ["## Requirements by session", ""]
    lines += [
        "A requirement is counted for a session when the session is listed in its `sessions` or has recorded",
        "a contribution; the status is the overall status of the requirement. Requirements shared by several",
        "sessions appear in several rows; the last row counts every requirement once.",
        "",
    ]
    lines += [
        f"| session | requirements | {' | '.join(status_values)} |",
        "|---|---:|" + "---:|" * len(status_values),
    ]
    for session, members in requirements_by_session(requirements).items():
        counts = _counts(requirement["status"] for requirement in members)
        cells = " | ".join(str(counts.get(status, 0)) for status in status_values)
        lines.append(f"| {session} | {len(members)} | {cells} |")
    counts = _counts(requirement["status"] for requirement in requirements)
    cells = " | ".join(str(counts.get(status, 0)) for status in status_values)
    lines += [f"| **all** | {len(requirements)} | {cells} |", ""]

    lines += ["## Acceptance tests by session", ""]
    lines += [
        f"| session | acceptance tests | {' | '.join(acceptance_values)} |",
        "|---|---:|" + "---:|" * len(acceptance_values),
    ]
    for session in SESSIONS:
        members_at = [entry for entry in entries if entry["session"] == session]
        counts = _counts(entry["status"] for entry in members_at)
        cells = " | ".join(str(counts.get(status, 0)) for status in acceptance_values)
        lines.append(f"| {session} | {len(members_at)} | {cells} |")
    counts = _counts(entry["status"] for entry in entries)
    cells = " | ".join(str(counts.get(status, 0)) for status in acceptance_values)
    lines += [f"| **all** | {len(entries)} | {cells} |", ""]

    lines += ["## Requirements that are blocked, partial, in progress or implemented", ""]
    lines += [
        "Everything that is started but not verified, with what each session delivered and what blocks it.",
        "",
    ]
    for status in ATTENTION_STATUSES:
        members_req = [requirement for requirement in requirements if requirement["status"] == status]
        lines += [f"### {status} ({len(members_req)})", ""]
        if not members_req:
            lines += ["None.", ""]
            continue
        for requirement in members_req:
            lines.append(
                f"- **{requirement['id']}** ({_sessions_text(requirement['sessions'])}; review: "
                f"{_review_text(requirement)}) - {_inline(requirement['text'])}"
            )
            lines += [f"  - {_inline(item)}" for item in _requirement_details(requirement)]
        lines.append("")

    lines += ["## Open blockers, change requests and open items", ""]
    lines += [f"`{file}` does not exist." for file in logs.missing_files]
    if logs.missing_files:
        lines.append("")
    open_entries = [
        entry
        for entry in [*logs.of_kind("B"), *logs.of_kind("CR"), *logs.of_kind("U")]
        if not entry.status_valid or entry.is_open
    ]
    for kind in ("B", "CR", "U"):
        of_kind = logs.of_kind(kind)
        open_count = sum(1 for entry in of_kind if entry.is_open)
        lines.append(f"- {LOG_KINDS[kind].name}s: {open_count} open of {len(of_kind)}")
    lines.append("")
    lines += _log_table(open_entries, kind_column=True) if open_entries else ["None.", ""]

    lines += ["## Decisions", ""]
    decisions = logs.of_kind("D")
    for status in LOG_KINDS["D"].statuses:
        lines.append(
            f"- {status}: {sum(1 for entry in decisions if entry.status_valid and entry.status == status)}"
        )
    lines.append("")
    lines += _log_table(decisions, kind_column=False) if decisions else ["None.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_documents(registers: Registers) -> dict[str, str]:
    """The three generated Markdown files (repository-relative path -> content)."""
    return {
        MATRIX_MD: render_matrix_md(registers.matrix),
        ACCEPTANCE_MD: render_acceptance_md(registers.acceptance),
        STATUS_MD: render_status_md(registers),
    }


def render(root: Path = REPO_ROOT, *, check: bool = False) -> Report:
    """Write the generated Markdown files, or with ``check`` report the missing and stale ones."""
    registers, report = _load_structurally_valid(root, "render")
    if registers is None:
        return report
    for file, text in render_documents(registers).items():
        path = root / file
        current = path.read_bytes() if path.is_file() else None
        if current == text.encode("utf-8"):
            report.messages.append(f"{file}: up to date")
        elif check:
            state = "is missing" if current is None else "is stale (differs from the registers)"
            report.problem(
                f"{file}: {state}; run 'python -m tools.gie_registers render' and commit the result"
            )
        else:
            _write_text(path, text)
            report.messages.append(f"{file}: written")
    return report


# --------------------------------------------------------------------------- evidence
def derive_acceptance_status(passed: int, failed: int, skipped: int, has_blockers: bool) -> str:
    """Status of an acceptance test from the counts of its marked tests.

    ``failed`` includes errors, unexpected passes and expected failures; ``skipped`` includes
    tests that did not run.  'passing' is only ever returned when at least one test passed and
    none failed or was skipped.
    """
    if passed + failed + skipped == 0:
        return "blocked" if has_blockers else "planned"
    if failed > 0:
        return "failing"
    if skipped == 0:
        return "passing"
    return "partial" if passed > 0 else "blocked"


@dataclass(frozen=True)
class Evidence:
    """Content of a pytest evidence file written by ``tests/conftest.py`` (schema version 1)."""

    #: One mapping per collected test: nodeid, requirements, acceptance, outcome.
    tests: Sequence[Mapping[str, Any]]
    #: Exit status of the pytest run (None when the file does not record it).
    exit_status: int | None = None
    #: Node ids of the modules that could not be imported or collected.
    collection_errors: tuple[str, ...] = ()
    #: Commit the tests ran on and whether the working tree had uncommitted changes (None: unknown).
    commit: str | None = None
    tree_dirty: bool | None = None

    def refusal(self) -> str | None:
        """Why the run behind this file is no evidence, or None when the file can be used."""
        if self.exit_status not in COMPLETED_EXIT_STATUSES:
            found = "is missing" if self.exit_status is None else f"is {self.exit_status}"
            return f"'exit_status' {found}, not 0 or 1: the test run did not complete"
        if self.collection_errors:
            return (
                f"{len(self.collection_errors)} module(s) could not be collected; a test that could not "
                f"be imported is not a pass: {', '.join(self.collection_errors)}"
            )
        return None


def load_evidence(path: Path, label: str | None = None) -> Evidence:
    """Read a pytest evidence file; a file of another shape raises :class:`RegisterError`.

    ``exit_status`` and the keys ``collection_errors``, ``commit`` and ``tree_dirty`` are optional
    (older files do not have them) but must have the right type when present.  Whether the run
    can serve as evidence at all is a separate question, answered by :meth:`Evidence.refusal`.
    """
    label = str(path) if label is None else label
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        raise RegisterError(f"{label}: cannot read the evidence file ({exc.strerror or exc})") from exc
    except (ValueError, RecursionError) as exc:
        raise RegisterError(f"{label}: not a JSON evidence file ({exc})") from exc
    if (
        not isinstance(payload, dict)
        or not _is_int(payload.get("schema_version"))
        or payload["schema_version"] != 1
    ):
        raise RegisterError(f"{label}: not a pytest evidence file with schema_version 1")
    tests = payload.get("tests")
    if not isinstance(tests, list):
        raise RegisterError(f"{label}: 'tests' must be a list")
    outcomes = PASSED_OUTCOMES | FAILED_OUTCOMES | SKIPPED_OUTCOMES
    seen: set[str] = set()
    for index, test in enumerate(tests):
        where = f"{label}: tests[{index}]"
        if not isinstance(test, dict) or not _is_text(test.get("nodeid")):
            raise RegisterError(f"{where} must be a mapping with a 'nodeid'")
        if test["nodeid"] in seen:
            raise RegisterError(f"{where}: nodeid {test['nodeid']!r} occurs twice")
        seen.add(test["nodeid"])
        for key in ("requirements", "acceptance"):
            if not _is_str_list(test.get(key)):
                raise RegisterError(f"{where}: '{key}' must be a list of ids")
        if test.get("outcome") not in outcomes:
            raise RegisterError(f"{where}: unknown outcome {test.get('outcome')!r}")
    exit_status = payload.get("exit_status")
    if exit_status is not None and not _is_int(exit_status):
        raise RegisterError(f"{label}: 'exit_status' must be an integer")
    collection_errors = payload.get("collection_errors", [])
    if not _is_str_list(collection_errors):
        raise RegisterError(f"{label}: 'collection_errors' must be a list of node ids")
    commit = payload.get("commit")
    if commit is not None and not isinstance(commit, str):
        raise RegisterError(f"{label}: 'commit' must be a string or null")
    tree_dirty = payload.get("tree_dirty")
    if tree_dirty is not None and not isinstance(tree_dirty, bool):
        raise RegisterError(f"{label}: 'tree_dirty' must be true, false or null")
    return Evidence(
        tests=tests,
        exit_status=exit_status,
        collection_errors=tuple(collection_errors),
        commit=commit,
        tree_dirty=tree_dirty,
    )


@dataclass(frozen=True)
class DerivedEntry:
    """What an evidence file says about one acceptance test."""

    #: Tests whose ``acceptance`` marker names the requirement of the entry.
    marked: tuple[Mapping[str, Any], ...]
    tests: int
    passed: int
    failed: int
    skipped: int
    #: Marked tests that were collected but did not run (they are part of ``skipped``).
    not_run: int
    status: str

    def counts(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in _EVIDENCE_COUNT_KEYS}


def derive_entry(entry: Mapping[str, Any], tests: Sequence[Mapping[str, Any]]) -> DerivedEntry:
    """Counts and status of an acceptance test entry according to the tests of an evidence file."""
    marked = tuple(test for test in tests if entry["requirement"] in test["acceptance"])
    passed = sum(1 for test in marked if test["outcome"] in PASSED_OUTCOMES)
    failed = sum(1 for test in marked if test["outcome"] in FAILED_OUTCOMES)
    skipped = sum(1 for test in marked if test["outcome"] in SKIPPED_OUTCOMES)
    return DerivedEntry(
        marked=marked,
        tests=len(marked),
        passed=passed,
        failed=failed,
        skipped=skipped,
        not_run=sum(1 for test in marked if test["outcome"] == "not_run"),
        status=derive_acceptance_status(passed, failed, skipped, bool(entry["blockers"])),
    )


def _insert_key(mapping: dict[str, Any], key: str, value: Any, *, after: str = "", before: str = "") -> None:
    """Set ``mapping[key]`` keeping the key order of the file: in place, or next to the anchor key."""
    if key in mapping:
        mapping[key] = value
        return
    items = list(mapping.items())
    mapping.clear()
    placed = False
    for existing_key, existing_value in items:
        if existing_key == before and not placed:
            mapping[key] = value
            placed = True
        mapping[existing_key] = existing_value
        if existing_key == after and not placed:
            mapping[key] = value
            placed = True
    if not placed:
        mapping[key] = value


def _unknown_marker_ids(tests: Sequence[Mapping[str, Any]], registers: Registers, report: Report) -> None:
    known = {entry["id"] for entry in registers.matrix["requirements"]}
    registered = {entry["requirement"] for entry in registers.acceptance["acceptance_tests"]}
    users: dict[tuple[str, str], list[str]] = {}
    for test in tests:
        for key in ("requirements", "acceptance"):
            for marker_id in test[key]:
                if marker_id not in known:
                    users.setdefault((key, marker_id), []).append(test["nodeid"])
                elif key == "acceptance" and marker_id not in registered:
                    users.setdefault(("unregistered", marker_id), []).append(test["nodeid"])
    for (key, marker_id), nodeids in sorted(users.items()):
        example = f"{len(nodeids)} test(s), e.g. {nodeids[0]}"
        if key == "unregistered":
            report.problem(
                f"evidence: acceptance id {marker_id!r} used in a test marker has no entry AT-{marker_id} in "
                f"the acceptance test register ({example})"
            )
        else:
            name = "requirement id" if key == "requirements" else "acceptance id"
            report.problem(
                f"evidence: {name} {marker_id!r} used in a test marker is not in the requirements matrix "
                f"({example})"
            )


def _evidence_text(status: str, counts: Mapping[str, int] | None) -> str:
    if counts is None:
        return f"status '{status}' without recorded evidence"
    return (
        f"status '{status}' with {counts['tests']} test(s) ({counts['passed']} passed, "
        f"{counts['failed']} failed, {counts['skipped']} skipped)"
    )


def sync_evidence(root: Path, evidence: Path, *, check: bool = False) -> Report:
    """Update the acceptance test register from a pytest evidence file.

    For every entry the tests whose ``acceptance`` marker names its requirement are counted and
    the status is derived with :func:`derive_acceptance_status`.  Entries with marked tests get an
    ``evidence`` mapping ``{file, tests, passed, failed, skipped}``; entries without marked tests
    carry none (a stale one is removed, so 'passing' can never survive without evidence).
    Unknown marker ids, tests that did not run, a 'partial'/'blocked' result without a blocker id
    and a listed test module that contributed no executed test are reported as problems; the
    register is still updated, because the counts are facts.

    Nothing is written for a malformed or misplaced evidence file, for a run that did not complete
    (``exit_status`` other than 0 or 1) and for a run with collection errors.

    With ``check`` nothing is ever written and the evidence file may be outside the repository:
    every entry whose status or counts differ from what would be written is reported, in addition
    to the problems above.  Only the recorded path ``evidence.file`` is not compared.
    """
    registers, report = _load_structurally_valid(root, "check the evidence" if check else "sync evidence")
    if registers is None:
        return report
    relative = ""
    try:
        relative = evidence.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        if not check:
            report.problem(
                f"{evidence}: the evidence file must be inside the repository ({root}) because the register "
                "records its repository-relative path (nothing was written)"
            )
            return report
    try:
        loaded = load_evidence(evidence)
    except RegisterError as exc:
        report.problem(f"{exc} (nothing was written)")
        return report
    refusal = loaded.refusal()
    if refusal is not None:
        report.problem(f"{evidence}: {refusal} (nothing was written)")
        return report
    tests = loaded.tests
    _unknown_marker_ids(tests, registers, report)

    document = copy.deepcopy(registers.acceptance)
    differing = 0
    for entry in document["acceptance_tests"]:
        derived = derive_entry(entry, tests)
        status = derived.status
        recorded = entry.get("evidence")
        recorded_counts = None if recorded is None else {key: recorded[key] for key in _EVIDENCE_COUNT_KEYS}
        derived_counts = derived.counts() if derived.marked else None
        if check and (entry["status"], recorded_counts) != (status, derived_counts):
            differing += 1
            report.problem(
                f"{entry['id']}: the register records {_evidence_text(entry['status'], recorded_counts)}, "
                f"but {evidence} gives {_evidence_text(status, derived_counts)}"
            )
        if derived.marked:
            _insert_key(entry, "evidence", {"file": relative, **derived.counts()}, after="status")
        else:
            entry.pop("evidence", None)
        if status != entry["status"]:
            if not check:
                report.messages.append(
                    f"{entry['id']}: {entry['status']} -> {status} "
                    f"({derived.passed} passed, {derived.failed} failed, {derived.skipped} skipped)"
                )
            entry["status"] = status
        if derived.not_run:
            report.problem(
                f"{entry['id']}: {derived.not_run} marked test(s) did not run; the evidence is incomplete "
                "(they are counted as skipped, never as passed)"
            )
        if derived.marked and status in ("partial", "blocked") and not entry["blockers"]:
            report.problem(
                f"{entry['id']}: status '{status}' ({derived.passed} passed, {derived.skipped} skipped) "
                f"needs a blocker id - record why the tests are skipped in {CHANGE_REQUESTS_FILE} and add "
                "the id to 'blockers'"
            )
        found_in = {test["nodeid"].split("::")[0] for test in derived.marked}
        if derived.marked:
            executed_in = {
                test["nodeid"].split("::")[0]
                for test in derived.marked
                if test["outcome"] in EXECUTED_OUTCOMES
            }
            for module in entry["test_modules"]:
                if module not in executed_in:
                    report.problem(
                        f"{entry['id']}: listed test module contributed no test: {module} (no test marked "
                        f'acceptance("{entry["requirement"]}") from it was executed; a module that lost '
                        "its tests must not go unnoticed)"
                    )
        modules = sorted(found_in - set(entry["test_modules"]))
        if modules:
            report.note(
                f"{entry['id']}: marked tests found in {', '.join(modules)}, not listed in 'test_modules'"
            )

    if check:
        if differing:
            report.problems.insert(
                0,
                f"{ACCEPTANCE_FILE}: {differing} entr{'y differs' if differing == 1 else 'ies differ'} from "
                f"what sync-evidence would write for {evidence} (nothing was written)",
            )
        else:
            report.messages.append(f"{ACCEPTANCE_FILE}: equals what sync-evidence would write for {evidence}")
        return report
    if document == registers.acceptance:
        report.messages.append(f"{ACCEPTANCE_FILE}: unchanged")
        return report
    if dump_yaml(registers.acceptance) != registers.acceptance_text:
        report.note(f"{ACCEPTANCE_FILE} was not in the tool's formatting; it has been normalised")
    _write_text(root / ACCEPTANCE_FILE, dump_yaml(document))
    report.messages.append(f"{ACCEPTANCE_FILE}: written")
    return report


# --------------------------------------------------------------------------- patches
def _is_glob(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def _check_update(
    registers: Registers, update: object, where: str, info: _MatrixInfo, report: Report
) -> list[str]:
    """Validate one update of a patch; returns the requirement ids it selects (in matrix order)."""
    if not isinstance(update, dict):
        report.problem(f"{where}: an update must be a mapping")
        return []
    for key in update:
        if key != "ids" and key not in _PATCH_CHANGE_KEYS:
            report.problem(f"{where}: unknown key {key!r} (allowed: ids, {', '.join(_PATCH_CHANGE_KEYS)})")
    if not any(key in update for key in _PATCH_CHANGE_KEYS):
        report.problem(f"{where}: nothing to change (give at least one of {', '.join(_PATCH_CHANGE_KEYS)})")
    if "status" in update and update["status"] not in info.status_values:
        report.problem(f"{where}: status {update['status']!r} is not one of {', '.join(info.status_values)}")
    if "review_status" in update and update["review_status"] not in info.review_values:
        report.problem(
            f"{where}: review_status {update['review_status']!r} is not one of "
            f"{', '.join(info.review_values)}"
        )
    if "review_reference" in update and not _is_decision_id(update["review_reference"]):
        report.problem(f"{where}: 'review_reference' must be the id of a decision (D-nnn)")
    if "notes" in update and not isinstance(update["notes"], str):
        report.problem(f"{where}: 'notes' must be a string")
    if "blockers" in update and (
        not _is_str_list(update["blockers"])
        or any(not _REFERENCE_ID_RE.fullmatch(i) for i in update["blockers"])
    ):
        report.problem(f"{where}: 'blockers' must be a list of ids (B-nnn, CR-nnn, D-nnn, U-nn)")
    if "contribution" in update:
        scratch = Report()
        _check_contribution(
            registers.root, update["contribution"], f"{where}: contribution", info.status_values, scratch
        )
        report.problems.extend(scratch.problems)
    ids = update.get("ids")
    if not _is_str_list(ids) or not ids or any(not item.strip() for item in ids):
        report.problem(f"{where}: 'ids' must be a non-empty list of requirement ids or glob patterns")
        return []
    order = [entry["id"] for entry in registers.matrix["requirements"]]
    selected: set[str] = set()
    for pattern in ids:
        if _is_glob(pattern):
            matches = [entry_id for entry_id in order if fnmatch.fnmatchcase(entry_id, pattern)]
            if not matches:
                report.problem(f"{where}: pattern {pattern!r} matches no requirement")
            selected.update(matches)
        elif pattern in info.all_ids:
            selected.add(pattern)
        else:
            report.problem(f"{where}: unknown requirement id {pattern!r}")
    return [entry_id for entry_id in order if entry_id in selected]


def _apply_update(requirement: dict[str, Any], update: Mapping[str, Any]) -> None:
    for key in ("status", "review_status", "notes"):
        if key in update:
            requirement[key] = update[key]
    if "review_reference" in update:
        _insert_key(requirement, "review_reference", update["review_reference"], after="review_status")
    if "blockers" in update:
        _insert_key(requirement, "blockers", list(update["blockers"]), before="notes")
    if "contribution" in update:
        source = update["contribution"]
        contribution: dict[str, Any] = {key: copy.deepcopy(source[key]) for key in _CONTRIBUTION_KEYS}
        if "blockers" in source:
            contribution["blockers"] = list(source["blockers"])
        existing: list[dict[str, Any]] = requirement["contributions"]
        for index, other in enumerate(existing):
            if other["session"] == contribution["session"]:
                existing[index] = contribution
                break
        else:
            existing.append(contribution)


def apply_patch(root: Path, patch: Path) -> Report:
    """Apply status updates to the requirements matrix; all or nothing.

    Patch format (YAML)::

        updates:
          - ids: [S00-ASG-27, 'S00-DEL-*']      # ids or shell-style globs
            status: verified                     # optional
            review_status: approved              # optional; other than 'unverified' needs a reference
            review_reference: D-031              # optional, the APPROVED decision recording the review
            notes: text                          # optional, replaces the notes
            blockers: [B-001]                    # optional, requirement-level blocker ids
            contribution:                        # optional, replaces the contribution of that session
              session: S00
              summary: what was delivered
              status: verified
              evidence: [docs/DECISION_LOG.md]
              blockers: [B-001]                  # optional

    Updates are applied in order, so a later update overrides an earlier one.  An unknown id, a
    pattern that matches nothing, a malformed update, or a result that :func:`check_registers`
    would reject is an error and nothing is written.  Requirements are never added or deleted,
    the locked fields (id, source, section, text, source_clause) cannot be patched, and the
    requirement lock is never touched.
    """
    registers, report = _load_structurally_valid(root, "apply the patch")
    if registers is None:
        return report
    try:
        document = parse_yaml(_read_text(patch), str(patch))
    except (OSError, UnicodeDecodeError) as exc:
        report.problem(f"{patch}: cannot read the patch ({exc}) (nothing was written)")
        return report
    except RegisterError as exc:
        report.problem(f"{exc} (nothing was written)")
        return report
    if (
        not isinstance(document, dict)
        or list(document) != ["updates"]
        or not isinstance(document["updates"], list)
        or not document["updates"]
    ):
        report.problem(
            f"{patch}: a patch is a mapping with the single key 'updates' holding a non-empty list"
        )
        return report

    info = _check_matrix(registers, Report())
    selections: list[tuple[list[str], dict[str, Any]]] = []
    for index, update in enumerate(document["updates"]):
        selections.append(
            (_check_update(registers, update, f"{patch}: updates[{index}]", info, report), update)
        )
    if report.problems:
        report.problems.insert(0, f"{patch}: the patch was refused, nothing was written")
        return report

    matrix = copy.deepcopy(registers.matrix)
    by_id: dict[str, dict[str, Any]] = {entry["id"]: entry for entry in matrix["requirements"]}
    for selected, update in selections:
        for requirement_id in selected:
            _apply_update(by_id[requirement_id], update)
    changed = [
        old["id"]
        for old, new in zip(registers.matrix["requirements"], matrix["requirements"], strict=True)
        if old != new
    ]

    result = check_registers(replace(registers, matrix=matrix))
    before_ids = [entry["id"] for entry in registers.matrix["requirements"]]
    if [entry["id"] for entry in matrix["requirements"]] != before_ids:
        result.problem("internal error: the patch changed the list of requirements")
    report.notes.extend(result.notes)
    if result.problems:
        report.problem(f"{patch}: refused, nothing was written - the patched registers would not validate:")
        report.problems.extend(result.problems)
        return report

    if not changed:
        report.messages.append(f"{MATRIX_FILE}: unchanged (the patch describes the current state)")
        return report
    if dump_yaml(registers.matrix) != registers.matrix_text:
        report.note(f"{MATRIX_FILE} was not in the tool's formatting; it has been normalised")
    _write_text(root / MATRIX_FILE, dump_yaml(matrix))
    report.messages.append(f"{MATRIX_FILE}: {len(changed)} requirement(s) updated: {', '.join(changed)}")
    return report


# --------------------------------------------------------------------------- stats
def stats(root: Path = REPO_ROOT) -> dict[str, Any]:
    """Totals of the registers; raises :class:`RegisterError` when they cannot be read."""
    registers, report = _load_structurally_valid(root, "compute totals")
    if registers is None:
        raise RegisterError("\n".join(report.problems))
    requirements: list[dict[str, Any]] = registers.matrix["requirements"]
    entries: list[dict[str, Any]] = registers.acceptance["acceptance_tests"]
    status_values = list(registers.matrix["status_values"])
    acceptance_values: list[str] = list(registers.acceptance["status_values"])

    def by_status(members: Iterable[Mapping[str, Any]], values: Sequence[str]) -> dict[str, int]:
        counts = _counts(member["status"] for member in members)
        return {value: counts.get(value, 0) for value in values}

    review = _counts(requirement["review_status"] for requirement in requirements)
    return {
        "requirements": {
            "total": len(requirements),
            "by_status": by_status(requirements, status_values),
            "by_review_status": {
                value: review.get(value, 0) for value in registers.matrix["review_status_values"]
            },
            "by_session": {
                session: {"total": len(members), "by_status": by_status(members, status_values)}
                for session, members in requirements_by_session(requirements).items()
            },
        },
        "acceptance": {
            "total": len(entries),
            "by_status": by_status(entries, acceptance_values),
            "by_session": {
                session: {
                    "total": sum(1 for entry in entries if entry["session"] == session),
                    "by_status": by_status(
                        [entry for entry in entries if entry["session"] == session], acceptance_values
                    ),
                }
                for session in SESSIONS
            },
        },
    }


# --------------------------------------------------------------------------- command line
_PATCH_HELP = """\
Patch file (YAML). Updates are applied in order; any error means nothing is written.

  updates:
    - ids: [S00-ASG-27, 'S00-DEL-*']    # requirement ids or shell-style globs
      status: verified                   # optional
      review_status: approved            # optional; other than 'unverified' needs a review_reference
      review_reference: D-031            # optional, id of the APPROVED decision that records the review
      notes: replaces the notes          # optional
      blockers: [B-001]                  # optional, requirement-level blocker ids
      contribution:                      # optional, replaces the contribution of the same session
        session: S00
        summary: what the session delivered
        status: verified
        evidence: [docs/DECISION_LOG.md]
        blockers: [B-001]                # optional

A requirement is 'verified' only with a 'verified' contribution of every session in its 'sessions',
each listing at least one existing evidence path; an acceptance requirement (-ACC-) additionally needs
its acceptance test to be 'passing'. The requirement lock is never changed by a patch.
"""

_LOCK_HELP = f"""\
Maintain the requirement lock ({LOCK_FILE}): for every requirement id the SHA-256 of its id,
source, section, text and source_clause. 'validate' fails when a locked requirement is missing from
the matrix or when one of these fields changed.

  lock                                  add the requirements that are not locked yet (creates the file)
  lock --reword ID [ID ...] --reason TEXT
                                        additionally record a deliberate correction of the named ids:
                                        their hash is replaced and one entry per id is appended to 'changes'

Nothing is written while a locked id is missing from the matrix or a locked hash differs without being
named by --reword. Each named id must be in the matrix, be locked and really differ. Ids are never
removed and 'changes' is append-only.
"""


def _make_output_robust() -> None:
    """Never crash on a console that cannot encode a character (for example cp1252 on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def _emit(report: Report) -> int:
    for line in report.messages:
        print(line)
    for line in report.lines():
        print(line)
    if report.problems:
        print(f"{len(report.problems)} problem(s)")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.gie_registers",
        description="Validate, render, update and lock the persistent GIE project registers.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("validate", parents=[common], help="check the registers; exit 1 on any problem")
    render_parser = commands.add_parser("render", parents=[common], help="write the generated Markdown files")
    render_parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if a generated file is missing or stale"
    )
    sync_parser = commands.add_parser(
        "sync-evidence",
        parents=[common],
        help="update the acceptance test register from pytest evidence",
        description="Derive the status of every acceptance test from a pytest evidence file. The file is "
        "refused when its exit_status is not 0 or 1 (the run did not complete) or when it lists "
        "collection_errors; a listed test module that contributed no executed test is reported.",
    )
    sync_parser.add_argument(
        "--evidence", type=Path, required=True, help="file written by pytest --gie-evidence"
    )
    sync_parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 unless the register already holds exactly what this evidence file "
        "gives (the file may be outside the repository, e.g. fresh CI evidence)",
    )
    apply_parser = commands.add_parser(
        "apply",
        parents=[common],
        help="apply status updates from a YAML patch",
        description=_PATCH_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply_parser.add_argument("--patch", type=Path, required=True, help="YAML patch file")
    lock_parser = commands.add_parser(
        "lock",
        parents=[common],
        help="lock new requirements; record a deliberate rewording with --reword",
        description=_LOCK_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lock_parser.add_argument(
        "--reword",
        nargs="+",
        metavar="ID",
        help="requirement ids whose locked words were corrected on purpose (needs --reason)",
    )
    lock_parser.add_argument("--reason", metavar="TEXT", help="why the named requirements were corrected")
    commands.add_parser("stats", parents=[common], help="print totals as JSON")
    arguments = parser.parse_args(argv)
    _make_output_robust()

    root: Path = arguments.root
    if arguments.command == "validate":
        return _emit(validate(root))
    if arguments.command == "render":
        return _emit(render(root, check=arguments.check))
    if arguments.command == "sync-evidence":
        return _emit(sync_evidence(root, arguments.evidence, check=arguments.check))
    if arguments.command == "apply":
        return _emit(apply_patch(root, arguments.patch))
    if arguments.command == "lock":
        if arguments.reword is not None and not (arguments.reason or "").strip():
            lock_parser.error("--reword needs --reason TEXT (why the locked words are corrected)")
        if arguments.reword is None and arguments.reason is not None:
            lock_parser.error("--reason is only used together with --reword ID [ID ...]")
        return _emit(lock_requirements(root, reword=arguments.reword or (), reason=arguments.reason))
    try:
        print(json.dumps(stats(root), indent=2))
    except RegisterError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
