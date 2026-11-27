"""No credentials in the repository (rules SC000-SC006).

Requirement: M-AUTH-15 ("Do not expose credentials"); CLAUDE.md rule 6 (no credentials in code,
logs, fixtures, evidence or commits).

Usage::

    python -m tools.check_no_secrets [--root PATH] [--json]

One line per violation (``path:line: RULE message``) on standard output and exit status 1; with no
violation a one-line ``OK`` summary and exit status 0; exit status 2 for a command-line error.
Informational notes are printed as ``check_no_secrets: note: ...`` and never change the exit
status.  ``--json`` prints one JSON document instead.  Nothing is written.

**The output never contains a matched value.**  A finding names the rule, the file, the line and,
where there is one, the *name* the value is assigned to - never the value, a part of it, the user
name or the host of a URL.  Printing a secret into a CI log would expose it a second time.

What is scanned
---------------
Every file git tracks plus every untracked file that is not ignored
(``git ls-files -co --exclude-standard``, read-only).  Without git, or when the root is not the top
level of a work tree, the tree is walked instead (everything except ``.git`` and the tool caches
of :data:`WALK_SKIPPED_DIRECTORIES`) and a note says so.  Symbolic links are not followed.

* Text files are decoded as UTF-8 (UTF-16 with a byte-order mark is decoded too).
* Binary files - a NUL byte in the first 8 KiB - are skipped, except PDF files, which are scanned
  as raw bytes.  The bodies of *encoded* PDF streams (``/Filter``) are left out: they are
  compressed or ASCII-armoured data in which nothing is readable and everything looks random.
* ``specifications/source/*.pdf`` (the owner's specification) is not opened at all.
* SC004 looks at file names only, so it applies to binary files as well.

Rules
-----
SC000  A line of the allow-list cannot be used (unknown rule, malformed entry).  Fail closed.
SC001  Private key header (``-----BEGIN`` ... ``PRIVATE KEY-----``, any key type).
SC002  A name that contains ``password``, ``passwd``, ``pwd``, ``secret``, ``token``, ``api_key``,
       ``apikey``, ``access_key`` or ``auth`` is given a literal value of six or more characters
       that is not an obvious placeholder.  Forms: ``NAME = "value"``, ``NAME: value`` (YAML),
       ``"name": "value"`` (JSON), ``NAME=value`` (.env).  In Python files the forms are read from
       the syntax tree: assignments, dictionary entries, keyword arguments and parameter defaults
       whose value is a string or bytes literal.
SC003  URL with embedded credentials: a user name and a password before the host, as in
       ``scheme://user:<password>@host``.
SC004  A file that must never be committed: ``.env``, ``.env.*``, ``*.pem``, ``*.key``, ``*.pfx``,
       ``*.p12``, ``id_rsa*``, ``*.kdbx``.
SC005  Broker login with a literal password (Python, syntax tree): a call named ``login`` or
       ``initialize`` with a keyword ``password=`` whose value is a string literal.
SC006  A token of 32 or more characters from ``A-Z a-z 0-9 + / = _ -`` whose Shannon entropy is
       above 4.5 bits per character.  Hexadecimal-only strings are excluded (hashes, commit ids and
       GIE identifiers are hexadecimal), and so are files below ``specifications/source/`` and
       ``gie/contracts/schemas/``.  A run that is nothing but ordinary pieces joined by separators
       is not a token (see below).

Placeholders (not reported by SC002, SC003 and SC005)
-----------------------------------------------------
A value that contains ``FAKE``, ``EXAMPLE``, ``PLACEHOLDER``, ``REDACTED`` or ``CHANGEME`` (in any
case), an angle-bracket placeholder (``<...>``), a dollar-brace or double-brace placeholder, a
reference to the environment (``os.environ``, ``getenv``, a leading ``$``), the words ``None``,
``null`` and ``empty``, the empty value, and a mask made of one repeated character (``********``).

The allow-list
--------------
``specifications/secret_scan_allowlist.txt`` (optional) suppresses reviewed false positives.  One
entry per line, ``RULE path:line`` or ``RULE path`` (the whole file for that rule); blank lines and
``#`` comments are allowed.  An entry that suppresses nothing is reported as a note.  SC000 cannot
be allow-listed.

Deliberate reading of the rule text
-----------------------------------
* SC006, narrower than the plain rule text: the characters ``/ = _ - +`` belong to the token
  alphabet but are also what joins words into paths, identifiers and keyword arguments, and such
  runs reach more than 4.5 bits per character as soon as they mix upper case, lower case and a
  date (``specifications/source/OWNER_DIRECTIVE_2026-11-27``).  A run is therefore not reported
  when every piece between those separators is *ordinary*: a hexadecimal piece of any length, or
  a piece of at most 24 characters that is a number, a lower-case, capitalised or camel-case word
  with optional trailing digits, or an upper-case word with optional digits.  A run without any
  separator is never ordinary (it is longer than 24 characters), so for such tokens the rule is
  exactly the rule text; a random base64 token would need every one of its pieces to look like a
  word to slip through.
* SC002, narrower than "contains ``auth``": the name parts ``author``, ``authors``, ``authored``,
  ``authorship``, ``authority``, ``authorities`` and ``authoritative`` say who wrote or approved
  something and are not counted; ``auth``, ``authorization``, ``authentication`` and every other
  name that contains ``auth`` are.  Names are split at ``_``, ``-``, ``.`` and camel-case humps
  before they are compared, so ``apiKey`` and ``api-key`` are read like ``api_key``.
* Typed formats: in Python, JSON and TOML only *string* literals count as a value; numbers,
  booleans and expressions are not credentials.  In YAML, ``.env``, INI and other text an unquoted
  value counts when it is a single token without brackets or quotes.
* ``NAME: value`` is only read as a key and a value where YAML would read it so: the name starts
  the line (after indentation, a list dash or ``export``) or follows ``{`` or ``,``, and the colon
  is followed by white space.  A sentence that merely contains a colon is not an assignment.
* An unquoted value followed by further words is prose, not a value.
* Python source is read through :mod:`ast`, never with a regular expression, so a credential that
  appears only in a comment or inside a longer string is seen by SC001, SC003 and SC006 but not by
  SC002.  A Python file that cannot be parsed is read line by line like any other text.

Honest limits
-------------
Pattern matching finds the ordinary ways of committing a credential by accident; it does not find
a secret that is split across lines, encoded, compressed, built at run time, or stored under a
name that says nothing.  A low-entropy password in prose is invisible to it.  Passing this check is
not evidence that the history of the repository is clean: only the current files are read.
"""

