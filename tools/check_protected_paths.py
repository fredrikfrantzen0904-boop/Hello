"""No production changes inside this repository: protected paths stay byte-identical (PP000-PP006).

Requirements: S00-ACC-06 (acceptance test "no production changes"), OWN-13 (preserve all existing
trading systems), M-AUTH-12 (do not modify the existing systems' files).

The files that existed in the repository before the project started must never change.  They are
recorded in ``specifications/dependency_graph.yaml``::

    protected_paths:
      base_commit: <40-digit commit id of the repository before the project>
      paths:
        <path>: <git blob SHA-1 of the file content at base_commit>

This tool compares the working tree, the record and - when git history is available - the base
commit itself.  It **never writes anything**: no file, no git object, no ref, no configuration.

Usage::

    python -m tools.check_protected_paths [--root PATH] [--json]

One line per violation (``path:line: RULE message``) on standard output and exit status 1; with no
violation a one-line ``OK`` summary and exit status 0; exit status 2 for a command-line error.
Informational notes are printed as ``check_protected_paths: note: ...`` and never change the exit
status.  ``--json`` prints one JSON document instead.

Rules
-----
PP000  The record cannot be used: the dependency graph is missing or not valid YAML, it has no
       ``protected_paths`` mapping, ``base_commit`` or a recorded hash is not a 40-digit lower-case
       hexadecimal string, a path is not a relative POSIX path inside the repository, a key occurs
       twice, or nothing at all is protected.  Fail closed: an unreadable record is never a pass.
PP001  A protected path is missing from the working tree.
PP002  The git blob SHA-1 of the working-tree content differs from the recorded hash.  The hash is
       computed here, ``sha1(b"blob " + <decimal length> + NUL + <bytes>)``, from the file read in
       binary mode: no end-of-line conversion and no git filter is applied, so a checkout that
       rewrote line endings is reported too.
PP003  (git history) The recorded hash differs from the blob of the path at ``base_commit``, or
       the path does not exist there: the record itself was changed.
PP004  The protected path is a symbolic link or not a regular file, or a directory on the way to
       it is a symbolic link.
PP005  (git history) ``main`` / ``origin/main`` was advanced by this project: the ref exists and is
       neither ``base_commit`` nor one of its ancestors.
PP006  (git history) A file of ``base_commit`` is not listed under ``protected_paths``: everything
       that existed before the project is protected, so an entry cannot be dropped from the record
       to make a change pass.

What "git history is available" means
-------------------------------------
PP003, PP005 and PP006 need a git work tree whose top level is the root and, except for the
comparison of a ref with ``base_commit``, the base commit itself.  Each missing piece is an
informational note that says exactly which rule was not evaluated, **not** a violation: without
git; in a directory that is not the top level of a work tree; when the base commit is not in the
clone (a shallow clone, for example); when ``main`` or ``origin/main`` does not exist locally.
PP001, PP002 and PP004 never need git.  A ref that exists and differs from ``base_commit`` while
the base commit is absent cannot be shown to be an ancestor and is reported (PP005): fail closed.

PP006 is stricter than the minimum rule text; it is covered by a test.

Git is only ever asked to read: ``rev-parse``, ``cat-file -e``, ``ls-tree`` and
``merge-base --is-ancestor``, through ``subprocess`` with an argument list (no shell), with
optional locks disabled.  Every value that reaches a command line is validated first.

Honest limits
-------------
The check covers the content of the files, not their mode bits or timestamps, and it looks at the
working tree of the scanned root only: other branches, other clones and the production tree on the
owner's machine are outside its reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final

import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: Rule ids and their one-line titles (also written to the ``--json`` output).
RULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "PP000": "the record of protected paths cannot be used",
        "PP001": "protected path is missing",
        "PP002": "protected path changed (content differs from the recorded blob)",
        "PP003": "recorded blob differs from the base commit",
        "PP004": "protected path is a symbolic link or not a regular file",
        "PP005": "main was advanced past the base commit",
        "PP006": "file of the base commit is not protected",
    }
)

GRAPH_PATH: Final = "specifications/dependency_graph.yaml"
RECORD_KEY: Final = "protected_paths"
#: Refs that this project never moves (docs/WORKTREES.md).
MAIN_REFS: Final[tuple[str, ...]] = ("refs/heads/main", "refs/remotes/origin/main")

_HEX: Final = frozenset("0123456789abcdef")
_GIT_TIMEOUT_S: Final = 60
#: Environment variables that would point git at another repository than ``--root``.
_GIT_LOCATION_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


# ============================================================================== results
@dataclass(frozen=True)
class Violation:
    """One finding.  ``path`` is POSIX and relative to the scanned root (for PP005 the name of the
    git ref); ``line`` 0 is the whole file."""

    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


@dataclass(frozen=True)
class Report:
    """Everything one check found: violations (sorted), informational notes and what was compared."""

    violations: tuple[Violation, ...]
    notes: tuple[str, ...]
    #: The protected paths of the record, sorted (empty when the record cannot be used).
    protected: tuple[str, ...]
    base_commit: str | None
    #: Whether the base commit could be read, i.e. whether PP003 and PP006 were evaluated.
    history_checked: bool


def _order(violation: Violation) -> tuple[str, int, str, str]:
    return (violation.path, violation.line, violation.rule, violation.message)


# ============================================================================== the record
def blob_sha1(data: bytes) -> str:
    """The SHA-1 that git gives a blob with exactly these bytes (the identity git itself uses)."""
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _is_sha1(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= _HEX


def _is_safe_relative(path: str) -> bool:
    parts = path.split("/")
    return (
        bool(path)
        and "\\" not in path
        and "\0" not in path
        and not PurePosixPath(path).is_absolute()
        and all(part not in {"", ".", ".."} for part in parts)
        and parts[0] != ".git"
    )


@dataclass(frozen=True)
class _Record:
    base_commit: str
    #: protected path -> recorded git blob SHA-1
    paths: Mapping[str, str]
    #: protected path -> line of its entry in the dependency graph
    lines: Mapping[str, int]
    line: int


def _record_nodes(document: yaml.Node | None) -> list[tuple[yaml.Node, int]]:
    """Every top-level ``protected_paths`` value of the composed document with the line of its key."""
    if not isinstance(document, yaml.MappingNode):
        return []
    return [
        (value_node, int(key_node.start_mark.line) + 1)
        for key_node, value_node in document.value
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == RECORD_KEY
    ]


def _scalar_items(node: yaml.MappingNode) -> list[tuple[str, yaml.Node, int]]:
    return [
        (str(key_node.value), value_node, int(key_node.start_mark.line) + 1)
        for key_node, value_node in node.value
        if isinstance(key_node, yaml.ScalarNode)
    ]


def _read_record(root: Path) -> _Record | list[Violation]:
    """The record of protected paths, or the PP000 violations that make it unusable."""

    def unusable(message: str, line: int = 0) -> list[Violation]:
        return [Violation("PP000", GRAPH_PATH, line, message)]

    try:
        text = (root / GRAPH_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return unusable(f"{GRAPH_PATH} is missing, so no protected path can be verified")
    except (OSError, UnicodeDecodeError) as error:
        return unusable(f"{GRAPH_PATH} cannot be read ({type(error).__name__})")
    try:
        document = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        return unusable(f"{GRAPH_PATH} is not valid YAML", int(mark.line) + 1 if mark is not None else 0)
    candidates = _record_nodes(document)
    if len(candidates) > 1:
        return unusable(f"duplicate key '{RECORD_KEY}'", candidates[1][1])
    record, line = candidates[0] if candidates else (None, 0)
    if not isinstance(record, yaml.MappingNode):
        return unusable(f"'{RECORD_KEY}' must be a mapping with 'base_commit' and 'paths'", line)

    problems: list[Violation] = []
    seen: set[str] = set()
    fields: dict[str, yaml.Node] = {}
    for key, value_node, key_line in _scalar_items(record):
        if key in seen:
            problems.extend(unusable(f"duplicate key '{RECORD_KEY}.{key}'", key_line))
        seen.add(key)
        fields[key] = value_node

    base_node = fields.get("base_commit")
    base_commit = base_node.value if isinstance(base_node, yaml.ScalarNode) else None
    if not _is_sha1(base_commit):
        problems.extend(unusable("'base_commit' must be a 40-digit lower-case hexadecimal commit id", line))
    paths_node = fields.get("paths")
    paths: dict[str, str] = {}
    lines: dict[str, int] = {}
    if not isinstance(paths_node, yaml.MappingNode):
        problems.extend(unusable("'paths' must map each protected path to its git blob SHA-1", line))
    else:
        for path, value_node, key_line in _scalar_items(paths_node):
            recorded = value_node.value if isinstance(value_node, yaml.ScalarNode) else None
            if path in paths:
                problems.extend(unusable(f"protected path {path!r} is listed twice", key_line))
            elif not _is_safe_relative(path):
                problems.extend(
                    unusable(
                        f"protected path {path!r} is not a relative POSIX path inside the repository",
                        key_line,
                    )
                )
            elif not _is_sha1(recorded):
                problems.extend(
                    unusable(
                        f"the recorded hash of {path} is not a 40-digit lower-case hexadecimal git blob "
                        "SHA-1",
                        key_line,
                    )
                )
            else:
                paths[path] = str(recorded)
                lines[path] = key_line
        if len(paths_node.value) == 0:
            problems.extend(
                unusable("no path is protected: the check would pass without verifying anything", line)
            )
    if problems:
        return problems
    return _Record(
        base_commit=str(base_commit),
        paths=MappingProxyType(paths),
        lines=MappingProxyType(lines),
        line=line,
    )


# ============================================================================== working tree (no git)
def _working_tree(root: Path, path: str, recorded: str) -> Iterator[Violation]:
    """PP001, PP002 and PP004 for one protected path; reads the file, touches nothing."""
    parts = path.split("/")
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            yield Violation(
                "PP004",
                path,
                0,
                f"the directory {current.relative_to(root).as_posix()} on the way to the protected path "
                "is a symbolic link",
            )
            return
    target = root.joinpath(*parts)
    try:
        status = target.lstat()
    except (FileNotFoundError, NotADirectoryError):
        yield Violation("PP001", path, 0, "protected path is missing from the working tree")
        return
    except OSError as error:
        yield Violation("PP001", path, 0, f"protected path cannot be examined ({type(error).__name__})")
        return
    if stat.S_ISLNK(status.st_mode):
        yield Violation("PP004", path, 0, "protected path is a symbolic link, not the original file")
        return
    if not stat.S_ISREG(status.st_mode):
        kind = "a directory" if stat.S_ISDIR(status.st_mode) else "not a regular file"
        yield Violation("PP004", path, 0, f"protected path is {kind}, not the original file")
        return
    try:
        data = target.read_bytes()
    except OSError as error:
        yield Violation(
            "PP002", path, 0, f"content cannot be read ({type(error).__name__}), so it is not verified"
        )
        return
    actual = blob_sha1(data)
    if actual != recorded:
        yield Violation(
            "PP002",
            path,
            0,
            f"content changed: its git blob SHA-1 is {actual}, the recorded one is {recorded} "
            f"({len(data)} bytes read without any end-of-line conversion)",
        )


# ============================================================================== git history
def _git(root: Path, *arguments: str) -> tuple[int, str] | None:
    """Run one read-only git command in *root*; ``None`` when git cannot be started."""
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARIABLES}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            check=False,
            env=environment,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode, completed.stdout.decode("utf-8", errors="surrogateescape")


def _git_unavailable(root: Path) -> str | None:
    """Why git history cannot be used for *root* (``None``: it can)."""
    probe = _git(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    if probe is None:
        return "git cannot be started"
    if probe[0] != 0 or not probe[1].startswith("true"):
        return "the root is not inside a git work tree"
    if probe[1].split("\n", 1)[-1].strip():
        return "the root is not the top level of the git work tree"
    return None


def _resolve(root: Path, revision: str) -> str | None:
    """The object id *revision* names in the repository, or ``None`` when it names nothing."""
    result = _git(root, "rev-parse", "--verify", "--quiet", revision)
    if result is None or result[0] != 0:
        return None
    resolved = result[1].strip()
    return resolved if _is_sha1(resolved) else None


def _base_commit_blobs(root: Path, record: _Record) -> Iterator[Violation]:
    """PP003 and PP006; only called when the base commit is present in the clone."""
    for path, recorded in sorted(record.paths.items()):
        at_base = _resolve(root, f"{record.base_commit}:{path}")
        if at_base is None:
            yield Violation(
                "PP003",
                GRAPH_PATH,
                record.lines[path],
                f"{path} is recorded as protected but does not exist at base commit {record.base_commit}",
            )
        elif at_base != recorded:
            yield Violation(
                "PP003",
                GRAPH_PATH,
                record.lines[path],
                f"the recorded blob {recorded} of {path} differs from its blob {at_base} at base commit "
                f"{record.base_commit}: the record was changed",
            )
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", record.base_commit)
    if listing is None or listing[0] != 0:
        yield Violation(
            "PP006", GRAPH_PATH, record.line, "the files of the base commit cannot be listed (git ls-tree)"
        )
        return
    for path in sorted(name for name in listing[1].split("\0") if name):
        if path not in record.paths:
            yield Violation(
                "PP006",
                path,
                0,
                f"the file exists at base commit {record.base_commit} but is not listed under "
                f"'{RECORD_KEY}' in {GRAPH_PATH}: everything that existed before the project is protected",
            )


def _main_refs(root: Path, record: _Record, base_present: bool, notes: list[str]) -> Iterator[Violation]:
    """PP005 for every main ref that exists locally."""
    for ref in MAIN_REFS:
        tip = _resolve(root, f"{ref}^{{commit}}")
        if tip is None:
            notes.append(
                f"{ref} does not exist in this clone: PP005 was not evaluated for it (not a violation)"
            )
            continue
        if tip == record.base_commit:
            continue
        if base_present:
            ancestor = _git(root, "merge-base", "--is-ancestor", tip, record.base_commit)
            if ancestor is not None and ancestor[0] == 0:
                notes.append(f"{ref} ({tip}) is an ancestor of the base commit: it was not advanced")
                continue
            reason = "it is neither the base commit nor one of its ancestors"
        else:
            reason = "the base commit is not in this clone, so an ancestor relationship cannot be shown"
        yield Violation(
            "PP005",
            ref,
            0,
            f"{ref} points at {tip}, not at base commit {record.base_commit} ({reason}): main must not be "
            "advanced by this project",
        )


def _history(root: Path, record: _Record, notes: list[str]) -> tuple[list[Violation], bool]:
    """The git-based rules; returns ``(violations, whether the base commit could be read)``."""
    unavailable = _git_unavailable(root)
    if unavailable is not None:
        notes.append(
            f"git history is not available ({unavailable}): PP003, PP005 and PP006 were not evaluated "
            "(not a violation); PP001, PP002 and PP004 do not need git"
        )
        return [], False
    violations: list[Violation] = []
    present = _git(root, "cat-file", "-e", f"{record.base_commit}^{{commit}}")
    base_present = present is not None and present[0] == 0
    if base_present:
        violations.extend(_base_commit_blobs(root, record))
    else:
        notes.append(
            f"base commit {record.base_commit} is not present in this clone (shallow or partial clone?): "
            "PP003 and PP006 were not evaluated - this is information, not a violation"
        )
    violations.extend(_main_refs(root, record, base_present, notes))
    return violations, base_present


# ============================================================================== entry points
def scan(repo_root: Path, *, use_git: bool = True) -> Report:
    """Check the protected paths of the repository at *repo_root*.

    With ``use_git=False`` only the working tree is compared with the record (PP000, PP001, PP002,
    PP004).  Nothing is written, in the working tree or in the repository.
    """
    record = _read_record(repo_root)
    if not isinstance(record, _Record):
        return Report(tuple(sorted(record, key=_order)), (), (), None, False)
    violations: list[Violation] = []
    notes: list[str] = []
    for path, recorded in sorted(record.paths.items()):
        violations.extend(_working_tree(repo_root, path, recorded))
    history_checked = False
    if use_git:
        found, history_checked = _history(repo_root, record, notes)
        violations.extend(found)
    else:
        notes.append("git was not used: PP003, PP005 and PP006 were not evaluated (not a violation)")
    return Report(
        violations=tuple(sorted(set(violations), key=_order)),
        notes=tuple(notes),
        protected=tuple(sorted(record.paths)),
        base_commit=record.base_commit,
        history_checked=history_checked,
    )


def check(repo_root: Path, *, use_git: bool = True) -> list[Violation]:
    """Every violation for the repository at *repo_root*, sorted by path, line and rule (see :func:`scan`)."""
    return list(scan(repo_root, use_git=use_git).violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_protected_paths",
        description="Protected paths stay byte-identical: no production changes (rules PP000-PP006).",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    if not root.is_dir():
        print(f"check_protected_paths: {root} is not a directory", file=sys.stderr)
        return 2
    report = scan(root)
    violations = report.violations
    if arguments.as_json:
        document = {
            "schema_version": 1,
            "tool": "tools/check_protected_paths.py",
            "ok": not violations,
            "base_commit": report.base_commit,
            "protected_paths": list(report.protected),
            "history_checked": report.history_checked,
            "violation_count": len(violations),
            "rules": dict(RULES),
            "violations": [asdict(violation) for violation in violations],
            "notes": list(report.notes),
        }
        print(json.dumps(document, indent=2, sort_keys=True))
        return 1 if violations else 0
    for violation in violations:
        print(violation.render())
    for note in report.notes:
        print(f"check_protected_paths: note: {note}")
    if violations:
        print(f"check_protected_paths: FAILED - {len(violations)} violation(s)", file=sys.stderr)
        return 1
    history = (
        f"and to base commit {report.base_commit}"
        if report.history_checked
        else "(the base commit was not compared, see note)"
    )
    print(
        f"check_protected_paths: OK - {len(report.protected)} protected path(s) byte-identical to the "
        f"record {history}; no violation of {min(RULES)}-{max(RULES)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
