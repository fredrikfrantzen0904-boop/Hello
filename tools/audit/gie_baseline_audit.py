#!/usr/bin/env python3
"""Read-only baseline audit of an existing trading system tree (GIE Session 00).

Requirements: S00-ASG-01 .. S00-ASG-07 (audit and inventory of ``D:\\ftmo_eurusd``), S00-ACC-06
("no production changes"), S00-DEL-04 (baseline audit), M-AUTH-12 (existing systems are not
modified) and M-AUTH-15 (credentials are not exposed).

The production tree is on the owner's machine and cannot be reached from the development
session (blocker B-001), so this file is a self-contained tool the owner runs locally and whose
output folder the owner reviews and sends back.  It is one file, uses the Python standard
library only and runs unmodified on Python 3.8+ on Windows 10/11 and on Linux.

Safety model (every point is enforced in code, not only promised):

* The audited tree is only *read*: files are opened with mode ``'rb'`` through one function
  (`_open_readonly`), nothing under ``--root`` is created, renamed, deleted, chmod-ed or touched,
  nothing from it is executed, imported or evaluated, and the current directory is never changed.
* Symbolic links, junctions and other reparse-point directories are recorded, never followed.
* Output is written only inside ``--out`` (which must be new or empty and must not overlap
  ``--root``), with exclusive-create mode ``'xb'`` so that no existing file can be overwritten.
* While a tree is being scanned an interpreter audit hook (`sys.addaudithook`, PEP 578) is active:
  it blocks any attempt of this process to open a file under the root for writing, to rename /
  delete / chmod / utime anything under it, to use the network or to start a process other than
  the two fixed read-only Windows queries of ``--processes``.
* No file *content* leaves the tree, with three narrow exceptions: package names and version
  specifiers from dependency manifests, column names of delimited text files, and Python version
  strings.  Everything else is reported as path, size, modification time, SHA-256, counts and
  keyword *locations* (path + line number + rule name; never the matched text).
* Files whose name suggests secrets are listed but neither hashed nor read unless
  ``--hash-sensitive`` is given.  The host name is reported only as a SHA-256 hash.
* No ``git`` process is started (git may refresh its index); ``.git/HEAD``, refs and
  ``packed-refs`` are read as plain files.

Sub-commands::

    audit    --root R --out DIR [--processes] [--count-rows] [--hash-sensitive] ...
    manifest --root R --out FILE [--include GLOB ...]
    compare  A.jsonl B.jsonl [--strict-mtime]

Importable API (used by ``tools/baseline_freeze.py``): `build_manifest`, `write_manifest`,
`read_manifest`, `compare_manifests`.

Exit codes: 0 success (unreadable files are listed, they do not fail the run), 1 ``compare``
found differences, 2 arguments refused, 3 the read-only guard blocked something (never expected).
"""

# The repository lints for Python 3.11, but this one file must also run on the owner's Python 3.8:
# the typing aliases (List, Dict, Optional, ...) and ``datetime.timezone.utc`` are deliberate.
# ruff: noqa: UP006, UP017, UP035, UP045

from __future__ import annotations

import argparse
import bisect
import configparser
import contextlib
import csv
import datetime
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import IO, Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, Union

if sys.platform == "win32":
    import ctypes
    import msvcrt
    import winreg
    from ctypes import wintypes

__all__ = [
    "DEFAULT_EXCLUDED_DIRS",
    "READ_ONLY_OK_LINE",
    "SCHEMA_VERSION",
    "TOOL_NAME",
    "TOOL_VERSION",
    "AuditRefused",
    "ReadOnlyViolation",
    "build_manifest",
    "compare_manifests",
    "main",
    "read_manifest",
    "write_manifest",
]

TOOL_NAME = "gie_baseline_audit"
TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = 1

READ_ONLY_OK_LINE = "READ-ONLY CHECK: no file under root was opened for writing"

EXIT_OK = 0
EXIT_DIFFERENT = 1
EXIT_REFUSED = 2
EXIT_GUARD_VIOLATION = 3

#: Directory names that are not descended into.  Each one found is still listed in
#: ``inventory.json`` with its file count and total size.
DEFAULT_EXCLUDED_DIRS: Tuple[str, ...] = (
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
)

_CHUNK_BYTES = 1 << 20
_BINARY_PROBE_BYTES = 8192
_HEADER_PROBE_BYTES = 64 * 1024
_HEADER_MAX_CHARS = 2000
_MANIFEST_PARSE_BYTES = 1 << 20
_VERSION_FILE_BYTES = 8192
_GIT_SMALL_FILE_BYTES = 4096
_GIT_PACKED_REFS_BYTES = 8 << 20
_LARGEST_FILES = 50

_DEFAULT_MAX_LINE_COUNT_BYTES = 64 << 20
_DEFAULT_MAX_SCAN_BYTES = 8 << 20
#: configuration-like text (.json, .txt, .xml, .yaml ...) is scanned up to this size only: real
#: configuration files are small, large ones are data
_CONFIG_SCAN_BYTES = 1 << 20
_DEFAULT_MAX_LIST_ENTRIES = 5000
_DEFAULT_MAX_HITS_PER_FILE = 100
_DEFAULT_PROGRESS_EVERY = 500

_TASKLIST_TIMEOUT_SECONDS = 60
_SCHTASKS_TIMEOUT_SECONDS = 120

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_OFFLINE_ATTRIBUTES = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
_O_NOATIME: int = getattr(os, "O_NOATIME", 0)
_WRITE_INTENT_FLAGS: int = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_TEMPORARY", 0)
)

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

PathLike = Union[str, "os.PathLike[str]"]


class AuditRefused(ValueError):
    """The arguments were refused before anything was read or written (exit code 2)."""


class ReadOnlyViolation(PermissionError):
    """The runtime guard blocked an operation that could change the audited tree or leave the machine."""


# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------


def _is_windows() -> bool:
    return os.name == "nt"


def _strip_extended_prefix(path: str) -> str:
    """``\\\\?\\C:\\x`` -> ``C:\\x`` and ``\\\\?\\UNC\\server\\share`` -> ``\\\\server\\share``."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _extended_path(path: str) -> str:
    """Windows extended-length form of an absolute path, so that paths over 260 characters work."""
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _normalised(path: str) -> str:
    plain = _strip_extended_prefix(path) if _is_windows() else path
    return os.path.normcase(os.path.normpath(plain))


def _real(path: PathLike) -> str:
    """Absolute path with links in the existing part resolved (reads link metadata only)."""
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _is_within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lies below it (both already absolute)."""
    inner, outer = _normalised(child), _normalised(parent)
    if inner == outer:
        return True
    try:
        return os.path.commonpath([inner, outer]) == outer
    except ValueError:  # different drives, or a mix of absolute and relative paths
        return False


def _display_name(name: str) -> str:
    """One path component as clean, reversible text.

    Backslashes are doubled and bytes that are not valid UTF-8 (Linux) or lone surrogates
    (Windows) become ``\\xNN`` escapes, so every reported path is valid UTF-8 and two different
    names never map to the same text.
    """
    escaped = name.replace("\\", "\\\\")
    try:
        escaped.encode("utf-8")
    except UnicodeEncodeError:
        try:
            raw = os.fsencode(escaped)
        except UnicodeEncodeError:
            raw = escaped.encode("utf-8", "backslashreplace")
        return raw.decode("utf-8", "backslashreplace")
    return escaped


def _clean_text(text: str) -> str:
    """Any text as valid UTF-8 (undecodable parts become backslash escapes)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8", "backslashreplace")


def _join_rel(parent: str, name: str) -> str:
    shown = _display_name(name)
    return parent + "/" + shown if parent else shown


def _iso_from_ns(nanoseconds: int) -> Optional[str]:
    try:
        moment = _EPOCH + datetime.timedelta(microseconds=nanoseconds // 1000)
    except (OverflowError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_now(seconds: float) -> str:
    moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------------
# Runtime guard (interpreter audit hook)
# --------------------------------------------------------------------------------------------

_GUARD_PATH_EVENTS = frozenset(
    {
        "os.chdir",
        "os.chflags",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.mkfifo",
        "os.mknod",
        "os.remove",
        "os.removexattr",
        "os.rename",
        "os.rmdir",
        "os.setxattr",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.chown",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.make_archive",
        "shutil.move",
        "shutil.rmtree",
        "shutil.unpack_archive",
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
    }
)
_GUARD_EXEC_EVENTS = frozenset(
    {"os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.startfile", "os.system"}
)
_GUARD_NETWORK_PREFIXES = (
    "socket.",
    "ssl.",
    "urllib.",
    "http.client.",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "nntplib.",
    "telnetlib.",
    "webbrowser.",
)
#: ``socket.gethostname`` is a local lookup used by `platform.node`; it sends nothing.
_GUARD_LOCAL_EVENTS = frozenset({"socket.gethostname"})


def _has_write_intent(mode: Any, flags: Any) -> bool:
    if isinstance(flags, int) and flags & _WRITE_INTENT_FLAGS:
        return True
    return isinstance(mode, str) and any(letter in mode for letter in "wax+")


class _Guard:
    """State of the read-only guard.  One instance per process; inactive unless a scan is running."""

    def __init__(self) -> None:
        self.installed = False
        self.active = False
        self.prefixes: Tuple[str, ...] = ()
        self.allowed_commands: Set[str] = set()
        self.read_opens = 0
        self.write_violations: List[str] = []
        self.other_violations: List[str] = []

    def covers(self, candidate: Any) -> bool:
        """True when ``candidate`` is a path inside the guarded root."""
        try:
            text = os.fspath(candidate)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        if not text:
            return False
        full = _normalised(os.path.abspath(text))
        for prefix in self.prefixes:
            if full == prefix or full.startswith(prefix if prefix.endswith(os.sep) else prefix + os.sep):
                return True
        return False

    def handle(self, event: str, args: Tuple[Any, ...]) -> None:
        if event == "open":
            if len(args) < 3 or isinstance(args[0], int) or not self.covers(args[0]):
                return
            if _has_write_intent(args[1], args[2]):
                self.write_violations.append("open for writing")
                raise ReadOnlyViolation("blocked: attempt to open a file under the audited root for writing")
            if isinstance(args[1], str):
                self.read_opens += 1
        elif event in _GUARD_PATH_EVENTS:
            if any(self.covers(arg) for arg in args):
                self.write_violations.append(event)
                raise ReadOnlyViolation("blocked: " + event + " on a path under the audited root")
        elif event == "subprocess.Popen":
            # the interpreter reports the command as a list on POSIX and as one string on Windows
            command = args[1] if len(args) > 1 else None
            if not isinstance(command, str):
                command = subprocess.list2cmdline([os.fsdecode(part) for part in command or ()])
            if command not in self.allowed_commands:
                self.other_violations.append(event)
                raise ReadOnlyViolation(
                    "blocked: process start that is not one of the fixed read-only queries"
                )
        elif event in _GUARD_EXEC_EVENTS or (
            event.startswith(_GUARD_NETWORK_PREFIXES) and event not in _GUARD_LOCAL_EVENTS
        ):
            self.other_violations.append(event)
            raise ReadOnlyViolation("blocked: " + event + " is not permitted during an audit")


_GUARD = _Guard()


def _audit_hook(event: str, args: Tuple[Any, ...]) -> None:
    if _GUARD.active:
        _GUARD.handle(event, args)


@contextlib.contextmanager
def _guarded(*roots: str) -> Iterator[_Guard]:
    """Activate the read-only guard for the given root path(s) for the duration of the block."""
    guard = _GUARD
    if not guard.installed and hasattr(sys, "addaudithook"):
        sys.addaudithook(_audit_hook)
        guard.installed = True
    saved = (guard.active, guard.prefixes, guard.allowed_commands)
    nested = guard.active
    prefixes = list(guard.prefixes) if nested else []
    for root in roots:
        for candidate in (os.path.abspath(root), os.path.realpath(root)):
            prefix = _normalised(candidate)
            if prefix not in prefixes:
                prefixes.append(prefix)
    guard.prefixes = tuple(prefixes)
    if not nested:
        guard.allowed_commands = set()
        guard.read_opens = 0
        guard.write_violations = []
        guard.other_violations = []
    guard.active = guard.installed
    try:
        yield guard
    finally:
        guard.active, guard.prefixes, guard.allowed_commands = saved


# --------------------------------------------------------------------------------------------
# Read-only file access
# --------------------------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        _KERNEL32: Optional[ctypes.WinDLL] = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        _KERNEL32 = None
    if _KERNEL32 is not None:
        _KERNEL32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        _KERNEL32.CreateFileW.restype = wintypes.HANDLE
        _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
        _KERNEL32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        _KERNEL32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    def _open_shared_windows(path: str, flags: int) -> int:
        """Open for reading while letting other processes write, rename and delete the file.

        Python's own ``open`` does not grant FILE_SHARE_DELETE, so a production process that
        rotates or atomically replaces a file while it is being hashed would fail.  This opener
        requests read access only and shares everything.
        """
        if _KERNEL32 is None:
            raise OSError("kernel32 is not available")
        handle = _KERNEL32.CreateFileW(
            path,
            _GENERIC_READ,
            _SHARE_READ_WRITE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except OSError:
            _KERNEL32.CloseHandle(handle)
            raise


def _readonly_opener(path: str, flags: int) -> int:
    """File-descriptor opener for `_open_readonly`; ``flags`` are the read-only flags of mode 'rb'."""
    if sys.platform == "win32":
        try:
            return _open_shared_windows(path, flags)
        except OSError:
            return os.open(path, flags)
    else:
        # POSIX: never follow a link that replaced the file after it was listed, never block on
        # a FIFO, and do not update the access time when the system allows that (O_NOATIME needs
        # ownership of the file; without it the plain read-only open is used).
        base = flags | _O_NOFOLLOW | _O_NONBLOCK
        if _O_NOATIME:
            try:
                return os.open(path, base | _O_NOATIME)
            except PermissionError:
                return os.open(path, base)
        return os.open(path, base)


def _open_readonly(path: str) -> IO[bytes]:
    """The only place where a file is opened for reading.  Mode is always ``'rb'``."""
    return open(path, "rb", opener=_readonly_opener)


def _create_new_file(path: str, forbidden_root: Optional[str]) -> IO[bytes]:
    """The only place where a file is opened for writing: exclusive create, never under the root."""
    if forbidden_root is not None and _is_within(_real(path), forbidden_root):
        raise ReadOnlyViolation("refusing to create a file under the audited root")
    return open(path, "xb")


@dataclass
class _ReadResult:
    sha256: Optional[str] = None
    bytes_read: int = 0
    complete: bool = False
    newlines: int = 0
    ends_with_newline: bool = False
    text_kind: str = "empty"  # "empty" | "bytes" | "utf-16-le" | "utf-16-be" | "binary"
    head: bytes = b""
    error: Optional[str] = None
    errno: Optional[int] = None
    changed: bool = False


def _text_kind(first_chunk: bytes) -> str:
    if first_chunk.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if first_chunk.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if b"\x00" in first_chunk[:_BINARY_PROBE_BYTES]:
        return "binary"
    return "bytes"


def _read_file(
    path: str, listed: os.stat_result, want_hash: bool, want_full: bool, head_limit: int
) -> _ReadResult:
    """Stream one regular file once: SHA-256, newline count and the first ``head_limit`` bytes."""
    result = _ReadResult()
    digest = hashlib.sha256() if want_hash else None
    to_end = want_hash or want_full
    head: List[bytes] = []
    head_size = 0
    newline = b"\n"
    carry = b""
    tail = b""
    try:
        with _open_readonly(path) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                result.error = "NotARegularFile"
                return result
            while to_end or head_size < head_limit:
                # the first block always covers the binary probe, whatever the chunk size is
                size = _CHUNK_BYTES if result.bytes_read else max(_CHUNK_BYTES, _BINARY_PROBE_BYTES)
                chunk = handle.read(size if to_end else min(size, head_limit - head_size))
                if not chunk:
                    result.complete = True
                    break
                if result.bytes_read == 0:
                    result.text_kind = _text_kind(chunk)
                    if result.text_kind == "utf-16-le":
                        newline = b"\n\x00"
                    elif result.text_kind == "utf-16-be":
                        newline = b"\x00\n"
                result.bytes_read += len(chunk)
                if digest is not None:
                    digest.update(chunk)
                if head_size < head_limit:
                    piece = chunk[: head_limit - head_size]
                    head.append(piece)
                    head_size += len(piece)
                if to_end and result.text_kind != "binary":
                    if len(newline) == 1:
                        result.newlines += chunk.count(newline)
                    else:  # UTF-16: count line feeds on whole 2-byte units only
                        joined = carry + chunk
                        usable = len(joined) - len(joined) % 2
                        result.newlines += joined[:usable].decode(result.text_kind, "replace").count("\n")
                        carry = joined[usable:]
                    tail = (tail + chunk)[-2:]
            after = os.fstat(handle.fileno())
    except OSError as exc:
        return _ReadResult(error=type(exc).__name__, errno=exc.errno)
    result.head = b"".join(head)
    result.ends_with_newline = tail.endswith(newline) and result.bytes_read % len(newline) == 0
    if digest is not None and result.complete:
        result.sha256 = digest.hexdigest()
    result.changed = (
        after.st_size != listed.st_size
        or after.st_mtime_ns != listed.st_mtime_ns
        or (result.complete and result.bytes_read != listed.st_size)
    )
    return result


def _read_head(path: str, limit: int) -> Optional[bytes]:
    """First ``limit`` bytes of a small metadata file, or None when it cannot be read."""
    try:
        with _open_readonly(path) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            return handle.read(limit)
    except OSError:
        return None


def _decode_text(data: bytes) -> Optional[str]:
    """Text for scanning only (never emitted): BOM-aware, undecodable bytes replaced; None if binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    if b"\x00" in data[:_BINARY_PROBE_BYTES]:
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------------------------
# Classification tables
# --------------------------------------------------------------------------------------------