from __future__ import annotations

import argparse
import ast
import codecs
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import warnings
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# ============================================================================== configuration
#: Rule ids and their one-line titles (also written to the ``--json`` output).
RULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "SC000": "allow-list entry cannot be used",
        "SC001": "private key",
        "SC002": "credential name with a literal value",
        "SC003": "URL with embedded credentials",
        "SC004": "file that must never be committed",
        "SC005": "broker login with a literal password",
        "SC006": "long high-entropy token",
    }
)

ALLOWLIST_PATH: Final = "specifications/secret_scan_allowlist.txt"
#: Files that are never opened (the owner's specification PDFs).
UNOPENED_PATTERNS: Final[tuple[str, ...]] = ("specifications/source/*.pdf",)
#: SC006 does not apply below these prefixes (source documents; schemas with their hashes).
ENTROPY_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("specifications/source/", "gie/contracts/schemas/")
#: SC004 - file names (matched without regard to case) that must never be committed.
FORBIDDEN_FILE_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "*.kdbx",
)
#: SC002 - a name that contains one of these (case-insensitive, parts joined by ``_``) holds a credential.
CREDENTIAL_WORDS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "auth",
)
#: SC002 - name parts that contain ``auth`` without naming a credential.
NOT_CREDENTIAL_PARTS: Final[frozenset[str]] = frozenset(
    {"author", "authors", "authored", "authorship", "authority", "authorities", "authoritative"}
)
PLACEHOLDER_WORDS: Final[tuple[str, ...]] = ("FAKE", "EXAMPLE", "PLACEHOLDER", "REDACTED", "CHANGEME")
ABSENT_WORDS: Final[frozenset[str]] = frozenset({"NONE", "NULL", "EMPTY"})
ENVIRONMENT_MARKERS: Final[tuple[str, ...]] = ("os.environ", "getenv", "${", "{{")
#: SC005 - call names of broker terminals that take a password.
LOGIN_CALL_NAMES: Final[frozenset[str]] = frozenset({"login", "initialize"})
#: Formats in which only a quoted string is a string: an unquoted value is a number or a keyword.
TYPED_SUFFIXES: Final[frozenset[str]] = frozenset({".json", ".jsonl", ".toml"})
PYTHON_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyw", ".pyi"})
#: Directories that hold no project content; skipped only when the tree is walked without git.
WALK_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis", ".venv"}
)

