"""Documents name only paths that exist (rules DP000-DP001).

Requirements: M-AUTH-08 (never mark placeholders complete), OWN-21 (do not declare incomplete
components finished).  A document that names ``tools/some_tool.py`` claims that the file exists; a
name that leads nowhere is either a typo or a component that was announced and never delivered.

Usage::

    python -m tools.check_docs_paths [--root PATH] [--json]

One line per violation (``path:line: RULE message``) on standard output and exit status 1; with no
violation a one-line ``OK`` summary and exit status 0; exit status 2 for a command-line error.
Informational notes are printed as ``check_docs_paths: note: ...`` and never change the exit
status.  ``--json`` prints one JSON document instead.  Nothing is written.

What is scanned
---------------
:data:`DOCUMENT_PATTERNS`: ``README.md``, ``CLAUDE.md``, ``docs/*.md``, ``specifications/*.md``,
``specifications/baselines/*.md`` and ``tools/audit/README.md`` (patterns do not descend into
sub-directories).  In each document every inline-code span - text between backticks - outside
fenced code blocks is looked at.  Fenced blocks (three or more backticks or tildes) are skipped:
they hold commands and examples, not claims about the repository.

What counts as a repository path
--------------------------------
A span is checked when it

* consists only of the characters ``A-Z a-z 0-9 _ . / -``,
* contains a ``/`` **or** ends with one of :data:`KNOWN_EXTENSIONS` after a non-empty name,
* does not start with ``/`` and does not contain ``..``.

Everything else is ignored, in particular spans that contain ``<``, ``>``, ``*``, ``{``, ``}``,
``|``, a space, a backslash, a colon (drive letters such as ``D:``, URLs, ``path:line``
references), spans that start with ``www.``, and Python dotted module names without a slash
(``gie.core.clock`` has no file extension, so it is never taken for a path).  A span without any
letter (a date, a fraction) is not a path either.

A checked path must exist relative to the repository root, with exactly the spelling used in the
document (upper and lower case are compared exactly on every operating system).  A directory may
be written with or without a trailing slash; a path written with a trailing slash must be a
directory.

Rules
-----
DP000  The ignore list cannot be used: ``specifications/dependency_graph.yaml`` exists but is not
       valid YAML, or ``docs_path_ignore`` is not a list of strings.  Fail closed.
DP001  A document names a path that does not exist.

The ignore list
---------------
``docs_path_ignore`` in ``specifications/dependency_graph.yaml`` (optional; a missing key or a
missing file means "empty") lists exact strings that are not reported.  It is meant for paths that
are planned and that the documents explicitly describe as not existing yet (``gie/app/``,
``gie/market_data/``).  An entry matches the same path with or without a trailing slash; it does
not cover paths below it.  An entry whose path exists by now is reported as a note, so that the
list does not keep describing delivered work as planned.

Honest limits
-------------
The scan reads Markdown with a small line-based reader, not a full CommonMark parser: indented
code blocks are treated as text, and a bare file name is resolved against the repository root, not
against the directory of the document.  A span that merely looks like a path - a git branch name
such as ``origin/main``, a MIME type - is reported; such names belong on the ignore list or outside
backticks.  The check proves that a named path exists, not that its content is complete.
"""

from __future__ import annotations

import argparse
import json
import os
import string
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: Rule ids and their one-line titles (also written to the ``--json`` output).
RULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "DP000": "the ignore list cannot be used",
        "DP001": "document names a path that does not exist",
    }
)

GRAPH_PATH: Final = "specifications/dependency_graph.yaml"
IGNORE_KEY: Final = "docs_path_ignore"
#: Documents that are scanned; ``*`` matches within one directory only.
DOCUMENT_PATTERNS: Final[tuple[str, ...]] = (
    "README.md",
    "CLAUDE.md",
    "docs/*.md",
    "specifications/*.md",
    "specifications/baselines/*.md",
    "tools/audit/README.md",
)
#: A span without a slash is a path only when it ends with one of these.
KNOWN_EXTENSIONS: Final[tuple[str, ...]] = (
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".jsonl",
    ".pdf",
    ".txt",
    ".ini",
    ".cfg",
)

_PATH_CHARACTERS: Final = frozenset(string.ascii_letters + string.digits + "_./-")
_FENCE_CHARACTERS: Final = "`~"
_FENCE_MINIMUM: Final = 3
_LIST_MARKERS: Final[tuple[str, ...]] = ("- ", "* ", "+ ")


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
    documents: tuple[str, ...]
    #: Number of inline-code spans that were taken for a repository path and checked.
    paths_checked: int