#: extension -> (language, counted as text)
LANGUAGE_BY_EXTENSION: Dict[str, Tuple[str, bool]] = {
    ".py": ("Python", True),
    ".pyw": ("Python", True),
    ".pyi": ("Python", True),
    ".pyx": ("Cython", True),
    ".mq4": ("MQL4", True),
    ".mq5": ("MQL5", True),
    ".mqh": ("MQL4/MQL5 include", True),
    ".ex4": ("MQL4 (compiled)", False),
    ".ex5": ("MQL5 (compiled)", False),
    ".c": ("C", True),
    ".h": ("C/C++ header", True),
    ".cpp": ("C++", True),
    ".cc": ("C++", True),
    ".cxx": ("C++", True),
    ".hpp": ("C++", True),
    ".hh": ("C++", True),
    ".hxx": ("C++", True),
    ".cs": ("C#", True),
    ".java": ("Java", True),
    ".js": ("JavaScript", True),
    ".jsx": ("JavaScript", True),
    ".mjs": ("JavaScript", True),
    ".cjs": ("JavaScript", True),
    ".ts": ("TypeScript", True),
    ".tsx": ("TypeScript", True),
    ".r": ("R", True),
    ".rmd": ("R", True),
    ".jl": ("Julia", True),
    ".rs": ("Rust", True),
    ".go": ("Go", True),
    ".sql": ("SQL", True),
    ".ps1": ("PowerShell", True),
    ".psm1": ("PowerShell", True),
    ".psd1": ("PowerShell", True),
    ".bat": ("Batch", True),
    ".cmd": ("Batch", True),
    ".sh": ("Shell", True),
    ".bash": ("Shell", True),
    ".zsh": ("Shell", True),
    ".vbs": ("VBScript", True),
    ".ipynb": ("Jupyter Notebook", True),
    ".html": ("HTML", True),
    ".htm": ("HTML", True),
    ".css": ("CSS", True),
    ".scss": ("CSS", True),
    ".yaml": ("YAML", True),
    ".yml": ("YAML", True),
    ".toml": ("TOML", True),
    ".json": ("JSON", True),
    ".ini": ("INI/config", True),
    ".cfg": ("INI/config", True),
    ".conf": ("INI/config", True),
    ".xml": ("XML", True),
    ".md": ("Markdown", True),
    ".rst": ("reStructuredText", True),
}

_SOURCE_EXTENSIONS = frozenset(
    {
        ".py", ".pyw", ".pyi", ".pyx", ".ipynb", ".mq4", ".mq5", ".mqh", ".c", ".h", ".cpp", ".cc", ".cxx",
        ".hpp", ".hh", ".hxx", ".cs", ".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".r", ".rmd",
        ".jl", ".rs", ".go", ".sql", ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".sh", ".bash", ".zsh", ".vbs",
    }
)  # fmt: skip
_CONFIG_EXTENSIONS = frozenset(
    {
        ".ini", ".cfg", ".conf", ".config", ".json", ".yaml", ".yml", ".toml", ".xml", ".properties", ".set",
        ".txt", ".service", ".timer",
    }
)  # fmt: skip
_CONFIG_FILE_NAMES = frozenset({"dockerfile", "procfile", "makefile", "pipfile"})
_PYTHON_EXTENSIONS = frozenset({".py", ".pyw", ".ipynb"})

DATA_EXTENSIONS = frozenset(
    {
        ".csv", ".tsv", ".parquet", ".feather", ".arrow", ".h5", ".hdf5", ".npy", ".npz", ".pkl", ".pickle",
        ".joblib", ".db", ".sqlite", ".sqlite3", ".duckdb", ".json", ".jsonl", ".ndjson", ".xlsx", ".xls",
        ".hst", ".fxt", ".tick", ".hcc", ".tkc", ".bin", ".zip", ".gz", ".7z", ".tar", ".bz2", ".xz", ".zst",
        ".rar",
    }
)  # fmt: skip
_DELIMITED_EXTENSIONS = {".csv": ",", ".tsv": "\t"}

MODEL_EXTENSIONS = frozenset(
    {
        ".pkl", ".pickle", ".joblib", ".onnx", ".pt", ".pth", ".h5", ".keras", ".bin", ".ckpt",
        ".safetensors", ".pb", ".tflite", ".cbm", ".xgb", ".ubj", ".model", ".sav", ".pmml",
    }
)  # fmt: skip
_MODEL_TEXT_NAME = re.compile(r"lgb|lightgbm|booster|xgb|catboost|model", re.IGNORECASE)

ENGINE_HINTS: Tuple[str, ...] = (
    "cavemen", "silver", "bullet", "liquidity", "hunter", "orb", "vwap", "trend", "rider", "sweep",
    "reversal", "gce", "gold", "breakout", "ai_trader", "round1", "round_1", "round2", "round_2",
    "baseline", "backtest", "dashboard",
)  # fmt: skip

_SENSITIVE_NAME_GLOBS: Tuple[str, ...] = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_dsa*", "id_ecdsa*",
    "id_ed25519*", "*credential*", "*secret*", "*password*", "*passwd*", "*token*", "*.kdbx", "*.jks",
    "*.keystore", "*.ppk", "*.ovpn", ".netrc", "_netrc", ".pypirc", ".npmrc", ".git-credentials",
)  # fmt: skip
_SENSITIVE_ACCOUNT_EXTENSIONS = frozenset(
    {".json", ".ini", ".cfg", ".conf", ".dat", ".yaml", ".yml", ".toml", ".xml", ".txt"}
)
_SENSITIVE_DIRECTORY_NAMES = frozenset({"secrets", ".secrets", "credentials", ".ssh", ".aws", ".gnupg"})

_TERMINAL_FILE_HINTS = frozenset(
    {"terminal64.exe", "terminal.exe", "metaeditor64.exe", "metaeditor.exe", "metatester64.exe", "origin.txt"}
)
_TERMINAL_DIRECTORY_HINTS = frozenset({"mql4", "mql5"})
_LAUNCHER_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".sh", ".vbs", ".lnk"})
_TEST_DIRECTORY_NAMES = frozenset({"tests", "test"})
_TEST_CONFIG_NAMES = frozenset({"pytest.ini", "tox.ini", "noxfile.py", ".coveragerc"})
_OTHER_TEST_GLOBS = (
    "*.test.js",
    "*.test.ts",
    "*.spec.js",
    "*.spec.ts",
    "*_test.go",
    "test*.mq5",
    "test*.mq4",
)
_LISTED_ONLY_MANIFESTS = frozenset(
    {
        "setup.py", "poetry.lock", "pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "uv.lock", "conda-lock.yml", "cargo.toml", "go.mod", "renv.lock", "packages.config",
    }
)  # fmt: skip
_VCS_OTHER_DIRECTORIES = frozenset({".hg", ".svn"})


def _sensitive_reason(name: str, parents: Sequence[str]) -> Optional[str]:
    """Why a file is treated as a possible secret (decided from names only), or None."""
    lowered = name.lower()
    for pattern in _SENSITIVE_NAME_GLOBS:
        if fnmatch.fnmatchcase(lowered, pattern):
            return "name matches " + pattern
    if lowered.startswith("accounts") and os.path.splitext(lowered)[1] in _SENSITIVE_ACCOUNT_EXTENSIONS:
        return "name matches accounts*"
    for parent in parents:
        if parent.lower() in _SENSITIVE_DIRECTORY_NAMES:
            return "inside a directory named " + _display_name(parent.lower())
    return None


def _container_kind(lowered: str) -> Optional[str]:
    if lowered == "dockerfile" or lowered.startswith("dockerfile.") or lowered.endswith(".dockerfile"):
        return "dockerfile"
    if lowered.endswith((".yml", ".yaml")) and lowered.startswith(("docker-compose", "compose")):
        return "compose"
    if lowered == "procfile" or lowered.startswith("procfile."):
        return "procfile"
    if lowered.startswith("supervisor") and lowered.endswith((".conf", ".ini")):
        return "supervisor"
    if lowered.endswith((".service", ".timer")):
        return "systemd unit"
    if lowered in ("ecosystem.config.js", "ecosystem.config.cjs"):
        return "pm2"
    return None


def _dependency_kind(lowered: str, parents: Sequence[str]) -> Optional[str]:
    extension = os.path.splitext(lowered)[1]
    if extension in (".txt", ".in") and (
        lowered.startswith(("requirements", "constraints"))
        or (parents and parents[-1].lower() == "requirements")
    ):
        return "requirements"
    if lowered == "pyproject.toml":
        return "pyproject"
    if lowered == "setup.cfg":
        return "setup.cfg"
    if lowered.startswith("environment") and extension in (".yml", ".yaml"):
        return "conda environment"
    if lowered == "package.json":
        return "package.json"
    if lowered == "pipfile":
        return "Pipfile"
    return None


# --------------------------------------------------------------------------------------------
# Keyword-location rules.  A hit is reported as (path, line number, rule name) and nothing else.
# --------------------------------------------------------------------------------------------


_CREDENTIAL_TRIGGERS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "api-key", "login", "account",
)  # fmt: skip

#: rule name -> lower-case literals of which at least one occurs in every text the rule can match.
#: A rule is only run on a file that contains one of its triggers (a cheap substring test), which
#: keeps the scan of large data-like text files fast.  Rules match ASCII case-insensitively, so a
#: lower-cased copy of the text is an exact pre-filter.
_TRIGGERS: Dict[str, Tuple[str, ...]] = {
    "metatrader5_import": ("metatrader5",),
    "metatrader5_reference": ("metatrader5",),
    "mt5_initialize": ("initialize",),
    "mt5_login": ("login",),
    "order_send": ("order_send",),
    "order_check": ("order_check",),
    "positions_get": ("positions_get",),
    "copy_ticks": ("copy_ticks",),
    "copy_rates": ("copy_rates",),
    "symbol_info_tick": ("symbol_info_tick",),
    "mql_OrderSend": ("ordersend",),
    "mql_CTrade": ("ctrade",),
    "zeromq": ("zmq", "zeromq"),
    "socket_usage": ("socket", "wsastartup"),
    "dwx": ("dwx", "darwinex"),
    "ctrader": ("ctrader", "calgo"),
    "fix_protocol": ("fix",),
    "oanda": ("oanda",),
    "ccxt": ("ccxt",),
    "ib_insync": ("ib_insync", "ib_async", "ibapi"),
    "http_client": ("requests", "httpx", "aiohttp", "urllib", "webrequest"),
    "websocket": ("websocket", "ws://", "wss://"),
    "telegram": ("telegram", "telebot"),
    "smtplib": ("smtp", "sendmail"),
    "discord": ("discord",),
    "credential_assignment_literal": _CREDENTIAL_TRIGGERS,
    "credential_assignment_number": ("login", "account"),
    "mt5_login_or_initialize_literal_arguments": ("login", "initialize"),
    "private_key_header": ("private key",),
    "connection_string_with_credentials": ("://",),
    "authorization_header_literal": ("authorization",),
    "credential_config_value": _CREDENTIAL_TRIGGERS,
    "python_main_guard": ("__main__",),
    "test_function": ("test",),
    "unittest_usage": ("unittest",),
    "pytest_usage": ("pytest",),
    "windows_scheduled_task_xml": ("schemas.microsoft.com",),
    "windows_service_wrapper_xml": ("<executable>",),
}


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    triggers: Tuple[str, ...]


