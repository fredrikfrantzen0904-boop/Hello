"""Freeze and verify the baselines of the existing system (baseline regression harness).

Requirements: G02-08 "Preserve GCE-B", G02-09 "Preserve exact Round 1 baselines", M-AUTH-12 (the
GCE baselines are not modified), S00-ACC-05 "Baseline regression" and S00-ACC-06 "No production
changes" (helper `compare_production_manifests`).

The artifacts of the baselines live on the owner's machine, in the production tree named in rule 2
of ``CLAUDE.md``; they are not reachable from the development session (blocker B-001).  This module
never names that location: the directory that holds a baseline is always given on the command line
(``--root``) or through the environment variable named by the registry entry (``root_env``).

Procedure (see ``specifications/baselines/README.md``)::

    python -m tools.baseline_freeze freeze --id ID --root DIR --include GLOB [--include GLOB ...]
    python -m tools.baseline_freeze freeze ... --replace --decision D-nnn
    python -m tools.baseline_freeze verify [--id ID] [--root DIR]
    python -m tools.baseline_freeze status

Every command accepts ``--repo-root`` (default: this repository) so that tests work on a copy.

``freeze`` builds the SHA-256 manifest of the files of one baseline with
`tools.audit.gie_baseline_audit.build_manifest` (nothing excluded, every file hashed - names that
suggest secrets included, no size cap), writes ``specifications/baselines/frozen/<id>.manifest.jsonl``
and ``<id>.summary.json`` and marks the registry entry ``frozen``.  It refuses

* a baseline id that is not in the registry, an include pattern that is absolute, contains ``..``
  or matches no file;
* a file that could not be hashed or changed while it was read, and a symbolic link, special file or
  unreadable directory that an include pattern could reach (a baseline with an entry whose content
  is not pinned is not frozen);
* a root inside ``specifications/baselines/frozen`` and a root that contains any file this command
  writes (the baseline tree is only read, never written);
* overwriting an existing frozen manifest unless ``--replace`` is given together with
  ``--decision D-nnn`` naming an entry of ``docs/DECISION_LOG.md``; the decision id is recorded.

``verify`` rebuilds the manifest of every frozen baseline (or of ``--id``) with the recorded include
patterns and compares it with the frozen one.  Exit status:

* 0 - every selected baseline is frozen, its root is available and it is byte-identical (same paths,
  sizes and SHA-256; modification times are reported but do not count);
* 1 - at least one baseline differs, or its frozen manifest is missing or corrupt;
* 2 - arguments refused (unknown id, invalid registry, ``--root`` without ``--id``);
* 3 - nothing differs, but at least one selected baseline is ``UNVERIFIED``: not frozen yet, or its
  root is not available on this machine.  Never 0.

All files are written as bytes with LF line ends.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from tools.audit.gie_baseline_audit import (
    TOOL_VERSION as AUDIT_TOOL_VERSION,
)
from tools.audit.gie_baseline_audit import (
    AuditRefused,
    build_manifest,
    compare_manifests,
    read_manifest,
    write_manifest,
)

__all__ = [
    "DEFAULT_ROOT_ENV",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNVERIFIED",
    "FROZEN_REL",
    "PRODUCTION_MANIFEST_AFTER_ENV",
    "PRODUCTION_MANIFEST_BEFORE_ENV",
    "REGISTRY_REL",
    "BaselineRefused",
    "build_baseline_manifest",
    "compare_production_manifests",
    "freeze",
    "load_registry",
    "main",
    "render_registry",
    "resolve_production_manifests",
    "status",
    "validate_registry",
    "verify",
    "verify_baseline",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_REL = "specifications/baselines/registry.yaml"
FROZEN_REL = "specifications/baselines/frozen"
DECISION_LOG_REL = "docs/DECISION_LOG.md"
DEFAULT_ROOT_ENV = "GIE_BASELINE_ROOT"
PRODUCTION_MANIFEST_BEFORE_ENV = "GIE_PRODUCTION_MANIFEST_BEFORE"
PRODUCTION_MANIFEST_AFTER_ENV = "GIE_PRODUCTION_MANIFEST_AFTER"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_REFUSED = 2
EXIT_UNVERIFIED = 3

REGISTRY_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
STATUSES = ("not_frozen", "frozen")
ENTRY_KEYS = (
    "id",
    "title",
    "description",
    "requirement_ids",
    "root_env",
    "include",
    "manifest",
    "status",
    "blocker",
)
OPTIONAL_ENTRY_KEYS = ("decision",)

_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_BLOCKER = re.compile(r"^B-[0-9]{3}$")
_DECISION = re.compile(r"^D-[0-9]{3}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_REQUIREMENT_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]*-[0-9]{2}$")
_WILDCARDS = frozenset("*?[")
_REPORT_LIMIT = 50

_REGISTRY_HEADER = (
    "# Registry of the baselines of the existing system (G02-08, G02-09, M-AUTH-12, S00-ACC-05).\n"
    "#\n"
    "# Written by tools/baseline_freeze.py - edit only through 'freeze' (which needs owner approval\n"
    "# after the audit of the production tree). The artifacts are not reachable from the development\n"
    "# session (blocker B-001): every entry stays not_frozen, with include and manifest null, until\n"
    "# the audit has established which files make up the baseline. The numbers the specification\n"
    "# quotes about these baselines are unverified claims (specifications/baselines/claims.yaml).\n"
)


class BaselineRefused(ValueError):
    """A command was refused: the request is invalid or would not produce a sound baseline."""


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def _within(child: str, parent: str) -> bool:
    """True when the real path ``child`` is ``parent`` or below it."""
    child_n, parent_n = os.path.normcase(child), os.path.normcase(parent)
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:  # different drives on Windows
        return False


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a sibling temporary file and an atomic rename."""
    temporary = path.with_name("." + path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _json_bytes(document: Any) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _limited(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_REPORT_LIMIT])
    more = len(items) - _REPORT_LIMIT
    return shown + (f" (+{more} more)" if more > 0 else "")


