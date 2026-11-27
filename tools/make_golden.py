"""Golden contract messages (acceptance test S00-ACC-01 / S00-ACC-02).

A golden file is the canonical JSON of one labelled synthetic sample record
(``tests/gie/samples.py``), stored under
``tests/gie/golden/<Contract>-<version>/<variant>.json``.  Golden files of a
released contract version are the compatibility reference: every later reader of
the same MAJOR version must decode them.  A directory of another MAJOR version
than the current one is not decoded here (the current reader does not read it;
that is the job of the migration tool that a MAJOR change must ship) - it is
listed as a note, never silently ignored.

``tests/gie/golden/traces/run_story.jsonl`` is a golden *trace*: the records of
one coherent synthetic run (``samples.run_story()``) in journal order, one
canonical message per line.  Single messages cannot show that the life-cycle
rules agree with each other; the trace must be accepted, in order, by a fresh
journal with its referential ledger.

``--write`` adds missing files.  An existing file is only rewritten with
``--force`` and only while the schema lock is still ``PROPOSED`` (before owner
approval); afterwards a changed message means a new contract version and a new
directory.  ``--check`` verifies that every sample variant has a golden file,
that every golden file decodes with the current code, that every golden file is
a message of the contract and version its directory is named after, that
golden files of the current version re-encode byte for byte, and that the
golden trace equals the current encoding of the sample run and is accepted by
the journal.  Files are read and written as bytes (LF line ends on every
platform).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from gie.contracts import MemoryJournal, Record, from_json, to_json
from gie.contracts.registry import all_contracts
from gie.contracts.schema_gen import APPROVED, load_lock
from gie.core.canonical import strict_loads
from gie.core.errors import GieError
from tests.gie import samples

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "gie" / "golden"
#: Directory (below the golden root) and file of the golden run trace.
TRACE_DIR = "traces"
TRACE_FILE = "run_story.jsonl"

#: A golden directory is named ``<Contract>-<MAJOR.MINOR.PATCH>``.
_DIRECTORY_RE = re.compile(r"(?P<contract>[A-Za-z][A-Za-z0-9]*)-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")


def golden_path(contract: str, version: str, variant: str, root: Path = GOLDEN_DIR) -> Path:
    return root / f"{contract}-{version}" / f"{variant}.json"


def expected_files(root: Path = GOLDEN_DIR) -> dict[Path, str]:
    """Golden path -> canonical text for every sample variant at the current contract versions."""
    files: dict[Path, str] = {}
    contracts = all_contracts()
    for contract, by_variant in samples.variants().items():
        version = contracts[contract].VERSION
        assert version is not None
        for variant, record in by_variant.items():
            files[golden_path(contract, version, variant, root)] = to_json(record) + "\n"
    return files


def envelope_problem(directory: str, text: str) -> str | None:
    """Why the message *text* is not a golden message of *directory*, or ``None`` when it is.

    A golden file is the compatibility reference of exactly the contract version its directory
    names; a message of another contract or another version proves nothing about that version.
    """
    match = _DIRECTORY_RE.fullmatch(directory)
    if match is None:
        return "directory is not named <Contract>-<MAJOR.MINOR.PATCH>"
    message = strict_loads(text)
    contract, version = message.get("contract"), message.get("schema_version")
    if (contract, version) != (match["contract"], match["version"]):
        return (
            f"is a message of {contract} {version}, "
            f"not of the contract version of its directory ({match['contract']} {match['version']})"
        )
    return None


def _read(path: Path) -> str:
    """The file as text without newline translation (a CRLF file is a different file)."""
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def trace_path(root: Path = GOLDEN_DIR) -> Path:
    return root / TRACE_DIR / TRACE_FILE


def expected_trace() -> str:
    """The golden run trace: every record of the sample run, one canonical message per line."""
    return "".join(to_json(record) + "\n" for record in samples.run_story())


def other_major_directories(root: Path = GOLDEN_DIR) -> list[str]:
    """Golden directories of another MAJOR version than the current contract (not decoded here)."""
    contracts = all_contracts()
    skipped: list[str] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        match = _DIRECTORY_RE.fullmatch(directory.name)
        current = contracts.get(match["contract"]) if match else None
        if match is None or current is None or current.VERSION is None:
            continue
        if match["version"].split(".")[0] != current.VERSION.split(".")[0]:
            skipped.append(directory.name)
    return skipped


def trace_problems(root: Path = GOLDEN_DIR) -> list[str]:
    """Problems of the golden run trace (missing, changed, undecodable, refused by the journal)."""
    path = trace_path(root)
    name = f"{TRACE_DIR}/{TRACE_FILE}"
    if not path.exists():
        return [f"{name}: golden trace is missing (run --write)"]
    text = _read(path)
    problems: list[str] = []
    if text != expected_trace():
        problems.append(
            f"{name}: the sample run no longer encodes to the golden trace (contract or sample changed)"
        )
    journal = MemoryJournal()
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            journal.publish(from_json(line))
        except GieError as exc:
            problems.append(f"{name}:{number}: not accepted by the journal: {exc}")
            break
    return problems


def check(root: Path = GOLDEN_DIR) -> list[str]:
    problems: list[str] = []
    expected = expected_files(root)
    for path, text in sorted(expected.items()):
        if not path.exists():
            problems.append(f"{path.relative_to(root)}: golden file is missing (run --write)")
        elif _read(path) != text:
            problems.append(
                f"{path.relative_to(root)}: sample no longer encodes to the golden bytes "
                "(contract or sample changed)"
            )
    current_dirs = {path.parent for path in expected}
    skipped = set(other_major_directories(root))
    for path in sorted(root.glob("*/*.json")):
        if path.parent.name in skipped:
            continue
        text = _read(path)
        try:
            record: Record = from_json(text)
        except Exception as exc:
            problems.append(f"{path.relative_to(root)}: no longer decodes: {exc}")
            continue
        mismatch = envelope_problem(path.parent.name, text)
        if mismatch is not None:
            problems.append(f"{path.relative_to(root)}: {mismatch}")
        if path.parent in current_dirs and to_json(record) + "\n" != text:
            problems.append(f"{path.relative_to(root)}: does not re-encode byte for byte")
        if path.parent in current_dirs and path not in expected:
            problems.append(f"{path.relative_to(root)}: golden file has no sample variant")
    problems.extend(trace_problems(root))
    return problems


def write(force: bool, root: Path = GOLDEN_DIR) -> list[str]:
    approved = load_lock().get("approval_status") == APPROVED
    written: list[str] = []
    files = {**expected_files(root), trace_path(root): expected_trace()}
    for path, text in sorted(files.items()):
        if path.exists():
            if _read(path) == text:
                continue
            if not force:
                raise SystemExit(f"{path} differs from the sample; use --force (only before approval)")
            if approved:
                raise SystemExit(
                    f"{path} belongs to an approved contract version and may not be rewritten; "
                    "bump the contract version instead"
                )
        _write(path, text)
        written.append(str(path.relative_to(root)))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain golden contract messages.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    parser.add_argument("--force", action="store_true", help="rewrite changed files (PROPOSED lock only)")
    args = parser.parse_args(argv)
    if args.write:
        for name in write(args.force):
            print(f"wrote {name}")
    problems = check()
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    for directory in other_major_directories():
        print(f"NOTE: {directory}: another MAJOR version than the current contract - not decoded here")
    if not problems:
        print(
            f"golden messages consistent: {len(expected_files())} files and the run trace "
            f"({len(samples.run_story())} records)"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
