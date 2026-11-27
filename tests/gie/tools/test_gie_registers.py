"""Tests for ``tools/gie_registers.py`` - the persistent project registers.

Requirements: OWN-08 / S00-ASG-28 (requirements matrix), OWN-16 (implementation status),
OWN-17 / S00-ASG-27 (decision log), OWN-18 (acceptance test register).

The expected behaviour is taken from the documented rules of the registers (module docstring of
the tool, ``CLAUDE.md``, the descriptions inside the two YAML registers and decision D-015), not
from the implementation:

* a requirement is never deleted, a changed specification text is noticed, ``verified`` needs
  existing evidence, ``blocked`` needs a blocker id that exists in the Markdown logs;
* an acceptance test is never ``passing`` without recorded evidence and an expected failure is
  not a pass;
* the generated Markdown files are deterministic and ``render --check`` notices a stale copy;
* updates written by the tool keep the formatting of the YAML files, so diffs stay small.

SYNTHETIC FIXTURE: every requirement, acceptance entry, decision, blocker, test module, patch and
evidence file created in this module is invented for testing and lives below pytest's ``tmp_path``.
Nothing here is project evidence.  The real registers of the repository are only read, or copied
to ``tmp_path`` before anything is written.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools import gie_registers as gr

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Deterministic hypothesis runs: no random seed, no example database, no wall-clock deadline.
_PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=300)

ALL_SESSIONS = [f"S{number:02d}" for number in range(13)]

#: SYNTHETIC FIXTURE status vocabulary (same names as the real matrix, invented meanings).
STATUS_VALUES = {
    "not_started": "SYNTHETIC FIXTURE: nothing yet.",
    "in_progress": "SYNTHETIC FIXTURE: started.",
    "partial": "SYNTHETIC FIXTURE: part delivered.",
    "implemented": "SYNTHETIC FIXTURE: exists, evidence incomplete.",
    "verified": "SYNTHETIC FIXTURE: backed by evidence.",
    "blocked": "SYNTHETIC FIXTURE: cannot proceed | see blocker.",
    "not_applicable": "SYNTHETIC FIXTURE: nothing to implement.",
}

#: SYNTHETIC FIXTURE extracted specification texts (file name -> content).
SPECIFICATION_TEXTS = {
    "FIXTURE_BRIEF.txt": "SYNTHETIC FIXTURE: text of an invented session brief.\n",
    "FIXTURE_MASTER.txt": "SYNTHETIC FIXTURE: text of an invented master specification.\n",
}

#: SYNTHETIC FIXTURE decision log.
DECISION_LOG = """\
# Synthetic fixture decision log

SYNTHETIC FIXTURE. The format of the headings and of the `- Status:` lines is parsed by the tool.

---

## D-001 - Use widgets

- Status: APPROVED
- Date: 2026-11-27
- Requirements: FIX-GOV-01, FIX-ASG-01
- Decided by: nobody (synthetic fixture)

Invented decision text.

## D-002 - Gadgets | later

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: FIX-ASG-02

# Open items

## U-01 - Which colour?

- Status: OPEN
- Requirements: FIX-ASG-04

## U-02 - Answered question

- Status: RESOLVED
"""

#: SYNTHETIC FIXTURE blockers and change requests; ``{blocker_status}`` is the status of B-001.
CHANGE_REQUESTS = """\
# Synthetic fixture blockers and change requests

SYNTHETIC FIXTURE.

# Blockers

## B-001 - Gadget supplier unreachable

- Status: {blocker_status}
- Raised: 2026-11-27 (synthetic fixture)
- Requirements: FIX-ASG-03, FIX-ACC-02

Invented blocker text.

## B-002 - Old blocker

- Status: RESOLVED
- Requirements: FIX-ASG-01

# Change requests

## CR-001 - Different layout

- Status: OPEN
- Requirements: FIX-GOV-01

## CR-002 - Approved change

- Status: APPROVED
"""

#: SYNTHETIC FIXTURE test module that carries the acceptance marker of FIX-ACC-01 (never collected).
WIDGET_TESTS = """\
import pytest


@pytest.mark.requirement("FIX-ASG-01")
@pytest.mark.acceptance("FIX-ACC-01")
def test_widget_round_trip():
    assert True
"""


@pytest.fixture(autouse=True)
def _every_test_names_its_requirements(request: pytest.FixtureRequest) -> None:
    """Traceability guard: a test of this module without requirement ids is an error."""
    marker = request.node.get_closest_marker("requirement")
    assert marker is not None, "every test must carry @pytest.mark.requirement(...)"
    assert marker.args, "the requirement marker must name at least one id"


# --------------------------------------------------------------------------- fixture builders
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _requirement(
    req_id: str, kind: str, sessions: list[str], text: str, *, section: str = "Your assignment"
) -> dict[str, Any]:
    return {
        "id": req_id,
        "source": "FIXTURE_BRIEF.pdf",
        "section": section,
        "kind": kind,
        "text": text,
        "source_clause": f"SYNTHETIC FIXTURE clause of {req_id}: {text}",
        "sessions": list(sessions),
        "status": "not_started",
        "review_status": "unverified",
        "contributions": [],
        "notes": "",
    }


def _matrix_document() -> dict[str, Any]:
    governance = _requirement(
        "FIX-GOV-01", "governance", ALL_SESSIONS, "Keep the registers.", section="G01 | Governance"
    )
    governance["source"] = "FIXTURE_MASTER.pdf"
    governance["notes"] = "SYNTHETIC FIXTURE note with a | pipe."
    return {
        "schema_version": 1,
        "title": "Synthetic fixture requirements matrix",
        "description": "SYNTHETIC FIXTURE: invented requirements for the register tool tests.",
        "conventions": "SYNTHETIC FIXTURE: ids are stable and never deleted.",
        "specification": {
            "version": "FIXTURE-1",
            "note": "SYNTHETIC FIXTURE.",
            "extracted_text_sha256": {
                name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in SPECIFICATION_TEXTS.items()
            },
        },
        "minimum_requirement_count": 7,
        "status_values": dict(STATUS_VALUES),
        "review_status_values": ["unverified", "approved", "conditional", "rejected"],
        "requirements": [
            governance,
            _requirement("FIX-ASG-01", "assignment", ["S00"], "Build the widget."),
            _requirement("FIX-ASG-02", "assignment", ["S00", "S01"], "Build the gadget."),
            _requirement("FIX-ASG-03", "assignment", ["S01"], "Connect the supplier."),
            _requirement(
                "FIX-ACC-01", "acceptance", ["S00"], "Widget round trip.", section="Acceptance tests"
            ),
            _requirement(
                "FIX-ACC-02", "acceptance", ["S01"], "Supplier handshake.", section="Acceptance tests"
            ),
            # An atom appended later: same section as FIX-ASG-01..03, after the acceptance section.
            _requirement("FIX-ASG-04", "assignment", ["S02"], "Paint the widget."),
        ],
    }


def _acceptance_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": "Synthetic fixture acceptance test register",
        "description": "SYNTHETIC FIXTURE: invented acceptance tests.",
        "status_values": ["planned", "passing", "failing", "blocked", "partial"],
        "acceptance_tests": [
            {
                "id": "AT-FIX-ACC-01",
                "requirement": "FIX-ACC-01",
                "session": "S00",
                "title": "Widget round trip",
                "verification": 'pytest tests marked @pytest.mark.acceptance("FIX-ACC-01")',
                "test_modules": ["tests/test_widget.py"],
                "status": "planned",
                "blockers": [],
                "notes": "",
            },
            {
                "id": "AT-FIX-ACC-02",
                "requirement": "FIX-ACC-02",
                "session": "S01",
                "title": "Supplier handshake",
                "verification": 'pytest tests marked @pytest.mark.acceptance("FIX-ACC-02")',
                "test_modules": [],
                "status": "planned",
                "blockers": [],
                "notes": "SYNTHETIC FIXTURE: an evaluation.",
            },
        ],
    }


def make_repo(tmp_path: Path, *, blocker_status: str = "OPEN") -> Path:
    """A small, valid SYNTHETIC FIXTURE repository below ``tmp_path`` (all statuses not_started)."""
    root = tmp_path / "repo"
    for name, text in SPECIFICATION_TEXTS.items():
        _write(root / gr.EXTRACTED_DIR / name, text)
    _write(root / gr.MATRIX_FILE, gr.dump_yaml(_matrix_document()))
    _write(root / gr.ACCEPTANCE_FILE, gr.dump_yaml(_acceptance_document()))
    _write(root / gr.DECISION_LOG_FILE, DECISION_LOG)
    _write(root / gr.CHANGE_REQUESTS_FILE, CHANGE_REQUESTS.format(blocker_status=blocker_status))
    _write(root / "tests/test_widget.py", WIDGET_TESTS)
    _write(root / "docs/widget.md", "SYNTHETIC FIXTURE: an invented evidence document.\n")
    return root


def _drop(container: Any, key: Any) -> None:
    """Remove a key (or a list index); returns None so that ``edit`` keeps the mutated document."""
    container.pop(key)


def load(root: Path, file: str) -> Any:
    return yaml.safe_load((root / file).read_text(encoding="utf-8"))


def edit(root: Path, file: str, change: Callable[[Any], Any]) -> None:
    """Rewrite a YAML register after ``change``; a returned value replaces the whole document."""
    document = load(root, file)
    replacement = change(document)
    _write(root / file, gr.dump_yaml(document if replacement is None else replacement))


def edit_requirement(root: Path, req_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    def apply(document: dict[str, Any]) -> None:
        change(next(entry for entry in document["requirements"] if entry["id"] == req_id))

    edit(root, gr.MATRIX_FILE, apply)


def edit_acceptance(root: Path, entry_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    def apply(document: dict[str, Any]) -> None:
        change(next(entry for entry in document["acceptance_tests"] if entry["id"] == entry_id))

    edit(root, gr.ACCEPTANCE_FILE, apply)


def requirement_of(root: Path, req_id: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = load(root, gr.MATRIX_FILE)["requirements"]
    return next(entry for entry in entries if entry["id"] == req_id)


def acceptance_of(root: Path, entry_id: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = load(root, gr.ACCEPTANCE_FILE)["acceptance_tests"]
    return next(entry for entry in entries if entry["id"] == entry_id)


def contribution(
    session: str, status: str, evidence: list[str], blockers: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "session": session,
        "summary": f"SYNTHETIC FIXTURE: what {session} delivered.",
        "status": status,
        "evidence": evidence,
    }
    if blockers is not None:
        result["blockers"] = blockers
    return result


def start(root: Path) -> None:
    """Give the SYNTHETIC FIXTURE requirements consistent statuses (the registers stay valid)."""

    def change(document: dict[str, Any]) -> None:
        by_id = {entry["id"]: entry for entry in document["requirements"]}
        by_id["FIX-GOV-01"]["status"] = "implemented"
        by_id["FIX-GOV-01"]["contributions"] = [contribution("S00", "implemented", ["docs/widget.md"])]
        by_id["FIX-ASG-01"]["status"] = "verified"
        by_id["FIX-ASG-01"]["contributions"] = [contribution("S00", "verified", ["docs/widget.md"])]
        by_id["FIX-ASG-02"]["status"] = "in_progress"
        by_id["FIX-ASG-03"]["status"] = "blocked"
        by_id["FIX-ASG-03"]["contributions"] = [contribution("S01", "blocked", [], ["B-001"])]
        # Requirement-level blocker id, placed before 'notes' like the tool does.
        blocked = by_id["FIX-ACC-02"]
        notes = blocked.pop("notes")
        blocked.update(status="blocked", blockers=["B-001"], notes=notes)
        # S00 is not listed in the sessions of FIX-ASG-04 but contributed to it.
        by_id["FIX-ASG-04"]["status"] = "partial"
        by_id["FIX-ASG-04"]["contributions"] = [contribution("S00", "partial", ["docs/widget.md"])]

    edit(root, gr.MATRIX_FILE, change)


def problems(root: Path) -> list[str]:
    return gr.validate(root).problems


def only_problem(root: Path) -> str:
    found = problems(root)
    assert len(found) == 1, found
    return found[0]


def snapshot(root: Path) -> dict[str, bytes]:
    """Content of every file below ``root`` (to prove that a command wrote nothing, or what it wrote)."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def blocks(text: str) -> tuple[str, dict[str, str]]:
    """Split a register file into its header and the text block of every ``- id:`` entry."""
    parts = re.split(r"(?m)^(?=- id: )", text)
    entries: dict[str, str] = {}
    for part in parts[1:]:
        entry_id = part.splitlines()[0].removeprefix("- id: ")
        assert entry_id not in entries
        entries[entry_id] = part
    return parts[0], entries


def result_record(
    nodeid: str, outcome: str, *, acceptance: tuple[str, ...] = (), requirements: tuple[str, ...] = ()
) -> dict[str, Any]:
    """One SYNTHETIC FIXTURE test result in the format written by tests/conftest.py."""
    return {
        "nodeid": nodeid,
        "requirements": list(requirements),
        "acceptance": list(acceptance),
        "outcome": outcome,
        "duration_s": 0.0,
        "detail": "",
    }


def write_evidence(root: Path, tests: list[dict[str, Any]], name: str = "evidence/pytest.json") -> Path:
    """A SYNTHETIC FIXTURE pytest evidence file inside the fixture repository."""
    payload = {"schema_version": 1, "generated_by": "SYNTHETIC FIXTURE", "tests": tests}
    path = root / name
    _write(path, json.dumps(payload, indent=2) + "\n")
    return path


def widget_results(*outcomes: str) -> list[dict[str, Any]]:
    return [
        result_record(f"tests/test_widget.py::test_{index}", outcome, acceptance=("FIX-ACC-01",))
        for index, outcome in enumerate(outcomes)
    ]


def write_patch(root: Path, updates: Any) -> Path:
    """A SYNTHETIC FIXTURE patch file next to (not inside) the fixture repository."""
    path = root.parent / "patch.yaml"
    _write(path, yaml.safe_dump({"updates": updates}, sort_keys=False))
    return path


def section_of(markdown: str, heading: str) -> str:
    """Text of the level-2 section whose heading starts with ``heading``."""
    match = re.search(rf"(?ms)^## {re.escape(heading)}.*?(?=^## |\Z)", markdown)
    assert match is not None, f"no section '## {heading}'"
    return match.group(0)


def copy_real_registers(tmp_path: Path) -> Path:
    """Copy the real registers, logs and every existing file they reference to ``tmp_path``."""
    root = tmp_path / "real"
    for file in (gr.MATRIX_FILE, gr.ACCEPTANCE_FILE, gr.DECISION_LOG_FILE, gr.CHANGE_REQUESTS_FILE):
        if (REPO_ROOT / file).is_file():
            (root / file).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / file, root / file)
    shutil.copytree(REPO_ROOT / gr.EXTRACTED_DIR, root / gr.EXTRACTED_DIR)
    referenced: set[str] = set()
    for entry in load(root, gr.ACCEPTANCE_FILE)["acceptance_tests"]:
        referenced.update(entry["test_modules"])
    for entry in load(root, gr.MATRIX_FILE)["requirements"]:
        for item in entry["contributions"]:
            referenced.update(item["evidence"])
    for relative in sorted(referenced):
        source, target = REPO_ROOT / relative, root / relative
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        elif source.is_dir() and not target.exists():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    return root


