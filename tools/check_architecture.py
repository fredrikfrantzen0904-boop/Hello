"""Architecture, ownership and determinism rules of the dependency graph (rules AR000-AR007).

Requirements: S00-ASG-10 / S00-DEL-03 (dependency graph), S00-ASG-08 (repository and worktrees),
S00-ASG-09 (ownership), COM-06 (shared contracts are not redefined unilaterally: subsystems import
only ``gie.core`` and ``gie.contracts``), G03-05 (nothing but the standard library can enter the
foundation packages, so no network or LLM client can be imported into the critical path there).

Source of truth: ``specifications/dependency_graph.yaml``.  The file is read, never written.  Keys
this tool does not know (``gates``, ``contract_extensions``, ``integrates_later``, ``ongoing``,
``path_mapping``, ``docs_path_ignore`` and whatever the coordinator adds later) are tolerated.

Usage::

    python -m tools.check_architecture [--root PATH] [--json]

One line per violation (``path:line: RULE message``) on standard output and exit status 1; with no
violation a one-line ``OK`` summary and exit status 0; exit status 2 for a command-line error.
Informational notes (for example "git is not available, AR007 was skipped") are printed as
``check_architecture: note: ...`` and never change the exit status.  ``--json`` prints one JSON
document instead.

How it works
------------
Python files are parsed with :mod:`ast` only.  Scanned code is never imported or executed and no
regular expression is run over code.  Paths are POSIX and relative to the scanned root; line 0
means "the file as a whole".  Names are resolved through the import table of each file
(``import x as y``, ``from x import y as z``, ``from x import *`` and plain re-assignments such as
``now = datetime.now``); the resolution is flow-insensitive and ignores scopes, so it
over-approximates: a name counts as everything it is bound to anywhere in the file.

Rules
-----
AR000  Scanned code cannot be read or parsed (syntax error, undecodable source, nested too deeply),
       or it is a symbolic link below a scanned tree (links are not followed, so the code behind
       one cannot be verified).  Reported, never a crash and never a silent pass.
AR001  Undeclared package: every directory ``gie/<name>/`` that contains Python files must be
       declared under ``packages`` as ``gie.<name>``.
AR002  Forbidden internal import: a module of package ``gie.X`` may import ``gie.Y`` only if
       ``gie.Y`` is ``X`` itself or is listed in ``may_import`` of ``gie.X`` (``gie.*`` means
       anything).  Covered forms: ``import gie.a.b``, ``from gie.a import b``, ``from gie import a``,
       relative imports resolved against the package of the file, imports inside functions and
       under ``TYPE_CHECKING``.  The tree ``dashboard/gie/`` is checked as package ``dashboard.gie``
       when that entry exists under ``packages``.
AR003  Third-party import in a package that has an entry under ``third_party`` (an empty list means
       standard library only, :data:`sys.stdlib_module_names` of the running interpreter).  Packages
       without an entry are not restricted by this rule.
AR004  Wall-clock or real-time timer access (:data:`WALL_CLOCK_NAMES`) anywhere below ``gie/``
       except in ``gie/core/clock.py``: calls and plain references both count.  ``time.perf_counter``,
       ``time.perf_counter_ns``, ``time.monotonic`` and ``time.monotonic_ns`` are allowed everywhere
       (latency measurement).
AR005  Non-deterministic identity or randomness anywhere below ``gie/``: ``uuid.uuid1`` /
       ``uuid.uuid4``, ``os.urandom``, everything in ``secrets``, the module-level functions of
       ``random`` and of ``numpy.random`` (they share one hidden, unseeded generator),
       ``random.SystemRandom``, a generator constructed without a seed (``random.Random()``,
       ``numpy.random.default_rng()``) and every call of the built-in ``hash()``, whose value for
       ``str`` and ``bytes`` is salted per process (``PYTHONHASHSEED``).  Explicitly seeded
       generators (``random.Random(seed)``, ``numpy.random.default_rng(seed)``) are allowed and so
       is ``id()``.
AR006  The dependency graph itself: ``schema_version`` is 1; every session S00..S12 is present with
       ``wave``, ``depends_on``, ``contract_only``, ``optional`` and ``owns``; dependencies name
       existing sessions; ``depends_on`` has no cycle; a session never depends on a session of a
       later wave; every package names an existing session; paths owned by different sessions do
       not overlap (no owned path equals, or is a prefix of, a path owned by another session).
AR007  Ownership coverage: every file tracked by git is owned by a session (longest matching
       ``owns`` prefix; an entry without a trailing slash is one exact file) or is listed under
       ``protected_paths``.  The file list comes from ``git ls-files`` (read-only).  Without git, or
       when the root is not the top level of a work tree, the rule is skipped with a note.

Stricter than the minimum rule text (each covered by a test)
-------------------------------------------------------------
* AR001 also reports a module directly below ``gie/`` (other than ``__init__.py``) that is not
  declared: code there would belong to no package and escape AR002.
* AR002 also covers ``importlib.import_module("...")`` and ``__import__("...")`` with a literal
  name, ``from gie import *`` and relative imports that leave the top-level package.  Files of the
  root package itself (``gie/__init__.py``) may import nothing below ``gie`` unless ``gie`` is
  declared under ``packages``.
* AR003 treats every top-level module that is neither standard library nor a GIE tree as not
  allowed, which includes the repository's own ``tools`` and ``tests``.
* AR004 / AR005 also see ``getattr(module, "name")`` with a literal name; AR005 also covers
  ``os.getrandom``, ``uuid.uuid6`` / ``uuid.uuid7``, a literal ``None`` seed and the unseeded
  constructors of ``numpy.random`` (``RandomState()``, ``SeedSequence()``, bit generators).
* AR006 also reports duplicate keys, malformed owned paths, a ``may_import`` entry that names no
  declared package, a ``third_party`` entry for an undeclared package, a package whose directory
  is not owned by the session it names, and a protected path that a session claims to own.

Deliberate reading of the rule text
-----------------------------------
* "``__hash__`` definitions are fine": a ``hash(...)`` call lexically inside a method named
  ``__hash__`` is not reported - that is the hash protocol itself, and its value is only ever used
  for look-ups inside one process.  Every other ``hash(...)`` call is reported.
* ``numpy.random.Generator`` and ``numpy.random.BitGenerator`` are types, not module-level
  functions; naming them (for example in an annotation) is allowed.
* An undeclared package is reported once (AR001); its imports cannot be judged against a rule that
  does not exist and are not reported one by one.

Honest limits
-------------
A static scan is not a sandbox.  Values are not followed through calls, containers or object
attributes; dynamic imports with a computed name and ``getattr`` with a computed name are not seen
here (``tools/check_live_lock.py`` reports dynamic code in GIE trees).  ``datetime.now`` called on
an object whose type the scan cannot see, and the built-in ``hash`` passed around under another
name, go undetected.  AR007 only sees files that git already tracks.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final

import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# ============================================================================== configuration
#: Rule ids and their one-line titles (also written to the ``--json`` output).
RULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AR000": "scanned code cannot be read, parsed or followed",
        "AR001": "package is not declared in the dependency graph",
        "AR002": "forbidden import between GIE packages",
        "AR003": "third-party import in a package with an allow-list",
        "AR004": "wall-clock or real-time timer access outside the clock module",
        "AR005": "non-deterministic identity or randomness",
        "AR006": "dependency graph is invalid",
        "AR007": "tracked file is not owned by any session",
    }
)

GRAPH_PATH: Final = "specifications/dependency_graph.yaml"
#: The only module below ``gie/`` that may read the system clock (AR004).
CLOCK_MODULE_PATH: Final = "gie/core/clock.py"
#: The tree every rule about code applies to, and its root package.
GIE_TREE: Final = "gie"
#: Checked by AR002 when this package is declared in the graph.
DASHBOARD_PACKAGE: Final = "dashboard.gie"
DASHBOARD_TREE: Final = "dashboard/gie"
#: ``may_import`` entry that allows every internal package (the composition root).
ANYTHING: Final = "gie.*"

REQUIRED_SESSIONS: Final[tuple[str, ...]] = tuple(f"S{number:02d}" for number in range(13))
SESSION_REFERENCE_KEYS: Final[tuple[str, ...]] = ("depends_on", "contract_only", "optional")
SESSION_REQUIRED_KEYS: Final[tuple[str, ...]] = ("wave", *SESSION_REFERENCE_KEYS, "owns")

SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyw", ".pyi"})

#: AR004 - reading the wall clock, sleeping on it or arming a real-time timer.
WALL_CLOCK_NAMES: Final[frozenset[str]] = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.localtime",
        "time.gmtime",
        "time.ctime",
        "time.sleep",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "threading.Timer",
        "sched.scheduler",
        "asyncio.sleep",
        "signal.alarm",
    }
)
#: AR005 - names that are non-deterministic whenever they are used.
NON_DETERMINISTIC_NAMES: Final[frozenset[str]] = frozenset(
    {
        "uuid.uuid1",
        "uuid.uuid4",
        "uuid.uuid6",
        "uuid.uuid7",
        "os.urandom",
        "os.getrandom",
        "random.SystemRandom",
    }
)
#: AR005 - modules of which every attribute is refused, except the names listed below.
NON_DETERMINISTIC_MODULES: Final[tuple[str, ...]] = ("secrets", "random", "numpy.random")
#: AR005 - generator constructors: allowed when called with a seed, refused when called without.
SEEDABLE_CONSTRUCTORS: Final[frozenset[str]] = frozenset(
    {
        "random.Random",
        "numpy.random.default_rng",
        "numpy.random.RandomState",
        "numpy.random.SeedSequence",
        "numpy.random.PCG64",
        "numpy.random.PCG64DXSM",
        "numpy.random.MT19937",
        "numpy.random.Philox",
        "numpy.random.SFC64",
    }
)
#: AR005 - types of ``numpy.random`` that carry no randomness of their own.
RANDOM_TYPE_NAMES: Final[frozenset[str]] = frozenset({"numpy.random.Generator", "numpy.random.BitGenerator"})
#: AR005 - the built-in ``hash`` under its qualified name.
BUILTIN_HASH: Final = "builtins.hash"

_CLOCK_ADVICE: Final = (
    "time comes from the injected gie.core.clock.Clock / Scheduler; only gie/core/clock.py may touch "
    "the system clock"
)
_HASH_ADVICE: Final = (
    "hash() is salted per process for str and bytes (PYTHONHASHSEED), so its value differs from run to "
    "run; use gie.core.canonical.content_hash or gie.contracts.identity"
)
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
    """One finding.  ``path`` is POSIX and relative to the scanned root; ``line`` 0 is the whole file."""

    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


@dataclass(frozen=True)
class Report:
    """Everything one scan found: violations (sorted), informational notes and what was looked at."""

    violations: tuple[Violation, ...]
    notes: tuple[str, ...]
    python_files: int
    #: Number of tracked files checked by AR007; ``None`` when the rule was skipped.
    tracked_files: int | None


def _order(violation: Violation) -> tuple[str, int, str, str]:
    return (violation.path, violation.line, violation.rule, violation.message)


# ============================================================================== the dependency graph
@dataclass(frozen=True)
class _Session:
    wave: int | None
    depends_on: tuple[str, ...]
    owns: tuple[str, ...]


@dataclass(frozen=True)
class _Package:
    session: str | None
    may_import: tuple[str, ...]


@dataclass(frozen=True)
class _Graph:
    sessions: Mapping[str, _Session]
    packages: Mapping[str, _Package]
    #: package -> allowed third-party top-level modules, for the packages that have an entry.
    third_party: Mapping[str, frozenset[str]]
    protected: frozenset[str]

    def owner_of(self, path: str) -> str | None:
        """The session whose longest ``owns`` entry covers *path* (``None``: nobody)."""
        best: tuple[int, str] | None = None
        for session_id, session in self.sessions.items():
            for entry in session.owns:
                if _covers(entry, path) and (best is None or len(entry) > best[0]):
                    best = (len(entry), session_id)
        return None if best is None else best[1]


def _covers(entry: str, path: str) -> bool:
    """Whether the ``owns`` entry covers *path*: a trailing slash is a directory, anything else one file."""
    return path.startswith(entry) if entry.endswith("/") else path == entry


def _is_safe_relative(path: str) -> bool:
    parts = path.rstrip("/").split("/")
    return (
        bool(path.strip("/"))
        and "\\" not in path
        and not PurePosixPath(path).is_absolute()
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _is_dotted_name(name: str) -> bool:
    return bool(name) and all(part.isidentifier() for part in name.split("."))


def _string_list(value: object) -> tuple[str, ...] | None:
    """*value* as a tuple of non-empty strings, or ``None`` when it is anything else."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(value)