def _rule(name: str, pattern: str, flags: int = 0) -> _Rule:
    return _Rule(name, re.compile(pattern, flags | re.ASCII), _TRIGGERS[name])


_I = re.IGNORECASE
_M = re.MULTILINE
_LINE_START = r"^[ \t\"']*"

BROKER_RULES: Tuple[_Rule, ...] = (
    _rule(
        "metatrader5_import",
        _LINE_START + r"(?:import[ \t]+MetaTrader5|from[ \t]+MetaTrader5[ \t]+import)\b",
        _M,
    ),
    _rule("metatrader5_reference", r"\bMetaTrader5\b", _I),
    _rule("mt5_initialize", r"\b(?:mt5|MetaTrader5|mt)[ \t]*\.[ \t]*initialize[ \t]*\(", _I),
    _rule("mt5_login", r"\b(?:mt5|MetaTrader5|mt)[ \t]*\.[ \t]*login[ \t]*\(", _I),
    _rule("order_send", r"\border_send[ \t]*\("),
    _rule("order_check", r"\border_check[ \t]*\("),
    _rule("positions_get", r"\bpositions_get[ \t]*\("),
    _rule("copy_ticks", r"\bcopy_ticks\w*[ \t]*\("),
    _rule("copy_rates", r"\bcopy_rates\w*[ \t]*\("),
    _rule("symbol_info_tick", r"\bsymbol_info_tick[ \t]*\("),
    _rule("mql_OrderSend", r"\bOrderSend(?:Async)?[ \t]*\("),
    _rule("mql_CTrade", r"\bCTrade\b"),
    _rule("zeromq", r"\b(?:zmq|zeromq|pyzmq|libzmq)\b", _I),
    _rule(
        "socket_usage",
        _LINE_START
        + r"(?:import[ \t]+socket\b|from[ \t]+socket[ \t]+import\b)"
        + r"|\bsocket[ \t]*\.[ \t]*(?:socket|create_connection)[ \t]*\("
        + r"|\bSocket(?:Create|Connect|Send|Read)[ \t]*\(|\bSystem\.Net\.Sockets\b"
        + r"|\bjava\.net\.Socket\b|\bWSAStartup\b",
        _M,
    ),
    _rule("dwx", r"\bdwx(?:_\w+)?\b|darwinex", _I),
    _rule("ctrader", r"\bctrader\w*|\bcalgo\b", _I),
    _rule(
        "fix_protocol",
        r"\b(?:quickfix|QuickFIX|simplefix|FIX\.[45]\.[0-9]|FIXT\.1\.1|FIX4[0-4]|FIX50)\b"
        + r"|\b[Ff][Ii][Xx][ \t]+(?:[Aa][Pp][Ii]|[Pp]rotocol|[Ss]ession|[Ee]ngine|[Gg]ateway)\b",
    ),
    _rule("oanda", r"\boanda\w*", _I),
    _rule("ccxt", r"\bccxt\b"),
    _rule("ib_insync", r"\b(?:ib_insync|ib_async|ibapi)\b"),
    _rule(
        "http_client",
        _LINE_START
        + r"(?:import|from)[ \t]+(?:requests|httpx|aiohttp|urllib3)\b"
        + r"|\b(?:requests|httpx)[ \t]*\.[ \t]*"
        + r"(?:get|post|put|patch|delete|request|Session|Client|AsyncClient)[ \t]*\("
        + r"|\bWebRequest[ \t]*\(|\burllib\.request\b",
        _M,
    ),
    _rule("websocket", r"\bwebsockets?\b|\bwss?://", _I),
    _rule("telegram", r"\btelegram\b|\btelebot\b|api\.telegram\.org", _I),
    _rule("smtplib", r"\bsmtplib\b|\bSMTP(?:_SSL)?[ \t]*\(|\bSendMail[ \t]*\("),
    _rule("discord", r"\bdiscord(?:app)?\b", _I),
)

_CREDENTIAL_WORDS = r"(?:password|passwd|pwd|secret|token|api[_\-]?key|login|account)"
_NAME_BEFORE = r"(?<![A-Za-z0-9_])[A-Za-z0-9_.\-]{0,40}"
_NAME_AFTER = r"[A-Za-z0-9_.\-]{0,40}[\"']?[ \t]{0,5}"
_ASSIGN = r"(?::[ \t]{0,3}[A-Za-z_][A-Za-z0-9_.\[\], ]{0,40}=(?!=)|:|=(?!=))[ \t]{0,5}"

CREDENTIAL_RULES: Tuple[_Rule, ...] = (
    _rule(
        "credential_assignment_literal",
        _NAME_BEFORE + _CREDENTIAL_WORDS + _NAME_AFTER + _ASSIGN + r"[rbuRBU]{0,2}\\?([\"'])(?!\\?\1)[^\n]",
        _I,
    ),
    _rule(
        "credential_assignment_number",
        _NAME_BEFORE + r"(?:login|account)" + _NAME_AFTER + r"(?::|=(?!=))[ \t]{0,5}[0-9]{4,}\b",
        _I,
    ),
    _rule(
        "mt5_login_or_initialize_literal_arguments",
        r"\b(?:mt5|MetaTrader5|mt)[ \t]*\.[ \t]*(?:login|initialize)[ \t]*\([^)\n]{0,300}?"
        + r"(?:[0-9]{4,}|[\"'][^\"'\n])"
        + r"|\binitialize[ \t]*\([^)\n]{0,300}?(?:login|password|server)[ \t]*=[ \t]*(?:[0-9]|[\"'][^\"'\n])",
        _I,
    ),
    _rule("private_key_header", r"-----BEGIN (?:[A-Z0-9]+ ){0,3}PRIVATE KEY(?: BLOCK)?-----"),
    _rule(
        "connection_string_with_credentials",
        r"\b[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s/:@\"']{1,100}:[^\s/@\"']{1,200}@",
    ),
    _rule(
        "authorization_header_literal",
        r"\bauthorization[\"']?[ \t]{0,3}[:=][ \t]{0,3}[\"']?(?:bearer|basic)[ \t]+[A-Za-z0-9._~+/=\-]{8,}",
        _I,
    ),
)
CREDENTIAL_CONFIG_RULES: Tuple[_Rule, ...] = (
    _rule(
        "credential_config_value",
        r"^[ \t]*(?:export[ \t]+)?[A-Za-z0-9_.\-]{0,40}"
        + _CREDENTIAL_WORDS
        + r"[A-Za-z0-9_.\-]{0,40}[ \t]{0,5}[:=][ \t]{0,5}[^\s\"'#;:=][^\n]*",
        _I | _M,
    ),
)
PYTHON_RULES: Tuple[_Rule, ...] = (
    _rule(
        "python_main_guard", r"^[ \t\"']*if[ \t]+__name__[ \t]*==[ \t]*\\?[\"']__main__\\?[\"'][ \t]*:", _M
    ),
    _rule("test_function", r"^[ \t\"']*(?:async[ \t]+)?def[ \t]+test\w*[ \t]*\(", _M),
    _rule(
        "unittest_usage",
        _LINE_START + r"(?:import[ \t]+unittest\b|from[ \t]+unittest\b)|\bunittest\.TestCase\b",
        _M,
    ),
    _rule("pytest_usage", _LINE_START + r"(?:import[ \t]+pytest\b|from[ \t]+pytest\b)|@pytest\.", _M),
)
XML_RULES: Tuple[_Rule, ...] = (
    _rule("windows_scheduled_task_xml", r"schemas\.microsoft\.com/windows/2004/02/mit/task"),
    _rule("windows_service_wrapper_xml", r"<service>[\s\S]{0,4000}?<executable>"),
)

_NEWLINE = re.compile("\n")


def _apply_rules(
    text: str, rules: Sequence[_Rule], max_hits: int, prefilter: bool = True
) -> Dict[str, Tuple[List[int], int]]:
    """rule name -> (line numbers listed, number of lines that matched).  Matched text is discarded."""
    offsets: Optional[List[int]] = None
    found: Dict[str, Tuple[List[int], int]] = {}
    lowered = text.lower() if prefilter else ""
    present: Dict[str, bool] = {}
    for rule in rules:
        if prefilter:
            for trigger in rule.triggers:
                if trigger not in present:
                    present[trigger] = trigger in lowered
            if not any(present[trigger] for trigger in rule.triggers):
                continue
        lines: List[int] = []
        last = 0
        total = 0
        for match in rule.pattern.finditer(text):
            if offsets is None:
                offsets = [newline.start() for newline in _NEWLINE.finditer(text)]
            line = bisect.bisect_left(offsets, match.start()) + 1
            if line == last:
                continue
            last = line
            total += 1
            if len(lines) < max_hits:
                lines.append(line)
        if total:
            found[rule.name] = (lines, total)
    return found


# --------------------------------------------------------------------------------------------
# Dependency manifests: package names and version specifiers only
# --------------------------------------------------------------------------------------------

_SENSITIVE_VALUE_MARKERS = ("://", "@", "token", "password", "passwd", "secret", "key=", "pwd=")
_REQUIREMENT_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._\-]{0,98}[A-Za-z0-9])?"
_SPEC_CLAUSE = r"(?:===|==|~=|!=|<=|>=|<|>)[ \t]*[A-Za-z0-9.*+!_\-]{1,60}"
_REQUIREMENT = re.compile(
    r"^(?P<name>" + _REQUIREMENT_NAME + r")[ \t]*(?P<extras>\[[A-Za-z0-9._,\- \t]{0,100}\])?[ \t]*"
    r"(?P<spec>" + _SPEC_CLAUSE + r"(?:[ \t]*,[ \t]*" + _SPEC_CLAUSE + r"){0,8})?$"
)
_CONDA_REQUIREMENT = re.compile(
    r"^(?P<name>" + _REQUIREMENT_NAME + r")"
    r"(?:[ \t]*(?P<spec>[=<>!~]{1,3}[ \t]*[A-Za-z0-9.*+!_\-]{1,60}(?:=[A-Za-z0-9.*+_\-]{1,60})?"
    r"(?:[ \t]*,[ \t]*[=<>!~]{1,3}[ \t]*[A-Za-z0-9.*+!_\-]{1,60}){0,8}))?$"
)
_LOOSE_SPEC = re.compile(r"^[\^~<>=!*,.|xX \tA-Za-z0-9+_\-]{0,80}$")
_NPM_NAME = re.compile(r"^(?:@[A-Za-z0-9~][A-Za-z0-9._~\-]{0,80}/)?[A-Za-z0-9~][A-Za-z0-9._~\-]{0,120}$")
_VERSION_STRING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,59}$")
_TOML_TABLE = re.compile(r"^\[\[?[ \t]*([^\[\]]+?)[ \t]*\]\]?$")
_TOML_KEY = re.compile(r"^([A-Za-z0-9_.\-]+|\"[^\"]*\"|'[^']*')[ \t]*=[ \t]*(.*)$")
_TOML_STRING = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'([^']*)'")
_TOML_VERSION_IN_TABLE = re.compile(r"\bversion[ \t]*=[ \t]*(?:\"([^\"]*)\"|'([^']*)')")


def _looks_sensitive(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SENSITIVE_VALUE_MARKERS)


class _Dependencies:
    """Entries taken from one dependency manifest, plus counts of everything that was dropped."""

    def __init__(self) -> None:
        self.entries: List[Dict[str, str]] = []
        self.dropped = {"sensitive": 0, "options": 0, "unparsed": 0}

    def add_requirement(self, raw: str, section: str, conda: bool = False) -> None:
        """Add one PEP 508 / conda style requirement; only the validated name and specifier survive."""
        if _looks_sensitive(raw):
            self.dropped["sensitive"] += 1
            return
        text = raw.split(" #", 1)[0].split(";", 1)[0]
        text = re.sub(r"[ \t]+--hash=\S+", "", text).strip().strip("\\").strip()
        if not text or text.startswith("#"):
            return
        if text.startswith("-"):
            self.dropped["options"] += 1
            return
        match = (_CONDA_REQUIREMENT if conda else _REQUIREMENT).match(text)
        if match is None and conda:
            match = _REQUIREMENT.match(text)
        if match is None:
            self.dropped["unparsed"] += 1
            return
        entry = {"name": match.group("name"), "section": section}
        specifier = match.group("spec")
        if specifier:
            entry["specifier"] = re.sub(r"[ \t]+", "", specifier)
        self.entries.append(entry)

    def add_named(self, name: str, specifier: Optional[str], section: str, npm: bool = False) -> None:
        """Add a ``name = "specifier"`` style entry (Pipfile, poetry, package.json)."""
        if _looks_sensitive(name.lstrip("@") if npm else name) or (specifier and _looks_sensitive(specifier)):
            self.dropped["sensitive"] += 1
            return
        valid_name = _NPM_NAME.match(name) if npm else re.match("^" + _REQUIREMENT_NAME + "$", name)
        if valid_name is None or (specifier is not None and _LOOSE_SPEC.match(specifier) is None):
            self.dropped["unparsed"] += 1
            return
        entry = {"name": name, "section": section}
        if specifier:
            entry["specifier"] = specifier.strip()
        self.entries.append(entry)

    def as_dict(self) -> Dict[str, Any]:
        ordered = sorted(
            self.entries, key=lambda item: (item["section"], item["name"].lower(), item.get("specifier", ""))
        )
        return {"entries": ordered, "dropped": dict(self.dropped)}


def _strip_toml_comment(line: str) -> str:
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _toml_items(text: str) -> Iterator[Tuple[str, str, str]]:
    """Yield ``(table, key, raw value)`` from TOML-like text; multi-line arrays are joined.

    A deliberately small line reader: ``tomllib`` only exists from Python 3.11, and only string
    arrays and ``key = value`` lines of a few known tables are needed.
    """
    table = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = _strip_toml_comment(lines[index]).strip()
        index += 1
        if not line:
            continue
        header = _TOML_TABLE.match(line)
        if header is not None:
            table = header.group(1).strip()
            continue
        item = _TOML_KEY.match(line)
        if item is None:
            continue
        key = item.group(1).strip("\"'")
        value = item.group(2)
        if value.startswith("["):
            parts = [value]
            while (
                "]" not in _TOML_STRING.sub("", " ".join(parts)) and index < len(lines) and len(parts) < 5000
            ):
                parts.append(_strip_toml_comment(lines[index]).strip())
                index += 1
            value = " ".join(parts)
        yield table, key, value


def _toml_strings(value: str) -> List[str]:
    return [
        match.group(1) if match.group(1) is not None else match.group(2)
        for match in _TOML_STRING.finditer(value)
    ]