def _order(violation: Violation) -> tuple[str, int, str, str]:
    return (violation.path, violation.line, violation.rule, violation.message)


# ============================================================================== Markdown
def _content(line: str) -> str:
    """The line without indentation and block-quote markers (where a fence or a marker would start)."""
    return line.lstrip(" \t>")


def _fence_start(line: str) -> tuple[str, int] | None:
    """``(fence character, length)`` when the line opens a fenced code block."""
    text = _content(line)
    if not text or text[0] not in _FENCE_CHARACTERS:
        return None
    character = text[0]
    length = len(text) - len(text.lstrip(character))
    if length < _FENCE_MINIMUM:
        return None
    if character == "`" and "`" in text[length:]:
        return None  # ```like this``` on one line is an inline span, not a fence
    return character, length


def _fence_end(line: str, character: str, length: int) -> bool:
    text = _content(line).rstrip()
    return len(text) >= length and set(text) == {character}


def _starts_block(line: str) -> bool:
    """Whether the line begins a new block: a heading, a table row or a list item."""
    text = _content(line)
    if text.startswith(("#", "|", *_LIST_MARKERS)):
        return True
    digits = len(text) - len(text.lstrip(string.digits))
    return 0 < digits < len(text) - 1 and text[digits] in ".)" and text[digits + 1] == " "


def _ends_block(line: str) -> bool:
    """Whether nothing can continue the line: headings and table rows are one line long."""
    return _content(line).startswith(("#", "|"))


def _blocks(text: str) -> Iterator[tuple[int, str]]:
    """``(first line number, text)`` of every block outside fenced code blocks."""
    block: list[str] = []
    first = 0
    fence: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            if _fence_end(line, *fence):
                fence = None
            continue
        opening = _fence_start(line)
        boundary = opening is not None or not line.strip() or _starts_block(line)
        if boundary and block:
            yield first, "\n".join(block)
            block = []
        if opening is not None:
            fence = opening
            continue
        if not line.strip():
            continue
        if not block:
            first = number
        block.append(line)
        if _ends_block(line):
            yield first, "\n".join(block)
            block = []
    if block:
        yield first, "\n".join(block)


def _run_length(text: str, start: int) -> int:
    end = start
    while end < len(text) and text[end] == "`":
        end += 1
    return end - start


def _closing_run(text: str, start: int, length: int) -> int:
    """Index of the next run of exactly *length* backticks at or after *start*, or -1."""
    position = text.find("`", start)
    while position != -1:
        found = _run_length(text, position)
        if found == length:
            return position
        position = text.find("`", position + found)
    return -1


def _spans_of_block(text: str) -> Iterator[tuple[int, str]]:
    """``(offset, content)`` of every inline-code span of one block (CommonMark delimiter rule)."""
    position = 0
    while position < len(text):
        character = text[position]
        if character == "\\" and text[position + 1 : position + 2] == "`":
            position += 2  # an escaped backtick is literal text
            continue
        if character != "`":
            position += 1
            continue
        length = _run_length(text, position)
        closing = _closing_run(text, position + length, length)
        if closing == -1:
            position += length  # an unmatched run is literal text
            continue
        content = text[position + length : closing].replace("\n", " ")
        if len(content) > 1 and content[0] == " " == content[-1] and content.strip():
            content = content[1:-1]
        yield position, content
        position = closing + length


def inline_code_spans(text: str) -> Iterator[tuple[int, str]]:
    """``(line number, content)`` of every inline-code span of a Markdown text outside fenced blocks."""
    for first, block in _blocks(text):
        for offset, content in _spans_of_block(block):
            yield first + block.count("\n", 0, offset), content


# ============================================================================== paths
def looks_like_path(span: str) -> bool:
    """Whether an inline-code span is taken for a repository path (see the module documentation)."""
    if not span or not set(span) <= _PATH_CHARACTERS:
        return False
    if span.startswith("/") or ".." in span or span.lower().startswith("www."):
        return False
    if not any(character.isalpha() for character in span):
        return False
    if "/" in span:
        return True
    lowered = span.lower()
    return any(lowered.endswith(extension) and len(span) > len(extension) for extension in KNOWN_EXTENSIONS)