def _matches(rel: str, patterns: Sequence[str]) -> bool:
    """Same matching rule as the audit tool: ``*`` crosses ``/``, a directory selects its subtree.

    The audit tool exposes this rule only privately (`_matches_any`); it is restated here so that
    entries the manifest leaves out (links, special files) can be checked against the same patterns.
    """
    windows = os.name == "nt"
    candidate = rel.lower() if windows else rel
    for raw in patterns:
        pattern = raw.replace("\\", "/") if windows else raw
        pattern = (pattern.lower() if windows else pattern).rstrip("/")
        if fnmatch.fnmatchcase(candidate, pattern) or candidate.startswith(pattern + "/"):
            return True
    return False


def _could_reach(rel: str, patterns: Sequence[str]) -> bool:
    """Fail-closed test whether a pattern could select something at or below ``rel``."""
    if _matches(rel, patterns):
        return True
    for pattern in patterns:
        static = pattern
        for index, char in enumerate(pattern):
            if char in _WILDCARDS:
                static = pattern[:index]
                break
        if static.startswith(rel + "/") or rel.startswith(static):
            return True
    return False


def _check_includes(includes: Sequence[str]) -> list[str]:
    if not includes:
        raise BaselineRefused("at least one --include pattern is required")
    checked: list[str] = []
    for pattern in includes:
        if not isinstance(pattern, str) or not pattern.strip():
            raise BaselineRefused("an include pattern is empty")
        normal = pattern.replace("\\", "/")
        if normal.startswith("/") or re.match(r"^[A-Za-z]:", normal):
            raise BaselineRefused(
                f"include pattern {pattern!r} is absolute; patterns are relative to the root"
            )
        if ".." in normal.split("/"):
            raise BaselineRefused(f"include pattern {pattern!r} leaves the root ('..')")
        checked.append(pattern)
    if len(set(checked)) != len(checked):
        raise BaselineRefused("an include pattern is given twice")
    return checked


# --------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------


def _registry_path(repo_root: Path) -> Path:
    return repo_root / REGISTRY_REL


def known_requirement_ids(repo_root: Path) -> set[str]:
    """Ids of ``specifications/requirements_matrix.yaml`` (empty set when the file is missing)."""
    path = repo_root / "specifications" / "requirements_matrix.yaml"
    if not path.is_file():
        return set()
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    loaded = yaml.load(path.read_bytes().decode("utf-8"), Loader=loader)  # noqa: S506 - safe loader
    rows = loaded.get("requirements", loaded) if isinstance(loaded, dict) else loaded
    if not isinstance(rows, list):
        return set()
    return {str(row["id"]) for row in rows if isinstance(row, dict) and "id" in row}