def _parse_requirements(text: str, found: _Dependencies) -> None:
    for line in text.splitlines():
        found.add_requirement(line.strip(), "requirements")


def _parse_named_table_value(key: str, value: str, section: str, found: _Dependencies) -> None:
    if value.startswith("{"):
        if _looks_sensitive(value):
            found.dropped["sensitive"] += 1
            return
        version = _TOML_VERSION_IN_TABLE.search(value)
        found.add_named(key, (version.group(1) or version.group(2)) if version else None, section)
        return
    strings = _toml_strings(value)
    found.add_named(key, strings[0] if strings else None, section)


def _parse_pyproject(text: str, found: _Dependencies) -> None:
    for table, key, value in _toml_items(text):
        if (table == "project" and key == "dependencies") or (table == "build-system" and key == "requires"):
            for item in _toml_strings(value):
                found.add_requirement(item, table + "." + key)
        elif table == "project.optional-dependencies":
            for item in _toml_strings(value):
                found.add_requirement(item, "project.optional-dependencies")
        elif table.startswith("tool.poetry") and table.endswith("dependencies"):
            _parse_named_table_value(key, value, table, found)


def _parse_pipfile(text: str, found: _Dependencies) -> None:
    for table, key, value in _toml_items(text):
        if table in ("packages", "dev-packages"):
            _parse_named_table_value(key, value, table, found)


def _parse_setup_cfg(text: str, found: _Dependencies) -> None:
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(text)
    for section, option in (
        ("options", "install_requires"),
        ("options", "setup_requires"),
        ("options", "tests_require"),
    ):
        if parser.has_option(section, option):
            for line in parser.get(section, option).splitlines():
                found.add_requirement(line.strip(), section + "." + option)
    if parser.has_section("options.extras_require"):
        for _name, value in parser.items("options.extras_require"):
            for line in value.splitlines():
                found.add_requirement(line.strip(), "options.extras_require")


def _parse_conda_environment(text: str, found: _Dependencies) -> None:
    in_dependencies = False
    section = "conda"
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[0].isspace() and not stripped.startswith("-"):
            in_dependencies = stripped.split("#", 1)[0].strip() == "dependencies:"
            section = "conda"
            continue
        if not in_dependencies or not stripped.startswith("-"):
            continue
        item = stripped[1:].strip()
        if item.split("#", 1)[0].strip().rstrip(":") == "pip" and item.split("#", 1)[0].strip().endswith(":"):
            section = "pip"
            continue
        found.add_requirement(item.strip("\"'"), section, conda=(section == "conda"))


def _parse_package_json(text: str, found: _Dependencies) -> None:
    document = json.loads(text)
    if not isinstance(document, dict):
        return
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = document.get(section)
        if not isinstance(block, dict):
            continue
        for name, specifier in block.items():
            found.add_named(str(name), specifier if isinstance(specifier, str) else None, section, npm=True)


_DEPENDENCY_PARSERS = {
    "requirements": _parse_requirements,
    "pyproject": _parse_pyproject,
    "setup.cfg": _parse_setup_cfg,
    "conda environment": _parse_conda_environment,
    "package.json": _parse_package_json,
    "Pipfile": _parse_pipfile,
}


def _parse_dependency_manifest(kind: str, text: str) -> Dict[str, Any]:
    found = _Dependencies()
    try:
        _DEPENDENCY_PARSERS[kind](text, found)
    except (ValueError, configparser.Error) as exc:
        result = found.as_dict()
        result["parse_error"] = type(exc).__name__
        return result
    return found.as_dict()


def _python_versions(kind: str, text: str) -> List[str]:
    """Validated Python version strings from pyvenv.cfg / .python-version / runtime.txt."""
    versions: List[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if kind == "pyvenv.cfg":
            key, _, value = line.partition("=")
            if key.strip().lower() not in ("version", "version_info"):
                continue
            line = value.strip()
        if _VERSION_STRING.match(line) and line not in versions:
            versions.append(line)
        if kind == "runtime.txt":
            break
    return versions


# --------------------------------------------------------------------------------------------
# Delimited text headers (column names only)
# --------------------------------------------------------------------------------------------

_COLUMN_NAME = re.compile(r"^(?:[^\W\d]|[<\[(#%$])[\w .\-/<>()\[\]%#:$]{0,63}$")
_LONG_DIGIT_RUN = re.compile(r"[0-9]{5,}")


def _looks_like_column_name(value: str) -> bool:
    if value == "":
        return True  # an unnamed index column, as written by common export tools
    return (
        bool(_COLUMN_NAME.match(value)) and not _LONG_DIGIT_RUN.search(value) and not _looks_sensitive(value)
    )


def _delimited_header(head: bytes, extension: str) -> Dict[str, Any]:
    """Column names of a .csv/.tsv file, or the reason why the first line is withheld.

    The first line is only reported when every field looks like a column name; a first line
    that looks like data (numbers, dates, addresses) is never emitted.
    """
    text = _decode_text(head)
    if text is None:
        return {"header_withheld": "binary content"}
    first_line, separator, _rest = text.partition("\n")
    first_line = first_line.rstrip("\r")
    truncated = len(first_line) > _HEADER_MAX_CHARS or (not separator and len(head) >= _HEADER_PROBE_BYTES)
    first_line = first_line[:_HEADER_MAX_CHARS]
    delimiter = _DELIMITED_EXTENSIONS[extension]
    if extension == ".csv":
        delimiter = max(",;\t|", key=first_line.count)
    try:
        fields = next(csv.reader([first_line], delimiter=delimiter), [])
    except csv.Error:
        return {"header_withheld": "first line is not parseable"}
    if truncated and fields:
        fields = fields[:-1]
    names = [field_value.strip() for field_value in fields]
    if not names or not any(names) or not all(_looks_like_column_name(name) for name in names):
        return {
            "header_withheld": "first line does not look like column names",
            "first_line_fields": len(names),
        }
    return {
        "columns": [_clean_text(name) for name in names],
        "delimiter": {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}[delimiter],
        "header_truncated": truncated,
    }


# --------------------------------------------------------------------------------------------
# Walking the tree (links are never followed, excluded directories are only counted)
# --------------------------------------------------------------------------------------------


@dataclass
class _Options:
    excluded: Dict[str, str] = field(default_factory=dict)  # normalised directory name -> reason
    detect_environments: bool = True
    hash_sensitive: bool = False
    max_hash_bytes: Optional[int] = None
    includes: Optional[Tuple[str, ...]] = None
    count_rows: bool = False
    csv_headers: bool = True
    max_line_count_bytes: int = _DEFAULT_MAX_LINE_COUNT_BYTES
    max_scan_bytes: int = _DEFAULT_MAX_SCAN_BYTES
    max_list_entries: int = _DEFAULT_MAX_LIST_ENTRIES
    max_hits_per_file: int = _DEFAULT_MAX_HITS_PER_FILE
    progress_every: int = 0


@dataclass
class _FileEntry:
    os_path: str
    rel: str
    name: str
    parents: Tuple[str, ...]
    st: os.stat_result
    offline: bool


@dataclass
class _WalkReport:
    directories: List[Tuple[str, str]] = field(default_factory=list)  # (relative path, raw name)
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)
    special: List[Dict[str, Any]] = field(default_factory=list)
    unreadable: List[Dict[str, Any]] = field(default_factory=list)
    git_directories: List[Tuple[str, str]] = field(default_factory=list)  # (os path, relative path)
    environments: List[Dict[str, Any]] = field(default_factory=list)
    other_vcs: List[str] = field(default_factory=list)


def _entry_kind(st: Any) -> str:
    """'link', 'dir', 'file', 'offline_file' or 'special' for an ``os.lstat`` result."""
    attributes = getattr(st, "st_file_attributes", 0)
    if stat.S_ISLNK(st.st_mode):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "link" if attributes & _FILE_ATTRIBUTE_REPARSE_POINT else "dir"
    if stat.S_ISREG(st.st_mode):
        return "offline_file" if attributes & _OFFLINE_ATTRIBUTES else "file"
    return "special"


def _special_kind(mode: int) -> str:
    for check, label in (
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISBLK, "block device"),
    ):
        if check(mode):
            return label
    return "other"


def _error_record(rel: str, exc: OSError, stage: str) -> Dict[str, Any]:
    return {"path": rel, "error": type(exc).__name__, "errno": exc.errno, "stage": stage}