class _Tree:
    """Exact-case existence checks below one root, with the directory listings cached."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._listings: dict[Path, frozenset[str] | None] = {}

    def _names(self, directory: Path) -> frozenset[str] | None:
        if directory not in self._listings:
            try:
                self._listings[directory] = frozenset(os.listdir(directory))
            except OSError:
                self._listings[directory] = None
        return self._listings[directory]

    def exists(self, path: str) -> bool:
        """Whether *path* exists below the root in exactly this spelling (trailing slash: a directory)."""
        current = self._root
        for part in path.split("/"):
            if part in {"", "."}:
                continue
            names = self._names(current)
            if names is None or part not in names:
                return False
            current = current / part
        return current.is_dir() if path.endswith("/") else True


def documents(root: Path) -> list[str]:
    """The documents to scan below *root*, as sorted POSIX paths."""
    found: set[str] = set()
    for pattern in DOCUMENT_PATTERNS:
        found.update(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    return sorted(found)


def _read_ignore(root: Path) -> frozenset[str] | Violation:
    """The ignore list (without trailing slashes), or the DP000 violation that makes it unusable."""
    try:
        text = (root / GRAPH_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError) as error:
        return Violation("DP000", GRAPH_PATH, 0, f"the file cannot be read ({type(error).__name__})")
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as error:
        mark = getattr(error, "problem_mark", None)
        line = int(mark.line) + 1 if mark is not None else 0
        return Violation(
            "DP000", GRAPH_PATH, line, "the file is not valid YAML, so the ignore list is unknown"
        )
    entries = document.get(IGNORE_KEY) if isinstance(document, dict) else None
    if entries is None:
        return frozenset()
    if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
        return Violation("DP000", GRAPH_PATH, 0, f"'{IGNORE_KEY}' must be a list of non-empty strings")
    return frozenset(entry.rstrip("/") for entry in entries)


# ============================================================================== entry points
def scan(repo_root: Path) -> Report:
    """Check the documents of the repository at *repo_root*; nothing is written."""
    violations: list[Violation] = []
    notes: list[str] = []
    ignore = _read_ignore(repo_root)
    if isinstance(ignore, Violation):
        violations.append(ignore)
        ignore = frozenset()
    tree = _Tree(repo_root)
    scanned = documents(repo_root)
    checked = 0
    for document in scanned:
        try:
            text = (repo_root / document).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            violations.append(
                Violation("DP001", document, 0, f"the document cannot be read ({type(error).__name__})")
            )
            continue
        for line, span in inline_code_spans(text):
            if not looks_like_path(span) or span.rstrip("/") in ignore:
                continue
            checked += 1
            if not tree.exists(span):
                violations.append(
                    Violation(
                        "DP001", document, line, f"`{span}` is named but does not exist in the repository"
                    )
                )
    notes.extend(
        f"'{IGNORE_KEY}' lists {entry}, which exists by now: remove the entry"
        for entry in sorted(ignore)
        if entry.strip("/") and tree.exists(entry)
    )
    return Report(
        violations=tuple(sorted(set(violations), key=_order)),
        notes=tuple(notes),
        documents=tuple(scanned),
        paths_checked=checked,
    )


def check(repo_root: Path) -> list[Violation]:
    """Every violation in the documents below *repo_root*, sorted by path, line and rule."""
    return list(scan(repo_root).violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_docs_paths",
        description="Documents name only paths that exist (rules DP000-DP001).",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    if not root.is_dir():
        print(f"check_docs_paths: {root} is not a directory", file=sys.stderr)
        return 2
    report = scan(root)
    violations = report.violations
    if arguments.as_json:
        payload = {
            "schema_version": 1,
            "tool": "tools/check_docs_paths.py",
            "ok": not violations,
            "documents": list(report.documents),
            "paths_checked": report.paths_checked,
            "violation_count": len(violations),
            "rules": dict(RULES),
            "violations": [asdict(violation) for violation in violations],
            "notes": list(report.notes),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if violations else 0
    for violation in violations:
        print(violation.render())
    for note in report.notes:
        print(f"check_docs_paths: note: {note}")
    if violations:
        print(f"check_docs_paths: FAILED - {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print(
        f"check_docs_paths: OK - {len(report.documents)} documents scanned, {report.paths_checked} named "
        f"paths exist; no violation of {min(RULES)}-{max(RULES)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