MINIMUM_VALUE_LENGTH: Final = 6
MINIMUM_RUN_LENGTH: Final = 32
ENTROPY_THRESHOLD: Final = 4.5
#: SC006 - longest word or number that still counts as an ordinary piece of a path or identifier.
ORDINARY_PIECE_LIMIT: Final = 24
BINARY_PROBE_BYTES: Final = 8192
_NAME_DISPLAY_LIMIT: Final = 48
_PDF_DICTIONARY_REACH: Final = 4096

_PRIVATE_KEY: Final = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_URL_CREDENTIALS: Final = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://[^\s/:@\"'`<>]*:(?P<word>[^\s/@\"'`]+)@[^\s/@:\"'`]"
)
_LONG_RUN: Final = re.compile(rf"[A-Za-z0-9+/=_\-]{{{MINIMUM_RUN_LENGTH},}}")
_HEXADECIMAL: Final = re.compile(r"[0-9A-Fa-f]+")
_RUN_SEPARATORS: Final = re.compile(r"[/=_+\-]")
#: A number; a lower-case, capitalised or camel-case word with optional digits; an upper-case word.
_ORDINARY_WORD: Final = re.compile(r"[0-9]+|[A-Za-z]?[a-z]+(?:[A-Z][a-z]+)*[0-9]*|[A-Z]+[0-9]*[A-Z]*")
_NAME_PARTS: Final = re.compile(r"[_.\-]|(?<=[a-z0-9])(?=[A-Z])")
_ASSIGNMENT: Final = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<quote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)(?P=quote)"
    r"[ \t]*(?P<separator>:=|=|:)(?![=:])"
)
_LINE_START: Final = re.compile(r"\s*(?:[-*+]\s+)?(?:export\s+)?")
_PDF_STREAM: Final = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_NOT_A_LITERAL: Final = frozenset("()[]{}<>|`\"'\\")
_NAME_CHARACTERS: Final = re.compile(r"[^A-Za-z0-9_.\-]")

_GIT_TIMEOUT_S: Final = 120
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
    """One finding.  ``path`` is POSIX and relative to the scanned root; ``line`` 0 is the whole file.

    ``message`` never contains the matched value.
    """

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
    files: int
    #: Files whose content was not read: binary files, the unopened PDFs, links, unreadable files.
    content_skipped: int
    #: Findings that an allow-list entry suppressed.
    suppressed: int


def _order(violation: Violation) -> tuple[str, int, str, str]:
    return (violation.path, violation.line, violation.rule, violation.message)