def validate_registry(document: Any, requirement_ids: set[str] | None = None) -> list[str]:
    """Problems of a registry document (an empty list means well-formed)."""
    if not isinstance(document, dict):
        return ["registry is not a mapping"]
    problems: list[str] = []
    if set(document) != {"schema_version", "baselines"}:
        problems.append("registry keys must be exactly schema_version and baselines")
    if document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        problems.append(f"schema_version must be {REGISTRY_SCHEMA_VERSION}")
    baselines = document.get("baselines")
    if not isinstance(baselines, list):
        return [*problems, "baselines must be a list"]
    seen: set[str] = set()
    for number, entry in enumerate(baselines):
        where = f"baselines[{number}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not a mapping")
            continue
        missing = [key for key in ENTRY_KEYS if key not in entry]
        unknown = [key for key in entry if key not in ENTRY_KEYS and key not in OPTIONAL_ENTRY_KEYS]
        if missing:
            problems.append(f"{where} misses {', '.join(missing)}")
        if unknown:
            problems.append(f"{where} has unknown keys {', '.join(map(str, unknown))}")
        ident = entry.get("id")
        if not isinstance(ident, str) or not _ID.match(ident):
            problems.append(f"{where}.id is not a lower-case id")
            continue
        where = f"baseline {ident}"
        if ident in seen:
            problems.append(f"{where} is listed twice")
        seen.add(ident)
        for key in ("title", "description"):
            if not isinstance(entry.get(key), str) or not str(entry.get(key)).strip():
                problems.append(f"{where}.{key} must be a non-empty string")
        reqs = entry.get("requirement_ids")
        if not isinstance(reqs, list) or not reqs or not all(isinstance(r, str) for r in reqs):
            problems.append(f"{where}.requirement_ids must be a non-empty list of strings")
        else:
            for req in reqs:
                if not _REQUIREMENT_ID.match(req):
                    problems.append(f"{where}: requirement id {req!r} is malformed")
                elif requirement_ids is not None and req not in requirement_ids:
                    problems.append(f"{where}: requirement id {req} is not in the requirements matrix")
            if len(set(reqs)) != len(reqs):
                problems.append(f"{where}.requirement_ids lists an id twice")
        root_env = entry.get("root_env")
        if not isinstance(root_env, str) or not _ENV_NAME.match(root_env):
            problems.append(f"{where}.root_env must be an environment variable name")
        include = entry.get("include")
        if include is not None:
            try:
                _check_includes(include if isinstance(include, list) else [])
            except BaselineRefused as exc:
                problems.append(f"{where}.include: {exc}")
        blocker = entry.get("blocker")
        if blocker is not None and (not isinstance(blocker, str) or not _BLOCKER.match(blocker)):
            problems.append(f"{where}.blocker must be null or an id B-nnn")
        decision = entry.get("decision")
        if "decision" in entry and (not isinstance(decision, str) or not _DECISION.match(decision)):
            problems.append(f"{where}.decision must be an id D-nnn")
        state = entry.get("status")
        manifest = entry.get("manifest")
        if state not in STATUSES:
            problems.append(f"{where}.status must be one of {', '.join(STATUSES)}")
        elif state == "frozen":
            expected = f"{FROZEN_REL}/{ident}.manifest.jsonl"
            if manifest != expected:
                problems.append(f"{where} is frozen, so manifest must be {expected}")
            if include is None:
                problems.append(f"{where} is frozen, so include must be the list of patterns")
        else:
            if manifest is not None:
                problems.append(f"{where} is not frozen, so manifest must be null")
            if blocker is None:
                problems.append(f"{where} is not frozen, so it must name the blocker that holds it")
            if "decision" in entry:
                problems.append(f"{where} is not frozen, so it cannot record a replacement decision")
    return problems