def _link_record(path: str, rel: str, st: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {"path": rel, "kind": "symlink" if stat.S_ISLNK(st.st_mode) else "reparse point"}
    try:
        record["target"] = _clean_text(os.readlink(path))
    except (OSError, ValueError) as exc:
        record["target"] = None
        record["error"] = type(exc).__name__
    return record


def _summarise_directory(path: str) -> Dict[str, int]:
    """Count the content of a directory that is not descended into.  No file is opened."""
    totals = {"files": 0, "bytes": 0, "directories": 0, "links": 0, "other": 0, "errors": 0}
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError:
            totals["errors"] += 1
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                totals["errors"] += 1
                continue
            kind = _entry_kind(st)
            if kind == "dir":
                totals["directories"] += 1
                pending.append(entry.path)
            elif kind in ("file", "offline_file"):
                totals["files"] += 1
                totals["bytes"] += st.st_size
            elif kind == "link":
                totals["links"] += 1
            else:
                totals["other"] += 1
    return totals


def _lstat_kind(path: str) -> Optional[str]:
    try:
        return _entry_kind(os.lstat(path))
    except OSError:
        return None


def _environment_record(path: str, rel: str) -> Optional[Dict[str, Any]]:
    """Describe a Python environment directory (pyvenv.cfg or conda-meta), or None."""
    if _lstat_kind(os.path.join(path, "pyvenv.cfg")) == "file":
        head = _read_head(os.path.join(path, "pyvenv.cfg"), _VERSION_FILE_BYTES)
        text = _decode_text(head) if head is not None else None
        versions = _python_versions("pyvenv.cfg", text) if text is not None else []
        return {"path": rel, "kind": "venv", "detected_by": "pyvenv.cfg", "python_versions": versions}
    meta = os.path.join(path, "conda-meta")
    if _lstat_kind(meta) == "dir":
        versions = []
        try:
            with os.scandir(meta) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError:
            names = []
        for name in names:
            match = re.match(r"^python-([0-9]+(?:\.[0-9]+){1,3})-", name)
            if match and match.group(1) not in versions:
                versions.append(match.group(1))
        return {"path": rel, "kind": "conda", "detected_by": "conda-meta", "python_versions": versions}
    return None


def _walk_tree(root_os: str, options: _Options, report: _WalkReport) -> Iterator[_FileEntry]:
    """Yield every regular file below ``root_os`` in a deterministic order."""
    pending: List[Tuple[str, str, Tuple[str, ...]]] = [(root_os, "", ())]
    while pending:
        directory, directory_rel, parents = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            report.unreadable.append(_error_record(directory_rel or ".", exc, "list directory"))
            continue
        children: List[Tuple[str, str, Tuple[str, ...]]] = []
        for name in names:
            path = os.path.join(directory, name)
            rel = _join_rel(directory_rel, name)
            try:
                st = os.lstat(path)
            except OSError as exc:
                report.unreadable.append(_error_record(rel, exc, "stat"))
                continue
            kind = _entry_kind(st)
            if kind == "link":
                report.links.append(_link_record(path, rel, st))
            elif kind == "dir":
                if name == ".git":
                    report.git_directories.append((path, rel))
                elif name in _VCS_OTHER_DIRECTORIES:
                    report.other_vcs.append(rel)
                reason = options.excluded.get(os.path.normcase(name))
                environment = (
                    _environment_record(path, rel) if (reason or options.detect_environments) else None
                )
                if environment is not None:
                    report.environments.append(environment)
                    if reason is None:
                        reason = "python environment (" + environment["detected_by"] + " found)"
                if reason is not None:
                    summary: Dict[str, Any] = {"path": rel, "reason": reason}
                    summary.update(_summarise_directory(path))
                    report.excluded.append(summary)
                else:
                    report.directories.append((rel, name))
                    children.append((path, rel, (*parents, name)))
            elif kind == "special":
                report.special.append({"path": rel, "kind": _special_kind(st.st_mode)})
            else:
                yield _FileEntry(path, rel, name, parents, st, kind == "offline_file")
        pending.extend(reversed(children))


def _matches_any(rel: str, patterns: Sequence[str]) -> bool:
    """Glob match of a relative ``/``-separated path.

    ``*`` also crosses ``/`` and a directory path selects everything below it.
    """
    candidate = rel.lower() if _is_windows() else rel
    for raw in patterns:
        pattern = raw.replace("\\", "/") if os.sep == "\\" else raw
        pattern = pattern.lower() if _is_windows() else pattern
        pattern = pattern.rstrip("/")
        if fnmatch.fnmatchcase(candidate, pattern) or candidate.startswith(pattern + "/"):
            return True
    return False


# --------------------------------------------------------------------------------------------
# Inventory collector
# --------------------------------------------------------------------------------------------


def _brief(entry: _FileEntry) -> Dict[str, Any]:
    return {
        "path": entry.rel,
        "size": entry.st.st_size,
        "mtime_ns": entry.st.st_mtime_ns,
        "mtime_utc": _iso_from_ns(entry.st.st_mtime_ns),
    }


def _bump(table: Dict[str, List[int]], key: str, size: int) -> None:
    slot = table.setdefault(key, [0, 0])
    slot[0] += 1
    slot[1] += size


def _counts(table: Mapping[str, Sequence[int]]) -> Dict[str, Dict[str, int]]:
    return {key: {"files": value[0], "bytes": value[1]} for key, value in sorted(table.items())}


@dataclass
class _Plan:
    """What has to be read from one file for the inventory."""

    want_lines: bool = False
    want_rows: bool = False
    head_limit: int = 0
    scan_limit: int = 0
    rules: Tuple[_Rule, ...] = ()
    delimited: bool = False
    dependency_kind: Optional[str] = None
    version_kind: Optional[str] = None
    line_cap_hit: bool = False


class _Collector:
    """Accumulates the inventory while files stream past; emits no file content."""

    def __init__(self, options: _Options) -> None:
        self.options = options
        self.files = 0
        self.total_bytes = 0
        self.by_extension: Dict[str, List[int]] = {}
        self.by_top_level: Dict[str, List[int]] = {}
        self.sizes: List[Tuple[int, str]] = []
        self.languages: Dict[str, Dict[str, Any]] = {}
        self.data_files: List[Dict[str, Any]] = []
        self.data_by_extension: Dict[str, List[int]] = {}
        self.models: List[Dict[str, Any]] = []
        self.notebooks: List[str] = []
        self.sensitive: List[Dict[str, Any]] = []
        self.hits: Dict[str, List[Tuple[str, int]]] = {}
        self.hit_totals: Dict[str, List[int]] = {}
        self.hit_caps: List[Dict[str, Any]] = []
        self.test_files: Dict[str, Optional[int]] = {}
        self.test_functions = 0
        self.conftest: List[str] = []
        self.test_config: List[Dict[str, str]] = []
        self.other_tests: List[str] = []
        self.launchers: List[Dict[str, str]] = []
        self.containers: List[Dict[str, str]] = []
        self.mql_files: Dict[str, List[int]] = {}
        self.terminal_hints: List[Dict[str, str]] = []
        self.manifests: List[Dict[str, Any]] = []
        self.listed_only_manifests: List[str] = []
        self.version_files: List[Dict[str, Any]] = []
        self.gitignore: List[str] = []
        self.gitfiles: List[str] = []
        self.hints: List[Dict[str, Any]] = []
        self.not_hashed: List[str] = []
        self.line_cap: List[str] = []
        self.scan_truncated: List[str] = []
        self.scan_binary: List[str] = []
        self.offline: List[str] = []
        self.changed: List[str] = []
        self.unreadable: List[Dict[str, Any]] = []
        self.files_opened = 0
        self.bytes_read = 0

    # -- planning ---------------------------------------------------------------------------

    def plan(self, entry: _FileEntry, sensitive: bool) -> _Plan:
        plan = _Plan()
        lowered = entry.name.lower()
        extension = os.path.splitext(lowered)[1]
        size = entry.st.st_size
        language = LANGUAGE_BY_EXTENSION.get(extension)
        if language is not None and language[1]:
            plan.want_lines = size <= self.options.max_line_count_bytes
            plan.line_cap_hit = not plan.want_lines
        rules: List[_Rule] = []
        if extension in _SOURCE_EXTENSIONS:
            rules.extend(BROKER_RULES)
            rules.extend(CREDENTIAL_RULES)
        elif extension in _CONFIG_EXTENSIONS or lowered in _CONFIG_FILE_NAMES or _container_kind(lowered):
            rules.extend(BROKER_RULES)
            rules.extend(CREDENTIAL_RULES)
            rules.extend(CREDENTIAL_CONFIG_RULES)
        if extension in _PYTHON_EXTENSIONS:
            rules.extend(PYTHON_RULES)
        if extension == ".xml":
            rules.extend(XML_RULES)
        plan.rules = tuple(rules)
        if rules:
            plan.scan_limit = self.options.max_scan_bytes
            if extension not in _SOURCE_EXTENSIONS:
                plan.scan_limit = min(plan.scan_limit, _CONFIG_SCAN_BYTES)
            plan.head_limit = plan.scan_limit
        if sensitive:
            return plan  # hashed on request, scanned for locations, but nothing is extracted
        if extension in _DELIMITED_EXTENSIONS:
            plan.delimited = self.options.csv_headers
            plan.want_rows = self.options.count_rows
            if plan.delimited:
                plan.head_limit = max(plan.head_limit, _HEADER_PROBE_BYTES)
        plan.dependency_kind = _dependency_kind(lowered, entry.parents)
        if lowered in ("pyvenv.cfg", ".python-version", "runtime.txt"):
            plan.version_kind = lowered
            plan.head_limit = max(plan.head_limit, _VERSION_FILE_BYTES)
        if plan.dependency_kind is not None or lowered in ("setup.cfg", "pyproject.toml"):
            plan.head_limit = max(plan.head_limit, _MANIFEST_PARSE_BYTES)
        return plan

    # -- per file ---------------------------------------------------------------------------

    def add(
        self,
        entry: _FileEntry,
        plan: _Plan,
        read: Optional[_ReadResult],
        sensitive_reason: Optional[str],
        hashed: bool,
    ) -> None:
        lowered = entry.name.lower()
        extension = os.path.splitext(lowered)[1]
        size = entry.st.st_size
        self.files += 1
        self.total_bytes += size
        self.sizes.append((size, entry.rel))
        _bump(self.by_extension, _display_name(extension) if extension else "(none)", size)
        _bump(self.by_top_level, entry.rel.split("/", 1)[0] if entry.parents else "(files in root)", size)
        if read is not None:
            self.files_opened += 1
            self.bytes_read += read.bytes_read
            if read.error is not None:
                self.unreadable.append(
                    {"path": entry.rel, "error": read.error, "errno": read.errno, "stage": "read"}
                )
                read = None
            elif read.changed:
                self.changed.append(entry.rel)
        if entry.offline:
            self.offline.append(entry.rel)
        if sensitive_reason is not None:
            item = _brief(entry)
            item.update({"sensitive": True, "reason": sensitive_reason, "hashed": hashed})
            self.sensitive.append(item)
        self._add_language(entry, extension, plan, read)
        self._add_names(entry, lowered, extension)
        if extension in DATA_EXTENSIONS:
            self._add_data(entry, extension, plan, read)
        if extension in MODEL_EXTENSIONS or (
            extension in (".txt", ".json") and _MODEL_TEXT_NAME.search(lowered)
        ):
            item = _brief(entry)
            item["matched_by"] = "extension" if extension in MODEL_EXTENSIONS else "name pattern"
            self.models.append(item)
        self._add_hints(entry.rel, entry.name, "file")
        if read is None:
            return
        if plan.rules:
            self._scan(entry, lowered, read, plan)
        if plan.dependency_kind is not None:
            self._add_dependencies(entry, plan.dependency_kind, read)
        if plan.version_kind is not None:
            text = _decode_text(read.head[:_VERSION_FILE_BYTES])
            versions = _python_versions(plan.version_kind, text) if text is not None else []
            self.version_files.append(
                {"path": entry.rel, "kind": plan.version_kind, "python_versions": versions}
            )
        if lowered in ("setup.cfg", "pyproject.toml"):
            text = _decode_text(read.head) or ""
            marker = r"^\[tool:pytest\]" if lowered == "setup.cfg" else r"^\[tool\.pytest"
            if re.search(marker, text, _M):
                self.test_config.append({"path": entry.rel, "kind": lowered + " pytest section"})

    def add_directory(self, rel: str, name: str) -> None:
        self._add_hints(rel, name, "dir")
        if name.lower() in _TERMINAL_DIRECTORY_HINTS:
            self.terminal_hints.append({"path": rel, "hint": "directory named " + name.lower()})

    def _add_language(
        self, entry: _FileEntry, extension: str, plan: _Plan, read: Optional[_ReadResult]
    ) -> None:
        language = LANGUAGE_BY_EXTENSION.get(extension)
        if language is None:
            return
        slot = self.languages.setdefault(
            language[0],
            {"files": 0, "bytes": 0, "lines": 0, "files_with_line_count": 0, "extensions": set()},
        )
        slot["files"] += 1
        slot["bytes"] += entry.st.st_size
        slot["extensions"].add(extension)
        if plan.line_cap_hit:
            self.line_cap.append(entry.rel)
        if read is not None and plan.want_lines and read.complete and read.text_kind != "binary":
            slot["lines"] += _line_count(read)
            slot["files_with_line_count"] += 1

    def _add_names(self, entry: _FileEntry, lowered: str, extension: str) -> None:
        rel = entry.rel
        if extension == ".ipynb":
            self.notebooks.append(rel)
        if extension in (".mq4", ".mq5", ".mqh", ".ex4", ".ex5"):
            _bump(self.mql_files, extension, entry.st.st_size)
        if lowered in _TERMINAL_FILE_HINTS:
            self.terminal_hints.append({"path": rel, "hint": "file named " + lowered})
        if extension in _LAUNCHER_EXTENSIONS:
            self.launchers.append({"path": rel, "kind": extension})
        container = _container_kind(lowered)
        if container is not None:
            self.containers.append({"path": rel, "kind": container})
        if extension in (".py", ".pyw") and (
            fnmatch.fnmatchcase(lowered, "test_*.py") or lowered.endswith("_test.py")
        ):
            self.test_files.setdefault(rel, None)
        elif any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _OTHER_TEST_GLOBS):
            self.other_tests.append(rel)
        if lowered == "conftest.py":
            self.conftest.append(rel)
        if lowered in _TEST_CONFIG_NAMES:
            self.test_config.append({"path": rel, "kind": lowered})
        if lowered in _LISTED_ONLY_MANIFESTS:
            self.listed_only_manifests.append(rel)
        if lowered == ".gitignore":
            self.gitignore.append(rel)
        if lowered == ".git":
            self.gitfiles.append(rel)

    def _add_hints(self, rel: str, name: str, kind: str) -> None:
        lowered = name.lower()
        matched = [hint for hint in ENGINE_HINTS if hint in lowered]
        if matched:
            self.hints.append({"path": rel, "kind": kind, "hints": matched})

    def _add_data(self, entry: _FileEntry, extension: str, plan: _Plan, read: Optional[_ReadResult]) -> None:
        _bump(self.data_by_extension, extension, entry.st.st_size)
        item = _brief(entry)
        if read is not None and plan.delimited:
            header = _delimited_header(read.head[:_HEADER_PROBE_BYTES], extension)
            item.update(header)
            if plan.want_rows and read.complete and read.text_kind != "binary":
                lines = _line_count(read)
                item["row_count"] = max(lines - 1, 0) if "columns" in header else lines
                item["row_count_excludes_header"] = "columns" in header
        self.data_files.append(item)

    def _add_dependencies(self, entry: _FileEntry, kind: str, read: _ReadResult) -> None:
        item: Dict[str, Any] = {"path": entry.rel, "kind": kind}
        text = _decode_text(read.head[:_MANIFEST_PARSE_BYTES])
        if text is None:
            item.update({"entries": [], "dropped": {}, "parse_error": "binary content"})
        else:
            item.update(_parse_dependency_manifest(kind, text))
        item["truncated"] = entry.st.st_size > _MANIFEST_PARSE_BYTES
        self.manifests.append(item)

    def _scan(self, entry: _FileEntry, lowered: str, read: _ReadResult, plan: _Plan) -> None:
        text = _decode_text(read.head[: plan.scan_limit])
        if text is None:
            self.scan_binary.append(entry.rel)
            return
        if entry.st.st_size > plan.scan_limit:
            self.scan_truncated.append(entry.rel)
        found = _apply_rules(text, plan.rules, self.options.max_hits_per_file)
        for name, (lines, total) in found.items():
            self.hits.setdefault(name, []).extend((entry.rel, line) for line in lines)
            totals = self.hit_totals.setdefault(name, [0, 0])
            totals[0] += total
            totals[1] += 1
            if total > len(lines):
                self.hit_caps.append({"path": entry.rel, "rule": name, "hits": total, "listed": len(lines)})
        tests = found.get("test_function")
        if tests is not None:
            self.test_functions += tests[1]
            if entry.rel in self.test_files:
                self.test_files[entry.rel] = tests[1]
        elif entry.rel in self.test_files:
            self.test_files[entry.rel] = 0


def _line_count(read: _ReadResult) -> int:
    if read.bytes_read == 0:
        return 0
    return read.newlines + (0 if read.ends_with_newline else 1)


# --------------------------------------------------------------------------------------------
# Scanning: manifest records (+ optional inventory collection)
# --------------------------------------------------------------------------------------------


def _progress_line(files: int, read_bytes: int) -> None:
    print(
        f"[{TOOL_NAME}] {files} files listed, {read_bytes / float(1 << 20):.1f} MiB read",
        file=sys.stderr,
        flush=True,
    )


def _scan_tree(
    root_os: str, options: _Options, report: _WalkReport, collector: Optional[_Collector]
) -> List[Dict[str, Any]]:
    """Walk the tree once and return the manifest records sorted by path."""
    records: List[Dict[str, Any]] = []
    read_bytes = 0
    for entry in _walk_tree(root_os, options, report):
        if options.includes is not None and not _matches_any(entry.rel, options.includes):
            continue
        size = entry.st.st_size
        record: Dict[str, Any] = {
            "path": entry.rel,
            "size": size,
            "mtime_ns": entry.st.st_mtime_ns,
            "sha256": None,
        }
        sensitive_reason = _sensitive_reason(entry.name, entry.parents)
        if sensitive_reason is not None:
            record["sensitive"] = True
        plan = collector.plan(entry, sensitive_reason is not None) if collector is not None else _Plan()
        read: Optional[_ReadResult] = None
        want_hash = False
        if sensitive_reason is not None and not options.hash_sensitive:
            record["skipped"] = "sensitive"
        elif entry.offline:
            record["skipped"] = "offline_placeholder"
        else:
            want_hash = options.max_hash_bytes is None or size <= options.max_hash_bytes
            if not want_hash:
                record["skipped"] = "max_hash_bytes"
                if collector is not None:
                    collector.not_hashed.append(entry.rel)
            want_full = plan.want_lines or plan.want_rows
            if want_hash or want_full or plan.head_limit:
                read = _read_file(entry.os_path, entry.st, want_hash, want_full, plan.head_limit)
                read_bytes += read.bytes_read
                if read.error is not None:
                    record["skipped"] = "unreadable"
                    record["error"] = read.error
                    if collector is None:
                        report.unreadable.append(
                            {"path": entry.rel, "error": read.error, "errno": read.errno, "stage": "read"}
                        )
                else:
                    record["sha256"] = read.sha256
                    if read.changed:
                        record["unstable"] = True
        if collector is not None:
            collector.add(entry, plan, read, sensitive_reason, record["sha256"] is not None)
        records.append(record)
        if options.progress_every and len(records) % options.progress_every == 0:
            _progress_line(len(records), read_bytes)
    records.sort(key=lambda item: str(item["path"]))
    return records


def _excluded_names(names: Iterable[str], reason: str) -> Dict[str, str]:
    return {os.path.normcase(name): reason for name in names}


def _scan_root(root_real: str) -> str:
    """The path used for all operating-system calls on the root (extended-length on Windows)."""
    if _is_windows():
        extended = _extended_path(root_real)
        try:
            os.lstat(extended)
        except OSError:
            return root_real
        return extended
    return root_real


def _resolve_root(root: PathLike) -> str:
    root_real = _real(root)
    try:
        is_directory = stat.S_ISDIR(os.stat(root_real).st_mode)
    except OSError as exc:
        raise AuditRefused("--root cannot be read (" + type(exc).__name__ + "): " + os.fspath(root)) from None
    if not is_directory:
        raise AuditRefused("--root is not a directory: " + os.fspath(root))
    return root_real


# --------------------------------------------------------------------------------------------
# Public manifest API
# --------------------------------------------------------------------------------------------


