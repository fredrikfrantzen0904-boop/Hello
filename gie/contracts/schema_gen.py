"""JSON Schema publication for the contracts (deliverable S00-DEL-02).

``python -m gie.contracts.schema_gen --check`` (run in CI) fails when the
committed files differ from what the code generates, when a released schema was
edited, or when a contract changed without the version bump the evolution rules
require.  This module computes and checks; it writes nothing (code below
``gie/`` does not write files - only the journal does).  The files are written
by ``python -m tools.write_schemas``, which takes their content from
:func:`planned_files`.

The schemas are JSON Schema 2020-12.  Keywords starting with ``x-gie-`` carry
what plain JSON Schema cannot express: decimal bounds, units, canonical order
and cross-field invariants.  A message must be validated against the schema of
the version it declares.

Every object is closed and complete: ``additionalProperties: false`` together
with ``minProperties`` = number of declared properties, because every field is
always written (optional ones as explicit ``null``).  ``required`` lists the
fields that may not be ``null``.

The lock records the SHA-256 of the *bytes* of each schema file.  Files are
written with LF line ends on every platform, so the hashes are the same
everywhere and a file whose line ends were converted is reported as modified.
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
from gie.contracts.compat import ROUTING_VOCABULARY, check_evolution
from gie.contracts.registry import SEMVER_PATTERN, all_contracts, parse_semver
from gie.core.errors import UnknownContractError

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "schemas"
LOCK_NAME: Final[str] = "LOCK.json"
SCHEMA_DIALECT: Final[str] = "https://json-schema.org/draft/2020-12/schema"
PROPOSED: Final[str] = "PROPOSED"
APPROVED: Final[str] = "APPROVED"


def json_schema(record_type: type[Record]) -> dict[str, Any]:
    """JSON Schema of one top-level contract at its current version."""
    name, version = record_type.CONTRACT, record_type.VERSION
    if name is None or version is None:
        raise UnknownContractError(
            f"{record_type.__name__} is not a top-level contract", contract=record_type.__name__
        )
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
    body["minProperties"] = len(properties)  # every key is written: envelope and all fields
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


def _file_bytes(text: str) -> bytes:
    """Bytes of a published file: UTF-8, LF line ends on every platform."""
    return text.encode("utf-8")


def schema_filename(name: str, version: str) -> str:
    return f"{name}-{version}.schema.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_lock(schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    path = schema_dir / LOCK_NAME
    if not path.exists():
        return {"lock_version": 1, "approval_status": PROPOSED, "contracts": {}}
    loaded: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    return loaded


def load_schema(name: str, version: str, schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (schema_dir / schema_filename(name, version)).read_bytes().decode("utf-8")
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
            elif _sha256(path.read_bytes()) != digest:
                problems.append(f"{path.name}: released schema was modified (hash differs from LOCK.json)")
    expected_files = {LOCK_NAME}
    for name, record_type in all_contracts().items():
        version = record_type.VERSION
        assert version is not None
        current_schema = json_schema(record_type)
        problems.extend(routing_vocabulary_problems(name, current_schema))
        data = _file_bytes(render(current_schema))
        path = schema_dir / schema_filename(name, version)
        expected_files.add(path.name)
        if path.exists():
            problems.extend(
                routing_vocabulary_problems(path.name, json.loads(path.read_bytes().decode("utf-8")))
            )
        if not path.exists():
            problems.append(f"{path.name}: schema of the current version is not published")
        elif path.read_bytes() != data:
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


def planned_files(schema_dir: Path = SCHEMA_DIR) -> dict[str, bytes]:
    """File name -> bytes of every schema of the current versions, and of the lock that follows.

    The lock comes last.  Nothing is written here; ``tools/write_schemas.py`` writes the files.
    A locked schema may only change while the lock is ``PROPOSED`` (before the owner approved the
    contract set).  After approval every change needs a new version, which adds a new file and
    leaves released files untouched; an attempt to change a released file ends here.
    """
    lock = load_lock(schema_dir)
    locked: dict[str, dict[str, str]] = lock.setdefault("contracts", {})
    approved = lock.get("approval_status") == APPROVED
    files: dict[str, bytes] = {}
    for name, record_type in all_contracts().items():
        version = record_type.VERSION
        assert version is not None
        data = _file_bytes(render(json_schema(record_type)))
        digest = _sha256(data)
        filename = schema_filename(name, version)
        previous = locked.setdefault(name, {}).get(version)
        if previous is not None and previous != digest and approved:
            raise SystemExit(
                f"{filename} is released and approved; bump the version of {name} instead of editing it"
            )
        files[filename] = data
        locked[name][version] = digest
    files[LOCK_NAME] = _file_bytes(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--check",
        action="store_true",
        required=True,
        help="verify the committed schemas (they are written by: python -m tools.write_schemas)",
    )
    parser.parse_args(argv)
    problems = check()
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if not problems:
        print(f"contract schemas consistent: {len(all_contracts())} contracts")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
