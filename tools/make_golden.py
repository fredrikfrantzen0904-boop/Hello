"""Golden contract messages (acceptance test S00-ACC-01 / S00-ACC-02).

A golden file is the canonical JSON of one labelled synthetic sample record
(``tests/gie/samples.py``), stored under
``tests/gie/golden/<Contract>-<version>/<variant>.json``.  Golden files of a
released contract version are the compatibility reference: every later reader of
the same MAJOR version must decode them, forever.

``--write`` adds missing files.  An existing file is only rewritten with
``--force`` and only while the schema lock is still ``PROPOSED`` (before owner
approval); afterwards a changed message means a new contract version and a new
directory.  ``--check`` verifies that every sample variant has a golden file,
that every golden file decodes with the current code, and that golden files of
the current version re-encode byte for byte.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gie.contracts import Record, from_json, to_json
from gie.contracts.registry import all_contracts
from gie.contracts.schema_gen import APPROVED, load_lock
from tests.gie import samples

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "gie" / "golden"


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


def check(root: Path = GOLDEN_DIR) -> list[str]:
    problems: list[str] = []
    expected = expected_files(root)
    for path, text in sorted(expected.items()):
        if not path.exists():
            problems.append(f"{path.relative_to(root)}: golden file is missing (run --write)")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(
                f"{path.relative_to(root)}: sample no longer encodes to the golden bytes "
                "(contract or sample changed)"
            )
    current_dirs = {path.parent for path in expected}
    for path in sorted(root.glob("*/*.json")):
        text = path.read_text(encoding="utf-8")
        try:
            record: Record = from_json(text)
        except Exception as exc:
            problems.append(f"{path.relative_to(root)}: no longer decodes: {exc}")
            continue
        if path.parent in current_dirs and to_json(record) + "\n" != text:
            problems.append(f"{path.relative_to(root)}: does not re-encode byte for byte")
        if path.parent in current_dirs and path not in expected:
            problems.append(f"{path.relative_to(root)}: golden file has no sample variant")
    return problems


def write(force: bool, root: Path = GOLDEN_DIR) -> list[str]:
    approved = load_lock().get("approval_status") == APPROVED
    written: list[str] = []
    for path, text in sorted(expected_files(root).items()):
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                continue
            if not force:
                raise SystemExit(f"{path} differs from the sample; use --force (only before approval)")
            if approved:
                raise SystemExit(
                    f"{path} belongs to an approved contract version and may not be rewritten; "
                    "bump the contract version instead"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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
    if not problems:
        print(f"golden messages consistent: {len(expected_files())} files")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