def build_manifest(
    root: PathLike,
    includes: Optional[Sequence[str]] = None,
    *,
    excluded_dirs: Optional[Sequence[str]] = None,
    detect_environments: bool = True,
    hash_sensitive: bool = False,
    max_hash_bytes: Optional[int] = None,
    progress_every: int = 0,
) -> List[Dict[str, Any]]:
    """Return one record per regular file below ``root``, sorted by path.

    Each record has ``path`` (relative, ``/``-separated), ``size``, ``mtime_ns`` and ``sha256``
    (hex, or ``None`` together with a ``skipped`` reason: ``sensitive``, ``max_hash_bytes``,
    ``offline_placeholder`` or ``unreadable``).  Optional keys: ``sensitive`` (name suggests a
    secret), ``error`` (exception class of an unreadable file), ``unstable`` (the file changed
    while it was read).

    ``includes`` restricts the result to paths matching any of the glob patterns (``*`` also
    matches ``/``; a directory path selects everything below it).  ``excluded_dirs`` replaces
    `DEFAULT_EXCLUDED_DIRS` (pass ``()`` to exclude nothing).  The tree is only read; links are
    never followed.  Raises `AuditRefused` when ``root`` is not a readable directory.
    """
    root_real = _resolve_root(root)
    names = DEFAULT_EXCLUDED_DIRS if excluded_dirs is None else tuple(excluded_dirs)
    options = _Options(
        excluded=_excluded_names(names, "excluded directory name"),
        detect_environments=detect_environments,
        hash_sensitive=hash_sensitive,
        max_hash_bytes=max_hash_bytes,
        includes=tuple(includes) if includes else None,
        progress_every=progress_every,
    )
    with _guarded(root_real, os.fspath(root)):
        return _scan_tree(_scan_root(root_real), options, _WalkReport(), None)


def _manifest_line(record: Mapping[str, Any]) -> bytes:
    text = json.dumps(dict(record), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "backslashreplace") + b"\n"


def _write_manifest_file(
    records: Iterable[Mapping[str, Any]], path: str, forbidden_root: Optional[str]
) -> int:
    count = 0
    with _create_new_file(path, forbidden_root) as handle:
        for record in sorted(records, key=lambda item: str(item["path"])):
            handle.write(_manifest_line(record))
            count += 1
    return count


def write_manifest(records: Any, path: Any) -> int:
    """Write ``records`` as JSON lines (sorted by path, sorted keys, UTF-8) and return their number.

    The file is created exclusively: an existing file is never overwritten (`FileExistsError`).
    For convenience the two arguments may also be given as ``(path, records)``.
    """
    if isinstance(records, (str, bytes, os.PathLike)):
        records, path = path, records
    return _write_manifest_file(records, os.fsdecode(os.fspath(path)), None)


def read_manifest(path: PathLike) -> List[Dict[str, Any]]:
    """Read a manifest written by `write_manifest` / the ``manifest`` and ``audit`` sub-commands.

    Raises `ValueError` for a line that is not a JSON object with a string ``path``.
    """
    records: List[Dict[str, Any]] = []
    with _open_readonly(os.fspath(path)) as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except ValueError:
                raise ValueError(f"manifest line {number} is not valid UTF-8 JSON") from None
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise ValueError(f"manifest line {number} is not an object with a 'path'")
            records.append(record)
    return records


def _indexed(manifest: Any) -> Dict[str, Mapping[str, Any]]:
    records = read_manifest(manifest) if isinstance(manifest, (str, os.PathLike)) else manifest
    return {str(record["path"]): record for record in records}


def compare_manifests(a: Any, b: Any, strict_mtime: bool = False) -> Dict[str, Any]:
    """Compare two manifests (lists of records, or paths of manifest files).

    Returns a dict with ``added`` / ``removed`` (paths), ``changed`` (``{"path", "reasons"}``
    where reasons are ``size``, ``sha256`` or ``mtime_without_sha256``), ``mtime_only`` (same
    content, different modification time), ``unverified`` (same size and time but no hash on at
    least one side, so identity of content is not proven), ``content_identical`` and
    ``exit_code`` (0 when nothing was added, removed or changed - and, with ``strict_mtime``,
    no modification time differs - otherwise 1).
    """
    left, right = _indexed(a), _indexed(b)
    added = sorted(set(right) - set(left))
    removed = sorted(set(left) - set(right))
    changed: List[Dict[str, Any]] = []
    mtime_only: List[str] = []
    unverified: List[str] = []
    for path in sorted(set(left) & set(right)):
        old, new = left[path], right[path]
        old_hash, new_hash = old.get("sha256"), new.get("sha256")
        both_hashed = isinstance(old_hash, str) and isinstance(new_hash, str)
        mtime_differs = old.get("mtime_ns") != new.get("mtime_ns")
        reasons: List[str] = []
        if old.get("size") != new.get("size"):
            reasons.append("size")
        if both_hashed and old_hash != new_hash:
            reasons.append("sha256")
        if not reasons and not both_hashed and mtime_differs:
            reasons.append("mtime_without_sha256")
        if reasons:
            changed.append({"path": path, "reasons": reasons})
        elif not both_hashed:
            unverified.append(path)
        elif mtime_differs:
            mtime_only.append(path)
    content_identical = not (added or removed or changed)
    identical = content_identical and not (strict_mtime and mtime_only)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "mtime_only": mtime_only,
        "unverified": unverified,
        "content_identical": content_identical,
        "strict_mtime": bool(strict_mtime),
        "exit_code": EXIT_OK if identical else EXIT_DIFFERENT,
        "summary": {
            "files_a": len(left),
            "files_b": len(right),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "mtime_only": len(mtime_only),
            "unverified": len(unverified),
        },
    }


# --------------------------------------------------------------------------------------------
# Version control metadata (read as files; no git process)
# --------------------------------------------------------------------------------------------

_HEX_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_NAME = re.compile(r"^refs/[A-Za-z0-9._/+@{}\-]{1,200}$")


def _small_text(path: str, limit: int) -> Optional[str]:
    data = _read_head(path, limit)
    return None if data is None else data.decode("utf-8", "replace").strip()


def _git_info(git_directory: str, rel: str, root_real: str) -> Dict[str, Any]:
    """Branch, HEAD commit and worktree names from a ``.git`` directory.  ``.git/config`` is never read."""
    info: Dict[str, Any] = {
        "path": rel,
        "head": "unknown",
        "branch": None,
        "head_commit": None,
        "commit_source": None,
        "worktrees": [],
    }
    search = [git_directory]
    common = _small_text(os.path.join(git_directory, "commondir"), _GIT_SMALL_FILE_BYTES)
    if common:
        candidate = os.path.normpath(os.path.join(git_directory, common))
        if _is_within(_real(candidate), root_real):
            search.append(candidate)
    head = _small_text(os.path.join(git_directory, "HEAD"), _GIT_SMALL_FILE_BYTES)
    if head is None:
        return info
    if _HEX_OBJECT.match(head):
        info.update({"head": "detached", "head_commit": head, "commit_source": "HEAD"})
    elif head.startswith("ref:"):
        ref = head[4:].strip()
        if _REF_NAME.match(ref) and ".." not in ref:
            info["head"] = "branch"
            info["branch"] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            info.update(_resolve_ref(search, ref))
    worktrees = os.path.join(search[-1], "worktrees")
    try:
        with os.scandir(worktrees) as iterator:
            info["worktrees"] = sorted(_display_name(entry.name) for entry in iterator)
    except OSError:
        info["worktrees"] = []
    return info


def _resolve_ref(search: Sequence[str], ref: str) -> Dict[str, Any]:
    for base in search:
        loose = _small_text(os.path.join(base, *ref.split("/")), _GIT_SMALL_FILE_BYTES)
        if loose is not None and _HEX_OBJECT.match(loose):
            return {"head_commit": loose, "commit_source": "loose ref"}
    for base in search:
        packed = _read_head(os.path.join(base, "packed-refs"), _GIT_PACKED_REFS_BYTES)
        if packed is None:
            continue
        for line in packed.decode("utf-8", "replace").splitlines():
            commit, _, name = line.strip().partition(" ")
            if name == ref and _HEX_OBJECT.match(commit):
                return {"head_commit": commit, "commit_source": "packed-refs"}
    return {"head_commit": None, "commit_source": None}


# --------------------------------------------------------------------------------------------
# Host / hardware
# --------------------------------------------------------------------------------------------


def _first_match(path: str, pattern: str, limit: int) -> Optional[str]:
    data = _read_head(path, limit)
    if data is None:
        return None
    match = re.search(pattern, data.decode("utf-8", "replace"), _M)
    return match.group(1).strip() if match else None


def _total_memory_bytes() -> Optional[int]:
    if sys.platform == "win32":
        if _KERNEL32 is None:
            return None
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not _KERNEL32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys)
    else:
        kilobytes = _first_match("/proc/meminfo", r"^MemTotal:\s+([0-9]+)\s+kB", 4096)
        return int(kilobytes) * 1024 if kilobytes else None


def _cpu_model() -> Optional[str]:
    if sys.platform == "win32":
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "ProcessorNameString")
        except OSError:
            return None
        return str(value).strip() or None
    else:
        return _first_match("/proc/cpuinfo", r"^model name\s*:\s*(.+)$", 65536)


def _processor_string() -> Optional[str]:
    """Processor identifier without starting a helper process (``platform.processor`` may run ``uname``)."""
    if _is_windows():
        return os.environ.get("PROCESSOR_IDENTIFIER") or None
    return _cpu_model() or platform.machine() or None


def _attempt(errors: Dict[str, str], name: str, function: Any) -> Any:
    try:
        return function()
    except Exception as exc:  # host facts are best effort; the error class is recorded instead
        errors[name] = type(exc).__name__
        return None