# --------------------------------------------------------------------------- YAML conventions
@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize("file", [gr.MATRIX_FILE, gr.ACCEPTANCE_FILE])
def test_real_register_round_trips_byte_identical(tmp_path: Path, file: str) -> None:
    target = tmp_path / Path(file).name
    shutil.copyfile(REPO_ROOT / file, target)
    original = target.read_bytes()
    document = gr.parse_yaml(original.decode("utf-8"), file)
    assert gr.dump_yaml(document).encode("utf-8") == original
    assert target.read_bytes() == original


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_dump_follows_the_formatting_conventions() -> None:
    shared = ["docs/a.md", "docs/b.md"]
    document = {
        "zeta": "kept first although it sorts last",
        "sessions": ["S00", "S01"],
        "blockers": ["B-001", "CR-002"],
        "evidence": shared,
        "again": shared,
        "unicode": "Prague - Praha, cs: \u010desky",
        "long": "word " * 40,
        "empty": [],
    }
    text = gr.dump_yaml(document)
    lines = text.splitlines()
    assert lines[0] == "zeta: kept first although it sorts last"
    assert "sessions: [S00, S01]" in lines
    assert "blockers: [B-001, CR-002]" in lines
    assert "evidence:\n- docs/a.md\n- docs/b.md\n" in text
    assert "again:\n- docs/a.md\n- docs/b.md\n" in text
    assert "&id" not in text
    assert "*id" not in text
    assert "\u010desky" in text
    # PyYAML wraps at the first space after column 110, so a line exceeds the width by at most one word.
    assert max(len(line) for line in lines) <= 110 + len("word")
    assert sum(1 for line in lines if line.startswith("  word")) >= 1, "the long scalar must be wrapped"
    assert "empty: []" in lines
    assert yaml.safe_load(text) == document


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "requirements:\n- id: X-01\n  status: verified\n  status: blocked\n",
        "a: [unclosed\n",
        "when: 2026-13-45\n",
    ],
)
def test_parse_rejects_duplicate_keys_and_invalid_yaml(text: str) -> None:
    with pytest.raises(gr.RegisterError, match=r"^fixture\.yaml: not valid YAML"):
        gr.parse_yaml(text, "fixture.yaml")


# --------------------------------------------------------------------------- validate: baseline
@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-17", "OWN-18", "S00-ASG-27", "S00-ASG-28")
def test_synthetic_fixture_is_valid_before_and_after_statuses_are_set(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    report = gr.validate(root)
    assert report.problems == []
    assert report.ok
    assert report.structure_ok
    assert len(report.notes) == 1, "the OPEN blocker is announced while no status is set"
    start(root)
    started = gr.validate(root)
    assert started.problems == []
    assert started.notes == []
    assert changed_files(before, snapshot(root)) == {gr.MATRIX_FILE}, "validate itself writes nothing"


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize("file", [gr.MATRIX_FILE, gr.ACCEPTANCE_FILE])
def test_hand_formatted_register_is_announced_but_valid(tmp_path: Path, file: str) -> None:
    root = make_repo(tmp_path)
    path = root / file
    canonical = path.read_text(encoding="utf-8")
    _write(path, "# SYNTHETIC FIXTURE: a comment typed by hand\n" + canonical)
    report = gr.validate(root)
    assert report.problems == []
    formatting_notes = [note for note in report.notes if "formatting" in note]
    assert len(formatting_notes) == 1
    assert formatting_notes[0].startswith(file)
    assert "drops YAML comments" in formatting_notes[0]
    _write(path, canonical)
    assert [note for note in gr.validate(root).notes if "formatting" in note] == []


# --------------------------------------------------------------------------- validate: specification
@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_changed_specification_text_is_noticed(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    path = root / gr.EXTRACTED_DIR / "FIXTURE_BRIEF.txt"
    recorded = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(path.read_bytes() + b"One more invented sentence.\n")
    problem = only_problem(root)
    assert "FIXTURE_BRIEF.txt" in problem
    assert recorded in problem
    assert hashlib.sha256(path.read_bytes()).hexdigest() in problem
    assert "FIXTURE_MASTER.txt" not in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_single_changed_byte_of_the_specification_is_noticed(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    path = root / gr.EXTRACTED_DIR / "FIXTURE_MASTER.txt"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert "FIXTURE_MASTER.txt" in only_problem(root)


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_missing_specification_text_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / gr.EXTRACTED_DIR / "FIXTURE_BRIEF.txt").unlink()
    problem = only_problem(root)
    assert "FIXTURE_BRIEF.txt" in problem
    assert "does not exist" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_specification_text_without_recorded_hash_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _write(root / gr.EXTRACTED_DIR / "FIXTURE_NEW_BRIEF.txt", "SYNTHETIC FIXTURE: a brief nobody recorded.\n")
    problem = only_problem(root)
    assert "FIXTURE_NEW_BRIEF.txt" in problem
    assert "without a recorded hash" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize("clause", ["", "   ", "\n\t\n"])
def test_empty_source_clause_is_a_problem(tmp_path: Path, clause: str) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(source_clause=clause))
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "source_clause" in problem


# --------------------------------------------------------------------------- validate: never delete
@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_deleting_a_requirement_is_noticed(tmp_path: Path) -> None:
    root = make_repo(tmp_path)

    def delete(document: dict[str, Any]) -> None:
        document["requirements"] = [e for e in document["requirements"] if e["id"] != "FIX-ASG-04"]

    edit(root, gr.MATRIX_FILE, delete)
    found = problems(root)
    assert any(
        "minimum_requirement_count is 7" in problem and "6 requirements" in problem for problem in found
    )
    # U-01 names the deleted requirement: the dangling reference is reported as well.
    assert any("U-01" in problem and "FIX-ASG-04" in problem for problem in found)
    assert len(found) == 2, found


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_minimum_requirement_count_is_optional_and_a_lower_bound(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit(root, gr.MATRIX_FILE, lambda document: _drop(document, "minimum_requirement_count"))
    assert problems(root) == []
    edit(root, gr.MATRIX_FILE, lambda document: document.update(minimum_requirement_count=3))
    assert problems(root) == [], "more requirements than the minimum is fine"
    edit(root, gr.MATRIX_FILE, lambda document: document.update(minimum_requirement_count=8))
    assert "minimum_requirement_count is 8" in only_problem(root)


# --------------------------------------------------------------------------- validate: schema of the matrix
def _first(document: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = document["requirements"][1]
    assert entry["id"] == "FIX-ASG-01"
    return entry


def _with_contribution(document: dict[str, Any], **changes: Any) -> None:
    item = contribution("S00", "in_progress", ["docs/widget.md"])
    for key, value in changes.items():
        if value is _DROP:
            del item[key]
        else:
            item[key] = value
    _first(document)["contributions"] = [item]


_DROP = object()

MATRIX_SCHEMA_DEFECTS: list[tuple[str, Callable[[dict[str, Any]], Any], str]] = [
    ("top level is a list", lambda d: ["not", "a", "mapping"], "top level must be a mapping"),
    ("missing top-level key", lambda d: _drop(d, "conventions"), "missing key 'conventions'"),
    ("unknown top-level key", lambda d: d.update(requirments=[]), "unknown key 'requirments'"),
    ("schema version 2", lambda d: d.update(schema_version=2), "schema_version must be 1"),
    ("schema version true", lambda d: d.update(schema_version=True), "schema_version must be 1"),
    ("empty title", lambda d: d.update(title=" "), "'title' must be a non-empty string"),
    (
        "description is a list",
        lambda d: d.update(description=["x"]),
        "'description' must be a non-empty string",
    ),
    ("specification is text", lambda d: d.update(specification="V1"), "specification must be a mapping"),
    (
        "specification without version",
        lambda d: _drop(d["specification"], "version"),
        "specification: missing key 'version'",
    ),
    (
        "no recorded hashes",
        lambda d: d["specification"].update(extracted_text_sha256={}),
        "'extracted_text_sha256' must be a non-empty mapping",
    ),
    (
        "hash is not a digest",
        lambda d: d["specification"]["extracted_text_sha256"].update({"FIXTURE_BRIEF.txt": "abc123"}),
        "not a lower-case SHA-256 hex digest",
    ),
    (
        "hash names a path",
        lambda d: d["specification"]["extracted_text_sha256"].update({"../secret.txt": "0" * 64}),
        "is not a plain file name",
    ),
    (
        "status values as a list",
        lambda d: d.update(status_values=list(STATUS_VALUES)),
        "status_values must be",
    ),
    (
        "status vocabulary without verified",
        lambda d: _drop(d["status_values"], "verified"),
        "status_values must define 'verified'",
    ),
    ("no review status values", lambda d: d.update(review_status_values=[]), "review_status_values must be"),
    ("requirements is a mapping", lambda d: d.update(requirements={}), "requirements must be a list"),
    (
        "minimum count is text",
        lambda d: d.update(minimum_requirement_count="7"),
        "minimum_requirement_count must be a non-negative integer",
    ),
    (
        "minimum count is true",
        lambda d: d.update(minimum_requirement_count=True),
        "minimum_requirement_count must be a non-negative integer",
    ),
    (
        "minimum count is negative",
        lambda d: d.update(minimum_requirement_count=-1),
        "minimum_requirement_count must be a non-negative integer",
    ),
    (
        "requirement is a string",
        lambda d: d["requirements"].__setitem__(1, "FIX-ASG-01"),
        "requirements[1] must be a mapping",
    ),
    ("requirement without kind", lambda d: _drop(_first(d), "kind"), "FIX-ASG-01: missing key 'kind'"),
    ("requirement with a typo key", lambda d: _first(d).update(staus="verified"), "unknown key 'staus'"),
    ("id is a number", lambda d: _first(d).update(id=7), "'id' must be a string"),
    ("empty text", lambda d: _first(d).update(text=""), "'text' must be a non-empty string"),
    ("section is a number", lambda d: _first(d).update(section=3), "'section' must be a non-empty string"),
    (
        "source clause is null",
        lambda d: _first(d).update(source_clause=None),
        "'source_clause' must be a string",
    ),
    ("notes is null", lambda d: _first(d).update(notes=None), "'notes' must be a string"),
    ("status outside the list", lambda d: _first(d).update(status="done"), "status 'done' is not one of"),
    (
        "review status outside the list",
        lambda d: _first(d).update(review_status="ok"),
        "review_status 'ok' is not one of",
    ),
    (
        "contributions is a mapping",
        lambda d: _first(d).update(contributions={}),
        "'contributions' must be a list",
    ),
    (
        "contribution is a string",
        lambda d: _first(d).update(contributions=["S00 did it"]),
        "a contribution must be a mapping",
    ),
    (
        "contribution without evidence key",
        lambda d: _with_contribution(d, evidence=_DROP),
        "contributions[0]: missing key 'evidence'",
    ),
    (
        "contribution with unknown key",
        lambda d: _with_contribution(d, commit="FAKE-NOT-A-COMMIT"),
        "contributions[0]: unknown key 'commit'",
    ),
    (
        "contribution of an unknown session",
        lambda d: _with_contribution(d, session="S13"),
        "'session' must be one of S00..S12",
    ),
    (
        "contribution without summary text",
        lambda d: _with_contribution(d, summary=""),
        "'summary' must be a non-empty string",
    ),
    (
        "contribution with unknown status",
        lambda d: _with_contribution(d, status="done"),
        "contributions[0]: status 'done' is not one of",
    ),
    (
        "contribution evidence is a string",
        lambda d: _with_contribution(d, evidence="docs/widget.md"),
        "'evidence' must be a list",
    ),
    (
        "contribution blockers name a decision",
        lambda d: _with_contribution(d, blockers=["D-001"]),
        "'blockers' must be a list of blocker / change request ids",
    ),
    (
        "contribution blockers is a string",
        lambda d: _with_contribution(d, blockers="B-001"),
        "'blockers' must be a list of blocker / change request ids",
    ),
    (
        "two contributions of one session",
        lambda d: _first(d).update(
            contributions=[
                contribution("S00", "in_progress", ["docs/widget.md"]),
                contribution("S00", "in_progress", ["docs/widget.md"]),
            ]
        ),
        "more than one contribution of session S00",
    ),
    (
        "requirement blockers are not ids",
        lambda d: _first(d).update(blockers=["the supplier"]),
        "'blockers' must be a list of ids",
    ),
]


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    ("change", "expected"),
    [pytest.param(change, expected, id=name) for name, change, expected in MATRIX_SCHEMA_DEFECTS],
)
def test_matrix_schema_and_type_defects_are_reported(
    tmp_path: Path, change: Callable[[dict[str, Any]], Any], expected: str
) -> None:
    # B-001 is resolved here so that a status set by a case does not trigger the blocker rule.
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    assert problems(root) == []
    edit(root, gr.MATRIX_FILE, change)
    report = gr.validate(root)
    assert any(expected in problem for problem in report.problems), report.problems
    assert not report.ok
    assert not report.structure_ok


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "req_id", ["G01-01", "S00-ASG-27", "OWN-08", "M-AUTH-16", "COM-100", "A-01", "X9-ZZ-999", "S12-ACC-08"]
)
def test_requirement_ids_of_the_documented_form_are_accepted(tmp_path: Path, req_id: str) -> None:
    root = make_repo(tmp_path)
    edit(
        root,
        gr.MATRIX_FILE,
        lambda d: d["requirements"].append(_requirement(req_id, "assignment", ["S03"], "Invented atom.")),
    )
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "req_id",
    [
        "m-auth-01",
        "M-AUTH-1",
        "M-AUTH-0001",
        "M_AUTH-01",
        "1M-01",
        "M-AUTH-01\n",
        " M-AUTH-01",
        "M-AUTH-01 ",
        "M-auth-01",
        "M-A1-01",
        "M--01",
        "MAUTH01",
        "-01",
        "M-AUTH-01-02-A",
    ],
)
def test_requirement_ids_of_another_form_are_rejected(tmp_path: Path, req_id: str) -> None:
    root = make_repo(tmp_path)
    edit(
        root,
        gr.MATRIX_FILE,
        lambda d: d["requirements"].append(_requirement(req_id, "assignment", ["S03"], "Invented atom.")),
    )
    problem = only_problem(root)
    assert "id does not match" in problem
    assert r"^[A-Z][A-Z0-9]*(-[A-Z]+)?-[0-9]{2,3}$" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_duplicate_requirement_id_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit(
        root,
        gr.MATRIX_FILE,
        lambda d: d["requirements"].append(
            _requirement("FIX-ASG-02", "assignment", ["S03"], "A second one.")
        ),
    )
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "duplicate requirement id" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "sessions",
    [[], ["S13"], ["s00"], ["S0"], ["S00", "S00"], "S00", [0], ["S00", "S99"], None, ["S-1"]],
)
def test_invalid_sessions_are_rejected(tmp_path: Path, sessions: Any) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(sessions=sessions))
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "session" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize("session", ALL_SESSIONS)
def test_every_session_s00_to_s12_is_valid(tmp_path: Path, session: str) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(sessions=[session]))
    assert problems(root) == []