def load_registry(repo_root: Path = REPO_ROOT, *, check_requirements: bool = True) -> dict[str, Any]:
    """Load and validate ``specifications/baselines/registry.yaml``; raise `BaselineRefused` if invalid."""
    path = _registry_path(repo_root)
    try:
        document = yaml.safe_load(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BaselineRefused(f"cannot read {REGISTRY_REL} ({type(exc).__name__})") from None
    ids = known_requirement_ids(repo_root) if check_requirements else None
    problems = validate_registry(document, ids or None)
    if problems:
        raise BaselineRefused(f"{REGISTRY_REL} is invalid: " + "; ".join(problems))
    assert isinstance(document, dict)
    return document


def render_registry(document: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a registry document (fixed header, key order as in the schema, LF)."""
    entries = []
    for entry in document["baselines"]:
        ordered = {key: entry[key] for key in ENTRY_KEYS}
        ordered.update({key: entry[key] for key in OPTIONAL_ENTRY_KEYS if key in entry})
        entries.append(ordered)
    body = yaml.safe_dump(
        {"schema_version": document["schema_version"], "baselines": entries},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        width=100,
        line_break="\n",
    )
    return (_REGISTRY_HEADER + body).encode("ascii")


def _entry(document: Mapping[str, Any], baseline_id: str) -> dict[str, Any]:
    for entry in document["baselines"]:
        if entry["id"] == baseline_id:
            assert isinstance(entry, dict)
            return entry
    raise BaselineRefused(f"baseline {baseline_id!r} is not in {REGISTRY_REL}")


# --------------------------------------------------------------------------------------------
# Manifest of a baseline
# --------------------------------------------------------------------------------------------


def _unpinned_entries(root_real: str, includes: Sequence[str]) -> list[str]:
    """Links, special files and unreadable directories an include pattern could reach.

    `build_manifest` lists regular files only (its walk report with links and special files is not
    returned), so these entries are found by a separate listing; they are never followed or read.
    """
    found: list[str] = []
    pending: list[tuple[str, str]] = [(root_real, "")]
    while pending:
        directory, rel_dir = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            if not rel_dir or _could_reach(rel_dir, includes):
                found.append(f"{rel_dir or '.'} (unreadable directory: {type(exc).__name__})")
            continue
        for name in names:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            path = os.path.join(directory, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError as exc:
                if _could_reach(rel, includes):
                    found.append(f"{rel} (cannot stat: {type(exc).__name__})")
                continue
            if stat.S_ISLNK(mode):
                if _could_reach(rel, includes):
                    found.append(f"{rel} (symbolic link)")
            elif stat.S_ISDIR(mode):
                pending.append((path, rel))
            elif not stat.S_ISREG(mode) and _could_reach(rel, includes):
                found.append(f"{rel} (special file)")
    return sorted(found)


def build_baseline_manifest(root: str | os.PathLike[str], includes: Sequence[str]) -> list[dict[str, Any]]:
    """Manifest of the files of a baseline: nothing excluded, every file hashed, no size cap."""
    return build_manifest(
        root,
        list(includes),
        excluded_dirs=(),
        detect_environments=False,
        hash_sensitive=True,
        max_hash_bytes=None,
    )


def _unhashed(records: Sequence[Mapping[str, Any]]) -> list[str]:
    problems = []
    for record in records:
        if not isinstance(record.get("sha256"), str):
            problems.append(f"{record['path']} (not hashed: {record.get('skipped', 'unknown reason')})")
        elif record.get("unstable"):
            problems.append(f"{record['path']} (changed while it was read)")
    return problems


def _content_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over path, size and SHA-256 of every record (independent of modification times)."""
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: str(item["path"])):
        line = json.dumps(
            {"path": record["path"], "sha256": record["sha256"], "size": record["size"]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        digest.update(line.encode("ascii") + b"\n")
    return digest.hexdigest()


def _decision_exists(repo_root: Path, decision: str) -> bool:
    path = repo_root / DECISION_LOG_REL
    if not path.is_file():
        return False
    pattern = re.compile(r"^#+\s+" + re.escape(decision) + r"\b", re.MULTILINE)
    return bool(pattern.search(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------------------------


def freeze(
    baseline_id: str,
    root: str | os.PathLike[str],
    includes: Sequence[str],
    *,
    repo_root: Path = REPO_ROOT,
    replace: bool = False,
    decision: str | None = None,
) -> dict[str, Any]:
    """Freeze one baseline and return its summary; raise `BaselineRefused` when refused."""
    repo_root = Path(_real(repo_root))
    patterns = _check_includes(list(includes))
    document = load_registry(repo_root)
    entry = _entry(document, baseline_id)

    root_real = _real(root)
    if not os.path.isdir(root_real):
        raise BaselineRefused(f"--root is not a directory: {os.fspath(root)}")
    frozen_dir = repo_root / FROZEN_REL
    frozen_real = _real(frozen_dir)
    if _within(root_real, frozen_real):
        raise BaselineRefused(f"--root is inside {FROZEN_REL}; a frozen manifest cannot be a baseline")
    manifest_path = frozen_dir / f"{baseline_id}.manifest.jsonl"
    summary_path = frozen_dir / f"{baseline_id}.summary.json"
    for output in (frozen_dir, manifest_path, summary_path, _registry_path(repo_root)):
        if _within(_real(output), root_real):
            raise BaselineRefused(
                f"output {output} would be written inside --root; the baseline tree is never written"
            )

    already = entry["status"] == "frozen" or manifest_path.exists() or summary_path.exists()
    previous_sha: str | None = None
    if already:
        if not replace or decision is None:
            raise BaselineRefused(
                f"baseline {baseline_id} is already frozen; replacing it needs --replace and "
                "--decision D-nnn (an owner-approved entry of docs/DECISION_LOG.md)"
            )
        if manifest_path.is_file():
            previous_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    elif replace:
        raise BaselineRefused(f"--replace given, but baseline {baseline_id} is not frozen")
    if decision is not None:
        if not replace:
            raise BaselineRefused("--decision is only used together with --replace")
        if not _DECISION.match(decision):
            raise BaselineRefused(f"--decision must be an id D-nnn, not {decision!r}")
        if not _decision_exists(repo_root, decision):
            raise BaselineRefused(f"decision {decision} is not in {DECISION_LOG_REL}")

    try:
        records = build_baseline_manifest(root_real, patterns)
    except AuditRefused as exc:
        raise BaselineRefused(str(exc)) from None
    if not records:
        raise BaselineRefused("the include patterns match no file under --root")
    unmatched = [p for p in patterns if not any(_matches(str(r["path"]), [p]) for r in records)]
    if unmatched:
        raise BaselineRefused("include pattern(s) match no file: " + ", ".join(unmatched))
    problems = _unhashed(records) + _unpinned_entries(root_real, patterns)
    if problems:
        raise BaselineRefused(
            "not frozen - every entry of a baseline must be a regular file whose SHA-256 is recorded: "
            + _limited(problems)
        )

    frozen_dir.mkdir(parents=True, exist_ok=True)
    temporary = frozen_dir / f".{baseline_id}.manifest.jsonl.tmp"
    if temporary.exists():
        temporary.unlink()
    write_manifest(records, temporary)
    manifest_bytes = temporary.read_bytes()
    os.replace(temporary, manifest_path)

    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "id": baseline_id,
        "file_count": len(records),
        "total_bytes": sum(int(r["size"]) for r in records),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "content_sha256": _content_sha256(records),
        "includes": patterns,
        "decision": decision,
        "replaces_manifest_sha256": previous_sha,
        "audit_tool_version": AUDIT_TOOL_VERSION,
        "manifest_options": {
            "excluded_dirs": [],
            "detect_environments": False,
            "hash_sensitive": True,
            "max_hash_bytes": None,
        },
    }
    _write_bytes_atomic(summary_path, _json_bytes(summary))

    entry["status"] = "frozen"
    entry["manifest"] = f"{FROZEN_REL}/{baseline_id}.manifest.jsonl"
    entry["include"] = patterns
    if decision is not None:
        entry["decision"] = decision
    _write_bytes_atomic(_registry_path(repo_root), render_registry(document))
    return summary


# --------------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------------


def _result(ident: str, state: str, reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": ident,
        "status": state,
        "reason": reason,
        "added": [],
        "removed": [],
        "changed": [],
        "mtime_only": [],
        "problems": [],
    }
    result.update(extra)
    return result


def verify_baseline(
    entry: Mapping[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify one registry entry; ``status`` is OK, MISMATCH, INVALID or UNVERIFIED."""
    ident = str(entry["id"])
    env = os.environ if environ is None else environ
    if entry["status"] != "frozen":
        return _result(ident, "UNVERIFIED", f"not frozen (blocker {entry.get('blocker') or 'none recorded'})")
    root_env = str(entry["root_env"])
    if root is None:
        value = env.get(root_env)
        if not value:
            return _result(
                ident, "UNVERIFIED", f"root not available: environment variable {root_env} is not set"
            )
        root = value
    if not os.path.isdir(root):
        return _result(ident, "UNVERIFIED", "root not available: the given root is not a directory")

    manifest_path = repo_root / str(entry["manifest"])
    summary_path = manifest_path.with_name(f"{ident}.summary.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
        summary = json.loads(summary_path.read_bytes().decode("utf-8"))
        frozen = read_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        return _result(ident, "INVALID", f"frozen manifest or summary cannot be read ({type(exc).__name__})")
    if (
        not isinstance(summary, dict)
        or summary.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        return _result(ident, "INVALID", "frozen manifest does not match the SHA-256 recorded in its summary")
    if summary.get("includes") != entry["include"]:
        return _result(ident, "INVALID", "include patterns of the registry and of the summary differ")
    if not frozen or _unhashed(frozen):
        return _result(ident, "INVALID", "frozen manifest is empty or has a file without SHA-256")

    includes = list(entry["include"])
    try:
        current = build_baseline_manifest(root, includes)
    except AuditRefused as exc:
        return _result(ident, "UNVERIFIED", f"root not available: {exc}")
    comparison = compare_manifests(frozen, current)
    problems = _unhashed(current) + _unpinned_entries(_real(root), includes)
    changed = [f"{item['path']} ({'+'.join(item['reasons'])})" for item in comparison["changed"]]
    extra = {
        "added": list(comparison["added"]),
        "removed": list(comparison["removed"]),
        "changed": changed,
        "mtime_only": list(comparison["mtime_only"]),
        "problems": problems,
        "files": len(current),
    }
    if comparison["content_identical"] and not comparison["unverified"] and not problems:
        return _result(ident, "OK", f"{len(current)} files byte-identical", **extra)
    return _result(ident, "MISMATCH", "content differs from the frozen manifest", **extra)


def verify(
    *,
    repo_root: Path = REPO_ROOT,
    baseline_id: str | None = None,
    root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify the selected baselines; returns ``results`` and ``exit_code`` (see module docstring)."""
    if root is not None and baseline_id is None:
        raise BaselineRefused("--root needs --id: one root belongs to one baseline")
    repo_root = Path(_real(repo_root))
    document = load_registry(repo_root)
    entries = [_entry(document, baseline_id)] if baseline_id is not None else list(document["baselines"])
    results = [verify_baseline(e, repo_root=repo_root, root=root, environ=environ) for e in entries]
    states = {r["status"] for r in results}
    if states & {"MISMATCH", "INVALID"}:
        code = EXIT_MISMATCH
    elif "UNVERIFIED" in states or not results:
        code = EXIT_UNVERIFIED
    else:
        code = EXIT_OK
    return {"results": results, "exit_code": code}


def _print_verify(report: Mapping[str, Any]) -> None:
    for result in report["results"]:
        print(f"{result['status']} {result['id']}: {result['reason']}")
        for key in ("added", "removed", "changed", "problems"):
            for path in result[key]:
                print(f"  {key}: {path}")
        if result["mtime_only"]:
            print(f"  modification time only (not a mismatch): {len(result['mtime_only'])} file(s)")
    print(f"baseline_freeze verify: exit {report['exit_code']}")


# --------------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------------


def status(*, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """JSON-serialisable summary of the registry and of the frozen files present."""
    repo_root = Path(_real(repo_root))
    document = load_registry(repo_root)
    rows = []
    for entry in document["baselines"]:
        summary_path = repo_root / FROZEN_REL / f"{entry['id']}.summary.json"
        summary: Any = None
        if summary_path.is_file():
            summary = json.loads(summary_path.read_bytes().decode("utf-8"))
        rows.append(
            {
                "id": entry["id"],
                "title": entry["title"],
                "status": entry["status"],
                "blocker": entry["blocker"],
                "root_env": entry["root_env"],
                "include": entry["include"],
                "manifest": entry["manifest"],
                "file_count": summary.get("file_count") if isinstance(summary, dict) else None,
                "total_bytes": summary.get("total_bytes") if isinstance(summary, dict) else None,
            }
        )
    counts = {state: sum(1 for row in rows if row["status"] == state) for state in STATUSES}
    return {"registry": REGISTRY_REL, "baselines": rows, "counts": counts}


# --------------------------------------------------------------------------------------------
# No production changes (S00-ACC-06)
# --------------------------------------------------------------------------------------------


def resolve_production_manifests(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path] | str:
    """The two manifest paths of the production tree, or the reason why they are not available."""
    env = os.environ if environ is None else environ
    paths: list[Path] = []
    for name in (PRODUCTION_MANIFEST_BEFORE_ENV, PRODUCTION_MANIFEST_AFTER_ENV):
        value = env.get(name)
        if not value:
            return f"environment variable {name} is not set"
        path = Path(value)
        if not path.is_file():
            return f"the manifest named by {name} does not exist"
        paths.append(path)
    return paths[0], paths[1]


def compare_production_manifests(
    before: str | os.PathLike[str] | Sequence[Mapping[str, Any]],
    after: str | os.PathLike[str] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare manifests of the production tree taken before and after a session.

    ``identical`` is true only when the content is proven identical: the "before" manifest is not
    empty, no path was added, removed or changed, and every file is hashed on both sides (the
    audit tool skips names that suggest secrets unless ``--hash-sensitive`` is given; such a file
    is reported as not proven).  ``message`` lists the differences.
    """
    left = read_manifest(before) if isinstance(before, (str, os.PathLike)) else list(before)
    right = read_manifest(after) if isinstance(after, (str, os.PathLike)) else list(after)
    comparison = compare_manifests(left, right)
    unhashed = sorted({str(r["path"]) for r in [*left, *right] if not isinstance(r.get("sha256"), str)})
    changed = [f"{item['path']} ({'+'.join(item['reasons'])})" for item in comparison["changed"]]
    lines = []
    if not left:
        lines.append("the 'before' manifest lists no file: nothing would be proven")
    for label, items in (
        ("added", comparison["added"]),
        ("removed", comparison["removed"]),
        ("changed", changed),
        ("not hashed (content identity not proven; rebuild with --hash-sensitive)", unhashed),
    ):
        if items:
            lines.append(f"{label}: {_limited(items)}")
    identical = bool(left) and comparison["content_identical"] and not unhashed
    message = f"production tree content identical: {len(left)} files" if identical else "; ".join(lines)
    return {
        "identical": identical,
        "message": message,
        "added": list(comparison["added"]),
        "removed": list(comparison["removed"]),
        "changed": changed,
        "unhashed": unhashed,
        "mtime_only": list(comparison["mtime_only"]),
    }


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.baseline_freeze",
        description="Freeze and verify the baselines of the existing system; the baseline tree is only read.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", default=str(REPO_ROOT), help="repository root (default: this one)")

    freeze_cmd = commands.add_parser("freeze", parents=[common], help="freeze one baseline")
    freeze_cmd.add_argument("--id", required=True, dest="baseline_id")
    freeze_cmd.add_argument("--root", required=True, help="directory that holds the baseline (only read)")
    freeze_cmd.add_argument("--include", required=True, action="append", metavar="GLOB")
    freeze_cmd.add_argument("--replace", action="store_true", help="replace an existing frozen manifest")
    freeze_cmd.add_argument("--decision", default=None, help="decision id D-nnn approving the replacement")

    verify_cmd = commands.add_parser("verify", parents=[common], help="verify frozen baselines")
    verify_cmd.add_argument("--id", default=None, dest="baseline_id")
    verify_cmd.add_argument("--root", default=None, help="root of the baseline given with --id")

    commands.add_parser("status", parents=[common], help="JSON summary of the registry")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root)
    try:
        if args.command == "freeze":
            summary = freeze(
                args.baseline_id,
                args.root,
                args.include,
                repo_root=repo_root,
                replace=args.replace,
                decision=args.decision,
            )
            print(
                f"frozen {summary['id']}: {summary['file_count']} files, {summary['total_bytes']} bytes, "
                f"manifest sha256 {summary['manifest_sha256']}"
            )
            return EXIT_OK
        if args.command == "verify":
            report = verify(repo_root=repo_root, baseline_id=args.baseline_id, root=args.root)
            _print_verify(report)
            return int(report["exit_code"])
        sys.stdout.write(json.dumps(status(repo_root=repo_root), sort_keys=True, indent=2) + "\n")
        return EXIT_OK
    except BaselineRefused as exc:
        print(f"baseline_freeze: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
