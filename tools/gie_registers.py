"""Persistent project registers: validate, render, sync test evidence, apply status updates.

The project keeps four registers for its whole multi-session life (requirements OWN-08, OWN-16,
OWN-17, OWN-18, S00-ASG-27, S00-ASG-28):

* ``specifications/requirements_matrix.yaml`` - every requirement with its status (source of truth);
* ``specifications/acceptance_tests.yaml``    - the acceptance test register;
* ``docs/DECISION_LOG.md``    - decisions ``## D-001 - Title`` and open items ``## U-01 - Title``;
* ``docs/CHANGE_REQUESTS.md`` - blockers ``## B-001 - Title`` and change requests ``## CR-001 - Title``.

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
``sync-evidence --evidence FILE``
    Derive the status of every acceptance test from a pytest evidence file written by
    ``pytest --gie-evidence=FILE`` (see ``tests/conftest.py``) and record the counts.
``apply --patch FILE``
    Apply status updates to the requirements matrix from a YAML patch; all or nothing.
``stats``
    Print totals as JSON.

Exit status: 0 success, 1 problems were reported, 2 command line error.

Rules worth knowing (the complete list is the code of :func:`check_registers`):

* a requirement is never deleted: the count may not drop below ``minimum_requirement_count``;
* the SHA-256 of every extracted specification text must equal the recorded value - a changed
  specification is noticed and the matrix must be re-verified against it;
* ``verified`` needs a contribution with an existing evidence path; ``blocked`` needs a blocker id
  that exists as a heading in the Markdown logs;
* an acceptance test is never ``passing`` without recorded evidence (``passed > 0``, nothing failed,
  nothing skipped), and an expected failure (``xfailed``) is not a pass.

Nothing here reads a clock, the network or the environment: the same registers always give the
same output.
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
EXTRACTED_DIR = "specifications/extracted"
DECISION_LOG_FILE = "docs/DECISION_LOG.md"
CHANGE_REQUESTS_FILE = "docs/CHANGE_REQUESTS.md"

MATRIX_MD = "specifications/REQUIREMENTS_MATRIX.md"
ACCEPTANCE_MD = "docs/ACCEPTANCE_TEST_REGISTER.md"
STATUS_MD = "docs/IMPLEMENTATION_STATUS.md"
GENERATED_FILES = (MATRIX_MD, ACCEPTANCE_MD, STATUS_MD)
GENERATED_NOTICE = "GENERATED - do not edit"

SESSIONS = tuple(f"S{number:02d}" for number in range(13))
REQUIREMENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z]+)?-[0-9]{2,3}$")

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
_PATCH_CHANGE_KEYS = ("status", "review_status", "notes", "contribution", "blockers")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOCKER_ID_RE = re.compile(r"^(B|CR)-[0-9]{3,}$")
_REFERENCE_ID_RE = re.compile(r"^(?:(B|CR|D)-[0-9]{3,}|(U)-[0-9]{2,})$")
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
    this tool show up as small diffs.
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
            problems.append(f"{where} has no '- Status:' line (allowed for a {spec.name}: {allowed})")
        elif len(statuses) > 1:
            problems.append(f"{where} has {len(statuses)} '- Status:' lines; exactly one is required")
        elif statuses[0] not in spec.statuses:
            problems.append(
                f"{where} has status {statuses[0]!r}; allowed for a {spec.name}: {allowed} "
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
                problems.append(
                    f"{file}:{entry.line}: {entry.id} is a {spec.name}; {spec.name}s belong in {spec.file}"
                )
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
    #: Parsed documents, or None when the file is missing or unreadable.
    matrix: Any
    acceptance: Any
    logs: Logs
    #: File contents as read (None when missing), to detect whether an update changes anything.
    matrix_text: str | None = None
    acceptance_text: str | None = None


def _load_document(root: Path, file: str, report: Report) -> tuple[Any, str | None]:
    path = root / file
    if not path.is_file():
        report.structural(f"{file}: the register file does not exist")
        return None, None
    try:
        text = _read_text(path)
    except UnicodeDecodeError as exc:
        report.structural(f"{file}: not UTF-8 text ({exc.reason})")
        return None, None
    try:
        return parse_yaml(text, file), text
    except RegisterError as exc:
        report.structural(str(exc))
        return None, text


def load_registers(root: Path) -> tuple[Registers, Report]:
    """Load the registers below ``root``; the report holds what could not be loaded."""
    report = Report()
    matrix, matrix_text = _load_document(root, MATRIX_FILE, report)
    acceptance, acceptance_text = _load_document(root, ACCEPTANCE_FILE, report)
    registers = Registers(
        root=root,
        matrix=matrix,
        acceptance=acceptance,
        logs=load_logs(root),
        matrix_text=matrix_text,
        acceptance_text=acceptance_text,
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
@dataclass
class _MatrixInfo:
    """What the cross-register checks need to know about the matrix."""

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
    if not _is_str_list(blockers) or any(not _BLOCKER_ID_RE.match(item) for item in blockers):
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
    _check_keys(entry, where, _REQUIREMENT_KEYS, ("blockers",), report)
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
    blockers = entry.get("blockers", [])
    if not _is_str_list(blockers) or any(not _REFERENCE_ID_RE.match(item) for item in blockers):
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
    references = [*blockers, *(item for c in contributions for item in c.get("blockers", []))]
    for reference in references:
        _check_reference(reference, where, registers.logs, report)
    if entry["status"] == "verified" and not any(
        _repo_path_problem(registers.root, path) is None for c in contributions for path in c["evidence"]
    ):
        report.problem(
            f"{where}: status 'verified' needs at least one contribution with an existing evidence path"
        )
    if entry["status"] == "blocked" and not references:
        report.problem(
            f"{where}: status 'blocked' needs a blocker id (B-nnn / CR-nnn) in a contribution "
            "or in a 'blockers' key of the requirement"
        )


def _check_reference(reference: str, where: str, logs: Logs, report: Report) -> None:
    """A referenced B-/CR-/D-/U- id must exist as a heading in the Markdown logs."""
    match = _REFERENCE_ID_RE.match(reference)
    if match is None:
        return
    if reference in logs.by_id():
        return
    kind = LOG_KINDS[match[1] or match[2]]
    if kind.file in logs.missing_files:
        report.problem(f"{where}: references {reference} but {kind.file} does not exist")
    else:
        report.problem(f"{where}: references {reference}, which is not a heading in {kind.file}")


def _check_specification(root: Path, specification: object, report: Report) -> None:
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
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
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
    info = _MatrixInfo()
    matrix = registers.matrix
    if matrix is None:
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
        _check_specification(registers.root, matrix["specification"], report)

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
    if not _is_str_list(blockers) or any(not _REFERENCE_ID_RE.match(item) for item in blockers):
        report.structural(f"{where}: 'blockers' must be a list of ids (B-nnn, CR-nnn, D-nnn, U-nn)")
    if "notes" in entry and not isinstance(entry["notes"], str):
        report.structural(f"{where}: 'notes' must be a string")
    if "evidence" in entry:
        _check_evidence_record(entry["evidence"], where, report)
    if report.structural_count != before:
        return

    # From here on the entry has the right shape; the remaining rules are about its content.
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
    if status == "passing":
        evidence = entry.get("evidence")
        if evidence is None:
            report.problem(f"{where}: status 'passing' without recorded evidence (run sync-evidence)")
        elif evidence["passed"] <= 0 or evidence["failed"] != 0 or evidence["skipped"] != 0:
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


def _check_acceptance(registers: Registers, info: _MatrixInfo, report: Report) -> None:
    document = registers.acceptance
    if document is None:
        return
    if not isinstance(document, dict):
        report.structural(f"{ACCEPTANCE_FILE}: the top level must be a mapping")
        return
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
        return
    for index, entry in enumerate(entries):
        _check_acceptance_entry(registers, entry, index, state, report)
    if info.usable:
        for requirement_id, requirement in info.sound.items():
            if requirement["kind"] == "acceptance" and requirement_id not in state.requirements:
                report.problem(
                    f"requirement {requirement_id}: kind 'acceptance' but {ACCEPTANCE_FILE} has no entry "
                    f"AT-{requirement_id}"
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
    _check_acceptance(registers, info, report)
    _check_log_references(registers, info, report)
    return report


def validate(root: Path = REPO_ROOT) -> Report:
    """Check the registers below ``root``; ``report.problems`` is empty when they are valid."""
    registers, loaded = load_registers(root)
    report = check_registers(registers, loaded)
    if report.ok:
        report.messages.append(_summary(registers))
    return report


def _summary(registers: Registers) -> str:
    return (
        f"registers valid: {len(registers.matrix['requirements'])} requirements, "
        f"{len(registers.acceptance['acceptance_tests'])} acceptance tests, "
        f"{len(registers.logs.of_kind('D'))} decisions, {len(registers.logs.of_kind('U'))} open items, "
        f"{len(registers.logs.of_kind('B'))} blockers, {len(registers.logs.of_kind('CR'))} change requests"
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
            "| id | requirement | sessions | status | review | contributions / notes |",
            "|---|---|---|---|---|---|",
        ]
        for requirement in members:
            details = "<br>".join(_cell(item) for item in _requirement_details(requirement))
            lines.append(
                f"| {requirement['id']} | {_cell(requirement['text'])} "
                f"| {_sessions_text(requirement['sessions'])} | {requirement['status']} "
                f"| {requirement['review_status']} | {details} |"
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
                f"{requirement['review_status']}) - {_inline(requirement['text'])}"
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


def load_evidence(path: Path) -> list[dict[str, Any]]:
    """Tests of a pytest evidence file written by ``tests/conftest.py`` (schema version 1)."""
    label = str(path)
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        raise RegisterError(f"{label}: cannot read the evidence file ({exc.strerror or exc})") from exc
    except ValueError as exc:
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
    return tests


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


def sync_evidence(root: Path, evidence: Path) -> Report:
    """Update the acceptance test register from a pytest evidence file.

    For every entry the tests whose ``acceptance`` marker names its requirement are counted and
    the status is derived with :func:`derive_acceptance_status`.  Entries with marked tests get an
    ``evidence`` mapping ``{file, tests, passed, failed, skipped}``; entries without marked tests
    carry none (a stale one is removed, so 'passing' can never survive without evidence).
    Unknown marker ids, tests that did not run and a 'partial'/'blocked' result without a blocker
    id are reported as problems; the register is still updated, because the counts are facts.
    A malformed or misplaced evidence file changes nothing.
    """
    registers, report = _load_structurally_valid(root, "sync evidence")
    if registers is None:
        return report
    try:
        relative = evidence.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        report.problem(
            f"{evidence}: the evidence file must be inside the repository ({root}) because the register "
            "records its repository-relative path (nothing was written)"
        )
        return report
    try:
        tests = load_evidence(evidence)
    except RegisterError as exc:
        report.problem(f"{exc} (nothing was written)")
        return report
    _unknown_marker_ids(tests, registers, report)

    document = copy.deepcopy(registers.acceptance)
    for entry in document["acceptance_tests"]:
        marked = [test for test in tests if entry["requirement"] in test["acceptance"]]
        passed = sum(1 for test in marked if test["outcome"] in PASSED_OUTCOMES)
        failed = sum(1 for test in marked if test["outcome"] in FAILED_OUTCOMES)
        skipped = sum(1 for test in marked if test["outcome"] in SKIPPED_OUTCOMES)
        not_run = sum(1 for test in marked if test["outcome"] == "not_run")
        status = derive_acceptance_status(passed, failed, skipped, bool(entry["blockers"]))
        if marked:
            counts = {
                "file": relative,
                "tests": len(marked),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            }
            _insert_key(entry, "evidence", counts, after="status")
        else:
            entry.pop("evidence", None)
        if status != entry["status"]:
            report.messages.append(
                f"{entry['id']}: {entry['status']} -> {status} "
                f"({passed} passed, {failed} failed, {skipped} skipped)"
            )
            entry["status"] = status
        if not_run:
            report.problem(
                f"{entry['id']}: {not_run} marked test(s) did not run; the evidence is incomplete "
                "(they are counted as skipped, never as passed)"
            )
        if marked and status in ("partial", "blocked") and not entry["blockers"]:
            report.problem(
                f"{entry['id']}: status '{status}' ({passed} passed, {skipped} skipped) needs a blocker id - "
                f"record why the tests are skipped in {CHANGE_REQUESTS_FILE} and add the id to 'blockers'"
            )
        modules = sorted({test["nodeid"].split("::")[0] for test in marked} - set(entry["test_modules"]))
        if modules:
            report.note(
                f"{entry['id']}: marked tests found in {', '.join(modules)}, not listed in 'test_modules'"
            )

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
    if "notes" in update and not isinstance(update["notes"], str):
        report.problem(f"{where}: 'notes' must be a string")
    if "blockers" in update and (
        not _is_str_list(update["blockers"]) or any(not _REFERENCE_ID_RE.match(i) for i in update["blockers"])
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
            review_status: unverified            # optional
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
    would reject is an error and nothing is written.  Requirements are never added or deleted.
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
    changed: list[str] = []
    for selected, update in selections:
        for requirement_id in selected:
            before = copy.deepcopy(by_id[requirement_id])
            _apply_update(by_id[requirement_id], update)
            if by_id[requirement_id] != before and requirement_id not in changed:
                changed.append(requirement_id)

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
        description="Validate, render and update the persistent GIE project registers.",
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
        "sync-evidence", parents=[common], help="update the acceptance test register from pytest evidence"
    )
    sync_parser.add_argument(
        "--evidence", type=Path, required=True, help="file written by pytest --gie-evidence"
    )
    apply_parser = commands.add_parser(
        "apply", parents=[common], help="apply status updates from a YAML patch"
    )
    apply_parser.add_argument("--patch", type=Path, required=True, help="YAML patch file")
    commands.add_parser("stats", parents=[common], help="print totals as JSON")
    arguments = parser.parse_args(argv)

    root: Path = arguments.root
    if arguments.command == "validate":
        return _emit(validate(root))
    if arguments.command == "render":
        return _emit(render(root, check=arguments.check))
    if arguments.command == "sync-evidence":
        return _emit(sync_evidence(root, arguments.evidence))
    if arguments.command == "apply":
        return _emit(apply_patch(root, arguments.patch))
    try:
        print(json.dumps(stats(root), indent=2))
    except RegisterError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