# --------------------------------------------------------------------------- validate: evidence, blockers
@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "contributions",
    [
        [],
        [contribution("S00", "verified", [])],
        [contribution("S00", "verified", []), contribution("S01", "verified", [])],
    ],
    ids=["no contribution", "contribution without evidence", "two contributions without evidence"],
)
def test_verified_needs_a_contribution_with_evidence(
    tmp_path: Path, contributions: list[dict[str, Any]]
) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(
        root, "FIX-ASG-02", lambda entry: entry.update(status="verified", contributions=contributions)
    )
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "'verified'" in problem
    assert "evidence" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_verified_with_existing_evidence_is_valid(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(
        root,
        "FIX-ASG-02",
        lambda entry: entry.update(
            status="verified",
            contributions=[
                contribution("S00", "verified", []),
                contribution("S01", "verified", ["docs/widget.md"]),
            ],
        ),
    )
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_verified_with_only_a_missing_evidence_path_is_not_verified(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(
        root,
        "FIX-ASG-02",
        lambda entry: entry.update(
            status="verified", contributions=[contribution("S00", "verified", ["docs/never_written.md"])]
        ),
    )
    found = problems(root)
    assert any("'docs/never_written.md' does not exist" in problem for problem in found), found
    assert any(
        "'verified' needs at least one contribution with an existing evidence path" in p for p in found
    )
    assert len(found) == 2, found


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("docs/never_written.md", "does not exist"),
        ("/etc/hostname", "not a repository-relative path"),
        ("C:/fixture/widget.md", "not a repository-relative path"),
        ("docs\\widget.md", "not a repository-relative path"),
        ("docs/../docs/widget.md", "not a normalised repository-relative path"),
        ("../repo/docs/widget.md", "not a normalised repository-relative path"),
        ("./docs/widget.md", "not a normalised repository-relative path"),
        ("", "not a repository-relative path"),
    ],
)
def test_evidence_paths_must_exist_inside_the_repository(tmp_path: Path, path: str, reason: str) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(
        root,
        "FIX-ASG-02",
        lambda entry: entry.update(
            status="in_progress", contributions=[contribution("S00", "in_progress", [path])]
        ),
    )
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "evidence path" in problem
    assert reason in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_evidence_may_be_a_directory(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(
        root,
        "FIX-ASG-02",
        lambda entry: entry.update(
            status="verified", contributions=[contribution("S00", "verified", ["docs"])]
        ),
    )
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "contributions",
    [[], [contribution("S00", "blocked", [])], [contribution("S00", "blocked", [], [])]],
    ids=["no contribution", "contribution without blockers", "contribution with empty blockers"],
)
def test_blocked_needs_a_blocker_id(tmp_path: Path, contributions: list[dict[str, Any]]) -> None:
    root = make_repo(tmp_path)
    edit_requirement(
        root, "FIX-ASG-03", lambda entry: entry.update(status="blocked", contributions=contributions)
    )
    edit_requirement(root, "FIX-ACC-02", lambda entry: entry.update(status="partial"))
    problem = only_problem(root)
    assert "FIX-ASG-03" in problem
    assert "'blocked' needs a blocker id" in problem


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_blocked_with_a_blocker_in_a_contribution_or_at_requirement_level_is_valid(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_requirement(
        root,
        "FIX-ASG-03",
        lambda entry: entry.update(
            status="blocked", contributions=[contribution("S01", "blocked", [], ["B-001"])]
        ),
    )
    edit_requirement(root, "FIX-ACC-02", lambda entry: entry.update(status="blocked", blockers=["CR-001"]))
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize(
    "blocker_id",
    [
        "B-1",
        "B-01",
        "b-001",
        "B-001\n",
        " B-001",
        "B-001 ",
        "CR-1",
        "BCR-001",
        "X-001",
        "U-1",
        "D-01",
        "001",
        "",
    ],
)
def test_ids_in_blockers_lists_must_have_the_documented_form(tmp_path: Path, blocker_id: str) -> None:
    places: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
        "requirement": (gr.MATRIX_FILE, lambda d: _first(d).update(blockers=[blocker_id])),
        "contribution": (gr.MATRIX_FILE, lambda d: _with_contribution(d, blockers=[blocker_id])),
        "acceptance": (gr.ACCEPTANCE_FILE, lambda d: d["acceptance_tests"][0].update(blockers=[blocker_id])),
    }
    for place, (file, change) in places.items():
        root = make_repo(tmp_path / place)
        edit(root, file, change)
        report = gr.validate(root)
        assert any("'blockers' must be a list of" in problem for problem in report.problems), (
            place,
            report.problems,
        )
        assert not report.structure_ok, place


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize("blocker_id", ["D-001", "U-01", "U-02", "D-002"])
def test_contribution_blockers_accept_only_blockers_and_change_requests(
    tmp_path: Path, blocker_id: str
) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit(root, gr.MATRIX_FILE, lambda d: _with_contribution(d, blockers=[blocker_id]))
    assert "blocker / change request ids (B-nnn, CR-nnn)" in only_problem(root)
    # The same ids are accepted at requirement level and in the acceptance test register.
    other = make_repo(tmp_path / "other", blocker_status="RESOLVED")
    edit(other, gr.MATRIX_FILE, lambda d: _first(d).update(blockers=[blocker_id]))
    edit(other, gr.ACCEPTANCE_FILE, lambda d: d["acceptance_tests"][0].update(blockers=[blocker_id]))
    assert problems(other) == []


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
@pytest.mark.parametrize(
    ("reference", "file"),
    [
        ("B-099", gr.CHANGE_REQUESTS_FILE),
        ("CR-099", gr.CHANGE_REQUESTS_FILE),
        ("D-099", gr.DECISION_LOG_FILE),
        ("U-99", gr.DECISION_LOG_FILE),
    ],
)
def test_referenced_ids_must_exist_as_headings(tmp_path: Path, reference: str, file: str) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-03", lambda entry: entry.update(status="blocked", blockers=[reference]))
    edit_requirement(root, "FIX-ACC-02", lambda entry: entry.update(status="partial"))
    problem = only_problem(root)
    assert "FIX-ASG-03" in problem
    assert reference in problem
    assert f"not a heading in {file}" in problem


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
@pytest.mark.parametrize(
    "reference", ["B-001", "B-002", "CR-001", "CR-002", "D-001", "D-002", "U-01", "U-02"]
)
def test_existing_headings_can_be_referenced(tmp_path: Path, reference: str) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-03", lambda entry: entry.update(status="blocked", blockers=[reference]))
    edit_requirement(root, "FIX-ACC-02", lambda entry: entry.update(status="partial"))
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_missing_markdown_logs_are_only_a_problem_when_an_id_is_referenced(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / gr.DECISION_LOG_FILE).unlink()
    (root / gr.CHANGE_REQUESTS_FILE).unlink()
    report = gr.validate(root)
    assert report.problems == []
    assert report.notes == [], "without the log there is no blocker to announce"

    edit_requirement(root, "FIX-ASG-03", lambda entry: entry.update(status="blocked", blockers=["B-001"]))
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(blockers=["U-01"]))
    found = problems(root)
    assert any(
        "FIX-ASG-03" in p and "B-001" in p and f"{gr.CHANGE_REQUESTS_FILE} does not exist" in p for p in found
    ), found
    assert any(
        "AT-FIX-ACC-02" in p and "U-01" in p and f"{gr.DECISION_LOG_FILE} does not exist" in p for p in found
    ), found
    assert len(found) == 2, found


# --------------------------------------------------------------------------- validate: open blockers
@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_open_blocker_is_a_note_while_no_status_is_set(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    report = gr.validate(root)
    assert report.problems == []
    assert len(report.notes) == 1
    note = report.notes[0]
    assert "B-001" in note
    assert "FIX-ASG-03" in note
    assert "FIX-ACC-02" in note
    assert "FIX-ASG-01" not in note, "B-002 is RESOLVED and constrains nothing"
    assert report.lines() == [f"NOTE: {note}"]


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
@pytest.mark.parametrize("status", ["in_progress", "not_applicable", "implemented"])
def test_open_blocker_fails_once_any_status_is_set(tmp_path: Path, status: str) -> None:
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(status=status))
    report = gr.validate(root)
    assert report.notes == []
    assert len(report.problems) == 2, report.problems
    for req_id in ("FIX-ASG-03", "FIX-ACC-02"):
        assert any(
            req_id in problem and "B-001" in problem and "'blocked' or 'partial'" in problem
            for problem in report.problems
        ), report.problems


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
@pytest.mark.parametrize(
    "status", ["not_started", "in_progress", "implemented", "verified", "not_applicable"]
)
def test_requirement_of_an_open_blocker_must_be_blocked_or_partial(tmp_path: Path, status: str) -> None:
    root = make_repo(tmp_path)
    start(root)
    assert problems(root) == []
    edit_requirement(
        root,
        "FIX-ASG-03",
        lambda entry: entry.update(
            status=status, contributions=[contribution("S01", status, ["docs/widget.md"])]
        ),
    )
    problem = only_problem(root)
    assert "FIX-ASG-03" in problem
    assert "OPEN blocker B-001" in problem
    assert f"'{status}'" in problem


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_partial_satisfies_an_open_blocker_and_other_entries_constrain_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    start(root)
    edit_requirement(root, "FIX-ASG-03", lambda entry: entry.update(status="partial"))
    # FIX-ASG-01 is named by the RESOLVED blocker B-002 and FIX-GOV-01 by the OPEN change request
    # CR-001: neither is constrained (FIX-ASG-01 is verified, FIX-GOV-01 implemented).
    assert requirement_of(root, "FIX-ASG-01")["status"] == "verified"
    assert requirement_of(root, "FIX-GOV-01")["status"] == "implemented"
    assert problems(root) == []


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_resolving_the_blocker_lifts_the_constraint(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(status="in_progress"))
    report = gr.validate(root)
    assert report.problems == []
    assert report.notes == []


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
@pytest.mark.parametrize("entry_id", ["B-001", "D-002", "U-01", "CR-001"])
def test_log_entry_naming_an_unknown_requirement_is_a_problem(tmp_path: Path, entry_id: str) -> None:
    root = make_repo(tmp_path)
    file = gr.CHANGE_REQUESTS_FILE if entry_id[0] in "BC" else gr.DECISION_LOG_FILE
    text = (root / file).read_text(encoding="utf-8")
    pattern = rf"(## {entry_id} - [^\n]*\n\n- Status: \w+\n(?:- (?:Date|Raised): [^\n]*\n)?- Requirements: )"
    changed, count = re.subn(pattern, r"\1FIX-GONE-01, ", text)
    assert count == 1
    _write(root / file, changed)
    problem = only_problem(root)
    assert entry_id in problem
    assert "'FIX-GONE-01'" in problem
    assert "not in the requirements matrix" in problem