def _host_info(root_real: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (stable host facts, volatile facts).  The host name is only reported as a hash."""
    errors: Dict[str, str] = {}
    node = _attempt(errors, "hostname", platform.node)
    usage = _attempt(errors, "disk", lambda: shutil.disk_usage(root_real))
    host: Dict[str, Any] = {
        "platform": _attempt(errors, "platform", platform.platform),
        "system": _attempt(errors, "system", platform.system),
        "machine": _attempt(errors, "machine", platform.machine),
        "processor": _attempt(errors, "processor", _processor_string),
        "cpu_model": _attempt(errors, "cpu_model", _cpu_model),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": _attempt(errors, "memory", _total_memory_bytes),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "disk_total_bytes": usage.total if usage is not None else None,
        "hostname_sha256": hashlib.sha256(node.encode("utf-8", "replace")).hexdigest() if node else None,
        "errors": errors,
    }
    volatile = {"disk_free_bytes": usage.free if usage is not None else None}
    return host, volatile


# --------------------------------------------------------------------------------------------
# Processes and scheduled tasks (--processes): read-only OS queries, command lines never captured
# --------------------------------------------------------------------------------------------

_PROCESS_HINTS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("python", re.compile(r"^(?:python[0-9.]*|pythonw|py|ipython[0-9.]*)(?:\.exe)?$", _I)),
    (
        "metatrader_terminal",
        re.compile(r"^(?:terminal(?:64)?|metatrader\w*|mt[45]\w*|metatester(?:64)?)(?:\.exe)?$", _I),
    ),
    ("metaeditor", re.compile(r"^metaeditor(?:64)?(?:\.exe)?$", _I)),
    ("ctrader", re.compile(r"^ctrader\w*(?:\.exe)?$", _I)),
    ("interactive_brokers", re.compile(r"^(?:ibgateway\w*|tws\w*)(?:\.exe)?$", _I)),
    ("java", re.compile(r"^javaw?(?:\.exe)?$", _I)),
    ("node", re.compile(r"^node(?:js)?(?:\.exe)?$", _I)),
    ("script_host", re.compile(r"^(?:cmd|powershell|pwsh|wscript|cscript)(?:\.exe)?$", _I)),
    ("jupyter", re.compile(r"^jupyter[\w\-]*(?:\.exe)?$", _I)),
    (
        "database",
        re.compile(r"^(?:postgres|mysqld|sqlservr|redis-server|mongod|influxd|duckdb)(?:\.exe)?$", _I),
    ),
    (
        "container_runtime",
        re.compile(r"^(?:dockerd?|docker desktop|com\.docker\.\w+|containerd)(?:\.exe)?$", _I),
    ),
    ("service_wrapper", re.compile(r"^(?:nssm|winsw|srvany)\w*(?:\.exe)?$", _I)),
)
_TASK_HINT = re.compile(
    r"python|\.py\b|\.bat\b|\.cmd\b|\.ps1\b|powershell|terminal(?:64)?\.exe|metatrader|mt[45]|trader|trading|ftmo|"
    r"gold|xau|eurusd|forex|bot\b",
    _I,
)
_EXECUTABLE_END = re.compile(r"\.(?:exe|bat|cmd|com|ps1|vbs|js|py|pyw|msc|lnk)(?=\s|$)", _I)
_SCHTASKS_COLUMNS = {"task_name": 1, "next_run_time": 2, "status": 3, "last_run_time": 5, "task_to_run": 8}
_SCHTASKS_ENGLISH = {
    "task_name": "TaskName",
    "next_run_time": "Next Run Time",
    "status": "Status",
    "last_run_time": "Last Run Time",
    "task_to_run": "Task To Run",
}
_SCHTASKS_VERBOSE_COLUMNS = 28


def _process_hints(image: str) -> List[str]:
    return [name for name, pattern in _PROCESS_HINTS if pattern.match(image)]


def _redact_task_command(command: str) -> str:
    """Keep the executable path of a scheduled task action; replace every argument by ``<redacted>``."""
    text = command.strip()
    closing = text.find('"', 1) if text.startswith('"') else -1
    if closing > 0:
        executable, rest = text[1:closing], text[closing + 1 :]
    else:
        text = text.lstrip('"').strip()  # an unterminated quote does not show where the path ends
        first = text.split(None, 1)[0] if text else ""
        cut = len(first)
        if not _EXECUTABLE_END.search(first) and ("\\" in first or "/" in first):
            # an unquoted path that contains spaces: extend to the executable extension, unless
            # something that looks like a switch or a quoted argument comes first
            match = _EXECUTABLE_END.search(text)
            if match is not None and not re.search(r"\s[-/\"]", text[: match.end()]):
                cut = match.end()
        executable, rest = text[:cut], text[cut:]
    return _clean_text(executable) + (" <redacted>" if rest.strip() else "")


def _short_field(value: str) -> str:
    """A status or date field of a scheduled task; anything that looks like a command is withheld."""
    text = value.strip()
    if len(text) > 40 or any(char in text for char in '\\"=@'):
        return "<withheld>"
    return _clean_text(text)


def _decode_console(data: bytes) -> str:
    for encoding in ("oem", "mbcs") if _is_windows() else ():
        try:
            return data.decode(encoding, "replace")
        except LookupError:
            continue
    return data.decode("utf-8", "replace")


def _parse_tasklist(text: str) -> Dict[str, Any]:
    """Parse ``tasklist /FO CSV /NH``: image name, pid and memory of trading-relevant processes."""
    relevant: List[Dict[str, Any]] = []
    total = 0
    for row in csv.reader(text.splitlines()):
        if len(row) < 5 or not row[1].strip().isdigit():
            continue
        total += 1
        hints = _process_hints(row[0].strip())
        if not hints:
            continue
        digits = re.sub(r"[^0-9]", "", row[4])
        relevant.append(
            {
                "image": _clean_text(row[0].strip()),
                "pid": int(row[1]),
                "memory_kb": int(digits) if digits else None,
                "hints": hints,
            }
        )
    relevant.sort(key=lambda item: (item["image"].lower(), item["pid"]))
    return {"total": total, "relevant": relevant, "not_listed": total - len(relevant)}


def _parse_schtasks(text: str, root_real: str) -> Dict[str, Any]:
    """Parse ``schtasks /Query /FO CSV /V``; tasks below ``\\Microsoft\\`` are only counted."""
    rows = [row for row in csv.reader(text.splitlines()) if row]
    if not rows:
        return {"total": 0, "tasks": [], "microsoft_system_tasks_not_listed": 0, "column_mapping": "none"}
    header = rows[0]
    columns: Dict[str, int] = {}
    mapping = "by_name"
    for key, label in _SCHTASKS_ENGLISH.items():
        if label in header:
            columns[key] = header.index(label)
    if len(columns) != len(_SCHTASKS_ENGLISH):
        if len(header) != _SCHTASKS_VERBOSE_COLUMNS:
            return {
                "total": 0,
                "tasks": [],
                "microsoft_system_tasks_not_listed": 0,
                "column_mapping": "unrecognised",
            }
        columns, mapping = dict(_SCHTASKS_COLUMNS), "by_position"
    root_marker = _strip_extended_prefix(root_real).lower()
    seen: Set[Tuple[str, ...]] = set()
    tasks: List[Dict[str, Any]] = []
    system_tasks: Set[str] = set()
    names: Set[str] = set()
    for row in rows:
        if row == header or len(row) <= max(columns.values()):
            continue
        name = row[columns["task_name"]].strip()
        names.add(name)
        if name.lower().startswith("\\microsoft\\"):
            system_tasks.add(name)
            continue
        if not name.startswith("\\"):
            # every task path starts with a backslash; anything else means the columns are not the
            # expected ones, and then no field can be trusted to be what its label says
            return {
                "total": 0,
                "tasks": [],
                "microsoft_system_tasks_not_listed": 0,
                "column_mapping": "unrecognised",
            }
        command = row[columns["task_to_run"]]
        task: Dict[str, Any] = {
            "task_name": _clean_text(name),
            "status": _short_field(row[columns["status"]]),
            "next_run_time": _short_field(row[columns["next_run_time"]]),
            "last_run_time": _short_field(row[columns["last_run_time"]]),
            "task_to_run": _redact_task_command(command),
            "mentions_root": root_marker in " ".join(row).lower(),
            "trading_hint": bool(_TASK_HINT.search(name) or _TASK_HINT.search(command)),
        }
        identity = tuple(str(task[field_name]) for field_name in sorted(task))
        if identity not in seen:
            seen.add(identity)
            tasks.append(task)
    tasks.sort(key=lambda item: (item["task_name"].lower(), item["task_to_run"], item["next_run_time"]))
    return {
        "total": len(names),
        "tasks": tasks,
        "microsoft_system_tasks_not_listed": len(system_tasks),
        "column_mapping": mapping,
    }


def _windows_system_tool(name: str, root_real: str) -> str:
    """Absolute path of a tool in the Windows system directory (never resolved through PATH or the root)."""
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:\\Windows"
    path = os.path.join(system_root, "System32", name)
    if not os.path.isfile(path) or _is_within(_real(path), root_real):
        raise FileNotFoundError(name)
    return path


def _query_status(completed: subprocess.CompletedProcess[bytes]) -> Dict[str, Any]:
    if completed.returncode != 0:
        return {"status": "error", "error": "ExitCode" + str(completed.returncode)}
    return {"status": "ok", "error": None}


def _collect_processes_windows(root_real: str, guard: _Guard) -> Dict[str, Any]:
    processes: Dict[str, Any] = {"status": "error", "error": None, "query": "tasklist /FO CSV /NH"}
    tasks: Dict[str, Any] = {"status": "error", "error": None, "query": "schtasks /Query /FO CSV /V"}
    try:
        tasklist = _windows_system_tool("tasklist.exe", root_real)
        guard.allowed_commands.add(subprocess.list2cmdline((tasklist, "/FO", "CSV", "/NH")))
        completed = subprocess.run(
            [tasklist, "/FO", "CSV", "/NH"],
            shell=False,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_TASKLIST_TIMEOUT_SECONDS,
            cwd=os.path.dirname(tasklist),
        )
        processes.update(_query_status(completed))
        if completed.returncode == 0:
            processes.update(_parse_tasklist(_decode_console(completed.stdout)))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        processes["error"] = type(exc).__name__
    try:
        schtasks = _windows_system_tool("schtasks.exe", root_real)
        guard.allowed_commands.add(subprocess.list2cmdline((schtasks, "/Query", "/FO", "CSV", "/V")))
        completed = subprocess.run(
            [schtasks, "/Query", "/FO", "CSV", "/V"],
            shell=False,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_SCHTASKS_TIMEOUT_SECONDS,
            cwd=os.path.dirname(schtasks),
        )
        tasks.update(_query_status(completed))
        if completed.returncode == 0:
            tasks.update(_parse_schtasks(_decode_console(completed.stdout), root_real))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        tasks["error"] = type(exc).__name__
    return {"processes": processes, "scheduled_tasks": tasks}


def _collect_processes_proc(proc_root: str) -> Dict[str, Any]:
    """Linux: image name, pid and resident memory from /proc.  Command lines are not read."""
    processes: Dict[str, Any] = {"status": "error", "error": None, "query": "scan of " + proc_root}
    try:
        with os.scandir(proc_root) as iterator:
            pids = sorted(int(entry.name) for entry in iterator if entry.name.isdigit())
    except OSError as exc:
        processes["error"] = type(exc).__name__
        return {
            "processes": processes,
            "scheduled_tasks": {"status": "not_collected", "error": None, "query": None},
        }
    relevant: List[Dict[str, Any]] = []
    total = 0
    for pid in pids:
        image = _small_text(os.path.join(proc_root, str(pid), "comm"), 256)
        if image is None:
            continue  # the process ended between listing and reading
        total += 1
        hints = _process_hints(image)
        if not hints:
            continue
        resident = _first_match(os.path.join(proc_root, str(pid), "status"), r"^VmRSS:\s+([0-9]+)\s+kB", 8192)
        relevant.append(
            {
                "image": _clean_text(image),
                "pid": pid,
                "memory_kb": int(resident) if resident else None,
                "hints": hints,
            }
        )
    relevant.sort(key=lambda item: (item["image"].lower(), item["pid"]))
    processes.update(
        {"status": "ok", "total": total, "relevant": relevant, "not_listed": total - len(relevant)}
    )
    return {
        "processes": processes,
        "scheduled_tasks": {"status": "not_collected", "error": None, "query": None},
    }


def _collect_processes(root_real: str, guard: _Guard) -> Dict[str, Any]:
    if _is_windows():
        return _collect_processes_windows(root_real, guard)
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        return _collect_processes_proc("/proc")
    unsupported = {"status": "not_collected", "error": "UnsupportedPlatform", "query": None}
    return {"processes": dict(unsupported), "scheduled_tasks": dict(unsupported)}


# --------------------------------------------------------------------------------------------
# Inventory assembly
# --------------------------------------------------------------------------------------------


class _Capper:
    """Caps long lists and records every cap that was applied, so nothing is dropped silently."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.truncated: Dict[str, Dict[str, int]] = {}

    def cap(self, name: str, items: List[Any]) -> List[Any]:
        if len(items) > self.limit:
            self.truncated[name] = {"total": len(items), "listed": self.limit}
            return items[: self.limit]
        return items


def _by_path(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items, key=lambda item: (str(item["path"]), str(item.get("kind", "")), str(item.get("rule", "")))
    )


def _locations(collector: _Collector, rule: str, capper: _Capper, section: str) -> List[Dict[str, Any]]:
    listed = [{"path": path, "line": line} for path, line in sorted(collector.hits.get(rule, []))]
    return capper.cap(section + "." + rule, listed)


def _rule_totals(collector: _Collector, rules: Sequence[_Rule]) -> Dict[str, Dict[str, int]]:
    totals: Dict[str, Dict[str, int]] = {}
    for rule in rules:
        hits, files = collector.hit_totals.get(rule.name, [0, 0])
        totals[rule.name] = {"lines": hits, "files": files}
    return totals


def _build_inventory(
    collector: _Collector,
    report: _WalkReport,
    options: _Options,
    root_real: str,
    host: Dict[str, Any],
) -> Dict[str, Any]:
    capper = _Capper(options.max_list_entries)
    for rel, name in report.directories:
        collector.add_directory(rel, name)
    largest = [
        {"path": path, "size": size} for size, path in sorted(collector.sizes, key=lambda x: (-x[0], x[1]))
    ]

    languages: Dict[str, Any] = {}
    for name, slot in sorted(collector.languages.items()):
        languages[name] = {
            "files": slot["files"],
            "bytes": slot["bytes"],
            "lines": slot["lines"],
            "files_with_line_count": slot["files_with_line_count"],
            "extensions": sorted(slot["extensions"]),
        }

    test_directories = sorted(
        rel for rel, name in report.directories if name.lower() in _TEST_DIRECTORY_NAMES
    )
    test_files = [
        {"path": path, "test_functions": count} for path, count in sorted(collector.test_files.items())
    ]
    data_files = _by_path(collector.data_files)
    by_size = sorted(data_files, key=lambda item: (-int(item["size"]), str(item["path"])))
    data_largest = [
        {"path": item["path"], "size": item["size"], "mtime_utc": item["mtime_utc"]}
        for item in by_size[:_LARGEST_FILES]
    ]

    credential_rules = CREDENTIAL_RULES + CREDENTIAL_CONFIG_RULES
    findings: List[Dict[str, Any]] = []
    for rule in credential_rules:
        findings.extend(
            {"path": path, "line": line, "rule": rule.name}
            for path, line in collector.hits.get(rule.name, [])
        )
    findings.sort(key=lambda item: (item["path"], item["line"], item["rule"]))

    repositories = [
        _git_info(path, rel, root_real) for path, rel in sorted(report.git_directories, key=lambda x: x[1])
    ]
    root_repository = next((item for item in repositories if item["path"] == ".git"), None)

    environments = _by_path(report.environments)
    for item in collector.version_files:
        if item["kind"] == "pyvenv.cfg":
            parent = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "."
            environments.append(
                {
                    "path": parent,
                    "kind": "venv",
                    "detected_by": "pyvenv.cfg",
                    "python_versions": item["python_versions"],
                }
            )

    unreadable = _by_path(report.unreadable + collector.unreadable)
    inventory: Dict[str, Any] = {
        "host": host,
        "root_summary": {
            "files": collector.files,
            "total_bytes": collector.total_bytes,
            "directories": len(report.directories),
            "by_extension": _counts(collector.by_extension),
            "by_top_level_directory": _counts(collector.by_top_level),
            "largest_files": largest[:_LARGEST_FILES],
        },
        "languages": languages,
        "tests": {
            "test_files": capper.cap("tests.test_files", test_files),
            "test_file_count": len(test_files),
            "test_function_count": collector.test_functions,
            "test_directories": capper.cap("tests.test_directories", test_directories),
            "conftest_files": sorted(collector.conftest),
            "config_files": _by_path(collector.test_config),
            "unittest_usage": _locations(collector, "unittest_usage", capper, "tests"),
            "pytest_usage": _locations(collector, "pytest_usage", capper, "tests"),
            "other_language_test_files": capper.cap(
                "tests.other_language_test_files", sorted(collector.other_tests)
            ),
        },
        "data": {
            "file_count": len(data_files),
            "total_bytes": sum(int(item["size"]) for item in data_files),
            "by_extension": _counts(collector.data_by_extension),
            "files": capper.cap("data.files", data_files),
            "largest_files": data_largest,
        },
        "models": {
            "file_count": len(collector.models),
            "files": capper.cap("models.files", _by_path(collector.models)),
        },
        "broker_interfaces": {
            "rules": {
                rule.name: _locations(collector, rule.name, capper, "broker_interfaces")
                for rule in BROKER_RULES
            },
            "rule_totals": _rule_totals(collector, BROKER_RULES),
            "mql_files": _counts(collector.mql_files),
            "terminal_hints": capper.cap(
                "broker_interfaces.terminal_hints", _by_path(collector.terminal_hints)
            ),
        },
        "process_entry_points": {
            "python_main_guard": _locations(collector, "python_main_guard", capper, "process_entry_points"),
            "launchers": capper.cap("process_entry_points.launchers", _by_path(collector.launchers)),
            "service_and_task_definitions": sorted(
                [
                    {"path": path, "line": line, "rule": rule.name}
                    for rule in XML_RULES
                    for path, line in collector.hits.get(rule.name, [])
                ],
                key=lambda item: (item["path"], item["line"], item["rule"]),
            ),
            "containers_and_process_managers": _by_path(collector.containers),
        },
        "dependency_manifests": {
            "parsed": _by_path(collector.manifests),
            "listed_only": sorted(collector.listed_only_manifests),
        },
        "python_environments": {
            "environments": _by_path(environments),
            "version_files": _by_path(
                item for item in collector.version_files if item["kind"] != "pyvenv.cfg"
            ),
        },
        "version_control": {
            "has_git_directory": root_repository is not None,
            "branch": root_repository["branch"] if root_repository else None,
            "head": root_repository["head"] if root_repository else None,
            "head_commit": root_repository["head_commit"] if root_repository else None,
            "worktrees": root_repository["worktrees"] if root_repository else [],
            "repositories": repositories,
            "git_pointer_files": sorted(collector.gitfiles),
            "has_gitignore": ".gitignore" in collector.gitignore,
            "gitignore_files": sorted(collector.gitignore),
            "other_vcs_directories": sorted(report.other_vcs),
            "dirty_state": "not determined (would require running git)",
        },
        "notebooks": {
            "count": len(collector.notebooks),
            "paths": capper.cap("notebooks.paths", sorted(collector.notebooks)),
        },
        "engine_baseline_hints": {
            "hints": list(ENGINE_HINTS),
            "by_hint": {
                hint: sum(1 for item in collector.hints if hint in item["hints"]) for hint in ENGINE_HINTS
            },
            "paths": capper.cap("engine_baseline_hints.paths", _by_path(collector.hints)),
        },
        "credential_scan": {
            "note": "locations only (path, line, rule); matched text is never recorded",
            "findings": capper.cap("credential_scan.findings", findings),
            "rule_totals": _rule_totals(collector, credential_rules),
        },
    }
    inventory["exclusions_and_caps"] = {
        "excluded_directory_names": sorted(options.excluded),
        "python_environment_autodetect": options.detect_environments,
        "excluded_directories": _by_path(report.excluded),
        "links_not_followed": capper.cap("exclusions_and_caps.links_not_followed", _by_path(report.links)),
        "special_files_not_opened": _by_path(report.special),
        "offline_placeholders_not_read": sorted(collector.offline),
        "unreadable": capper.cap("exclusions_and_caps.unreadable", unreadable),
        "unreadable_count": len(unreadable),
        "changed_while_read": sorted(collector.changed),
        "sensitive_files": capper.cap("exclusions_and_caps.sensitive_files", _by_path(collector.sensitive)),
        "sensitive_file_count": len(collector.sensitive),
        "sensitive_files_hashed": options.hash_sensitive,
        "caps": {
            "max_hash_bytes": options.max_hash_bytes,
            "max_line_count_bytes": options.max_line_count_bytes,
            "max_scan_bytes": options.max_scan_bytes,
            "config_file_scan_bytes": min(options.max_scan_bytes, _CONFIG_SCAN_BYTES),
            "max_list_entries": options.max_list_entries,
            "max_hits_per_file": options.max_hits_per_file,
            "dependency_manifest_parse_bytes": _MANIFEST_PARSE_BYTES,
            "header_max_chars": _HEADER_MAX_CHARS,
        },
        "not_hashed_over_max_hash_bytes": capper.cap(
            "exclusions_and_caps.not_hashed", sorted(collector.not_hashed)
        ),
        "line_count_skipped_over_cap": capper.cap(
            "exclusions_and_caps.line_count", sorted(collector.line_cap)
        ),
        "scan_truncated_at_max_scan_bytes": capper.cap(
            "exclusions_and_caps.scan_truncated", sorted(collector.scan_truncated)
        ),
        "scan_skipped_binary_content": capper.cap(
            "exclusions_and_caps.scan_binary", sorted(collector.scan_binary)
        ),
        "hit_lists_capped_per_file": _by_path(collector.hit_caps),
        "truncated_lists": capper.truncated,
    }
    return inventory


def _json_bytes(document: Any) -> bytes:
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8", "backslashreplace")


# --------------------------------------------------------------------------------------------
# Sub-commands
# --------------------------------------------------------------------------------------------


def _refuse_if_tool_inside(root_real: str) -> None:
    """Nothing of this tool (or of the modules it loaded) may live inside the audited tree."""
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if isinstance(location, str) and _is_within(_real(location), root_real):
            raise AuditRefused(
                "Python module '" + name + "' was loaded from inside --root; copy the audit tool outside "
                "the audited tree and run it from there"
            )
    if _is_within(_real(__file__), root_real):
        raise AuditRefused("the audit tool must not be located inside --root")


def _check_separate(root_real: str, target_real: str, what: str) -> None:
    if _is_within(target_real, root_real):
        raise AuditRefused(what + " must not be inside --root (the audited tree is never written to)")
    if _is_within(root_real, target_real):
        raise AuditRefused("--root must not be inside " + what)


def _non_negative(args: argparse.Namespace, names: Sequence[str]) -> None:
    for name in names:
        value = getattr(args, name, None)
        if value is not None and value < 0:
            raise AuditRefused("--" + name.replace("_", "-") + " must not be negative")


def _options_from_args(args: argparse.Namespace) -> _Options:
    excluded: Dict[str, str] = {}
    if not args.no_default_excludes:
        excluded.update(_excluded_names(DEFAULT_EXCLUDED_DIRS, "default excluded directory name"))
    excluded.update(_excluded_names(args.exclude_dir or (), "--exclude-dir"))
    return _Options(
        excluded=excluded,
        detect_environments=not args.include_venvs,
        hash_sensitive=args.hash_sensitive,
        max_hash_bytes=args.max_hash_bytes,
        progress_every=args.progress_every,
    )


def _prepare_output_directory(out_real: str) -> None:
    if os.path.lexists(out_real):
        if not os.path.isdir(out_real):
            raise AuditRefused("--out exists and is not a directory")
        with os.scandir(out_real) as iterator:
            if next(iterator, None) is not None:
                raise AuditRefused("--out exists and is not empty; choose a new folder")
    else:
        try:
            os.makedirs(out_real)
        except OSError as exc:
            raise AuditRefused("--out cannot be created (" + type(exc).__name__ + ")") from None


def _tool_block(argv: Sequence[str]) -> Dict[str, Any]:
    return {"name": TOOL_NAME, "version": TOOL_VERSION, "schema_version": SCHEMA_VERSION, "argv": list(argv)}


def _command_audit(args: argparse.Namespace, argv: Sequence[str]) -> int:
    _non_negative(
        args,
        (
            "max_hash_bytes",
            "max_line_count_bytes",
            "max_scan_bytes",
            "max_list_entries",
            "max_hits_per_file",
            "progress_every",
        ),
    )
    root_real = _resolve_root(args.root)
    out_real = _real(args.out)
    _check_separate(root_real, out_real, "--out")
    _refuse_if_tool_inside(root_real)
    options = _options_from_args(args)
    options.count_rows = args.count_rows
    options.csv_headers = not args.no_csv_headers
    options.max_line_count_bytes = args.max_line_count_bytes
    options.max_scan_bytes = args.max_scan_bytes
    options.max_list_entries = args.max_list_entries
    options.max_hits_per_file = args.max_hits_per_file

    started = time.time()
    host, volatile = _host_info(root_real)
    _prepare_output_directory(out_real)
    written: List[str] = []

    def write(name: str, chunks: Iterable[bytes]) -> None:
        path = os.path.join(out_real, name)
        with _create_new_file(path, root_real) as handle:
            for chunk in chunks:
                handle.write(chunk)
        written.append(path)

    report = _WalkReport()
    collector = _Collector(options)
    with _guarded(root_real, args.root) as guard:
        records = _scan_tree(_scan_root(root_real), options, report, collector)
        inventory = _build_inventory(collector, report, options, root_real, host)
        processes = _collect_processes(root_real, guard) if args.processes else None
        finished = time.time()
        run = {
            "started_utc": _iso_now(started),
            "finished_utc": _iso_now(finished),
            "duration_seconds": round(finished - started, 3),
            "root": args.root,
            "out": args.out,
            "disk_free_bytes": volatile["disk_free_bytes"],
            "note": "this block and tool.argv differ between runs; "
            "everything else is identical for an unchanged tree",
        }
        document: Dict[str, Any] = {"tool": _tool_block(argv), "run": run}
        document.update(inventory)
        write("manifest.jsonl", (_manifest_line(record) for record in records))
        write("inventory.json", [_json_bytes(document)])
        if processes is not None:
            processes_document: Dict[str, Any] = {
                "tool": _tool_block(argv),
                "collected_utc": _iso_now(finished),
                "platform": sys.platform,
                "note": "snapshot at collection time; command lines are never captured, "
                "scheduled task arguments are replaced by <redacted>",
            }
            processes_document.update(processes)
            write("processes.json", [_json_bytes(processes_document)])
        writes_inside_root = [path for path in written if _is_within(_real(path), root_real)]
        read_only = not guard.write_violations and not writes_inside_root
        clean = read_only and not guard.other_violations
        hashed = sum(1 for record in records if record["sha256"] is not None)
        unreadable_count = len(report.unreadable) + len(collector.unreadable)
        location_count = sum(len(lines) for lines in collector.hits.values())
        output_names = sorted([*(os.path.basename(path) for path in written), "audit_log.txt"])
        log = [
            f"{TOOL_NAME} {TOOL_VERSION} (schema {SCHEMA_VERSION})",
            f"started {run['started_utc']}, finished {run['finished_utc']}",
            "root: " + _clean_text(str(args.root)),
            "out:  " + _clean_text(str(args.out)),
            f"files listed in manifest.jsonl: {len(records)} ({collector.total_bytes} bytes)",
            f"files hashed (SHA-256): {hashed}; not hashed: {len(records) - hashed}",
            f"manifest files opened read-only (mode 'rb'): {collector.files_opened}; "
            f"bytes read: {collector.bytes_read}",
            f"directories walked: {len(report.directories) + 1}; "
            f"excluded directories (counted, not read): {len(report.excluded)}",
            f"links recorded and not followed: {len(report.links)}; "
            f"special files not opened: {len(report.special)}",
            f"sensitive-named files: {len(collector.sensitive)} "
            + ("(hashed on request)" if options.hash_sensitive else "(not opened)"),
            f"unreadable paths: {unreadable_count}; "
            f"files that changed while being read: {len(collector.changed)}",
            f"keyword locations recorded: {location_count} (matched text is never recorded)",
            "processes.json: " + ("written" if processes is not None else "not requested"),
            "network: not used; git: not started; file content: not emitted",
            "runtime guard: " + ("active" if guard.installed else "not available on this Python"),
            f"opens under root seen by the interpreter (includes .git and environment metadata): "
            f"{guard.read_opens} read, {len(guard.write_violations)} write; "
            f"other blocked events: {len(guard.other_violations)}",
            "output files: " + ", ".join(output_names),
        ]
        if read_only:
            log.append(READ_ONLY_OK_LINE)
        else:
            log.append("READ-ONLY CHECK FAILED: " + ", ".join(guard.write_violations + writes_inside_root))
        write("audit_log.txt", [("\n".join(log) + "\n").encode("utf-8", "backslashreplace")])
    print(f"[{TOOL_NAME}] done: {len(records)} files, output in {args.out}", file=sys.stderr)
    return EXIT_OK if clean else EXIT_GUARD_VIOLATION


def _command_manifest(args: argparse.Namespace) -> int:
    _non_negative(args, ("max_hash_bytes", "progress_every"))
    root_real = _resolve_root(args.root)
    out_real = _real(args.out)
    _check_separate(root_real, out_real, "--out")
    _refuse_if_tool_inside(root_real)
    if os.path.lexists(out_real):
        raise AuditRefused("--out already exists; a manifest file is never overwritten")
    options = _options_from_args(args)
    options.includes = tuple(args.include) if args.include else None
    with _guarded(root_real, args.root) as guard:
        records = _scan_tree(_scan_root(root_real), options, _WalkReport(), None)
        parent = os.path.dirname(out_real)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        _write_manifest_file(records, out_real, root_real)
        clean = not guard.write_violations and not guard.other_violations
    print(f"[{TOOL_NAME}] manifest: {len(records)} files written to {args.out}", file=sys.stderr)
    return EXIT_OK if clean else EXIT_GUARD_VIOLATION


def _command_compare(args: argparse.Namespace) -> int:
    try:
        result = compare_manifests(args.a, args.b, strict_mtime=args.strict_mtime)
    except (OSError, ValueError) as exc:
        raise AuditRefused(f"cannot read manifest ({type(exc).__name__}): {exc}") from None
    sys.stdout.write(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n")
    return int(result["exit_code"])


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="tree to read (never modified)")
    parser.add_argument(
        "--hash-sensitive", action="store_true", help="also hash files whose name suggests secrets"
    )
    parser.add_argument("--max-hash-bytes", type=int, default=None, help="do not hash files larger than this")
    parser.add_argument(
        "--exclude-dir", action="append", metavar="NAME", help="additional directory name to skip"
    )
    parser.add_argument(
        "--no-default-excludes", action="store_true", help="do not skip the default directory names"
    )
    parser.add_argument(
        "--include-venvs", action="store_true", help="descend into detected Python environments"
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=_DEFAULT_PROGRESS_EVERY,
        metavar="N",
        help="progress line every N files",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Read-only baseline audit: inventory and SHA-256 manifest of an existing tree. "
        "Nothing under --root is modified, no network is used, no file content or credential is emitted.",
    )
    parser.add_argument("--version", action="version", version=TOOL_NAME + " " + TOOL_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="write inventory.json, manifest.jsonl and audit_log.txt")
    _add_scan_arguments(audit)
    audit.add_argument("--out", required=True, help="new or empty folder outside --root")
    audit.add_argument(
        "--processes", action="store_true", help="also write processes.json (read-only OS queries)"
    )
    audit.add_argument("--count-rows", action="store_true", help="count the rows of .csv/.tsv files")
    audit.add_argument(
        "--no-csv-headers", action="store_true", help="do not report column names of .csv/.tsv"
    )
    audit.add_argument("--max-line-count-bytes", type=int, default=_DEFAULT_MAX_LINE_COUNT_BYTES)
    audit.add_argument("--max-scan-bytes", type=int, default=_DEFAULT_MAX_SCAN_BYTES)
    audit.add_argument("--max-list-entries", type=int, default=_DEFAULT_MAX_LIST_ENTRIES)
    audit.add_argument("--max-hits-per-file", type=int, default=_DEFAULT_MAX_HITS_PER_FILE)

    manifest = commands.add_parser("manifest", help="write only a SHA-256 manifest (JSON lines)")
    _add_scan_arguments(manifest)
    manifest.add_argument("--out", required=True, help="new manifest file outside --root")
    manifest.add_argument(
        "--include", action="append", metavar="GLOB", help="only paths matching this pattern"
    )

    compare = commands.add_parser("compare", help="compare two manifests; exit 0 when content is identical")
    compare.add_argument("a", help="first manifest (before)")
    compare.add_argument("b", help="second manifest (after)")
    compare.add_argument(
        "--strict-mtime", action="store_true", help="also fail on modification-time differences"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.  Returns the exit code and never calls ``sys.exit`` itself."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:  # argparse reports usage errors (2) and --help/--version (0) this way
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    try:
        if args.command == "audit":
            return _command_audit(args, arguments)
        if args.command == "manifest":
            return _command_manifest(args)
        return _command_compare(args)
    except AuditRefused as exc:
        print(f"{TOOL_NAME}: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