_KeyPath = tuple[str, ...]


def _key_lines(
    node: yaml.Node | None,
    prefix: _KeyPath,
    lines: dict[_KeyPath, int],
    duplicates: list[tuple[_KeyPath, int]],
    seen: set[int],
) -> None:
    """Record the line of every mapping key (by key path) and every key that occurs twice."""
    if not isinstance(node, yaml.MappingNode) or id(node) in seen:
        return
    seen.add(id(node))
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        path = (*prefix, str(key_node.value))
        line = int(key_node.start_mark.line) + 1
        if path in lines:
            duplicates.append((path, line))
        else:
            lines[path] = line
        _key_lines(value_node, path, lines, duplicates, seen)


class _GraphReader:
    """Validates the graph (AR006) and extracts what the other rules need."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []
        self._lines: dict[_KeyPath, int] = {}

    def _bad(self, message: str, *key_path: str) -> None:
        line = 0
        for length in range(len(key_path), 0, -1):  # the nearest enclosing key that has a line
            if key_path[:length] in self._lines:
                line = self._lines[key_path[:length]]
                break
        self.violations.append(Violation("AR006", GRAPH_PATH, line, message))

    def read(self, root: Path) -> _Graph | None:
        """The graph, or ``None`` when the file is not a YAML mapping at all (reported as AR006)."""
        skipped = "the rules AR001, AR002, AR003 and AR007 were not evaluated"
        try:
            text = (root / GRAPH_PATH).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self._bad(f"the dependency graph cannot be read ({type(error).__name__}); {skipped}")
            return None
        try:
            node = yaml.compose(text, Loader=yaml.SafeLoader)
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            line = int(mark.line) + 1 if mark is not None else 0
            self.violations.append(
                Violation("AR006", GRAPH_PATH, line, f"the dependency graph is not valid YAML; {skipped}")
            )
            return None
        except (ValueError, RecursionError) as error:
            self._bad(f"the dependency graph cannot be loaded ({type(error).__name__}); {skipped}")
            return None
        duplicates: list[tuple[_KeyPath, int]] = []
        _key_lines(node, (), self._lines, duplicates, set())
        for key_path, line in duplicates:
            self.violations.append(
                Violation("AR006", GRAPH_PATH, line, f"duplicate key '{'.'.join(key_path)}'")
            )
        if not isinstance(document, dict):
            self._bad(f"the dependency graph must be a mapping; {skipped}")
            return None
        version = document.get("schema_version")
        if type(version) is not int or version != 1:
            self._bad(f"schema_version must be 1, found {version!r}", "schema_version")
        raw_sessions = self._mapping(document, "sessions")
        raw_packages = self._mapping(document, "packages")
        sessions = self._sessions(raw_sessions)
        self._session_references(raw_sessions)
        self._cycles(sessions)
        self._waves(sessions)
        self._ownership_overlaps(sessions)
        packages = self._packages(raw_packages, raw_sessions)
        graph = _Graph(
            sessions=MappingProxyType(sessions),
            packages=MappingProxyType(packages),
            third_party=MappingProxyType(self._third_party(document, raw_packages)),
            protected=self._protected(document),
        )
        self._package_directories(graph)
        self._protected_not_owned(graph)
        return graph

    def _mapping(self, document: dict[Any, Any], key: str) -> dict[Any, Any]:
        value = document.get(key)
        if isinstance(value, dict):
            return value
        self._bad(f"'{key}' must be a mapping", key)
        return {}

    # ------------------------------------------------------------------ sessions
    def _sessions(self, raw_sessions: dict[Any, Any]) -> dict[str, _Session]:
        for session_id in REQUIRED_SESSIONS:
            if session_id not in raw_sessions:
                self._bad(f"session {session_id} is missing", "sessions")
        sessions: dict[str, _Session] = {}
        for session_id, raw in raw_sessions.items():
            name = str(session_id)
            if not isinstance(session_id, str) or not isinstance(raw, dict):
                self._bad(f"session {name} must be a mapping with a string id", "sessions", name)
                continue
            for key in SESSION_REQUIRED_KEYS:
                if key not in raw:
                    self._bad(f"session {name} has no '{key}'", "sessions", name)
            wave = raw.get("wave")
            if "wave" in raw and (type(wave) is not int or wave < 1):
                self._bad(
                    f"session {name}: wave must be a positive integer, found {wave!r}", "sessions", name
                )
                wave = None
            lists: dict[str, tuple[str, ...]] = {}
            for key in (*SESSION_REFERENCE_KEYS, "owns"):
                items = _string_list(raw.get(key)) if key in raw else ()
                if items is None:
                    self._bad(f"session {name}: '{key}' must be a list of strings", "sessions", name, key)
                lists[key] = items or ()
            owns: list[str] = []
            for entry in lists["owns"]:
                if _is_safe_relative(entry):
                    owns.append(entry)
                else:
                    self._bad(
                        f"session {name}: owned path {entry!r} is not a relative POSIX path inside the "
                        "repository",
                        "sessions",
                        name,
                        "owns",
                    )
            sessions[name] = _Session(
                wave=wave if type(wave) is int else None,
                depends_on=lists["depends_on"],
                owns=tuple(owns),
            )
        return sessions

    def _session_references(self, raw_sessions: dict[Any, Any]) -> None:
        for session_id, raw in raw_sessions.items():
            if not isinstance(raw, dict):
                continue
            for key in SESSION_REFERENCE_KEYS:
                for reference in _string_list(raw.get(key)) or ():
                    if reference not in raw_sessions:
                        self._bad(
                            f"session {session_id}: {key} names {reference}, which is not a session",
                            "sessions",
                            str(session_id),
                            key,
                        )

    def _cycles(self, sessions: Mapping[str, _Session]) -> None:
        """Report every ``depends_on`` cycle once (depth-first search in a fixed order)."""
        finished: set[str] = set()
        reported: set[frozenset[str]] = set()

        def visit(session_id: str, trail: list[str]) -> None:
            if session_id in trail:
                cycle = trail[trail.index(session_id) :]
                if frozenset(cycle) not in reported:
                    reported.add(frozenset(cycle))
                    start = cycle.index(min(cycle))
                    ordered = [*cycle[start:], *cycle[:start]]
                    self._bad(
                        "depends_on has a cycle: " + " -> ".join([*ordered, ordered[0]]),
                        "sessions",
                        ordered[0],
                        "depends_on",
                    )
                return
            if session_id in finished or session_id not in sessions:
                return
            trail.append(session_id)
            for dependency in sessions[session_id].depends_on:
                visit(dependency, trail)
            trail.pop()
            finished.add(session_id)

        for session_id in sorted(sessions):
            visit(session_id, [])

    def _waves(self, sessions: Mapping[str, _Session]) -> None:
        for session_id, session in sessions.items():
            for dependency in session.depends_on:
                other = sessions.get(dependency)
                if other is None or session.wave is None or other.wave is None:
                    continue
                if other.wave > session.wave:
                    self._bad(
                        f"session {session_id} (wave {session.wave}) depends on {dependency}, which is in "
                        f"the later wave {other.wave}",
                        "sessions",
                        session_id,
                        "depends_on",
                    )

    def _ownership_overlaps(self, sessions: Mapping[str, _Session]) -> None:
        owned = sorted(
            (entry.rstrip("/"), entry, session_id)
            for session_id, session in sessions.items()
            for entry in session.owns
        )
        reported: set[tuple[str, str, str, str]] = set()
        for bare, entry, session_id in owned:
            for other_bare, other_entry, other_id in owned:
                if other_id == session_id:
                    continue
                if other_bare == bare or other_bare.startswith(bare + "/"):
                    key = (entry, session_id, other_entry, other_id)
                    mirrored = (other_entry, other_id, entry, session_id)
                    if key in reported or mirrored in reported:
                        continue
                    reported.add(key)
                    relation = "is also owned" if other_bare == bare else f"contains {other_entry}, owned"
                    self._bad(
                        f"owned paths overlap: {entry} of session {session_id} {relation} by session "
                        f"{other_id}",
                        "sessions",
                        session_id,
                        "owns",
                    )

    # ------------------------------------------------------------------ packages
    def _packages(self, raw_packages: dict[Any, Any], raw_sessions: dict[Any, Any]) -> dict[str, _Package]:
        packages: dict[str, _Package] = {}
        for package_name, raw in raw_packages.items():
            name = str(package_name)
            if not isinstance(package_name, str) or not _is_dotted_name(name) or not isinstance(raw, dict):
                self._bad(f"package {name} must be a dotted name mapped to its rules", "packages", name)
                continue
            session = raw.get("session")
            if not isinstance(session, str) or session not in raw_sessions:
                self._bad(
                    f"package {name}: session {session!r} is not a session of the graph", "packages", name
                )
                session = None
            may_import = _string_list(raw.get("may_import"))
            if may_import is None:
                self._bad(f"package {name}: 'may_import' must be a list of strings", "packages", name)
            packages[name] = _Package(session=session, may_import=may_import or ())
        for name, package in packages.items():
            for entry in package.may_import:
                target = entry.removesuffix(".*")
                known = entry.endswith(".*") and _is_dotted_name(target)
                known = known or any(target == other or target.startswith(other + ".") for other in packages)
                if not known:
                    self._bad(
                        f"package {name}: may_import names {entry}, which is not a declared package",
                        "packages",
                        name,
                    )
        return packages

    def _third_party(
        self, document: dict[Any, Any], raw_packages: dict[Any, Any]
    ) -> dict[str, frozenset[str]]:
        raw = document.get("third_party")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self._bad("'third_party' must map a package to its list of allowed modules", "third_party")
            return {}
        allowed: dict[str, frozenset[str]] = {}
        for package_name, modules in raw.items():
            name = str(package_name)
            if package_name not in raw_packages:
                self._bad(f"third_party names {name}, which is not a declared package", "third_party", name)
            items = _string_list(modules)
            if items is None:
                self._bad(f"third_party of {name} must be a list of module names", "third_party", name)
            # An unreadable list restricts the package to the standard library: fail closed.
            allowed[name] = frozenset(item.split(".")[0] for item in items or ())
        return allowed

    def _protected(self, document: dict[Any, Any]) -> frozenset[str]:
        raw = document.get("protected_paths")
        if raw is None:
            return frozenset()
        paths = raw.get("paths") if isinstance(raw, dict) else None
        if not isinstance(paths, dict) or not all(isinstance(path, str) for path in paths):
            self._bad("'protected_paths' must be a mapping with a 'paths' mapping", "protected_paths")
            return frozenset()
        return frozenset(paths)

    def _package_directories(self, graph: _Graph) -> None:
        for name, package in graph.packages.items():
            if package.session is None:
                continue
            directory = name.replace(".", "/") + "/"
            owner = graph.owner_of(directory)
            if owner != package.session:
                holder = "no session" if owner is None else f"session {owner}"
                self._bad(
                    f"package {name} belongs to session {package.session} but its directory {directory} "
                    f"is owned by {holder}",
                    "packages",
                    name,
                )

    def _protected_not_owned(self, graph: _Graph) -> None:
        for path in sorted(graph.protected):
            owner = graph.owner_of(path)
            if owner is not None:
                self._bad(
                    f"protected path {path} is also owned by session {owner}: nobody edits a protected path",
                    "protected_paths",
                    "paths",
                    path,
                )


# ============================================================================== source files
@dataclass(frozen=True)
class _Source:
    path: str
    #: Graph key of the package the file belongs to (``gie.core``; ``gie`` for the root package).
    package: str
    #: The Python package that relative imports of this file are resolved against.
    context: tuple[str, ...]
    tree: ast.Module


def _discover(root: Path, top: str) -> tuple[list[str], list[str]]:
    """``(Python source files, symbolic links)`` below ``root/top``, in a fixed order."""
    sources: list[str] = []
    links: list[str] = []
    base = root.joinpath(*top.split("/"))
    if base.is_symlink():
        return sources, [top]
    if not base.is_dir():
        return sources, links
    for directory, subdirectories, files in os.walk(base):
        here = Path(directory)
        kept: list[str] = []
        for name in sorted(subdirectories):
            if (here / name).is_symlink():
                links.append((here / name).relative_to(root).as_posix())
            elif name != "__pycache__":
                kept.append(name)
        subdirectories[:] = kept
        for name in sorted(files):
            relative = (here / name).relative_to(root).as_posix()
            if (here / name).is_symlink():
                links.append(relative)
            elif Path(name).suffix.lower() in SOURCE_SUFFIXES:
                sources.append(relative)
    return sources, links


def _package_of(path: str) -> tuple[str, tuple[str, ...]]:
    """``(graph key, import context)`` of the source file at *path*."""
    parts = PurePosixPath(path).parts
    context = parts[:-1]
    if parts[:2] == tuple(DASHBOARD_TREE.split("/")):
        return DASHBOARD_PACKAGE, context
    if len(parts) == 2:  # directly below gie/: the root package itself, or a module of its own
        stem = PurePosixPath(path).stem
        return (GIE_TREE if stem == "__init__" else f"{GIE_TREE}.{stem}"), context
    return ".".join(parts[:2]), context


def _load(root: Path, path: str) -> _Source | Violation:
    """Parse one file; a file that cannot be read or parsed is an AR000 violation, never an exception."""
    try:
        data = (root / path).read_bytes()
    except OSError as error:
        return Violation("AR000", path, 0, f"the file cannot be read ({type(error).__name__})")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escape sequences: not this tool's business
            tree = ast.parse(data, filename=path)
    except SyntaxError as error:
        return Violation("AR000", path, int(error.lineno or 0), f"the file cannot be parsed: {error.msg}")
    except (ValueError, RecursionError, MemoryError) as error:
        return Violation("AR000", path, 0, f"the file cannot be parsed ({type(error).__name__})")
    package, context = _package_of(path)
    return _Source(path=path, package=package, context=context, tree=tree)


# ============================================================================== names
class _Names:
    """What each name of a file may refer to, as dotted qualified names (flow-insensitive)."""

    def __init__(self, tree: ast.Module) -> None:
        self._table: dict[str, set[str]] = {}
        self._stars: set[str] = set()
        #: Names the file binds itself (assignment, parameter, definition): they shadow built-ins.
        self.local: set[str] = set()
        nodes = list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    self._table.setdefault(bound, set()).add(alias.name if alias.asname else bound)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name == "*":
                        self._stars.add(node.module)
                    else:
                        qualified = f"{node.module}.{alias.name}"
                        self._table.setdefault(alias.asname or alias.name, set()).add(qualified)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.local.add(node.id)
            elif isinstance(node, ast.arg):
                self.local.add(node.arg)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.local.add(node.name)
        self.imported: frozenset[str] = frozenset(self._table)
        for _ in range(4):  # plain re-assignments (``now = datetime.now``), to a small fixed point
            changed = False
            for node in nodes:
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name | ast.Attribute):
                    continue
                found = self.qualify(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name) and not found <= self._table.get(target.id, set()):
                        self._table.setdefault(target.id, set()).update(found)
                        changed = True
            if not changed:
                break

    def qualify(self, node: ast.expr) -> frozenset[str]:
        """The qualified names the expression may denote (empty when it is not a plain dotted name)."""
        if isinstance(node, ast.Name):
            found = set(self._table.get(node.id, ()))
            if node.id not in self._table:
                found.update(f"{module}.{node.id}" for module in self._stars)
            return frozenset(found)
        if isinstance(node, ast.Attribute):
            return frozenset(f"{base}.{node.attr}" for base in self.qualify(node.value))
        return frozenset()


def _literal_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# ============================================================================== AR001 undeclared packages
def _contains_python(directory: Path) -> bool:
    for _current, subdirectories, files in os.walk(directory):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        if any(Path(name).suffix.lower() in SOURCE_SUFFIXES for name in files):
            return True
    return False


def _undeclared_packages(root: Path, graph: _Graph) -> Iterator[Violation]:
    base = root / GIE_TREE
    if not base.is_dir() or base.is_symlink():
        return
    for entry in sorted(base.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            continue  # reported as AR000 by the discovery of source files
        if entry.is_dir() and entry.name != "__pycache__":
            name = f"{GIE_TREE}.{entry.name}"
            if name not in graph.packages and _contains_python(entry):
                yield Violation(
                    "AR001",
                    f"{GIE_TREE}/{entry.name}/",
                    0,
                    f"package {name} contains Python files but is not declared under 'packages' in "
                    f"{GRAPH_PATH}; until it is, its imports cannot be checked",
                )
        elif entry.is_file() and entry.suffix.lower() in SOURCE_SUFFIXES and entry.stem != "__init__":
            name = f"{GIE_TREE}.{entry.stem}"
            if name not in graph.packages:
                yield Violation(
                    "AR001",
                    f"{GIE_TREE}/{entry.name}",
                    0,
                    f"module {name} lies outside every declared package; move it into a package or "
                    f"declare it under 'packages' in {GRAPH_PATH}",
                )


# ============================================================================== AR002 / AR003 imports
@dataclass(frozen=True)
class _ImportRef:
    line: int
    #: Absolute dotted name of the imported module, or ``None`` for a relative import that leaves
    #: the top-level package.
    module: str | None
    star: bool = False


def _module_exists(root: Path, parts: Sequence[str], graph: _Graph) -> bool:
    if ".".join(parts) in graph.packages:
        return True
    directory = root.joinpath(*parts)
    return directory.is_dir() or any(
        root.joinpath(*parts[:-1], parts[-1] + suffix).is_file() for suffix in sorted(SOURCE_SUFFIXES)
    )


def _from_import_refs(
    source: _Source, node: ast.ImportFrom, root: Path, graph: _Graph
) -> Iterator[_ImportRef]:
    if node.level == 0:
        base = tuple((node.module or "").split("."))
    else:
        climb = node.level - 1
        if climb >= len(source.context):
            yield _ImportRef(node.lineno, None)
            return
        base = source.context[: len(source.context) - climb]
        if node.module:
            base = (*base, *node.module.split("."))
    for alias in node.names:
        if alias.name == "*":
            yield _ImportRef(node.lineno, ".".join(base), star=True)
        elif _module_exists(root, (*base, alias.name), graph):
            yield _ImportRef(node.lineno, ".".join((*base, alias.name)))
        else:  # an attribute of the module, not a sub-module
            yield _ImportRef(node.lineno, ".".join(base))


def _import_refs(source: _Source, names: _Names, root: Path, graph: _Graph) -> Iterator[_ImportRef]:
    """Every import of the file, wherever it is written, with relative imports made absolute."""
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _ImportRef(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            yield from _from_import_refs(source, node, root, graph)
        elif isinstance(node, ast.Call) and node.args:
            called = names.qualify(node.func)
            dynamic = "importlib.import_module" in called or (
                isinstance(node.func, ast.Name) and node.func.id == "__import__" and not called
            )
            literal = _literal_str(node.args[0])
            if dynamic and literal and not literal.startswith("."):
                yield _ImportRef(node.lineno, literal)


def _internal_roots(graph: _Graph) -> frozenset[str]:
    return frozenset({GIE_TREE, *(name.split(".")[0] for name in graph.packages)})


def _package_key(module: str, graph: _Graph) -> str:
    """The graph key of an internal module: the longest declared package, else its first two parts."""
    parts = module.split(".")
    for length in range(len(parts), 0, -1):
        if ".".join(parts[:length]) in graph.packages:
            return ".".join(parts[:length])
    return ".".join(parts[:2])


def _allowed(importer: str, module: str, graph: _Graph) -> bool:
    key = _package_key(module, graph)
    if key in {importer, GIE_TREE}:  # its own package; the bare root package holds no subsystem code
        return True
    package = graph.packages.get(importer)
    for entry in package.may_import if package is not None else ():
        if entry == ANYTHING:
            return True
        target = entry.removesuffix(".*")
        if module == target or module.startswith(target + "."):
            return True
    return False


def _import_rules(source: _Source, names: _Names, root: Path, graph: _Graph) -> Iterator[Violation]:
    internal = _internal_roots(graph)
    restricted = graph.third_party.get(source.package)
    package = graph.packages.get(source.package)
    allowed_text = ", ".join(["itself", *(package.may_import if package is not None else ())])
    for ref in _import_refs(source, names, root, graph):
        if ref.module is None:
            yield Violation(
                "AR002", source.path, ref.line, "relative import leaves the top-level package of the file"
            )
            continue
        top = ref.module.split(".")[0]
        if top in internal:
            if ref.star and ref.module == GIE_TREE:
                yield Violation(
                    "AR002",
                    source.path,
                    ref.line,
                    f"star import from the root package {GIE_TREE} hides what is imported; name the package",
                )
            elif not _allowed(source.package, ref.module, graph):
                yield Violation(
                    "AR002",
                    source.path,
                    ref.line,
                    f"{source.package} may not import {ref.module} (package "
                    f"{_package_key(ref.module, graph)}); allowed: {allowed_text}",
                )
        elif restricted is not None and top not in sys.stdlib_module_names and top not in restricted:
            permitted = ", ".join(sorted(restricted)) or "none (standard library only)"
            yield Violation(
                "AR003",
                source.path,
                ref.line,
                f"{source.package} may not import {top}: it is neither the standard library nor on the "
                f"third_party allow-list of the package ({permitted})",
            )


# ============================================================================== AR004 / AR005 determinism
def _is_seeded(call: ast.Call) -> bool:
    """Whether a generator constructor call carries a seed (a literal ``None`` is not a seed)."""
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    if not values:
        return False
    only = values[0]
    return not (len(values) == 1 and isinstance(only, ast.Constant) and only.value is None)


def _verdict(qualified: str, call: ast.Call | None) -> tuple[str, str] | None:
    """``(rule, message)`` when using the qualified name breaks AR004 or AR005, else ``None``."""
    if qualified in WALL_CLOCK_NAMES:
        return "AR004", f"wall-clock access {qualified}: {_CLOCK_ADVICE}"
    if qualified == BUILTIN_HASH:
        return "AR005", _HASH_ADVICE
    if qualified in NON_DETERMINISTIC_NAMES:
        return "AR005", f"{qualified} is not deterministic: identifiers come from gie.contracts.identity"
    if qualified in RANDOM_TYPE_NAMES:
        return None
    if qualified in SEEDABLE_CONSTRUCTORS:
        if call is None or _is_seeded(call):
            return None
        return "AR005", f"{qualified}() without a seed is not reproducible: pass the seed of the run"
    for module in NON_DETERMINISTIC_MODULES:
        if qualified.startswith(module + ".") and "." not in qualified[len(module) + 1 :]:
            if module == "secrets":
                return "AR005", f"{qualified} is not deterministic: the secrets module is random by design"
            return (
                "AR005",
                f"{qualified} uses the hidden generator of the module {module}: use an explicitly seeded "
                "generator object",
            )
    return None


class _Determinism(ast.NodeVisitor):
    """Collects AR004 / AR005 findings of one file."""

    def __init__(self, source: _Source, names: _Names) -> None:
        self._source = source
        self._names = names
        self._clock_exempt = source.path == CLOCK_MODULE_PATH
        self._functions: list[str] = []
        self._calls: dict[int, ast.Call] = {}
        self.found: set[Violation] = set()

    def _report(self, node: ast.AST, qualified: str, call: ast.Call | None = None) -> None:
        verdict = _verdict(qualified, call)
        if verdict is None or (verdict[0] == "AR004" and self._clock_exempt):
            return
        if qualified == BUILTIN_HASH and "__hash__" in self._functions:
            return
        self.found.add(Violation(verdict[0], self._source.path, int(getattr(node, "lineno", 0)), verdict[1]))

    def _reference(self, node: ast.Name | ast.Attribute) -> None:
        call = self._calls.get(id(node))
        for qualified in sorted(self._names.qualify(node)):
            self._report(node, qualified, call)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "secrets":
                self.found.add(
                    Violation(
                        "AR005",
                        self._source.path,
                        node.lineno,
                        "import of secrets: the module is random by design and has no deterministic use",
                    )
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for alias in node.names:
            if alias.name != "*":
                self._report(node, f"{node.module}.{alias.name}")

    def visit_Call(self, node: ast.Call) -> None:
        self._calls[id(node.func)] = node
        function = node.func
        if isinstance(function, ast.Name) and function.id not in self._names.imported:
            if function.id == "hash":
                self._report(node, BUILTIN_HASH)
            elif function.id == "getattr" and len(node.args) >= 2:
                attribute = _literal_str(node.args[1])
                for base in sorted(self._names.qualify(node.args[0])) if attribute else ():
                    self._report(node, f"{base}.{attribute}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._reference(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        self._reference(node)
        is_builtin_hash = node.id == "hash" and node.id not in self._names.imported
        # The built-in handed over as a value (``key=hash``); calls are reported by visit_Call.
        if is_builtin_hash and node.id not in self._names.local and id(node) not in self._calls:
            self._report(node, BUILTIN_HASH)


def _determinism(source: _Source, names: _Names) -> list[Violation]:
    visitor = _Determinism(source, names)
    try:
        visitor.visit(source.tree)
    except RecursionError:
        return [Violation("AR000", source.path, 0, "the file is nested too deeply to analyse")]
    return sorted(visitor.found, key=_order)


# ============================================================================== AR007 ownership
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


def tracked_files(root: Path) -> list[str] | str:
    """The files git tracks below *root*, or the reason why they cannot be listed."""
    probe = _git(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    if probe is None:
        return "git cannot be started"
    if probe[0] != 0 or not probe[1].startswith("true"):
        return "the root is not inside a git work tree"
    if probe[1].split("\n", 1)[-1].strip():
        return "the root is not the top level of the git work tree"
    listing = _git(root, "ls-files", "-z")
    if listing is None or listing[0] != 0:
        return "git ls-files failed"
    return sorted(path for path in listing[1].split("\0") if path)


def _ownership(graph: _Graph, tracked: Iterable[str]) -> Iterator[Violation]:
    for path in sorted(set(tracked)):
        if path in graph.protected or graph.owner_of(path) is not None:
            continue
        yield Violation(
            "AR007",
            path,
            0,
            f"tracked file is not owned by any session ('owns' in {GRAPH_PATH}) and is not a protected path",
        )


# ============================================================================== entry points
def scan(repo_root: Path, *, tracked: Iterable[str] | None = None, use_git: bool = True) -> Report:
    """Check the repository at *repo_root* and return violations, notes and counters.

    *tracked* replaces the file list of ``git ls-files`` for AR007 (POSIX paths relative to the
    root); with ``use_git=False`` and no *tracked* list the rule is skipped with a note.  Nothing
    is written, imported or executed.
    """
    violations: list[Violation] = []
    notes: list[str] = []
    reader = _GraphReader()
    graph = reader.read(repo_root)
    violations.extend(reader.violations)

    trees = [GIE_TREE]
    if graph is not None and DASHBOARD_PACKAGE in graph.packages:
        trees.append(DASHBOARD_TREE)
    scanned = 0
    for top in trees:
        paths, links = _discover(repo_root, top)
        violations.extend(
            Violation("AR000", link, 0, "symbolic link: the scan does not follow links, so it is unverified")
            for link in links
        )
        for path in paths:
            scanned += 1
            loaded = _load(repo_root, path)
            if isinstance(loaded, Violation):
                violations.append(loaded)
                continue
            names = _Names(loaded.tree)
            declared = loaded.package in {GIE_TREE, DASHBOARD_PACKAGE} or (
                graph is not None and loaded.package in graph.packages
            )
            if graph is not None and declared:
                violations.extend(_import_rules(loaded, names, repo_root, graph))
            if top == GIE_TREE:
                violations.extend(_determinism(loaded, names))

    tracked_count: int | None = None
    if graph is not None:
        violations.extend(_undeclared_packages(repo_root, graph))
        listed: list[str] | str
        if tracked is not None:
            listed = sorted(set(tracked))
        elif use_git:
            listed = tracked_files(repo_root)
        else:
            listed = "git was not used"
        if isinstance(listed, str):
            notes.append(f"AR007 (ownership of tracked files) was skipped: {listed}")
        else:
            tracked_count = len(listed)
            violations.extend(_ownership(graph, listed))
    return Report(
        violations=tuple(sorted(set(violations), key=_order)),
        notes=tuple(notes),
        python_files=scanned,
        tracked_files=tracked_count,
    )


def check(repo_root: Path, *, tracked: Iterable[str] | None = None, use_git: bool = True) -> list[Violation]:
    """Every violation in the repository at *repo_root*, sorted by path, line and rule (see :func:`scan`)."""
    return list(scan(repo_root, tracked=tracked, use_git=use_git).violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_architecture",
        description="Architecture, ownership and determinism rules of the dependency graph (AR000-AR007).",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    if not root.is_dir():
        print(f"check_architecture: {root} is not a directory", file=sys.stderr)
        return 2
    report = scan(root)
    violations = report.violations
    if arguments.as_json:
        document = {
            "schema_version": 1,
            "tool": "tools/check_architecture.py",
            "ok": not violations,
            "python_files_scanned": report.python_files,
            "tracked_files_checked": report.tracked_files,
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
        print(f"check_architecture: note: {note}")
    if violations:
        print(f"check_architecture: FAILED - {len(violations)} violation(s)", file=sys.stderr)
        return 1
    ownership = (
        "ownership not checked (see note)"
        if report.tracked_files is None
        else f"{report.tracked_files} tracked files owned"
    )
    print(
        f"check_architecture: OK - {report.python_files} Python files scanned, {ownership}; no violation "
        f"of {min(RULES)}-{max(RULES)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