# --------------------------------------------------------------------------- Markdown logs
@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_parse_log_reads_headings_status_and_requirements() -> None:
    entries, found = gr.parse_log(DECISION_LOG, gr.DECISION_LOG_FILE)
    assert found == []
    assert [(entry.id, entry.kind, entry.status, entry.status_valid) for entry in entries] == [
        ("D-001", "D", "APPROVED", True),
        ("D-002", "D", "PROPOSED", True),
        ("U-01", "U", "OPEN", True),
        ("U-02", "U", "RESOLVED", True),
    ]
    assert entries[0].title == "Use widgets"
    assert entries[1].title == "Gadgets | later"
    assert entries[0].requirements == ("FIX-GOV-01", "FIX-ASG-01")
    assert entries[3].requirements == ()
    assert [entry.is_open for entry in entries] == [False, False, True, False]
    lines = DECISION_LOG.splitlines()
    for entry in entries:
        assert lines[entry.line - 1].startswith(f"## {entry.id} - ")
        assert entry.file == gr.DECISION_LOG_FILE


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_parse_log_blockers_and_change_requests() -> None:
    entries, found = gr.parse_log(CHANGE_REQUESTS.format(blocker_status="OPEN"), gr.CHANGE_REQUESTS_FILE)
    assert found == []
    assert [(entry.id, entry.kind, entry.status) for entry in entries] == [
        ("B-001", "B", "OPEN"),
        ("B-002", "B", "RESOLVED"),
        ("CR-001", "CR", "OPEN"),
        ("CR-002", "CR", "APPROVED"),
    ]
    assert entries[0].requirements == ("FIX-ASG-03", "FIX-ACC-02")


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_parse_log_details() -> None:
    text = """\
# Log

Prose before any entry, with a bullet that is not a field of an entry:

- Status: OPEN

```
## D-777 - A heading inside a code fence is an example, not a decision
- Status: REJECTED
```

## D-010 - Wrapped requirement list

- Status: SUPERSEDED
- Requirements: FIX-ASG-01,
  FIX-ASG-02,
  FIX-ASG-03

### A sub-heading stays inside the entry

- Date: 2026-11-27

## Notes that are not an entry

- Status: REJECTED

## U-100 - Many open items later
- Status:   OPEN
"""
    entries, found = gr.parse_log(text, gr.DECISION_LOG_FILE)
    assert found == []
    assert [(entry.id, entry.status, entry.status_valid) for entry in entries] == [
        ("D-010", "SUPERSEDED", True),
        ("U-100", "OPEN", True),
    ]
    assert entries[0].requirements == ("FIX-ASG-01", "FIX-ASG-02", "FIX-ASG-03")


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
@pytest.mark.parametrize(
    ("kind", "statuses"),
    [
        ("D-001", ["PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED"]),
        ("U-01", ["OPEN", "RESOLVED"]),
        ("B-001", ["OPEN", "RESOLVED", "APPROVED", "REJECTED"]),
        ("CR-001", ["OPEN", "RESOLVED", "APPROVED", "REJECTED"]),
    ],
)
def test_allowed_status_values_per_kind(kind: str, statuses: list[str]) -> None:
    every = ["PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED", "OPEN", "RESOLVED"]
    for status in every:
        entries, found = gr.parse_log(f"## {kind} - Title\n\n- Status: {status}\n", "log.md")
        assert len(entries) == 1
        assert entries[0].status_valid is (status in statuses)
        assert (found == []) is (status in statuses), (status, found)


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", "has no '- Status:' line"),
        ("- Date: 2026-11-27\n", "has no '- Status:' line"),
        ("- Status:\n", "has status ''"),
        ("- Status: DONE\n", "has status 'DONE'"),
        ("- Status: approved\n", "has status 'approved'"),
        ("- Status: APPROVED pending the owner\n", "has status 'APPROVED pending the owner'"),
        ("- Status: OPEN\n", "has status 'OPEN'"),
        ("- Status: PROPOSED\n- Status: APPROVED\n", "has 2 '- Status:' lines"),
        ("* Status: APPROVED\n", "has no '- Status:' line"),
        ("- **Status:** APPROVED\n", "has no '- Status:' line"),
    ],
)
def test_decision_without_one_valid_status_line_is_reported(body: str, expected: str) -> None:
    entries, found = gr.parse_log(f"## D-001 - Title\n\n{body}\nProse.\n", gr.DECISION_LOG_FILE)
    assert len(entries) == 1
    assert not entries[0].status_valid
    assert not entries[0].is_open
    assert len(found) == 1, found
    assert expected in found[0]
    assert found[0].startswith(f"{gr.DECISION_LOG_FILE}:1: D-001")


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
@pytest.mark.parametrize(
    "heading",
    [
        "## D-1 - Too few digits",
        "## U-1 - Too few digits",
        "## B-01 - Too few digits",
        "## CR-01 - Too few digits",
        "## D-001: Colon instead of a dash",
        "## D-001 Title without separator",
        "## D-001 -",
        "## D-001",
        "### D-001 - Wrong heading level",
        "# D-001 - Wrong heading level",
        "##D-001 - No space",
        "## D-001 \u2013 En dash",
        "## B-001a - Suffix",
    ],
)
def test_malformed_entry_heading_is_reported_not_skipped(heading: str) -> None:
    entries, found = gr.parse_log(f"{heading}\n\n- Status: OPEN\n", "log.md")
    assert entries == []
    assert len(found) == 1, found
    assert found[0].startswith("log.md:1: heading")
    assert "looks like a register entry" in found[0]


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_log_problems_fail_validation(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    path = root / gr.DECISION_LOG_FILE
    _write(path, path.read_text(encoding="utf-8").replace("- Status: PROPOSED", "- Status: MAYBE"))
    problem = only_problem(root)
    assert problem.startswith(f"{gr.DECISION_LOG_FILE}:")
    assert "D-002" in problem
    assert "'MAYBE'" in problem
    assert "PROPOSED, APPROVED, REJECTED, SUPERSEDED" in problem


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_duplicate_ids_within_and_across_logs_are_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    decisions = root / gr.DECISION_LOG_FILE
    _write(
        decisions, decisions.read_text(encoding="utf-8") + "\n## D-001 - Used twice\n\n- Status: PROPOSED\n"
    )
    problem = only_problem(root)
    assert "duplicate id D-001" in problem
    assert f"first defined at {gr.DECISION_LOG_FILE}:" in problem

    changes = root / gr.CHANGE_REQUESTS_FILE
    _write(
        changes,
        changes.read_text(encoding="utf-8") + "\n## U-01 - Open item in the wrong file\n\n- Status: OPEN\n",
    )
    found = problems(root)
    assert any("duplicate id U-01" in p and p.startswith(gr.CHANGE_REQUESTS_FILE) for p in found), found
    assert any("U-01 (open item) belongs in" in p and gr.DECISION_LOG_FILE in p for p in found), found
    assert len(found) == 3, found


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_entry_in_the_wrong_log_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    decisions = root / gr.DECISION_LOG_FILE
    _write(
        decisions, decisions.read_text(encoding="utf-8") + "\n## B-007 - A blocker here\n\n- Status: OPEN\n"
    )
    problem = only_problem(root)
    assert f"B-007 (blocker) belongs in {gr.CHANGE_REQUESTS_FILE}" in problem
    assert problem.startswith(f"{gr.DECISION_LOG_FILE}:")


@pytest.mark.requirement("OWN-17", "S00-ASG-27")
def test_real_logs_parse_without_format_problems() -> None:
    logs = gr.load_logs(REPO_ROOT)
    assert list(logs.problems) == []
    for file in logs.missing_files:
        assert file in gr.LOG_FILES
    ids = [entry.id for entry in logs.entries]
    assert len(ids) == len(set(ids))
    for file in set(gr.LOG_FILES) - set(logs.missing_files):
        text = (REPO_ROOT / file).read_text(encoding="utf-8")
        text = re.sub(r"(?ms)^```.*?^```[^\n]*$", "", text)  # examples inside code fences are not entries
        headings = re.findall(r"(?m)^## ((?:D|U|B|CR)-[0-9]+) - ", text)
        assert headings == [entry.id for entry in logs.entries if entry.file == file]
        assert headings, f"{file} exists but holds no entry"


# --------------------------------------------------------------------------- validate: acceptance register
ACCEPTANCE_SCHEMA_DEFECTS: list[tuple[str, Callable[[dict[str, Any]], Any], str]] = [
    ("top level is a list", lambda d: [], "top level must be a mapping"),
    ("missing top-level key", lambda d: _drop(d, "description"), "missing key 'description'"),
    ("unknown top-level key", lambda d: d.update(tests=[]), "unknown key 'tests'"),
    ("schema version 3", lambda d: d.update(schema_version=3), "schema_version must be 1"),
    (
        "status values as a mapping",
        lambda d: d.update(status_values={"planned": "x"}),
        "status_values must be",
    ),
    (
        "status vocabulary without partial",
        lambda d: d["status_values"].remove("partial"),
        "status_values must contain 'partial'",
    ),
    ("entries is a mapping", lambda d: d.update(acceptance_tests={}), "acceptance_tests must be a list"),
    (
        "entry is a string",
        lambda d: d["acceptance_tests"].__setitem__(0, "AT-FIX-ACC-01"),
        "acceptance_tests[0] must be a mapping",
    ),
    (
        "entry without blockers",
        lambda d: _drop(d["acceptance_tests"][0], "blockers"),
        "AT-FIX-ACC-01: missing key 'blockers'",
    ),
    ("entry with unknown key", lambda d: d["acceptance_tests"][0].update(owner="x"), "unknown key 'owner'"),
    (
        "empty title",
        lambda d: d["acceptance_tests"][0].update(title=""),
        "'title' must be a non-empty string",
    ),
    ("id is a number", lambda d: d["acceptance_tests"][0].update(id=1), "'id' must be a non-empty string"),
    ("unknown session", lambda d: d["acceptance_tests"][0].update(session="S13"), "'session' must be one of"),
    (
        "test modules is a string",
        lambda d: d["acceptance_tests"][0].update(test_modules="tests/test_widget.py"),
        "'test_modules' must be a list",
    ),
    ("status outside the list", lambda d: d["acceptance_tests"][0].update(status="green"), "status 'green'"),
    (
        "blockers are not ids",
        lambda d: d["acceptance_tests"][0].update(blockers=["B1"]),
        "'blockers' must be a list of ids",
    ),
    ("notes is null", lambda d: d["acceptance_tests"][0].update(notes=None), "'notes' must be a string"),
    (
        "evidence is a list",
        lambda d: d["acceptance_tests"][0].update(evidence=[1, 1, 0, 0]),
        "'evidence' must be a mapping",
    ),
    (
        "evidence without file",
        lambda d: d["acceptance_tests"][0].update(
            evidence={"tests": 1, "passed": 1, "failed": 0, "skipped": 0}
        ),
        "evidence: missing key 'file'",
    ),
    (
        "evidence with a negative count",
        lambda d: d["acceptance_tests"][0].update(
            evidence={"file": "e.json", "tests": 0, "passed": 1, "failed": -1, "skipped": 0}
        ),
        "evidence 'failed' must be a non-negative integer",
    ),
    (
        "evidence with a boolean count",
        lambda d: d["acceptance_tests"][0].update(
            evidence={"file": "e.json", "tests": 1, "passed": True, "failed": 0, "skipped": 0}
        ),
        "evidence 'passed' must be a non-negative integer",
    ),
    (
        "evidence counts do not add up",
        lambda d: d["acceptance_tests"][0].update(
            evidence={"file": "e.json", "tests": 5, "passed": 1, "failed": 0, "skipped": 0}
        ),
        "evidence counts are inconsistent",
    ),
    (
        "evidence without file name",
        lambda d: d["acceptance_tests"][0].update(
            evidence={"file": "", "tests": 1, "passed": 1, "failed": 0, "skipped": 0}
        ),
        "'file' must be a non-empty string",
    ),
]


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize(
    ("change", "expected"),
    [pytest.param(change, expected, id=name) for name, change, expected in ACCEPTANCE_SCHEMA_DEFECTS],
)
def test_acceptance_schema_and_type_defects_are_reported(
    tmp_path: Path, change: Callable[[dict[str, Any]], Any], expected: str
) -> None:
    root = make_repo(tmp_path)
    edit(root, gr.ACCEPTANCE_FILE, change)
    report = gr.validate(root)
    assert any(expected in problem for problem in report.problems), report.problems
    assert not report.structure_ok


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
def test_acceptance_requirement_without_register_entry_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit(root, gr.ACCEPTANCE_FILE, lambda d: _drop(d["acceptance_tests"], 1))
    problem = only_problem(root)
    assert "FIX-ACC-02" in problem
    assert "AT-FIX-ACC-02" in problem


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
def test_register_entry_without_acceptance_requirement_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    extra = copy.deepcopy(_acceptance_document()["acceptance_tests"][1])
    extra.update(id="AT-FIX-ACC-09", requirement="FIX-ACC-09")
    edit(root, gr.ACCEPTANCE_FILE, lambda d: d["acceptance_tests"].append(extra))
    problem = only_problem(root)
    assert "AT-FIX-ACC-09" in problem
    assert "FIX-ACC-09 is not in the requirements matrix" in problem


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
def test_register_entry_for_a_requirement_of_another_kind_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    extra = copy.deepcopy(_acceptance_document()["acceptance_tests"][1])
    extra.update(id="AT-FIX-ASG-02", requirement="FIX-ASG-02", session="S00")
    edit(root, gr.ACCEPTANCE_FILE, lambda d: d["acceptance_tests"].append(extra))
    problem = only_problem(root)
    assert "AT-FIX-ASG-02" in problem
    assert "kind 'assignment', not 'acceptance'" in problem


@pytest.mark.requirement("OWN-18")
def test_acceptance_id_must_be_at_plus_requirement_id(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(id="AT-FIX-ACC-2"))
    problem = only_problem(root)
    assert "AT-FIX-ACC-2" in problem
    assert "id must be 'AT-FIX-ACC-02'" in problem


@pytest.mark.requirement("OWN-18")
def test_two_entries_for_one_requirement_are_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    duplicate = copy.deepcopy(_acceptance_document()["acceptance_tests"][1])
    edit(root, gr.ACCEPTANCE_FILE, lambda d: d["acceptance_tests"].append(duplicate))
    found = problems(root)
    assert any("AT-FIX-ACC-02" in p and "duplicate acceptance test id" in p for p in found), found
    assert any("FIX-ACC-02 already has an acceptance test entry" in p for p in found), found
    assert len(found) == 2, found


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
def test_acceptance_session_must_equal_the_session_of_the_requirement(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(session="S02"))
    problem = only_problem(root)
    assert "AT-FIX-ACC-02" in problem
    assert "session S02 differs" in problem
    assert "S01" in problem


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
def test_acceptance_requirement_shared_by_two_sessions_does_not_equal_one_session(tmp_path: Path) -> None:
    """'Equal session' is exact: the register names one session, so must the requirement."""
    root = make_repo(tmp_path)
    edit_requirement(root, "FIX-ACC-02", lambda entry: entry.update(sessions=["S01", "S02"]))
    problem = only_problem(root)
    assert "AT-FIX-ACC-02" in problem
    assert "session S01 differs from the sessions [S01, S02]" in problem


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize("status", ["blocked", "partial"])
def test_blocked_or_partial_acceptance_needs_blockers(tmp_path: Path, status: str) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(status=status))
    problem = only_problem(root)
    assert "AT-FIX-ACC-01" in problem
    assert f"'{status}' needs at least one id in 'blockers'" in problem
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(blockers=["B-001"]))
    assert problems(root) == []
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(blockers=["B-404"]))
    assert "B-404" in only_problem(root)


def _evidence(passed: int, failed: int, skipped: int) -> dict[str, Any]:
    return {
        "file": "evidence/pytest.json",
        "tests": passed + failed + skipped,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }


@pytest.mark.requirement("OWN-18")
def test_passing_without_recorded_evidence_is_a_problem(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(status="passing"))
    problem = only_problem(root)
    assert "AT-FIX-ACC-01" in problem
    assert "'passing' without recorded evidence" in problem


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize(
    ("passed", "failed", "skipped"),
    [(0, 0, 0), (0, 1, 0), (3, 1, 0), (0, 0, 2), (3, 0, 1), (2, 2, 2)],
)
def test_passing_that_contradicts_the_recorded_counts_is_a_problem(
    tmp_path: Path, passed: int, failed: int, skipped: int
) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(
        root,
        "AT-FIX-ACC-01",
        lambda entry: entry.update(status="passing", evidence=_evidence(passed, failed, skipped)),
    )
    problem = only_problem(root)
    assert "AT-FIX-ACC-01" in problem
    assert "'passing' contradicts the recorded evidence" in problem
    assert f"{passed} passed, {failed} failed, {skipped} skipped" in problem


@pytest.mark.requirement("OWN-18")
def test_passing_with_evidence_and_marked_test_module_is_valid(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(
        root, "AT-FIX-ACC-01", lambda entry: entry.update(status="passing", evidence=_evidence(4, 0, 0))
    )
    assert problems(root) == []


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize("status", ["passing", "failing", "blocked", "partial"])
def test_listed_test_module_must_exist_unless_planned(tmp_path: Path, status: str) -> None:
    root = make_repo(tmp_path)
    assert problems(root) == [], "while the entry is planned the module may be missing or unmarked"
    edit_acceptance(
        root,
        "AT-FIX-ACC-01",
        lambda entry: entry.update(
            status=status,
            evidence=_evidence(2, 0, 0),
            blockers=["B-001"],
            test_modules=["tests/test_widget.py", "tests/test_never_written.py"],
        ),
    )
    problem = only_problem(root)
    assert "AT-FIX-ACC-01" in problem
    assert "'tests/test_never_written.py' does not exist" in problem


@pytest.mark.requirement("OWN-18")
def test_planned_entry_may_list_modules_that_do_not_exist_yet(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(
        root, "AT-FIX-ACC-01", lambda entry: entry.update(test_modules=["tests/test_never_written.py"])
    )
    (root / "tests/test_widget.py").unlink()
    assert problems(root) == []


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize(
    "module_text",
    [
        "import pytest\n\n\ndef test_unmarked():\n    assert True\n",
        '@pytest.mark.acceptance("FIX-ACC-02")\ndef test_other():\n    assert True\n',
        '@pytest.mark.requirement("FIX-ACC-01")\ndef test_requirement_only():\n    assert True\n',
        "# FIX-ACC-01 is only mentioned in a comment\n",
        '@pytest.mark.acceptance("FIX-ACC-011")\ndef test_longer_id():\n    assert True\n',
        "",
    ],
    ids=["no marker", "other acceptance id", "requirement marker only", "comment only", "longer id", "empty"],
)
def test_listed_test_module_must_contain_the_acceptance_marker(tmp_path: Path, module_text: str) -> None:
    root = make_repo(tmp_path)
    _write(root / "tests/test_widget.py", module_text)
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(status="failing"))
    problem = only_problem(root)
    assert "AT-FIX-ACC-01" in problem
    assert "'tests/test_widget.py' does not contain the marker" in problem
    assert 'acceptance("FIX-ACC-01")' in problem


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize(
    "marker",
    [
        '@pytest.mark.acceptance("FIX-ACC-01")',
        "@pytest.mark.acceptance('FIX-ACC-01')",
        '@pytest.mark.acceptance("FIX-ACC-02", "FIX-ACC-01")',
        'pytestmark = [pytest.mark.requirement("FIX-ASG-01"), pytest.mark.acceptance("FIX-ACC-01")]',
        '@pytest.mark.acceptance(\n    "FIX-ACC-01",\n)',
    ],
)
def test_acceptance_marker_spellings_that_are_recognised(tmp_path: Path, marker: str) -> None:
    root = make_repo(tmp_path)
    _write(root / "tests/test_widget.py", f"import pytest\n\n{marker}\ndef test_it():\n    assert True\n")
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(status="failing"))
    assert problems(root) == []


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize("status", ["passing", "failing", "partial"])
def test_status_derived_from_tests_needs_a_listed_test_module(tmp_path: Path, status: str) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(
        root,
        "AT-FIX-ACC-01",
        lambda entry: entry.update(
            status=status, evidence=_evidence(1, 0, 0), blockers=["B-001"], test_modules=[]
        ),
    )
    problem = only_problem(root)
    assert f"'{status}' needs at least one path in 'test_modules'" in problem


# --------------------------------------------------------------------------- validate: unreadable registers
@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize("file", [gr.MATRIX_FILE, gr.ACCEPTANCE_FILE])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "the register file does not exist"),
        (b"schema_version: 1\ntitle: [unclosed\n", "not valid YAML"),
        (b"schema_version: 1\nschema_version: 1\n", "duplicate key 'schema_version'"),
        (b"\xff\xfe\x00not utf-8", "not UTF-8 text"),
        (b"", "top level must be a mapping"),
    ],
    ids=["missing", "syntax error", "duplicate key", "not utf-8", "empty"],
)
def test_unreadable_register_is_a_problem_not_a_crash(
    tmp_path: Path, file: str, content: bytes | None, expected: str
) -> None:
    root = make_repo(tmp_path)
    if content is None:
        (root / file).unlink()
    else:
        (root / file).write_bytes(content)
    report = gr.validate(root)
    assert any(problem.startswith(file) and expected in problem for problem in report.problems), (
        report.problems
    )
    assert not report.structure_ok
    before = snapshot(root)
    for outcome in (gr.render(root), gr.render(root, check=True)):
        assert not outcome.ok
        assert "not structurally valid" in outcome.problems[0]
    assert snapshot(root) == before, "nothing is rendered from registers that cannot be trusted"
    with pytest.raises(gr.RegisterError, match="not structurally valid"):
        gr.stats(root)