# ============================================================================== values and names
def is_credential_name(name: str) -> bool:
    """Whether a name says that its value is a credential (SC002)."""
    parts = (part.lower() for part in _NAME_PARTS.split(name) if part)
    joined = "_".join(part for part in parts if part not in NOT_CREDENTIAL_PARTS)
    return any(word in joined for word in CREDENTIAL_WORDS)


def is_placeholder(value: str) -> bool:
    """Whether a value is an obvious placeholder, a reference to the environment or nothing at all."""
    stripped = value.strip()
    upper = stripped.upper()
    if not stripped or upper in ABSENT_WORDS or len(set(stripped)) == 1:
        return True
    if any(word in upper for word in PLACEHOLDER_WORDS):
        return True
    if "<" in stripped and ">" in stripped[stripped.index("<") :]:
        return True
    return stripped.startswith("$") or any(marker in stripped for marker in ENVIRONMENT_MARKERS)


def _is_reportable(value: str) -> bool:
    return len(value.strip()) >= MINIMUM_VALUE_LENGTH and not is_placeholder(value)


def _display_name(name: str) -> str:
    """A name that is safe to print: its characters restricted and its length bounded."""
    cleaned = _NAME_CHARACTERS.sub("?", name)
    return cleaned if len(cleaned) <= _NAME_DISPLAY_LIMIT else cleaned[:_NAME_DISPLAY_LIMIT] + "..."


def shannon_entropy(text: str) -> float:
    """Shannon entropy of the characters of *text*, in bits per character."""
    if not text:
        return 0.0
    total = len(text)
    return -sum(count / total * math.log2(count / total) for count in Counter(text).values())


def _is_ordinary_piece(piece: str) -> bool:
    if _HEXADECIMAL.fullmatch(piece):
        return True
    return len(piece) <= ORDINARY_PIECE_LIMIT and _ORDINARY_WORD.fullmatch(piece) is not None


def is_word_run(run: str) -> bool:
    """Whether a run is only ordinary pieces joined by separators: a path or an identifier, not a token."""
    return all(_is_ordinary_piece(piece) for piece in _RUN_SEPARATORS.split(run) if piece)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ============================================================================== rules on text
def _private_keys(path: str, text: str) -> Iterator[Violation]:
    for match in _PRIVATE_KEY.finditer(text):
        yield Violation("SC001", path, _line_of(text, match.start()), "private key header")


def _url_credentials(path: str, text: str) -> Iterator[Violation]:
    for match in _URL_CREDENTIALS.finditer(text):
        word = match.group("word")
        if is_placeholder(word) or "{" in word or "%s" in word:
            continue
        yield Violation(
            "SC003",
            path,
            _line_of(text, match.start()),
            f"URL of scheme '{match.group('scheme').lower()}' carries a user name and a password before "
            "the host",
        )


def _entropy_tokens(path: str, text: str) -> Iterator[Violation]:
    for match in _LONG_RUN.finditer(text):
        token = match.group(0)
        if _HEXADECIMAL.fullmatch(token) or is_word_run(token):
            continue
        entropy = shannon_entropy(token)
        if entropy > ENTROPY_THRESHOLD:
            yield Violation(
                "SC006",
                path,
                _line_of(text, match.start()),
                f"token of {len(token)} characters with an entropy of {entropy:.2f} bits per character "
                f"(above {ENTROPY_THRESHOLD})",
            )


def _value_of(line: str, start: int, separator: str, quoted_name: bool, typed: bool) -> str | None:
    """The literal value that follows an assignment at *start*, or ``None`` when there is none."""
    rest = line[start:]
    value = rest.lstrip(" \t")
    if not value:
        return None
    if separator == ":" and not quoted_name and value is rest:
        return None  # ``name:value`` without white space is not a YAML mapping
    if value[0] in "\"'":
        end = value.find(value[0], 1)
        return value[1:end] if end != -1 else value[1:]
    if typed:
        return None  # an unquoted JSON / TOML value is a number or a keyword, never a string
    token, _, remainder = value.partition(" ")
    remainder = remainder.strip()
    if remainder and not remainder.startswith("#"):
        return None  # further words: prose, not a value
    token = token.strip().rstrip(",;")
    if not token or any(character in _NOT_A_LITERAL for character in token):
        return None  # an expression, a collection or a placeholder - not a literal
    return token


