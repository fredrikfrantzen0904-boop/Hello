"""JSON Schema publication for the contracts (deliverable S00-DEL-02).

``python -m gie.contracts.schema_gen --write`` regenerates the schema files and
the lock; ``--check`` (run in CI) fails when the committed files differ from
what the code generates, when a released schema was edited, or when a contract
changed without the version bump the evolution rules require.

The schemas are JSON Schema 2020-12.  Keywords starting with ``x-gie-`` carry
what plain JSON Schema cannot express: decimal bounds, units, canonical order
and cross-field invariants.  A message must be validated against the schema of
the version it declares.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

import gie.contracts  # noqa: F401  (registers all contracts)
from gie.contracts._spec import ENVELOPE_CONTRACT, ENVELOPE_VERSION, Record, object_schema
from gie.contracts.behaviour import behaviour
from gie.contracts.compat import check_evolution
from gie.contracts.registry import SEMVER_PATTERN, all_contracts, parse_semver

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "schemas"
LOCK_NAME: Final[str] = "LOCK.json"
SCHEMA_DIALECT: Final[str] = "https://json-schema.org/draft/2020-12/schema"
PROPOSED: Final[str] = "PROPOSED"
APPROVED: Final[str] = "APPROVED"


def json_schema(record_type: type[Record]) -> dict[str, Any]:
    """JSON Schema of one top-level contract at its current version."""
    name, version = record_type.CONTRACT, record_type.VERSION
    if name is None or version is None:
        raise ValueError(f"{record_type.__name__} is not a top-level contract")
    defs: dict[str, Any] = {}
    body = object_schema(record_type, defs)
    properties = {
        ENVELOPE_CONTRACT: {"const": name, "description": "Contract name."},
        ENVELOPE_VERSION: {
            "type": "string",
            "pattern": SEMVER_PATTERN,
            "description": "Version of the contract the message was written with.",
        },
    }
    properties.update(body["properties"])
    body["properties"] = properties
    body["required"] = [ENVELOPE_CONTRACT, ENVELOPE_VERSION, *body["required"]]
    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"urn:gie:contract:{name}:{version}",
        "title": f"{name} {version}",
    }
    schema.update(body)
    schema["$comment"] = (
        "Necessary but not sufficient: decimal bounds (x-gie-*), canonical order, cross-field "
        "invariants (x-gie-invariants), lifecycle rules (x-gie-behaviour), exact 64-bit integer "
        "tokens and printable-text rules are enforced by the reference implementation gie.contracts."
    )
    schema["x-gie-behaviour"] = behaviour(record_type)
    if defs:
        schema["$defs"] = {key: defs[key] for key in sorted(defs)}
    return schema


#: The routing-lock vocabulary as it must appear in every schema that has the field.
ROUTING_VOCABULARY: Final[dict[str, list[str]]] = {"mode": ["REPLAY", "PAPER"], "venue": ["SIMULATED"]}


def routing_vocabulary_problems(name: str, schema: dict[str, Any]) -> list[str]:
    """Problems with the run-mode / venue vocabulary of a schema (independent of the lock status)."""
    problems = []
    for field, expected in ROUTING_VOCABULARY.items():
        fragment = schema.get("properties", {}).get(field)
        if fragment is not None and fragment.get("enum") != expected:
            problems.append(
                f"{name}: routing-lock vocabulary changed: {field} must be exactly {expected} "
                f"(found {fragment.get('enum')})"
            )
    return problems


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def schema_filename(name: str, version: str) -> str:
    return f"{name}-{version}.schema.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_lock(schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    path = schema_dir / LOCK_NAME
    if not path.exists():
        return {"lock_version": 1, "approval_status": PROPOSED, "contracts": {}}
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def load_schema(name: str, version: str, schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (schema_dir / schema_filename(name, version)).read_text(encoding="utf-8")
    )
    return loaded


def check(schema_dir: Path = SCHEMA_DIR) -> list[str]:
    """Return every problem found; an empty list means the published schemas are consistent."""
    problems: list[str] = []
    lock = load_lock(schema_dir)
    locked: dict[str, dict[str, str]] = lock.get("contracts", {})
    if lock.get("approval_status") not in (PROPOSED, APPROVED):
        problems.append("LOCK.json: approval_status must be PROPOSED or APPROVED")
    for locked_name, versions in sorted(locked.items()):
        for locked_version, digest in sorted(versions.items()):
            path = schema_dir / schema_filename(locked_name, locked_version)
            if not path.exists():
                problems.append(f"{path.name}: locked schema file is missing")
            elif _sha256(path.read_text(encoding="utf-8")) != digest:
                problems.append(f"{path.name}: released schema was modified (hash differs from LOCK.json)")
    expected_files = {LOCK_NAME}
    for name, record_type in all_contracts().items():
        version = record_type.VERSION
        assert version is not None
        current_schema = json_schema(record_type)
        problems.extend(routing_vocabulary_problems(name, current_schema))
        text = render(current_schema)
        path = schema_dir / schema_filename(name, version)
        expected_files.add(path.name)
        if path.exists():
            problems.extend(
                routing_vocabulary_problems(path.name, json.loads(path.read_text(encoding="utf-8")))
            )
        if not path.exists():
            problems.append(f"{path.name}: schema of the current version is not published")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(
                f"{path.name}: committed schema differs from the code "
                "(contract changed without a version bump?)"
            )
        if version not in locked.get(name, {}):
            problems.append(f"LOCK.json: {name} {version} is not locked")
        current = parse_semver(version)
        for old_version in sorted(locked.get(name, {})):
            expected_files.add(schema_filename(name, old_version))
            old = parse_semver(old_version)
            if old == current:
                continue
            if old > current:
                problems.append(f"{name}: locked version {old_version} is newer than the code ({version})")
                continue
            old_path = schema_dir / schema_filename(name, old_version)
            if old.major == current.major and old_path.exists():
                problems.extend(
                    f"{name}: {problem}"
                    for problem in check_evolution(
                        load_schema(name, old_version, schema_dir),
                        old_version,
                        json_schema(record_type),
                        version,
                    )
                )
    for removed in sorted(set(locked) - set(all_contracts())):
        problems.append(
            f"LOCK.json: contract {removed} is locked but no longer registered (contracts are never removed)"
        )
    for path in sorted(schema_dir.glob("*.json")):
        if path.name not in expected_files:
            problems.append(f"{path.name}: unexpected file in the schema directory")
    return problems


def write(schema_dir: Path = SCHEMA_DIR) -> list[str]:
    """Publish the current schemas.  Returns the names of the files written.

    A locked schema may only be rewritten while the lock is ``PROPOSED`` (before
    the owner approved the contract set).  After approval every change needs a
    new version, which adds a new file and leaves released files untouched.
    """
    schema_dir.mkdir(parents=True, exist_ok=True)
    lock = load_lock(schema_dir)
    locked: dict[str, dict[str, str]] = lock.setdefault("contracts", {})
    approved = lock.get("approval_status") == APPROVED
    written: list[str] = []
    for name, record_type in all_contracts().items():
        version = record_type.VERSION
        assert version is not None
        text = render(json_schema(record_type))
        digest = _sha256(text)
        path = schema_dir / schema_filename(name, version)
        previous = locked.setdefault(name, {}).get(version)
        if previous is not None and previous != digest and approved:
            raise SystemExit(
                f"{path.name} is released and approved; bump the version of {name} instead of editing it"
            )
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            written.append(path.name)
        locked[name][version] = digest
    lock_text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    lock_path = schema_dir / LOCK_NAME
    if not lock_path.exists() or lock_path.read_text(encoding="utf-8") != lock_text:
        lock_path.write_text(lock_text, encoding="utf-8")
        written.append(LOCK_NAME)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="verify the committed schemas")
    group.add_argument("--write", action="store_true", help="regenerate schemas and lock")
    args = parser.parse_args(argv)
    if args.write:
        for name in write():
            print(f"wrote {name}")
    problems = check()
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if not problems:
        print(f"contract schemas consistent: {len(all_contracts())} contracts")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