#: SYNTHETIC FIXTURE junk values that a damaged register could hold anywhere.
JUNK_VALUES: list[Any] = [
    None,
    True,
    0,
    -1,
    1.5,
    "",
    " ",
    "x",
    "S13",
    "verified",
    "B-001",
    "2026-11-27",
    [],
    [1],
    ["S00"],
    ["B-001"],
    ["nope.md"],
    [[]],
    {},
    {"a": 1},
    {1: 2},
    [{"session": "S00"}],
    {"file": "e.json", "tests": 1, "passed": 1, "failed": 0, "skipped": 0},
    [{"session": "S00", "summary": "s", "status": "blocked", "evidence": [], "blockers": ["B-001"]}],
]


def _paths(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [prefix] if prefix else []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _paths(value, (*prefix, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _paths(value, (*prefix, index))
    return found


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
@settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=120,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    damage=st.tuples(
        st.sampled_from([gr.MATRIX_FILE, gr.ACCEPTANCE_FILE]),
        st.integers(min_value=0, max_value=10_000),
        st.sampled_from(["replace", "delete", "add"]),
        st.sampled_from(JUNK_VALUES),
        st.booleans(),
    )
)
def test_damaged_registers_are_reported_never_a_crash(
    tmp_path: Path, damage: tuple[str, int, str, Any, bool]
) -> None:
    """Whatever a register holds, every command answers with problems instead of a traceback."""
    file, position, action, junk, started = damage
    root = make_repo(tmp_path)  # rewrites both registers, so every example starts from the valid fixture
    for generated in gr.GENERATED_FILES:
        (root / generated).unlink(missing_ok=True)
    if started:
        start(root)
    document = load(root, file)
    paths = _paths(document)
    path = paths[position % len(paths)]
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    if action == "delete":
        del parent[path[-1]]
    elif action == "add" and isinstance(parent, dict):
        parent[f"extra_{path[-1]}"] = copy.deepcopy(junk)
    else:
        parent[path[-1]] = copy.deepcopy(junk)
    _write(root / file, gr.dump_yaml(document))

    report = gr.validate(root)
    checked = gr.render(root, check=True)
    if report.structure_ok:
        assert len(checked.problems) == 3
        assert all("is missing" in problem for problem in checked.problems), checked.problems
        assert gr.stats(root)["requirements"]["total"] == len(load(root, gr.MATRIX_FILE)["requirements"])
        assert gr.render(root).problems == []
    else:
        assert not report.ok
        assert "not structurally valid" in checked.problems[0]
        with pytest.raises(gr.RegisterError):
            gr.stats(root)
    patch = write_patch(root, [{"ids": ["FIX-ASG-02"], "notes": "SYNTHETIC FIXTURE: a note."}])
    applied = gr.apply_patch(root, patch)
    synced = gr.sync_evidence(root, write_evidence(root, widget_results("passed", "skipped")))
    if not report.structure_ok:
        assert not applied.ok
        assert not synced.ok
    if not report.ok:
        assert not applied.ok, "apply never produces a state that validate rejects"


# --------------------------------------------------------------------------- render
@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_render_writes_exactly_the_three_generated_files(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    report = gr.render(root)
    assert report.problems == []
    after = snapshot(root)
    assert changed_files(before, after) == set(gr.GENERATED_FILES)
    assert set(gr.GENERATED_FILES) == {
        "specifications/REQUIREMENTS_MATRIX.md",
        "docs/ACCEPTANCE_TEST_REGISTER.md",
        "docs/IMPLEMENTATION_STATUS.md",
    }
    for file in gr.GENERATED_FILES:
        content = after[file]
        assert b"GENERATED - do not edit" in content.split(b"\n\n", 2)[1], "notice directly below the title"
        assert content.endswith(b"\n")
        assert not content.endswith(b"\n\n")
        assert b"\r" not in content
        content.decode("utf-8")


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_render_is_deterministic(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    start(root)
    first = gr.render_documents(gr.load_registers(root)[0])
    second = gr.render_documents(gr.load_registers(root)[0])
    assert first == second
    assert list(first) == list(gr.GENERATED_FILES)

    assert gr.render(root).ok
    written = snapshot(root)
    assert {file: written[file].decode("utf-8") for file in gr.GENERATED_FILES} == first
    # Another clone, another time: the same registers give the same bytes.
    other = tmp_path / "elsewhere" / "repo"
    shutil.copytree(root, other)
    for path in other.rglob("*"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    for file in gr.GENERATED_FILES:
        (other / file).unlink()
    assert gr.render(other).ok
    assert {file: (other / file).read_bytes() for file in gr.GENERATED_FILES} == {
        file: written[file] for file in gr.GENERATED_FILES
    }
    again = gr.render(root)
    assert again.messages == [f"{file}: up to date" for file in gr.GENERATED_FILES]
    assert snapshot(root) == written


def _table_rows(markdown: str) -> list[list[str]]:
    """Cells of every table row (split on unescaped pipes), separator rows excluded."""
    rows = []
    for line in markdown.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            cells = re.split(r"(?<!\\)\|", line)
            assert cells[0] == ""
            assert cells[-1] == ""
            rows.append([cell.strip() for cell in cells[1:-1]])
    return rows


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_requirements_matrix_markdown(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    start(root)
    assert gr.render(root).ok
    text = (root / gr.MATRIX_MD).read_text(encoding="utf-8")
    assert text.startswith("# Synthetic fixture requirements matrix\n\n> GENERATED - do not edit.")
    assert "FIXTURE-1" in text

    totals = {row[0]: row[1] for row in _table_rows(section_of(text, "Totals by status"))[1:]}
    assert totals == {
        "not_started": "1",
        "in_progress": "1",
        "partial": "1",
        "implemented": "1",
        "verified": "1",
        "blocked": "2",
        "not_applicable": "0",
        "**total**": "7",
    }
    assert r"cannot proceed \| see blocker" in section_of(text, "Totals by status")

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings[-3:] == [
        "## FIXTURE_MASTER.pdf - G01 | Governance",
        "## FIXTURE_BRIEF.pdf - Your assignment",
        "## FIXTURE_BRIEF.pdf - Acceptance tests",
    ], "one table per source section, in the order the sections first appear in the file"
    header = ["id", "requirement", "sessions", "status", "review", "contributions/notes"]
    assert sum(1 for row in _table_rows(text) if row == header) == 3

    assignment = _table_rows(section_of(text, "FIXTURE_BRIEF.pdf - Your assignment"))
    assert assignment[0] == header
    assert [row[0] for row in assignment[1:]] == ["FIX-ASG-01", "FIX-ASG-02", "FIX-ASG-03", "FIX-ASG-04"]
    assert all(len(row) == 6 for row in _table_rows(text) if row[0].startswith("FIX-"))

    rows = {row[0]: row for row in _table_rows(text) if row[0].startswith("FIX-")}
    assert sorted(rows) == sorted(entry["id"] for entry in load(root, gr.MATRIX_FILE)["requirements"])
    assert rows["FIX-GOV-01"][1:5] == ["Keep the registers.", "all (S00-S12)", "implemented", "unverified"]
    assert r"SYNTHETIC FIXTURE note with a \| pipe." in rows["FIX-GOV-01"][5]
    assert rows["FIX-ASG-02"][2] == "S00, S01"
    assert rows["FIX-ASG-01"][3] == "verified"
    assert "S00 [verified]" in rows["FIX-ASG-01"][5]
    assert "`docs/widget.md`" in rows["FIX-ASG-01"][5]
    assert "S01 [blocked]" in rows["FIX-ASG-03"][5]
    assert "B-001" in rows["FIX-ASG-03"][5]
    assert "B-001" in rows["FIX-ACC-02"][5], "requirement-level blockers are shown"
    assert rows["FIX-ACC-01"][5] == ""


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_table_cells_escape_pipes_and_keep_one_line(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_requirement(
        root,
        "FIX-ASG-02",
        lambda entry: entry.update(text="a | b || c", notes="first line\nsecond | line\n\nlast \\| line"),
    )
    assert gr.render(root).ok
    text = (root / gr.MATRIX_MD).read_text(encoding="utf-8")
    row = next(row for row in _table_rows(text) if row[0] == "FIX-ASG-02")
    assert len(row) == 6
    assert row[1] == r"a \| b \|\| c"
    assert row[5] == r"Note: first line<br>second \| line<br>last \\\| line"


@pytest.mark.requirement("OWN-18")
def test_acceptance_register_markdown(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    evidence = write_evidence(root, widget_results("passed", "passed"))
    assert gr.sync_evidence(root, evidence).ok
    edit_acceptance(
        root, "AT-FIX-ACC-02", lambda entry: entry.update(status="blocked", blockers=["B-001", "U-01"])
    )
    assert gr.render(root).ok
    text = (root / gr.ACCEPTANCE_MD).read_text(encoding="utf-8")
    assert text.startswith("# Synthetic fixture acceptance test register\n\n> GENERATED - do not edit.")

    totals = {row[0]: row[1] for row in _table_rows(section_of(text, "Totals by status"))[1:]}
    assert totals == {
        "planned": "0",
        "passing": "1",
        "failing": "0",
        "blocked": "1",
        "partial": "0",
        "**total**": "2",
    }
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Totals by status", "## S00", "## S01"], "one table per session that has entries"
    header = ["id", "acceptance test", "status", "tests passed/failed/skipped", "test modules", "blockers"]
    first = _table_rows(section_of(text, "S00"))
    assert first[0] == header
    assert first[1][0] == "AT-FIX-ACC-01"
    assert first[1][1] == "Widget round trip"
    assert first[1][2] == "passing"
    assert first[1][3].startswith("2 / 0 / 0")
    assert "evidence/pytest.json" in first[1][3]
    assert first[1][4] == "`tests/test_widget.py`"
    assert first[1][5] == ""
    second = _table_rows(section_of(text, "S01"))
    assert second[0] == header
    assert second[1][0] == "AT-FIX-ACC-02"
    assert "SYNTHETIC FIXTURE: an evaluation." in second[1][1]
    assert second[1][2:] == ["blocked", "-", "", "B-001, U-01"]


@pytest.mark.requirement("OWN-16")
def test_implementation_status_markdown(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    start(root)
    assert gr.render(root).ok
    text = (root / gr.STATUS_MD).read_text(encoding="utf-8")
    assert text.startswith("# GIE implementation status\n\n> GENERATED - do not edit.")

    by_session = _table_rows(section_of(text, "Requirements by session"))
    assert by_session[0] == ["session", "requirements", *STATUS_VALUES]
    rows = {row[0]: [int(cell) for cell in row[1:]] for row in by_session[1:]}
    assert list(rows) == [*ALL_SESSIONS, "**all**"]
    # Columns: total, not_started, in_progress, partial, implemented, verified, blocked, not_applicable.
    # S00 counts FIX-ASG-04 although S00 is not in its 'sessions': it recorded a contribution.
    assert rows["S00"] == [5, 1, 1, 1, 1, 1, 0, 0]
    assert rows["S01"] == [4, 0, 1, 0, 1, 0, 2, 0]
    assert rows["S02"] == [2, 0, 0, 1, 1, 0, 0, 0]
    for session in ALL_SESSIONS[3:]:
        assert rows[session] == [1, 0, 0, 0, 1, 0, 0, 0]
    assert rows["**all**"] == [7, 1, 1, 1, 1, 1, 2, 0]

    attention = section_of(text, "Requirements that are blocked, partial, in progress or implemented")
    listed = re.findall(r"(?m)^- \*\*([A-Z0-9-]+)\*\*", attention)
    assert listed == ["FIX-ASG-03", "FIX-ACC-02", "FIX-ASG-04", "FIX-ASG-02", "FIX-GOV-01"]
    assert "### blocked (2)" in attention
    assert "### partial (1)" in attention
    assert "### in_progress (1)" in attention
    assert "### implemented (1)" in attention
    assert "FIX-ASG-01" not in attention, "verified requirements are not listed"
    assert "FIX-ACC-01" not in attention, "requirements that are not started are not listed"
    blocked_item = attention[attention.index("**FIX-ASG-03**") : attention.index("**FIX-ACC-02**")]
    assert "S01 [blocked]" in blocked_item
    assert "B-001" in blocked_item
    requirement_level = attention[attention.index("**FIX-ACC-02**") : attention.index("### partial")]
    assert "B-001" in requirement_level
    partial_item = attention[attention.index("**FIX-ASG-04**") : attention.index("### in_progress")]
    assert "S00 [partial]" in partial_item
    assert "`docs/widget.md`" in partial_item

    open_section = section_of(text, "Open blockers, change requests and open items")
    open_rows = _table_rows(open_section)
    assert open_rows[0] == ["id", "type", "status", "title", "requirements"]
    assert [row[:3] for row in open_rows[1:]] == [
        ["B-001", "blocker", "OPEN"],
        ["CR-001", "change request", "OPEN"],
        ["U-01", "open item", "OPEN"],
    ]
    assert open_rows[1][3] == "Gadget supplier unreachable"
    assert open_rows[1][4] == "FIX-ASG-03, FIX-ACC-02"
    for closed in ("B-002", "CR-002", "U-02"):
        assert closed not in open_section

    decisions = _table_rows(section_of(text, "Decisions"))
    assert decisions[0] == ["id", "status", "title", "requirements"]
    assert decisions[1:] == [
        ["D-001", "APPROVED", "Use widgets", "FIX-GOV-01, FIX-ASG-01"],
        ["D-002", "PROPOSED", r"Gadgets \| later", "FIX-ASG-02"],
    ]


@pytest.mark.requirement("OWN-16", "OWN-17", "S00-ASG-27")
def test_implementation_status_without_markdown_logs_says_so(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / gr.DECISION_LOG_FILE).unlink()
    (root / gr.CHANGE_REQUESTS_FILE).unlink()
    assert gr.render(root).ok
    text = (root / gr.STATUS_MD).read_text(encoding="utf-8")
    open_section = section_of(text, "Open blockers, change requests and open items")
    assert f"`{gr.DECISION_LOG_FILE}` does not exist." in open_section
    assert f"`{gr.CHANGE_REQUESTS_FILE}` does not exist." in open_section
    assert "None." in open_section
    assert "None." in section_of(text, "Decisions")


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_render_check_reports_missing_files_and_writes_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    report = gr.render(root, check=True)
    assert len(report.problems) == 3
    for file, problem in zip(gr.GENERATED_FILES, report.problems, strict=True):
        assert problem.startswith(f"{file}: is missing")
    assert snapshot(root) == before

    assert gr.render(root).ok
    rendered = snapshot(root)
    checked = gr.render(root, check=True)
    assert checked.problems == []
    assert snapshot(root) == rendered


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_render_check_detects_stale_files_after_a_register_change(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert gr.render(root).ok
    edit_requirement(root, "FIX-ASG-04", lambda entry: entry.update(notes="SYNTHETIC FIXTURE: a new note."))
    before = snapshot(root)
    report = gr.render(root, check=True)
    assert [problem.split(":")[0] for problem in report.problems] == [gr.MATRIX_MD], report.problems
    assert "is stale" in report.problems[0]
    assert snapshot(root) == before, "--check never writes"

    start(root)
    stale = gr.render(root, check=True)
    assert [problem.split(":")[0] for problem in stale.problems] == [gr.MATRIX_MD, gr.STATUS_MD]

    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(title="Supplier handshake, renamed"))
    assert [p.split(":")[0] for p in gr.render(root, check=True).problems] == list(gr.GENERATED_FILES)
    assert gr.render(root).ok
    assert gr.render(root, check=True).ok


@pytest.mark.requirement("OWN-16", "OWN-17", "S00-ASG-27")
def test_render_check_detects_a_changed_markdown_log(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert gr.render(root).ok
    path = root / gr.DECISION_LOG_FILE
    _write(path, path.read_text(encoding="utf-8").replace("- Status: PROPOSED", "- Status: APPROVED"))
    report = gr.render(root, check=True)
    assert [problem.split(":")[0] for problem in report.problems] == [gr.STATUS_MD]


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize("file", gr.GENERATED_FILES)
@pytest.mark.parametrize(
    "tamper",
    [
        lambda content: content + b"\n",
        lambda content: content[:-1],
        lambda content: content.replace(b"\n", b"\r\n"),
        lambda content: content.replace(b"GENERATED", b"Generated", 1),
        lambda content: b"",
    ],
    ids=["extra newline", "missing final newline", "CRLF", "edited word", "emptied"],
)
def test_render_check_is_byte_for_byte(tmp_path: Path, file: str, tamper: Callable[[bytes], bytes]) -> None:
    root = make_repo(tmp_path)
    assert gr.render(root).ok
    path = root / file
    path.write_bytes(tamper(path.read_bytes()))
    report = gr.render(root, check=True)
    assert [problem.split(":")[0] for problem in report.problems] == [file]


# --------------------------------------------------------------------------- sync-evidence
SYNC_CASES: list[tuple[str, list[str], list[str], str, tuple[int, int, int] | None, str | None]] = [
    # name, outcomes of the marked tests, blockers, expected status, (passed, failed, skipped), problem
    ("no tests, no blockers", [], [], "planned", None, None),
    ("no tests, blockers", [], ["B-001"], "blocked", None, None),
    ("all passed", ["passed", "passed", "passed"], [], "passing", (3, 0, 0), None),
    ("all passed, blockers recorded", ["passed"], ["B-001"], "passing", (1, 0, 0), None),
    ("one failed", ["passed", "failed"], [], "failing", (1, 1, 0), None),
    ("one error", ["passed", "error"], [], "failing", (1, 1, 0), None),
    ("unexpected pass", ["passed", "xpassed"], [], "failing", (1, 1, 0), None),
    ("expected failure is not a pass", ["passed", "xfailed"], [], "failing", (1, 1, 0), None),
    ("only expected failures", ["xfailed", "xfailed"], [], "failing", (0, 2, 0), None),
    ("failed and skipped", ["failed", "skipped"], ["B-001"], "failing", (0, 1, 1), None),
    ("failed, passed and skipped", ["failed", "passed", "skipped"], [], "failing", (1, 1, 1), None),
    ("passed and skipped, blockers", ["passed", "skipped"], ["B-001"], "partial", (1, 0, 1), None),
    (
        "passed and skipped, no blockers",
        ["passed", "skipped"],
        [],
        "partial",
        (1, 0, 1),
        "needs a blocker id",
    ),
    ("all skipped, blockers", ["skipped", "skipped"], ["B-001"], "blocked", (0, 0, 2), None),
    ("all skipped, no blockers", ["skipped"], [], "blocked", (0, 0, 1), "needs a blocker id"),
    ("a test did not run", ["passed", "not_run"], ["B-001"], "partial", (1, 0, 1), "did not run"),
    ("no test ran", ["not_run"], ["B-001"], "blocked", (0, 0, 1), "did not run"),
]


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize("case", [pytest.param(case, id=case[0]) for case in SYNC_CASES])
def test_sync_evidence_derives_the_status(
    tmp_path: Path, case: tuple[str, list[str], list[str], str, tuple[int, int, int] | None, str | None]
) -> None:
    _name, outcomes, blockers, status, counts, problem = case
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(blockers=blockers))
    untouched = acceptance_of(root, "AT-FIX-ACC-02")
    evidence = write_evidence(root, widget_results(*outcomes))
    report = gr.sync_evidence(root, evidence)

    entry = acceptance_of(root, "AT-FIX-ACC-01")
    assert entry["status"] == status
    if counts is None:
        assert "evidence" not in entry
    else:
        passed, failed, skipped = counts
        assert entry["evidence"] == {
            "file": "evidence/pytest.json",
            "tests": len(outcomes),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }
    assert entry["blockers"] == blockers, "blocker ids are never invented or removed"
    assert acceptance_of(root, "AT-FIX-ACC-02") == untouched

    if problem is None:
        assert report.problems == []
        assert gr.validate(root).problems == [], "a clean sync leaves valid registers"
    else:
        assert len(report.problems) == 1, report.problems
        assert "AT-FIX-ACC-01" in report.problems[0]
        assert problem in report.problems[0]
    if status in ("partial", "blocked") and not blockers:
        assert f"'{status}' needs at least one id in 'blockers'" in only_problem(root)


@pytest.mark.requirement("OWN-18")
@_PROPERTY
@given(
    passed=st.integers(min_value=0, max_value=40),
    failed=st.integers(min_value=0, max_value=40),
    skipped=st.integers(min_value=0, max_value=40),
    has_blockers=st.booleans(),
)
def test_derived_status_properties(passed: int, failed: int, skipped: int, has_blockers: bool) -> None:
    status = gr.derive_acceptance_status(passed, failed, skipped, has_blockers)
    total = passed + failed + skipped
    assert status in ("planned", "passing", "failing", "blocked", "partial")
    assert (status == "passing") is (passed > 0 and failed == 0 and skipped == 0)
    assert (status == "failing") is (failed > 0)
    assert (status == "planned") is (total == 0 and not has_blockers)
    assert (status == "partial") is (failed == 0 and passed > 0 and skipped > 0)
    assert (status == "blocked") is (
        (total == 0 and has_blockers) or (failed == 0 and passed == 0 and skipped > 0)
    )


@pytest.mark.requirement("OWN-18")
def test_sync_never_leaves_passing_without_evidence(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert gr.sync_evidence(root, write_evidence(root, widget_results("passed", "passed"))).ok
    assert acceptance_of(root, "AT-FIX-ACC-01")["status"] == "passing"
    # A later run in which the marked tests no longer exist: the old counts must not survive.
    later = write_evidence(
        root,
        [result_record("tests/test_other.py::test_x", "passed", requirements=("FIX-ASG-01",))],
        "evidence/later.json",
    )
    report = gr.sync_evidence(root, later)
    assert report.problems == []
    entry = acceptance_of(root, "AT-FIX-ACC-01")
    assert entry["status"] == "planned"
    assert "evidence" not in entry
    assert gr.validate(root).problems == []


@pytest.mark.requirement("OWN-18")
def test_sync_replaces_a_hand_set_passing_status(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(status="passing"))
    assert "'passing' without recorded evidence" in only_problem(root)
    report = gr.sync_evidence(root, write_evidence(root, widget_results("passed", "failed")))
    assert report.problems == []
    entry = acceptance_of(root, "AT-FIX-ACC-01")
    assert entry["status"] == "failing"
    assert entry["evidence"]["failed"] == 1


@pytest.mark.requirement("OWN-18")
def test_sync_counts_only_tests_with_the_acceptance_marker_of_the_entry(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    tests = [
        result_record("tests/test_widget.py::test_a", "passed", acceptance=("FIX-ACC-01",)),
        result_record("tests/test_widget.py::test_b", "failed", requirements=("FIX-ACC-01",)),
        result_record("tests/test_supplier.py::test_c", "skipped", acceptance=("FIX-ACC-02",)),
        result_record("tests/test_both.py::test_d", "passed", acceptance=("FIX-ACC-01", "FIX-ACC-02")),
        result_record("tests/test_none.py::test_e", "failed"),
    ]
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(blockers=["B-001"]))
    report = gr.sync_evidence(root, write_evidence(root, tests))
    assert report.problems == []
    first = acceptance_of(root, "AT-FIX-ACC-01")
    assert first["status"] == "passing", (
        "a failing test that only carries the requirement marker is not counted"
    )
    assert first["evidence"] == {
        "file": "evidence/pytest.json",
        "tests": 2,
        "passed": 2,
        "failed": 0,
        "skipped": 0,
    }
    second = acceptance_of(root, "AT-FIX-ACC-02")
    assert second["status"] == "partial"
    assert second["evidence"] == {
        "file": "evidence/pytest.json",
        "tests": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 1,
    }
    assert any("AT-FIX-ACC-01" in note and "tests/test_both.py" in note for note in report.notes)


@pytest.mark.requirement("OWN-18")
def test_sync_writes_small_diffs_and_is_idempotent(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    header_before, blocks_before = blocks(before[gr.ACCEPTANCE_FILE].decode("utf-8"))
    evidence = write_evidence(root, widget_results("passed", "passed"))
    with_evidence = snapshot(root)
    assert gr.sync_evidence(root, evidence).ok

    after = snapshot(root)
    assert changed_files(with_evidence, after) == {gr.ACCEPTANCE_FILE}, "no other file, no temporary file"
    header_after, blocks_after = blocks(after[gr.ACCEPTANCE_FILE].decode("utf-8"))
    assert header_after == header_before
    assert list(blocks_after) == list(blocks_before)
    assert blocks_after["AT-FIX-ACC-02"] == blocks_before["AT-FIX-ACC-02"]
    expected = blocks_before["AT-FIX-ACC-01"].replace(
        "  status: planned\n",
        "  status: passing\n  evidence:\n    file: evidence/pytest.json\n    tests: 2\n    passed: 2\n"
        "    failed: 0\n    skipped: 0\n",
    )
    assert blocks_after["AT-FIX-ACC-01"] == expected

    again = gr.sync_evidence(root, evidence)
    assert again.problems == []
    assert again.messages == [f"{gr.ACCEPTANCE_FILE}: unchanged"]
    assert snapshot(root) == after


@pytest.mark.requirement("OWN-08", "OWN-18", "S00-ASG-28")
@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            result_record("tests/test_x.py::test_a", "passed", requirements=("FIX-ASG-01", "FIX-NOPE-01")),
            "requirement id 'FIX-NOPE-01'",
        ),
        (
            result_record("tests/test_x.py::test_a", "passed", acceptance=("FIX-ACC-77",)),
            "acceptance id 'FIX-ACC-77'",
        ),
        (
            result_record("tests/test_x.py::test_a", "passed", acceptance=("FIX-ASG-01",)),
            "acceptance id 'FIX-ASG-01' used in a test marker has no entry AT-FIX-ASG-01",
        ),
    ],
    ids=["unknown requirement id", "unknown acceptance id", "acceptance marker with a non-acceptance id"],
)
def test_sync_reports_marker_ids_that_are_not_in_the_registers(
    tmp_path: Path, record: dict[str, Any], expected: str
) -> None:
    root = make_repo(tmp_path)
    evidence = write_evidence(root, [*widget_results("passed"), record])
    report = gr.sync_evidence(root, evidence)
    assert len(report.problems) == 1, report.problems
    assert expected in report.problems[0]
    assert "tests/test_x.py::test_a" in report.problems[0]
    assert not report.ok
    assert acceptance_of(root, "AT-FIX-ACC-01")["status"] == "passing", "known results are still recorded"


@pytest.mark.requirement("OWN-18")
@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",
        '{"tests": []}',
        '{"schema_version": 2, "tests": []}',
        '{"schema_version": 1}',
        '{"schema_version": 1, "tests": {}}',
        '{"schema_version": 1, "tests": ["tests/test_widget.py::test_0"]}',
        '{"schema_version": 1, "tests": [{"requirements": [], "acceptance": [], "outcome": "passed"}]}',
        '{"schema_version": 1, "tests": [{"nodeid": "t::a", "requirements": [], "acceptance": [], '
        '"outcome": "ok"}]}',
        '{"schema_version": 1, "tests": [{"nodeid": "t::a", "requirements": [], "acceptance": [], '
        '"outcome": null}]}',
        '{"schema_version": 1, "tests": [{"nodeid": "t::a", "requirements": [], "acceptance": "FIX-ACC-01", '
        '"outcome": "passed"}]}',
        '{"schema_version": 1, "tests": [{"nodeid": "t::a", "acceptance": ["FIX-ACC-01"], '
        '"outcome": "passed"}]}',
        '{"schema_version": 1, "tests": ['
        '{"nodeid": "t::a", "requirements": [], "acceptance": ["FIX-ACC-01"], "outcome": "passed"}, '
        '{"nodeid": "t::a", "requirements": [], "acceptance": ["FIX-ACC-01"], "outcome": "passed"}]}',
    ],
)
def test_sync_refuses_a_malformed_evidence_file(tmp_path: Path, content: str) -> None:
    root = make_repo(tmp_path)
    # A recorded result and a blocker id: a sync that wrongly went on with "no tests" would change both.
    assert gr.sync_evidence(root, write_evidence(root, widget_results("passed"))).ok
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(blockers=["B-001"]))
    path = root / "evidence/broken.json"
    _write(path, content)
    before = snapshot(root)
    report = gr.sync_evidence(root, path)
    assert len(report.problems) == 1, report.problems
    assert "broken.json" in report.problems[0]
    assert "nothing was written" in report.problems[0]
    assert snapshot(root) == before


@pytest.mark.requirement("OWN-18")
def test_sync_refuses_missing_or_outside_evidence(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert gr.sync_evidence(root, write_evidence(root, widget_results("passed"))).ok
    edit_acceptance(root, "AT-FIX-ACC-02", lambda entry: entry.update(blockers=["B-001"]))
    before = snapshot(root)
    missing = gr.sync_evidence(root, root / "evidence/never_written.json")
    assert len(missing.problems) == 1
    assert "cannot read the evidence file" in missing.problems[0]

    outside = tmp_path / "outside.json"
    _write(outside, json.dumps({"schema_version": 1, "tests": widget_results("passed")}))
    report = gr.sync_evidence(root, outside)
    assert len(report.problems) == 1
    assert "must be inside the repository" in report.problems[0]
    assert snapshot(root) == before, "an evidence file the register cannot reference changes nothing"


@pytest.mark.requirement("OWN-18")
def test_sync_records_the_repository_relative_path_of_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repo(tmp_path)
    write_evidence(root, widget_results("passed"), "reviews/session_00/evidence/test_results.json")
    monkeypatch.chdir(root / "reviews")
    report = gr.sync_evidence(root, Path("session_00/evidence/test_results.json"))
    assert report.problems == []
    assert (
        acceptance_of(root, "AT-FIX-ACC-01")["evidence"]["file"]
        == "reviews/session_00/evidence/test_results.json"
    )


@pytest.mark.requirement("OWN-18")
def test_sync_accepts_evidence_written_by_the_real_conftest(tmp_path: Path) -> None:
    """The evidence format is the one of tests/conftest.py: run pytest on a SYNTHETIC FIXTURE test."""
    root = make_repo(tmp_path)
    project = tmp_path / "project"
    shutil.copytree(root, project)
    (project / "tests" / "__init__.py").write_bytes(b"")
    shutil.copyfile(REPO_ROOT / "tests" / "conftest.py", project / "tests" / "conftest.py")
    _write(
        project / "pytest.ini",
        "[pytest]\ntestpaths = tests\naddopts = --strict-markers\nmarkers =\n"
        "    requirement(*ids): synthetic fixture copy of the project marker\n"
        "    acceptance(id): synthetic fixture copy of the project marker\n",
    )
    _write(
        project / "tests/test_widget.py",
        "import pytest\n\n\n"
        '@pytest.mark.requirement("FIX-ASG-01")\n@pytest.mark.acceptance("FIX-ACC-01")\n'
        "def test_passes():\n    assert True\n\n\n"
        '@pytest.mark.requirement("FIX-ASG-01")\n@pytest.mark.acceptance("FIX-ACC-01")\n'
        'def test_blocked():\n    pytest.skip("BLOCKED: B-001 (synthetic fixture)")\n',
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--gie-evidence=evidence/pytest.json",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    edit_acceptance(project, "AT-FIX-ACC-01", lambda entry: entry.update(blockers=["B-001"]))
    report = gr.sync_evidence(project, project / "evidence/pytest.json")
    assert report.problems == []
    entry = acceptance_of(project, "AT-FIX-ACC-01")
    assert entry["status"] == "partial"
    assert entry["evidence"] == {
        "file": "evidence/pytest.json",
        "tests": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 1,
    }
    assert gr.validate(project).problems == []


# --------------------------------------------------------------------------- apply
@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_updates_status_review_status_and_notes(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    before = snapshot(root)
    patch = write_patch(
        root,
        [
            {
                "ids": ["FIX-ASG-02"],
                "status": "in_progress",
                "review_status": "conditional",
                "notes": "SYNTHETIC FIXTURE: work started.",
            }
        ],
    )
    report = gr.apply_patch(root, patch)
    assert report.problems == []
    entry = requirement_of(root, "FIX-ASG-02")
    assert entry["status"] == "in_progress"
    assert entry["review_status"] == "conditional"
    assert entry["notes"] == "SYNTHETIC FIXTURE: work started."
    assert entry["contributions"] == []
    assert changed_files(before, snapshot(root)) == {gr.MATRIX_FILE}
    assert gr.validate(root).problems == []


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("FIX-ASG-*", ["FIX-ASG-01", "FIX-ASG-02", "FIX-ASG-03", "FIX-ASG-04"]),
        ("FIX-ASG-0[12]", ["FIX-ASG-01", "FIX-ASG-02"]),
        ("FIX-A??-01", ["FIX-ASG-01", "FIX-ACC-01"]),
        ("*-02", ["FIX-ASG-02", "FIX-ACC-02"]),
        ("FIX-ASG-04", ["FIX-ASG-04"]),
        ("*", [e["id"] for e in _matrix_document()["requirements"]]),
    ],
)
def test_apply_selects_requirements_by_id_or_shell_style_glob(
    tmp_path: Path, pattern: str, expected: list[str]
) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    report = gr.apply_patch(root, write_patch(root, [{"ids": [pattern], "review_status": "approved"}]))
    assert report.problems == []
    approved = [
        entry["id"]
        for entry in load(root, gr.MATRIX_FILE)["requirements"]
        if entry["review_status"] == "approved"
    ]
    assert sorted(approved) == sorted(expected)


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["FIX-ASG-99"], "unknown requirement id 'FIX-ASG-99'"),
        (["fix-asg-01"], "unknown requirement id 'fix-asg-01'"),
        (["FIX-ASG-01 "], "unknown requirement id 'FIX-ASG-01 '"),
        (["NOPE-*"], "pattern 'NOPE-*' matches no requirement"),
        (["fix-*"], "pattern 'fix-*' matches no requirement"),
        (["FIX-ASG-0[9]"], "matches no requirement"),
    ],
)
def test_apply_is_all_or_nothing_on_unknown_ids(tmp_path: Path, ids: list[str], expected: str) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    before = snapshot(root)
    patch = write_patch(
        root,
        [
            {"ids": ["FIX-ASG-01"], "status": "in_progress"},
            {"ids": ["FIX-ASG-02", *ids], "status": "in_progress"},
            {"ids": ["FIX-ASG-04"], "notes": "SYNTHETIC FIXTURE: a valid update after the invalid one."},
        ],
    )
    report = gr.apply_patch(root, patch)
    assert any(expected in problem and "updates[1]" in problem for problem in report.problems), (
        report.problems
    )
    assert any("nothing was written" in problem for problem in report.problems)
    assert snapshot(root) == before, "not even the valid updates are written"


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_appends_or_replaces_the_contribution_of_a_session(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    first = contribution("S00", "in_progress", ["docs/widget.md"])
    assert gr.apply_patch(
        root, write_patch(root, [{"ids": ["FIX-ASG-02"], "status": "in_progress", "contribution": first}])
    ).ok
    assert requirement_of(root, "FIX-ASG-02")["contributions"] == [first]

    other = contribution("S01", "partial", ["docs", "docs/widget.md"], ["CR-001"])
    assert gr.apply_patch(root, write_patch(root, [{"ids": ["FIX-ASG-02"], "contribution": other}])).ok
    assert requirement_of(root, "FIX-ASG-02")["contributions"] == [first, other], (
        "another session is appended"
    )

    replacement = contribution("S00", "verified", ["tests/test_widget.py"])
    replacement["summary"] = "SYNTHETIC FIXTURE: the S00 part is finished."
    report = gr.apply_patch(
        root, write_patch(root, [{"ids": ["FIX-ASG-02"], "status": "partial", "contribution": replacement}])
    )
    assert report.problems == []
    entry = requirement_of(root, "FIX-ASG-02")
    assert entry["contributions"] == [replacement, other], "the same session is replaced in place"
    assert entry["status"] == "partial"
    assert requirement_of(root, "FIX-ASG-01")["contributions"] == []
    assert gr.validate(root).problems == []

    text = (root / gr.MATRIX_FILE).read_text(encoding="utf-8")
    assert "    blockers: [CR-001]\n" in text, "id lists stay in flow style"
    assert "    evidence:\n    - docs\n    - docs/widget.md\n" in text


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_gives_every_requirement_its_own_copy_of_a_contribution(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    shared = contribution("S00", "in_progress", ["docs/widget.md"], ["CR-001"])
    report = gr.apply_patch(
        root, write_patch(root, [{"ids": ["FIX-ASG-*"], "status": "in_progress", "contribution": shared}])
    )
    assert report.problems == []
    text = (root / gr.MATRIX_FILE).read_text(encoding="utf-8")
    assert "&id" not in text
    assert "*id" not in text
    for req_id in ("FIX-ASG-01", "FIX-ASG-02", "FIX-ASG-03", "FIX-ASG-04"):
        assert requirement_of(root, req_id)["contributions"] == [shared]


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_later_updates_override_earlier_ones(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    patch = write_patch(
        root,
        [
            {"ids": ["FIX-*"], "status": "in_progress", "notes": "SYNTHETIC FIXTURE: set by the glob."},
            {
                "ids": ["FIX-ASG-03", "FIX-ACC-02"],
                "status": "blocked",
                "contribution": contribution("S01", "blocked", [], ["B-001"]),
            },
        ],
    )
    report = gr.apply_patch(root, patch)
    assert report.problems == []
    statuses = {entry["id"]: entry["status"] for entry in load(root, gr.MATRIX_FILE)["requirements"]}
    assert statuses == {
        "FIX-GOV-01": "in_progress",
        "FIX-ASG-01": "in_progress",
        "FIX-ASG-02": "in_progress",
        "FIX-ASG-03": "blocked",
        "FIX-ACC-01": "in_progress",
        "FIX-ACC-02": "blocked",
        "FIX-ASG-04": "in_progress",
    }
    assert requirement_of(root, "FIX-ASG-03")["notes"] == "SYNTHETIC FIXTURE: set by the glob."


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_apply_refuses_to_leave_requirements_of_an_open_blocker_unblocked(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    report = gr.apply_patch(root, write_patch(root, [{"ids": ["FIX-ASG-01"], "status": "in_progress"}]))
    assert any("would not validate" in problem for problem in report.problems)
    assert any("FIX-ASG-03" in problem and "OPEN blocker B-001" in problem for problem in report.problems)
    assert any("FIX-ACC-02" in problem and "OPEN blocker B-001" in problem for problem in report.problems)
    assert snapshot(root) == before


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"status": "verified"}, "'verified' needs at least one contribution with an existing evidence path"),
        (
            {"status": "verified", "contribution": contribution("S00", "verified", [])},
            "'verified' needs at least one contribution with an existing evidence path",
        ),
        (
            {
                "status": "verified",
                "contribution": contribution("S00", "verified", ["docs/never_written.md"]),
            },
            "evidence path 'docs/never_written.md' does not exist",
        ),
        ({"status": "blocked"}, "'blocked' needs a blocker id"),
        (
            {"status": "blocked", "contribution": contribution("S00", "blocked", [], ["B-404"])},
            "references B-404, which is not a heading",
        ),
        ({"status": "blocked", "blockers": ["U-77"]}, "references U-77, which is not a heading"),
        ({"status": "done"}, "status 'done' is not one of"),
        ({"review_status": "fine"}, "review_status 'fine' is not one of"),
        ({"notes": ["a", "list"]}, "'notes' must be a string"),
        ({"blockers": "B-001"}, "'blockers' must be a list of ids"),
        ({"contribution": "S00 did it"}, "a contribution must be a mapping"),
        (
            {"contribution": {"session": "S00", "summary": "x", "status": "verified"}},
            "missing key 'evidence'",
        ),
        ({"contribution": contribution("S13", "verified", ["docs/widget.md"])}, "'session' must be one of"),
        (
            {"contribution": contribution("S00", "finished", ["docs/widget.md"])},
            "status 'finished' is not one of",
        ),
        ({"sessions": ["S05"]}, "unknown key 'sessions'"),
        ({"text": "A rewritten requirement."}, "unknown key 'text'"),
        ({"id": "FIX-ASG-77"}, "unknown key 'id'"),
        ({}, "nothing to change"),
    ],
)
def test_apply_refuses_a_state_that_validate_would_reject(
    tmp_path: Path, update: dict[str, Any], expected: str
) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    before = snapshot(root)
    report = gr.apply_patch(root, write_patch(root, [{"ids": ["FIX-ASG-02"], **update}]))
    assert any(expected in problem for problem in report.problems), report.problems
    assert any("nothing was written" in problem for problem in report.problems)
    assert snapshot(root) == before


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "document",
    [
        "updates: []\n",
        "updates: {}\n",
        "- ids: [FIX-ASG-01]\n  status: in_progress\n",
        "updates:\n- ids: [FIX-ASG-01]\n  status: in_progress\nextra: 1\n",
        "updates:\n- FIX-ASG-01\n",
        "updates:\n- status: in_progress\n",
        "updates:\n- ids: []\n  status: in_progress\n",
        "updates:\n- ids: FIX-ASG-01\n  status: in_progress\n",
        "updates:\n- ids: [7]\n  status: in_progress\n",
        "updates:\n- ids: ['']\n  status: in_progress\n",
        "updates:\n- ids: [FIX-ASG-01]\n  status: in_progress\n  status: verified\n",
        "updates: [unclosed\n",
        "",
    ],
)
def test_apply_refuses_a_malformed_patch(tmp_path: Path, document: str) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    before = snapshot(root)
    patch = root.parent / "patch.yaml"
    _write(patch, document)
    report = gr.apply_patch(root, patch)
    assert report.problems
    assert all("patch.yaml" in problem for problem in report.problems), report.problems
    assert snapshot(root) == before


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_refuses_a_missing_patch_file(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    before = snapshot(root)
    report = gr.apply_patch(root, tmp_path / "never_written.yaml")
    assert len(report.problems) == 1
    assert "never_written.yaml" in report.problems[0]
    assert snapshot(root) == before


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_never_deletes_or_adds_requirements(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    ids_before = [entry["id"] for entry in load(root, gr.MATRIX_FILE)["requirements"]]
    constant = {
        entry["id"]: {
            key: entry[key] for key in ("source", "section", "kind", "text", "source_clause", "sessions")
        }
        for entry in load(root, gr.MATRIX_FILE)["requirements"]
    }
    patch = write_patch(
        root,
        [
            {"ids": ["*"], "status": "in_progress", "review_status": "conditional", "notes": ""},
            {"ids": ["FIX-A*"], "contribution": contribution("S00", "in_progress", ["docs/widget.md"])},
            {"ids": ["FIX-GOV-01"], "status": "not_applicable", "notes": "SYNTHETIC FIXTURE: informational."},
        ],
    )
    assert gr.apply_patch(root, patch).problems == []
    after = load(root, gr.MATRIX_FILE)
    assert [entry["id"] for entry in after["requirements"]] == ids_before
    assert after["minimum_requirement_count"] == 7
    for entry in after["requirements"]:
        assert {key: entry[key] for key in constant[entry["id"]]} == constant[entry["id"]]
        assert list(entry) == list(_requirement("X-01", "k", ["S00"], "t")), "key order is unchanged"


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_requirement_level_blockers_are_placed_before_notes(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    patch = write_patch(
        root, [{"ids": ["FIX-ASG-03", "FIX-ACC-02"], "status": "blocked", "blockers": ["B-001", "U-01"]}]
    )
    report = gr.apply_patch(root, patch)
    assert report.problems == []
    entry = requirement_of(root, "FIX-ASG-03")
    assert entry["blockers"] == ["B-001", "U-01"]
    assert list(entry)[-2:] == ["blockers", "notes"]
    assert "  blockers: [B-001, U-01]\n" in (root / gr.MATRIX_FILE).read_text(encoding="utf-8")
    assert gr.validate(root).problems == []


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_writes_small_diffs_and_nothing_when_nothing_changes(tmp_path: Path) -> None:
    root = make_repo(tmp_path, blocker_status="RESOLVED")
    before = snapshot(root)
    header_before, blocks_before = blocks(before[gr.MATRIX_FILE].decode("utf-8"))
    item = contribution("S00", "verified", ["docs/widget.md"])
    patch = write_patch(root, [{"ids": ["FIX-ASG-02"], "status": "verified", "contribution": item}])
    report = gr.apply_patch(root, patch)
    assert report.problems == []
    assert "1 requirement(s) updated: FIX-ASG-02" in report.messages[0]

    after = snapshot(root)
    assert changed_files(before, after) == {gr.MATRIX_FILE}, "no other file, no temporary file"
    header_after, blocks_after = blocks(after[gr.MATRIX_FILE].decode("utf-8"))
    assert header_after == header_before
    assert list(blocks_after) == list(blocks_before)
    for req_id, block in blocks_before.items():
        if req_id != "FIX-ASG-02":
            assert blocks_after[req_id] == block
    expected = (
        blocks_before["FIX-ASG-02"]
        .replace("  status: not_started\n", "  status: verified\n")
        .replace(
            "  contributions: []\n",
            "  contributions:\n  - session: S00\n    summary: 'SYNTHETIC FIXTURE: what S00 delivered.'\n"
            "    status: verified\n    evidence:\n    - docs/widget.md\n",
        )
    )
    assert blocks_after["FIX-ASG-02"] == expected

    again = gr.apply_patch(root, patch)
    assert again.problems == []
    assert "unchanged" in again.messages[0]
    assert snapshot(root) == after


# --------------------------------------------------------------------------- stats
@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_stats_totals(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    start(root)
    assert gr.sync_evidence(root, write_evidence(root, widget_results("passed"))).ok
    before = snapshot(root)
    totals = gr.stats(root)
    assert snapshot(root) == before
    assert json.loads(json.dumps(totals)) == totals

    requirements = totals["requirements"]
    assert requirements["total"] == 7
    assert requirements["by_status"] == {
        "not_started": 1,
        "in_progress": 1,
        "partial": 1,
        "implemented": 1,
        "verified": 1,
        "blocked": 2,
        "not_applicable": 0,
    }
    assert list(requirements["by_status"]) == list(STATUS_VALUES), "statuses in the declared order"
    assert requirements["by_review_status"] == {
        "unverified": 7,
        "approved": 0,
        "conditional": 0,
        "rejected": 0,
    }
    assert list(requirements["by_session"]) == ALL_SESSIONS
    assert requirements["by_session"]["S00"]["total"] == 5
    assert requirements["by_session"]["S00"]["by_status"] == {
        "not_started": 1,
        "in_progress": 1,
        "partial": 1,
        "implemented": 1,
        "verified": 1,
        "blocked": 0,
        "not_applicable": 0,
    }
    assert requirements["by_session"]["S01"]["total"] == 4
    assert requirements["by_session"]["S01"]["by_status"]["blocked"] == 2
    assert requirements["by_session"]["S02"]["total"] == 2
    assert requirements["by_session"]["S12"] == {
        "total": 1,
        "by_status": {**dict.fromkeys(STATUS_VALUES, 0), "implemented": 1},
    }
    for session in ALL_SESSIONS:
        entry = requirements["by_session"][session]
        assert sum(entry["by_status"].values()) == entry["total"]

    acceptance = totals["acceptance"]
    assert acceptance["total"] == 2
    assert acceptance["by_status"] == {"planned": 1, "passing": 1, "failing": 0, "blocked": 0, "partial": 0}
    assert acceptance["by_session"]["S00"] == {
        "total": 1,
        "by_status": {"planned": 0, "passing": 1, "failing": 0, "blocked": 0, "partial": 0},
    }
    assert acceptance["by_session"]["S01"]["by_status"]["planned"] == 1
    assert acceptance["by_session"]["S02"]["total"] == 0


# --------------------------------------------------------------------------- command line
@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-17", "OWN-18", "S00-ASG-27", "S00-ASG-28")
def test_command_line_exit_codes_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path)
    arguments = ["--root", str(root)]

    assert gr.main(["validate", *arguments]) == 0
    output = capsys.readouterr().out
    assert "registers valid: 7 requirements, 2 acceptance tests" in output
    assert "\nNOTE: B-001 is OPEN" in output

    assert gr.main(["render", "--check", *arguments]) == 1
    assert "is missing" in capsys.readouterr().out
    assert gr.main(["render", *arguments]) == 0
    assert f"{gr.MATRIX_MD}: written" in capsys.readouterr().out
    assert gr.main(["render", "--check", *arguments]) == 0
    capsys.readouterr()

    evidence = write_evidence(root, widget_results("passed", "skipped"))
    assert gr.main(["sync-evidence", "--evidence", str(evidence), *arguments]) == 1
    output = capsys.readouterr().out
    assert "AT-FIX-ACC-01: planned -> partial (1 passed, 0 failed, 1 skipped)" in output
    assert "needs a blocker id" in output
    assert "1 problem(s)" in output
    assert gr.main(["validate", *arguments]) == 1
    assert "'partial' needs at least one id in 'blockers'" in capsys.readouterr().out
    edit_acceptance(root, "AT-FIX-ACC-01", lambda entry: entry.update(blockers=["B-001"]))
    assert gr.main(["sync-evidence", "--evidence", str(evidence), *arguments]) == 0
    capsys.readouterr()

    refused = write_patch(root, [{"ids": ["FIX-NOPE-01"], "status": "in_progress"}])
    assert gr.main(["apply", "--patch", str(refused), *arguments]) == 1
    assert "unknown requirement id 'FIX-NOPE-01'" in capsys.readouterr().out
    accepted = write_patch(
        root,
        [
            {
                "ids": ["FIX-ASG-03", "FIX-ACC-02"],
                "status": "blocked",
                "contribution": contribution("S01", "blocked", [], ["B-001"]),
            }
        ],
    )
    assert gr.main(["apply", "--patch", str(accepted), *arguments]) == 0
    assert "2 requirement(s) updated: FIX-ASG-03, FIX-ACC-02" in capsys.readouterr().out
    assert gr.main(["validate", *arguments]) == 0
    assert "NOTE:" not in capsys.readouterr().out

    assert gr.main(["render", "--check", *arguments]) == 1
    assert "is stale" in capsys.readouterr().out

    assert gr.main(["stats", *arguments]) == 0
    totals = json.loads(capsys.readouterr().out)
    assert totals == gr.stats(root)
    assert totals["requirements"]["by_status"]["blocked"] == 2
    assert totals["acceptance"]["by_status"]["partial"] == 1

    (root / gr.MATRIX_FILE).unlink()
    assert gr.main(["stats", *arguments]) == 1
    assert "the register file does not exist" in capsys.readouterr().out
    assert gr.main(["validate", *arguments]) == 1
    capsys.readouterr()


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["frobnicate"],
        ["sync-evidence"],
        ["apply"],
        ["validate", "--check"],
        ["render", "--evidence", "x"],
    ],
)
def test_command_line_usage_errors_exit_with_2(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        gr.main(arguments)
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_module_entry_point(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    command = [sys.executable, "-m", "tools.gie_registers", "validate", "--root", str(root)]
    valid = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "registers valid: 7 requirements" in valid.stdout
    assert "NOTE: B-001 is OPEN" in valid.stdout

    (root / gr.EXTRACTED_DIR / "FIXTURE_BRIEF.txt").write_bytes(
        b"SYNTHETIC FIXTURE: a changed specification.\n"
    )
    invalid = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert "the specification text changed" in invalid.stdout
    assert "1 problem(s)" in invalid.stdout


@pytest.mark.requirement("OWN-08", "OWN-17", "S00-ASG-27", "S00-ASG-28")
def test_output_survives_a_console_that_cannot_encode_a_character(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    decisions = root / gr.DECISION_LOG_FILE
    _write(
        decisions,
        decisions.read_text(encoding="utf-8")
        + "\n## D-003 - Caf\u00e9 \u2264 widget\n\n- Status: UNDECIDED\n",
    )
    environment = {**os.environ, "PYTHONIOENCODING": "ascii"}
    completed = subprocess.run(
        [sys.executable, "-m", "tools.gie_registers", "render", "--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Caf\u00e9 \u2264 widget" in (root / gr.STATUS_MD).read_text(encoding="utf-8"), "files stay UTF-8"

    _write(decisions, decisions.read_text(encoding="utf-8").replace("## D-003 - Caf", "## D-003: Caf"))
    completed = subprocess.run(
        [sys.executable, "-m", "tools.gie_registers", "validate", "--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        env=environment,
        check=False,
    )
    output = completed.stdout.decode("ascii")
    assert completed.returncode == 1
    assert b"Traceback" not in completed.stderr
    assert "looks like a register entry" in output
    assert "Caf" in output
    assert "1 problem(s)" in output


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_help_documents_the_patch_format(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        gr.main(["apply", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    for word in (
        "updates:",
        "ids:",
        "status:",
        "review_status:",
        "notes:",
        "contribution:",
        "evidence:",
        "blockers:",
    ):
        assert word in output


# --------------------------------------------------------------------------- the real registers
def _tolerated_on_the_real_repository(problem: str) -> bool:
    """Only a missing Markdown log / generated file may be reported for the real repository."""
    markdown = (*gr.LOG_FILES, *gr.GENERATED_FILES)
    return "does not exist" in problem and any(file in problem for file in markdown)


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-17", "OWN-18", "S00-ASG-27", "S00-ASG-28")
def test_real_repository_registers_load_and_validate() -> None:
    watched = [gr.MATRIX_FILE, gr.ACCEPTANCE_FILE, *gr.LOG_FILES, *gr.GENERATED_FILES]
    before = {file: (REPO_ROOT / file).read_bytes() for file in watched if (REPO_ROOT / file).is_file()}

    registers, loaded = gr.load_registers(REPO_ROOT)
    assert loaded.problems == []
    requirements = registers.matrix["requirements"]
    assert requirements
    assert len(requirements) >= registers.matrix.get("minimum_requirement_count", 0)
    ids = [entry["id"] for entry in requirements]
    assert len(set(ids)) == len(ids)
    for marker_id in ("OWN-08", "OWN-16", "OWN-17", "OWN-18", "S00-ASG-27", "S00-ASG-28"):
        assert marker_id in ids
    acceptance_requirements = [entry["id"] for entry in requirements if entry["kind"] == "acceptance"]
    assert sorted(entry["id"] for entry in registers.acceptance["acceptance_tests"]) == sorted(
        f"AT-{req_id}" for req_id in acceptance_requirements
    )

    report = gr.validate(REPO_ROOT)
    assert [p for p in report.problems if not _tolerated_on_the_real_repository(p)] == []
    assert report.structure_ok

    totals = gr.stats(REPO_ROOT)
    assert totals["requirements"]["total"] == len(requirements)
    assert sum(totals["requirements"]["by_status"].values()) == len(requirements)
    assert totals["acceptance"]["total"] == len(acceptance_requirements)

    after = {file: (REPO_ROOT / file).read_bytes() for file in watched if (REPO_ROOT / file).is_file()}
    assert after == before, "validate and stats only read the real repository"


@pytest.mark.requirement("OWN-08", "OWN-16", "OWN-18", "S00-ASG-28")
def test_real_registers_render_completely_and_deterministically(tmp_path: Path) -> None:
    root = copy_real_registers(tmp_path)
    registers, loaded = gr.load_registers(root)
    assert loaded.problems == []
    documents = gr.render_documents(registers)
    assert documents == gr.render_documents(gr.load_registers(root)[0])

    matrix_rows = {row[0]: row for row in _table_rows(documents[gr.MATRIX_MD])}
    for entry in registers.matrix["requirements"]:
        row = matrix_rows[entry["id"]]
        assert len(row) == 6, row
        assert row[3] == entry["status"]
        assert row[4] == entry["review_status"]
    sections = list(dict.fromkeys((e["source"], e["section"]) for e in registers.matrix["requirements"]))
    headings = [line for line in documents[gr.MATRIX_MD].splitlines() if line.startswith("## ")]
    assert headings[2:] == [f"## {source} - {section}" for source, section in sections]

    acceptance_rows = {row[0]: row for row in _table_rows(documents[gr.ACCEPTANCE_MD])}
    for entry in registers.acceptance["acceptance_tests"]:
        assert acceptance_rows[entry["id"]][2] == entry["status"]
        assert len(acceptance_rows[entry["id"]]) == 6

    assert gr.render(root).problems == []
    assert gr.render(root, check=True).problems == []
    for file, text in documents.items():
        assert (root / file).read_bytes() == text.encode("utf-8")
        assert gr.GENERATED_NOTICE in text


@pytest.mark.requirement("OWN-08", "S00-ASG-28")
def test_apply_on_a_copy_of_the_real_matrix_changes_only_the_intended_requirement(tmp_path: Path) -> None:
    root = copy_real_registers(tmp_path)
    before = snapshot(root)
    original = before[gr.MATRIX_FILE].decode("utf-8")
    header_before, blocks_before = blocks(original)
    note = "SYNTHETIC FIXTURE note written by test_gie_registers.py on a copy of the matrix."
    assert note not in original
    report = gr.apply_patch(root, write_patch(root, [{"ids": ["S00-ASG-28"], "notes": note}]))
    assert report.problems == []

    after = snapshot(root)
    assert changed_files(before, after) == {gr.MATRIX_FILE}
    header_after, blocks_after = blocks(after[gr.MATRIX_FILE].decode("utf-8"))
    assert header_after == header_before
    assert list(blocks_after) == list(blocks_before), "no requirement added, deleted or reordered"
    differing = [req_id for req_id, block in blocks_before.items() if blocks_after[req_id] != block]
    assert differing == ["S00-ASG-28"]
    old_lines = blocks_before["S00-ASG-28"].splitlines()
    new_lines = blocks_after["S00-ASG-28"].splitlines()
    notes_at = next(index for index, line in enumerate(old_lines) if line.startswith("  notes:"))
    assert new_lines[:notes_at] == old_lines[:notes_at], "only the notes of the requirement changed"
    assert requirement_of(root, "S00-ASG-28")["notes"] == note


@pytest.mark.requirement("OWN-18")
def test_sync_on_a_copy_of_the_real_register_changes_only_the_intended_entries(tmp_path: Path) -> None:
    root = copy_real_registers(tmp_path)
    before = snapshot(root)
    original = load(root, gr.ACCEPTANCE_FILE)
    header_before, blocks_before = blocks(before[gr.ACCEPTANCE_FILE].decode("utf-8"))
    # SYNTHETIC FIXTURE evidence: two invented passing results marked for S00-ACC-03, nothing else.
    tests = [
        result_record(f"tests/fixture/test_invented.py::test_{index}", "passed", acceptance=("S00-ACC-03",))
        for index in range(2)
    ]
    evidence = write_evidence(root, tests, "fixture_evidence/pytest.json")
    with_evidence = snapshot(root)
    report = gr.sync_evidence(root, evidence)
    assert report.problems == []

    # Expected register, derived independently from the documented rules.
    expected = copy.deepcopy(original)
    for entry in expected["acceptance_tests"]:
        entry.pop("evidence", None)
        if entry["requirement"] == "S00-ACC-03":
            entry["status"] = "passing"
            entry["evidence"] = {
                "file": "fixture_evidence/pytest.json",
                "tests": 2,
                "passed": 2,
                "failed": 0,
                "skipped": 0,
            }
        else:
            entry["status"] = "blocked" if entry["blockers"] else "planned"
    assert load(root, gr.ACCEPTANCE_FILE) == expected

    after = snapshot(root)
    assert changed_files(with_evidence, after) == {gr.ACCEPTANCE_FILE}
    header_after, blocks_after = blocks(after[gr.ACCEPTANCE_FILE].decode("utf-8"))
    assert header_after == header_before
    assert list(blocks_after) == list(blocks_before)
    intended = {
        new["id"]
        for old, new in zip(original["acceptance_tests"], expected["acceptance_tests"], strict=True)
        if old != new
    }
    assert "AT-S00-ACC-03" in intended
    assert {
        entry_id for entry_id, block in blocks_before.items() if blocks_after[entry_id] != block
    } == intended


# --------------------------------------------------------------------------- validate: owner's words only
@pytest.mark.requirement("OWN-03", "OWN-20", "S00-ASG-28")
@pytest.mark.parametrize(
    ("text", "reported"),
    [
        ("Build the widget (into risk).", ["into", "risk"]),
        ("Build the widget quickly.", ["quickly"]),
        ("Thresholds configurable.", ["configurable", "thresholds"]),
    ],
)
def test_a_gloss_in_the_requirement_text_is_a_problem(tmp_path: Path, text: str, reported: list[str]) -> None:
    root = make_repo(tmp_path)
    clause = "SYNTHETIC FIXTURE clause: Build the widget; test widgets under load."
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(text=text, source_clause=clause))
    problem = only_problem(root)
    assert "FIX-ASG-02" in problem
    assert "not in its source_clause" in problem
    assert f": {', '.join(reported)} (" in problem


@pytest.mark.requirement("OWN-03", "S00-ASG-28")
@pytest.mark.parametrize(
    "text",
    [
        "Build the widget.",  # a fragment
        "BUILD WIDGETS.",  # case and plural ending do not matter
        "Test a widget.",  # singular of a plural in the clause; function word
        "Widget is under load.",  # function words may connect the owner's words
        "Bid tests and ask tests at -1.75%.",  # '/' separates words; signed percentages are words
    ],
)
def test_fragments_of_the_source_clause_are_accepted(tmp_path: Path, text: str) -> None:
    root = make_repo(tmp_path)
    clause = "SYNTHETIC FIXTURE clause: Build the widget; test widgets under load; bid/ask tests at -1.75%."
    edit_requirement(root, "FIX-ASG-02", lambda entry: entry.update(text=text, source_clause=clause))
    assert problems(root) == []


@pytest.mark.requirement("OWN-03", "S00-ASG-28")
def test_words_not_in_clause_is_exact_about_what_it_reports() -> None:
    clause = "Aggregate gross/net and correlated risk, pending orders, margin, costs and gap stress."
    assert gr.words_not_in_clause("Aggregate margin.", clause) == []
    assert gr.words_not_in_clause("Aggregate margin (into exposure).", clause) == ["exposure", "into"]
    assert gr.words_not_in_clause("Aggregate net risk.", clause) == []
    assert gr.words_not_in_clause("Limit - orders.", "pending orders") == ["limit"]
    assert set(gr.TEXT_GLUE_WORDS) >= {"a", "the", "of", "and", "is"}
    assert "not" in gr.TEXT_GLUE_WORDS  # negations can only reuse the owner's own 'no' / 'not'


@pytest.mark.requirement("OWN-03", "OWN-20", "S00-ASG-28")
def test_every_real_requirement_text_comes_from_its_source_clause() -> None:
    document = yaml.safe_load((gr.REPO_ROOT / gr.MATRIX_FILE).read_text(encoding="utf-8"))
    offenders = {
        entry["id"]: added
        for entry in document["requirements"]
        if (added := gr.words_not_in_clause(entry["text"], entry["source_clause"]))
    }
    assert offenders == {}