def _line_assignments(path: str, text: str) -> Iterator[Violation]:
    """SC002 for text that is not Python: one line at a time."""
    typed = PurePosixPath(path).suffix.lower() in TYPED_SUFFIXES
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _ASSIGNMENT.finditer(line):
            name = match.group("name")
            if not is_credential_name(name):
                continue
            before = line[: match.start()]
            if before.endswith("="):
                continue  # the text is itself inside the value of another assignment
            separator = match.group("separator")
            quoted = bool(match.group("quote"))
            if separator == ":" and not quoted:
                opens_line = _LINE_START.fullmatch(before) is not None
                if not opens_line and not before.rstrip().endswith(("{", ",")):
                    continue
            value = _value_of(line, match.end(), separator, quoted, typed)
            if value is not None and _is_reportable(value):
                yield Violation(
                    "SC002",
                    path,
                    number,
                    f"name '{_display_name(name)}' is given a literal value (value not shown)",
                )


# ============================================================================== rules on Python
def _literal(node: ast.expr | None) -> str | None:
    """The text of a string or bytes literal, else ``None``."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1")
    return None


def _target_name(node: ast.expr) -> str | None:
    """The name an assignment target gives its value: a variable, an attribute or a string key."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _literal(node.slice)
    return None


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _pairs(node: ast.AST) -> Iterator[tuple[str | None, ast.expr | None]]:
    """``(name, value)`` of everything in *node* that binds a name to a value."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Tuple | ast.List) and isinstance(node.value, ast.Tuple | ast.List):
                if len(target.elts) == len(node.value.elts):
                    for element, value in zip(target.elts, node.value.elts, strict=True):
                        yield _target_name(element), value
            else:
                yield _target_name(target), node.value
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        yield _target_name(node.target), node.value
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            yield _literal(key), value
    elif isinstance(node, ast.Call):
        for keyword in node.keywords:
            yield keyword.arg, keyword.value
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        arguments = node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        defaults = arguments.defaults
        for argument, default in zip(positional[len(positional) - len(defaults) :], defaults, strict=True):
            yield argument.arg, default
        for argument, optional in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
            yield argument.arg, optional


def _python_rules(path: str, tree: ast.Module) -> Iterator[Violation]:
    """SC002 and SC005 for one parsed Python file."""
    for node in ast.walk(tree):
        login = isinstance(node, ast.Call) and _call_name(node.func) in LOGIN_CALL_NAMES
        for name, value_node in _pairs(node):
            value = _literal(value_node)
            if name is None or value is None or value_node is None:
                continue
            line = int(getattr(value_node, "lineno", 0))
            if login and name == "password":
                if not is_placeholder(value):
                    called = _call_name(node.func) if isinstance(node, ast.Call) else None
                    yield Violation(
                        "SC005",
                        path,
                        line,
                        f"call of {called}() passes a literal password (value not shown)",
                    )
            elif is_credential_name(name) and _is_reportable(value):
                yield Violation(
                    "SC002",
                    path,
                    line,
                    f"name '{_display_name(name)}' is given a literal value (value not shown)",
                )


def _parse(data: bytes, path: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escape sequences: not this tool's business
            return ast.parse(data, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


# ============================================================================== files
def _pdf_text(data: bytes) -> str:
    """The raw bytes of a PDF as text, without the bodies of encoded streams."""

    def without_encoded_body(match: re.Match[bytes]) -> bytes:
        head = data[max(0, match.start() - _PDF_DICTIONARY_REACH) : match.start()]
        dictionary = head[head.rfind(b"obj") :] if b"obj" in head else head
        if b"/Filter" not in dictionary:
            return match.group(0)
        return b"stream\n" + b"\n" * match.group(1).count(b"\n") + b"endstream"

    return _PDF_STREAM.sub(without_encoded_body, data).decode("latin-1")


def _is_forbidden_name(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    return next((pattern for pattern in FORBIDDEN_FILE_PATTERNS if fnmatch.fnmatchcase(name, pattern)), None)


def _decode(data: bytes) -> str | None:
    """The text of a file, or ``None`` when it is binary."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:BINARY_PROBE_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def _scan_file(root: Path, path: str) -> tuple[list[Violation], bool]:
    """The findings of one file and whether its content was read."""
    found: list[Violation] = []
    pattern = _is_forbidden_name(path)
    if pattern is not None:
        found.append(
            Violation("SC004", path, 0, f"file name matches '{pattern}': such a file must never be committed")
        )
    target = root.joinpath(*PurePosixPath(path).parts)
    if any(fnmatch.fnmatchcase(path, unopened) for unopened in UNOPENED_PATTERNS):
        return found, False
    if target.is_symlink() or not target.is_file():
        return found, False
    try:
        data = target.read_bytes()
    except OSError:
        return found, False
    suffix = PurePosixPath(path).suffix.lower()
    is_pdf = suffix == ".pdf" or data.startswith(b"%PDF-")
    text = _pdf_text(data) if is_pdf else _decode(data)
    if text is None:
        return found, False
    found.extend(_private_keys(path, text))
    found.extend(_url_credentials(path, text))
    if not path.startswith(ENTROPY_EXEMPT_PREFIXES):
        found.extend(_entropy_tokens(path, text))
    tree = _parse(data, path) if suffix in PYTHON_SUFFIXES else None
    if tree is not None:
        found.extend(_python_rules(path, tree))
    else:
        found.extend(_line_assignments(path, text))
    return found, True


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


def _git_files(root: Path) -> list[str] | str:
    """Tracked and untracked-but-not-ignored files, or the reason why git cannot list them."""
    probe = _git(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    if probe is None:
        return "git cannot be started"
    if probe[0] != 0 or not probe[1].startswith("true"):
        return "the root is not inside a git work tree"
    if probe[1].split("\n", 1)[-1].strip():
        return "the root is not the top level of the git work tree"
    listing = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    if listing is None or listing[0] != 0:
        return "git ls-files failed"
    return sorted({path for path in listing[1].split("\0") if path})


def _walk_files(root: Path) -> list[str]:
    """Every file below *root* except ``.git`` and tool caches (the fallback without git)."""
    found: list[str] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(name for name in subdirectories if name not in WALK_SKIPPED_DIRECTORIES)
        found.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def list_files(root: Path, *, use_git: bool = True) -> tuple[list[str], str | None]:
    """``(files to scan, note)``; the note explains a fallback to walking the tree."""
    listed = _git_files(root) if use_git else "git was not used"
    if isinstance(listed, str):
        skipped = ", ".join(sorted(WALK_SKIPPED_DIRECTORIES))
        return _walk_files(root), (
            f"the file list does not come from git ({listed}): every file below the root was scanned, "
            f"ignored files included, except the directories {skipped}"
        )
    return listed, None


# ============================================================================== the allow-list
@dataclass(frozen=True)
class _Allowed:
    rule: str
    path: str
    #: ``None``: the whole file.
    line: int | None
    source_line: int

    def covers(self, violation: Violation) -> bool:
        return (
            violation.rule == self.rule
            and violation.path == self.path
            and (self.line is None or violation.line == self.line)
        )

    def render(self) -> str:
        return f"{self.rule} {self.path}" + ("" if self.line is None else f":{self.line}")


def _read_allowlist(root: Path) -> tuple[list[_Allowed], list[Violation]]:
    """The entries of the allow-list and the SC000 violations of lines that cannot be used."""
    entries: list[_Allowed] = []
    problems: list[Violation] = []
    try:
        text = (root / ALLOWLIST_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries, problems
    except (OSError, UnicodeDecodeError) as error:
        problems.append(
            Violation("SC000", ALLOWLIST_PATH, 0, f"the allow-list cannot be read ({type(error).__name__})")
        )
        return entries, problems
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        rule, _, location = line.partition(" ")
        location = location.strip()
        path, separator, suffix = location.rpartition(":")
        if separator and suffix.isdigit() and path:
            line_number: int | None = int(suffix)
        else:
            path, line_number = location, None
        pure = PurePosixPath(path)
        usable = (
            rule in RULES
            and rule != "SC000"
            and bool(path)
            and "\\" not in path
            and " " not in path
            and not pure.is_absolute()
            and ".." not in pure.parts
        )
        if usable:
            entries.append(_Allowed(rule, path, line_number, number))
        else:
            problems.append(
                Violation(
                    "SC000",
                    ALLOWLIST_PATH,
                    number,
                    "entry cannot be used: expected 'RULE path' or 'RULE path:line' with a rule "
                    f"{min(set(RULES) - {'SC000'})}-{max(RULES)} and a relative POSIX path",
                )
            )
    return entries, problems


# ============================================================================== entry points
def scan(repo_root: Path, *, files: Iterable[str] | None = None, use_git: bool = True) -> Report:
    """Scan the repository at *repo_root*.

    *files* replaces the file list (POSIX paths relative to the root); with ``use_git=False`` the
    tree is walked.  Nothing is written, and no matched value is ever part of the result.
    """
    notes: list[str] = []
    if files is not None:
        listed = sorted(set(files))
    else:
        listed, note = list_files(repo_root, use_git=use_git)
        if note is not None:
            notes.append(note)
    found: list[Violation] = []
    skipped = 0
    for path in listed:
        violations, content_read = _scan_file(repo_root, path)
        found.extend(violations)
        skipped += 0 if content_read else 1
    allowed, problems = _read_allowlist(repo_root)
    used: set[_Allowed] = set()
    kept: list[Violation] = list(problems)
    unique = sorted(set(found), key=_order)
    for violation in unique:
        covering = [entry for entry in allowed if entry.covers(violation)]
        if covering:
            used.update(covering)
        else:
            kept.append(violation)
    notes.extend(
        f"{ALLOWLIST_PATH}:{entry.source_line}: entry '{entry.render()}' suppresses nothing: remove it"
        for entry in allowed
        if entry not in used
    )
    return Report(
        violations=tuple(sorted(set(kept), key=_order)),
        notes=tuple(notes),
        files=len(listed),
        content_skipped=skipped,
        suppressed=len(unique) + len(problems) - len(set(kept)),
    )


def check(repo_root: Path, *, files: Iterable[str] | None = None, use_git: bool = True) -> list[Violation]:
    """Every violation in the repository at *repo_root*, sorted by path, line and rule (see :func:`scan`)."""
    return list(scan(repo_root, files=files, use_git=use_git).violations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_no_secrets",
        description="No credentials in the repository (rules SC000-SC006). Matched values are never printed.",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    if not root.is_dir():
        print(f"check_no_secrets: {root} is not a directory", file=sys.stderr)
        return 2
    report = scan(root)
    violations = report.violations
    if arguments.as_json:
        document = {
            "schema_version": 1,
            "tool": "tools/check_no_secrets.py",
            "ok": not violations,
            "files_scanned": report.files,
            "files_content_skipped": report.content_skipped,
            "findings_suppressed_by_allowlist": report.suppressed,
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
        print(f"check_no_secrets: note: {note}")
    if violations:
        print(f"check_no_secrets: FAILED - {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print(
        f"check_no_secrets: OK - {report.files} files scanned ({report.content_skipped} binary or unopened, "
        f"names checked only), {report.suppressed} finding(s) allow-listed; no violation of "
        f"{min(RULES)}-{max(RULES)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
