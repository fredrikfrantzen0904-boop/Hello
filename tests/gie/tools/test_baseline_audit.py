"""Tests for the read-only baseline audit tool ``tools/audit/gie_baseline_audit.py``.

Requirements: S00-ASG-01 .. S00-ASG-07 (audit and inventory of the existing system without
modifying production) and M-AUTH-15 (credentials are not exposed).  The read-only tests are the
acceptance tests of S00-ACC-06 "no production changes".

The expected behaviour is taken from the tool's documented contract (module docstring and
``tools/audit/README.md``), not from its implementation:

* the audited tree is byte-identical afterwards, with unchanged metadata, and nothing is written
  outside ``--out``;
* no file content is emitted except package names / version specifiers, column names and Python
  version strings; planted fake secrets appear in no output byte;
* keyword scans report locations (path, line, rule) only;
* links are never followed, excluded directories are summarised, every cap is recorded;
* ``manifest`` / ``compare`` detect added, removed and changed files.

SYNTHETIC FIXTURE: every file, directory, "credential", account number, host name, process list
and scheduled task in this module is invented for the test.  The tree built by `build_tree` is not
a trading system and nothing in it is executed; all secret-looking values contain the marker
``FAKE-NOT-A-SECRET``.  Nothing here is market data or evidence of performance.

Not testable on Linux (reported in the session summary, exercised only through static checks and
through fakes): the Windows-only branches - shared-read ``CreateFileW`` opener, extended-length
paths, junction detection on a real NTFS volume, ``GlobalMemoryStatusEx``, the registry lookup of
the CPU name and the real ``tasklist`` / ``schtasks`` processes.
"""

from __future__ import annotations

import ast
import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.audit import gie_baseline_audit as audit

requirement = pytest.mark.requirement(
    "S00-ASG-01",
    "S00-ASG-02",
    "S00-ASG-03",
    "S00-ASG-04",
    "S00-ASG-05",
    "S00-ASG-06",
    "S00-ASG-07",
    "M-AUTH-15",
)
read_only = pytest.mark.acceptance("S00-ACC-06")

TOOL_PATH = Path(audit.__file__).resolve()
TOOL_SOURCE = TOOL_PATH.read_text(encoding="utf-8")
TOOL_AST = ast.parse(TOOL_SOURCE)

# ---------------------------------------------------------------------------------------------
# SYNTHETIC FIXTURE values.  None of these is a real credential, account, host or market value.
# ---------------------------------------------------------------------------------------------

MARKER = "FAKE-NOT-A-SECRET"
FAKE_ENV_SECRET = "FAKE-NOT-A-SECRET-env-7c1d"
FAKE_CONFIG_SECRET = "FAKE-NOT-A-SECRET-config-52aa"
FAKE_INDEX_SECRET = "FAKE-NOT-A-SECRET-index-9e0b"
FAKE_DB_SECRET = "FAKE-NOT-A-SECRET-db-3f44"
FAKE_MT5_SECRET = "FAKE-NOT-A-SECRET-mt5-b6d2"
FAKE_MQ5_SECRET = "FAKE-NOT-A-SECRET-mq5-0c0c"
FAKE_NOTEBOOK_SECRET = "FAKE-NOT-A-SECRET-notebook-aa01"
FAKE_KEY_BODY = "FAKE-NOT-A-SECRET-keybody-1a2b"
FAKE_TASK_ARGUMENT = "FAKE-NOT-A-SECRET-taskarg-e1e1"
ALL_FAKE_SECRETS = (
    FAKE_ENV_SECRET,
    FAKE_CONFIG_SECRET,
    FAKE_INDEX_SECRET,
    FAKE_DB_SECRET,
    FAKE_MT5_SECRET,
    FAKE_NOTEBOOK_SECRET,
    FAKE_KEY_BODY,
)
FAKE_ACCOUNT = "99000111222333"  # fake account number, deliberately longer than any real login
FAKE_ROW_VALUE = "FAKEROWVALUE-must-not-be-emitted-4242"
FAKE_HOSTNAME = "FAKE-HOSTNAME-gie-fixture"
FAKE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FAKE_DETACHED_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
FAKE_PACKED_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
# A multiple of 100 ns: the finest modification time a Windows file system stores.  A value with
# single nanoseconds would be truncated there, and every comparison with it would fail.
FIXED_MTIME_NS = 1_700_000_000_123_456_700

RUN_BOT = '''"""SYNTHETIC FIXTURE - not a trading program; it is never imported or executed."""
import MetaTrader5 as mt5

from config import PASSWORD


def connect():
    mt5.initialize()
    return mt5.login(99000111222333, password="FAKE-NOT-A-SECRET-mt5-b6d2", server="Fake-Server")


def send(request):
    tick = mt5.symbol_info_tick("XAUUSD")
    rates = mt5.copy_rates_from_pos("XAUUSD", 1, 0, 10)
    ticks = mt5.copy_ticks_from("XAUUSD", 0, 10, 0)
    check = mt5.order_check(request)
    result = mt5.order_send(request)
    return tick, rates, ticks, check, result, mt5.positions_get(), PASSWORD


if __name__ == "__main__":
    connect()
'''

CONFIG = """# SYNTHETIC FIXTURE - fake values only
LOGIN = 99000111222333
PASSWORD = "FAKE-NOT-A-SECRET-config-52aa"
SERVER = "Fake-Server"
EMPTY_PASSWORD = ""
DATABASE_URL = "postgresql://fakeuser:FAKE-NOT-A-SECRET-db-3f44@db.invalid:5432/fake"
"""

SILVER_BULLET = """# SYNTHETIC FIXTURE
import zmq
import requests
import smtplib
import websocket

TELEGRAM_URL = "api.telegram.org"


def notify(text):
    requests.post(TELEGRAM_URL, json={"text": text})
    return "discord"
"""

ORB_VWAP = """# SYNTHETIC FIXTURE
import socket

import ccxt
from ib_insync import IB

BRIDGES = ("oandapyV20", "ctrader_open_api", "DWX_ZeroMQ_Connector", "quickfix")


def open_socket():
    return socket.socket()
"""

EXPERT_MQ5 = """//+------------------------------------------------------------------+
//| SYNTHETIC FIXTURE - not a real expert advisor                    |
//+------------------------------------------------------------------+
#include <Trade\\Trade.mqh>
CTrade trade;
input string InpPassword = "FAKE-NOT-A-SECRET-mq5-0c0c";
void OnTick()
  {
   MqlTradeRequest request; MqlTradeResult result;
   if(!OrderSend(request, result)) Print("failed");
  }
"""

TEST_ENGINE = """# SYNTHETIC FIXTURE
import pytest


def test_one():
    assert True


def test_two():
    assert True


@pytest.mark.parametrize("value", [1])
async def test_three(value):
    assert value


def helper_not_a_test():
    return 1
"""

LEGACY_TEST = """# SYNTHETIC FIXTURE
import unittest


class LegacyCase(unittest.TestCase):
    def test_legacy(self):
        self.assertTrue(True)
"""

REQUIREMENTS = """# SYNTHETIC FIXTURE
numpy==1.26.4
pandas>=2.0,<3
MetaTrader5==5.0.45
pyzmq
requests[socks]>=2.31 ; python_version >= "3.9"
tokenizers==0.15.0
--extra-index-url https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@pypi.invalid/simple
private-lib @ https://files.invalid/private_lib-1.0.tar.gz
-r dev-requirements.txt
this is not a requirement line
"""

PYPROJECT = """# SYNTHETIC FIXTURE
[project]
name = "fake-bot"
dependencies = [
    "lightgbm>=4.0",   # a comment with a ] bracket
    "scikit-learn==1.4.2",
    "internal-sdk @ git+https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@git.invalid/sdk.git",
]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff"]

[build-system]
requires = ["setuptools>=68"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.poetry.dependencies]
python = "^3.11"
ccxt = {version = "^4.2", extras = ["async"]}
"""

SETUP_CFG = """# SYNTHETIC FIXTURE
[metadata]
name = fake-bot

[options]
install_requires =
    httpx>=0.27
    websockets

[options.extras_require]
notify =
    python-telegram-bot==21.0

[tool:pytest]
testpaths = tests
"""

ENVIRONMENT_YML = """# SYNTHETIC FIXTURE
name: fake-env
channels:
  - https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@conda.invalid/channel
  - conda-forge
dependencies:
  - python=3.11.9
  - numpy=1.26.4=py311h64a7726_0
  - ta-lib
  - pip:
      - ib_insync==0.9.86
      - git+https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@git.invalid/x.git
"""

PACKAGE_JSON = json.dumps(
    {
        "name": "fake-dashboard",
        "scripts": {"start": "node server.js --token FAKE-NOT-A-SECRET-index-9e0b"},
        "dependencies": {
            "express": "^4.19.2",
            "@types/node": "20.11.0",
            "private-sdk": "git+https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@git.invalid/sdk.git",
        },
        "devDependencies": {"ws": "~8.17.0"},
    },
    indent=2,
)

PIPFILE = """# SYNTHETIC FIXTURE
[[source]]
url = "https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@pypi.invalid/simple"
verify_ssl = true
name = "private"

[packages]
flask = "==3.0.3"
oandapyV20 = "*"
private-pkg = {git = "https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@git.invalid/p.git"}

[dev-packages]
pytest = ">=8"
"""

NOTEBOOK = json.dumps(
    {
        "cells": [
            {
                "cell_type": "code",
                "source": [
                    "import MetaTrader5 as mt5\n",
                    "mt5.initialize()\n",
                    'api_key = "FAKE-NOT-A-SECRET-notebook-aa01"\n',
                ],
            }
        ],
        "metadata": {"note": "SYNTHETIC FIXTURE"},
        "nbformat": 4,
        "nbformat_minor": 5,
    },
    indent=1,
)

TASK_XML = """<?xml version="1.0"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <!-- SYNTHETIC FIXTURE -->
  <Actions><Exec>
    <Command>python.exe</Command><Arguments>run_bot.py --password FAKE-NOT-A-SECRET-taskxml</Arguments>
  </Exec></Actions>
</Task>
"""

SERVICE_XML = """<service>
  <!-- SYNTHETIC FIXTURE -->
  <id>goldbot</id>
  <executable>python.exe</executable>
</service>
"""

TICKS_CSV = "time,bid,ask,flags\n" + "".join(
    f"2024-01-02 00:00:0{index},2063.1{index},2063.4{index},{FAKE_ROW_VALUE}\n" for index in range(5)
)
# U+0A05 followed by U+0100 is 05 0A 00 01 in UTF-16-LE: the bytes of a line feed (0A 00), but not on
# a character boundary.  Such a pair must not be counted as a line.
UTF16_CSV = (
    "\ufeffTime,Bid,Ask\r\n"
    + "".join(f"0{index}:00,1.1,\u0a05\u0100{FAKE_ROW_VALUE}\r\n" for index in range(3))
).encode("utf-16-le")
BIG_UTF16_ROWS = 700
BIG_UTF16_CSV = (
    "\ufeffTime,Bid,Ask\r\n"
    + "".join(f"2024.01.02 00:{i // 60:02d}:{i % 60:02d},1.1,1.2\r\n" for i in range(BIG_UTF16_ROWS))
).encode("utf-16-le")
BIG_MODULE = (
    "# SYNTHETIC FIXTURE\n"
    + "".join(f"VALUE_{index:04d} = {index}\n" for index in range(700))
    + 'LATE_BRIDGE = "ccxt"\n'
)
NON_UTF8_NAME = b"caf\xe9_notes.csv"
NON_UTF8_DISPLAY = "caf\\xe9_notes.csv"
NON_UTF8_CONTENT = b"time,bid\n1,2\n"
#: A file whose name is not valid UTF-8 can exist on POSIX file systems only: Windows file names
#: are UTF-16, so the fixture cannot contain such a file there and nothing about it can be asserted.
HAS_BYTE_NAMES = os.name != "nt"
#: display name -> content of the fixture files that exist only where byte names exist.
NON_UTF8_FILES: dict[str, bytes] = {NON_UTF8_DISPLAY: NON_UTF8_CONTENT} if HAS_BYTE_NAMES else {}
NON_UTF8_BYTES = sum(len(data) for data in NON_UTF8_FILES.values())

#: relative path -> content of every regular file that the audit must list (SYNTHETIC FIXTURE).
VISIBLE: dict[str, bytes] = {
    "README.md": b"# SYNTHETIC FIXTURE\n\nNot a trading system.\n",
    "config.py": CONFIG.encode(),
    "run_bot.py": RUN_BOT.encode(),
    "start_bot.bat": b"@echo off\r\nrem SYNTHETIC FIXTURE\r\npython run_bot.py\r\n",
    "start_bot.ps1": b"# SYNTHETIC FIXTURE\npython run_bot.py\n",
    "run.sh": b"#!/bin/sh\n# SYNTHETIC FIXTURE\npython run_bot.py\n",
    "requirements.txt": REQUIREMENTS.encode(),
    "pyproject.toml": PYPROJECT.encode(),
    "setup.cfg": SETUP_CFG.encode(),
    "environment.yml": ENVIRONMENT_YML.encode(),
    "package.json": PACKAGE_JSON.encode(),
    "Pipfile": PIPFILE.encode(),
    "pytest.ini": b"[pytest]\n# SYNTHETIC FIXTURE\n",
    ".env": b"# SYNTHETIC FIXTURE\nMT5_LOGIN=99000111222333\nMT5_PASSWORD=FAKE-NOT-A-SECRET-env-7c1d\n",
    ".env.production": b"TELEGRAM_BOT_TOKEN=FAKE-NOT-A-SECRET-env-7c1d\n",
    ".gitignore": b"# SYNTHETIC FIXTURE\n.env\n",
    ".python-version": b"3.11.9\n",
    "runtime.txt": b"python-3.11.9\n",
    "latin1_notes.txt": "# SYNTHETIC FIXTURE caf\xe9\npassword = 'FAKE-NOT-A-SECRET-config-52aa'\n".encode(
        "latin-1"
    ),
    "engines/silver_bullet.py": SILVER_BULLET.encode(),
    "engines/orb_vwap.py": ORB_VWAP.encode(),
    "engines/big_module.py": BIG_MODULE.encode(),
    "engines/gold_breakout/Expert.mq5": ("\ufeff" + EXPERT_MQ5).encode("utf-16-le"),
    "engines/gold_breakout/Include/helper.mqh": b"// SYNTHETIC FIXTURE\nint Helper() { return 1; }\n",
    "engines/gold_breakout/Expert.ex5": b"EX5\x00\x01\x02SYNTHETIC FIXTURE\x00" + bytes(range(64)),
    "engines/legacy/old.mq4": b"// SYNTHETIC FIXTURE\nint start() { return 0; }\n",
    "tests/conftest.py": b"# SYNTHETIC FIXTURE\n",
    "tests/test_engine.py": TEST_ENGINE.encode(),
    "tests/legacy_test.py": LEGACY_TEST.encode(),
    "data/ticks_xauusd.csv": TICKS_CSV.encode(),
    "data/bars.tsv": b"<DATE>\t<TIME>\t<OPEN>\t<CLOSE>\n2024.01.02\t00:00\t2063.1\t2063.2\n",
    "data/semicolon.csv": b"time;bid;ask\n1;2;3\n4;5;6",
    "data/headerless.csv": b"2024.01.02 00:00:01,2063.11,2063.45\n2024.01.02 00:00:02,2063.12,2063.46\n",
    "data/utf16_export.csv": UTF16_CSV,
    "data/big_utf16.csv": BIG_UTF16_CSV,
    "data/history.sqlite": b"SQLite format 3\x00FAKE-NOT-A-SECRET-sqlite-row" + b"\x00" * 100,
    "data/cache.parquet": b"PAR1" + bytes(range(200)) + b"PAR1",
    "models/model_lgb.txt": b"tree\nversion=v3\nSYNTHETIC FIXTURE\n",
    "models/classifier.pkl": b"\x80\x04SYNTHETIC FIXTURE\x00.",
    "models/net.onnx": b"\x08\x07SYNTHETIC FIXTURE\x00",
    "notebooks/analysis.ipynb": NOTEBOOK.encode(),
    "deploy/Dockerfile": b"# SYNTHETIC FIXTURE\nFROM scratch\n",
    "deploy/docker-compose.yml": b"# SYNTHETIC FIXTURE\nservices: {}\n",
    "deploy/gold_bot.service": b"# SYNTHETIC FIXTURE\n[Service]\nExecStart=/bin/false\n",
    "deploy/Procfile": b"worker: python run_bot.py\n",
    "deploy/task_export.xml": TASK_XML.encode(),
    "deploy/winsw.xml": SERVICE_XML.encode(),
    "secrets/id_rsa": (
        "-----BEGIN OPENSSH "
        + "PRIVATE KEY-----\nFAKE-NOT-A-SECRET-keybody-1a2b\n-----END OPENSSH "
        + "PRIVATE KEY-----\n"
    ).encode(),
    "secrets/accounts.json": b'{"login": "99000111222333", "password": "FAKE-NOT-A-SECRET-config-52aa"}',
    "secrets/prod.csv": b"user,value\nfakeuser,FAKE-NOT-A-SECRET-config-52aa\n",
    "keys/api_token.txt": (FAKE_KEY_BODY + "\n").encode(),
    "mt5_terminal/terminal64.exe": b"MZ\x00SYNTHETIC FIXTURE - not an executable\x00",
    "mt5_terminal/MQL5/Experts/GoldEA.ex5": b"EX5\x00SYNTHETIC FIXTURE\x00",
    "dashboard/app.py": b"# SYNTHETIC FIXTURE\nimport json\n",
    "baseline_round1/results.json": b'{"note": "SYNTHETIC FIXTURE", "trades": 0}\n',
    "vendor/nested_repo/lib.py": b"# SYNTHETIC FIXTURE\n",
    "binary_source.py": b"# SYNTHETIC FIXTURE\x00\nimport MetaTrader5\norder_send(1)\n",
    # the NUL byte lies beyond the first kilobyte but inside the 8 KiB that decide "binary"
    "engines/late_binary.py": b"# SYNTHETIC FIXTURE\n" + b"value = 1\n" * 300 + b"\x00order_send(1)\n",
}

#: files inside directories that must be summarised, never listed, hashed or scanned.
EXCLUDED: dict[str, bytes] = {
    "venv/pyvenv.cfg": b"home = /fake/python\nversion = 3.11.9\ninclude-system-site-packages = false\n",
    "venv/lib/site-packages/leak.py": (
        b'import MetaTrader5\nPASSWORD = "FAKE-NOT-A-SECRET-config-52aa"\norder_send(1)\n'
    ),
    "custom_runtime/pyvenv.cfg": b"home = /fake/python\nversion_info = 3.12.4.final.0\n",
    "custom_runtime/Lib/thing.py": b"import MetaTrader5\n",
    "__pycache__/run_bot.cpython-311.pyc": b"\x00\x01\x02SYNTHETIC FIXTURE",
    "node_modules/leftpad/index.js": b"// SYNTHETIC FIXTURE\nmodule.exports = 1;\n",
    ".git/HEAD": b"ref: refs/heads/main\n",
    ".git/refs/heads/main": (FAKE_COMMIT + "\n").encode(),
    ".git/config": (
        b'[remote "origin"]\n\turl = https://fakeuser:FAKE-NOT-A-SECRET-index-9e0b@git.invalid/repo.git\n'
    ),
    ".git/worktrees/wt-session-01/HEAD": b"ref: refs/heads/session-01\n",
    "vendor/nested_repo/.git/HEAD": (FAKE_DETACHED_COMMIT + "\n").encode(),
    "vendor/packed_repo/.git/HEAD": b"ref: refs/heads/dev\n",
    "vendor/packed_repo/.git/packed-refs": (
        "# pack-refs with: peeled fully-peeled sorted\n" + FAKE_PACKED_COMMIT + " refs/heads/dev\n"
    ).encode(),
}

SENSITIVE_PATHS = {
    ".env",
    ".env.production",
    "secrets/id_rsa",
    "secrets/accounts.json",
    "secrets/prod.csv",
    "keys/api_token.txt",
}
EXPECTED_PATHS = sorted([*VISIBLE, *NON_UTF8_FILES])
FORBIDDEN_OUTPUT_STRINGS = (MARKER, FAKE_ACCOUNT, FAKE_ROW_VALUE, FAKE_HOSTNAME, "conda-forge", "fakeuser")


def build_tree(root: Path) -> None:
    """Create the SYNTHETIC FIXTURE tree with fixed modification times."""
    for rel, data in {**VISIBLE, **EXCLUDED}.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if HAS_BYTE_NAMES:
        with open(os.fsencode(root) + b"/" + NON_UTF8_NAME, "wb") as handle:
            handle.write(NON_UTF8_CONTENT)
    os.symlink("config.py", root / "link_to_config.py")
    os.symlink("data", root / "link_to_data", target_is_directory=True)
    os.symlink("missing/target", root / "dangling_link")
    # Where the platform cannot set the times of a link itself (Windows), links keep their own
    # times and only files and directories get the fixed modification time.
    link_times = os.utime in os.supports_follow_symlinks
    for current, directories, files in os.walk(root, topdown=False):
        for name in [*directories, *files]:
            entry = os.path.join(current, name)
            if link_times:
                os.utime(entry, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS), follow_symlinks=False)
            elif not os.path.islink(entry):
                os.utime(entry, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    os.utime(root, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))


def plain(path: Any) -> str:
    """A path as the tests name it: without the extended-length prefix the tool uses on Windows."""
    return str(audit._strip_extended_prefix(os.fsdecode(path)))


def line_of(text: str, needle: str) -> int:
    """1-based line number of the first occurrence of ``needle`` (fails when it is absent)."""
    return text[: text.index(needle)].count("\n") + 1


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Metadata (``os.lstat``) and content digest of everything below ``root``; links are not followed."""
    seen: dict[str, tuple[Any, ...]] = {}
    pending = [str(root)]
    while pending:
        current = pending.pop()
        st = os.lstat(current)
        fields: tuple[Any, ...] = (
            st.st_mode,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            st.st_ino,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
        )
        if stat.S_ISLNK(st.st_mode):
            fields += (os.readlink(current),)
        elif stat.S_ISDIR(st.st_mode):
            names = sorted(os.listdir(current))
            fields += (tuple(names),)
            pending.extend(os.path.join(current, name) for name in names)
        elif stat.S_ISREG(st.st_mode):
            with open(current, "rb") as handle:
                fields += (hashlib.sha256(handle.read()).hexdigest(),)
        seen[current] = fields
    return seen


def run_main(*argv: str) -> tuple[int, str, str]:
    """Run the tool in-process and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = audit.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_audit(root: Path, out: Path, *extra: str) -> tuple[int, str, str]:
    return run_main("audit", "--root", str(root), "--out", str(out), "--progress-every", "0", *extra)


def load_inventory(out: Path) -> dict[str, Any]:
    document = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {record["path"]: record for record in records}


class Audited:
    """One standard audit run over the fixture tree (shared by the read-only inspection tests)."""

    def __init__(self, base: Path) -> None:
        self.root = base / "prod"
        self.out = base / "audit_out"
        self.root.mkdir()
        build_tree(self.root)
        self.before = snapshot(self.root)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(audit.platform, "node", lambda: FAKE_HOSTNAME)
            self.code, self.stdout, self.stderr = run_audit(
                self.root, self.out, "--processes", "--count-rows"
            )
        self.after = snapshot(self.root)
        self.inventory = load_inventory(self.out)
        self.manifest = load_manifest(self.out / "manifest.jsonl")


@pytest.fixture(scope="module")
def audited(tmp_path_factory: pytest.TempPathFactory) -> Audited:
    return Audited(tmp_path_factory.mktemp("audited"))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "prod"
    root.mkdir()
    build_tree(root)
    return root


def locations(items: list[dict[str, Any]]) -> set[tuple[str, int]]:
    return {(item["path"], item["line"]) for item in items}


# ---------------------------------------------------------------------------------------------
# Outputs and inventory content
# ---------------------------------------------------------------------------------------------


@requirement
def test_audit_writes_all_outputs_and_they_parse(audited: Audited) -> None:
    assert audited.code == 0
    assert sorted(path.name for path in audited.out.iterdir()) == [
        "audit_log.txt",
        "inventory.json",
        "manifest.jsonl",
        "processes.json",
    ]
    processes = json.loads((audited.out / "processes.json").read_text(encoding="utf-8"))
    for document in (audited.inventory, processes):
        tool = document["tool"]
        assert tool["name"] == "gie_baseline_audit"
        assert tool["version"] == audit.TOOL_VERSION
        assert tool["schema_version"] == audit.SCHEMA_VERSION
        assert tool["argv"] == [
            "audit",
            "--root",
            str(audited.root),
            "--out",
            str(audited.out),
            "--progress-every",
            "0",
            "--processes",
            "--count-rows",
        ]
    for name in ("inventory.json", "processes.json"):
        raw = (audited.out / name).read_text(encoding="utf-8")
        assert raw == json.dumps(json.loads(raw), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    expected_sections = {
        "tool",
        "run",
        "host",
        "root_summary",
        "languages",
        "tests",
        "data",
        "models",
        "broker_interfaces",
        "process_entry_points",
        "dependency_manifests",
        "python_environments",
        "version_control",
        "notebooks",
        "engine_baseline_hints",
        "credential_scan",
        "exclusions_and_caps",
    }
    assert set(audited.inventory) == expected_sections


@requirement
def test_manifest_lists_every_non_excluded_file_with_correct_hashes(audited: Audited) -> None:
    lines = (audited.out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["path"] for line in lines] == EXPECTED_PATHS
    for line in lines:
        assert line == json.dumps(json.loads(line), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    for rel, content in VISIBLE.items():
        record = audited.manifest[rel]
        assert record["size"] == len(content)
        assert record["mtime_ns"] == os.lstat(audited.root / rel).st_mtime_ns
        if rel in SENSITIVE_PATHS:
            assert record["sha256"] is None
            assert record["skipped"] == "sensitive"
            assert record["sensitive"] is True
        else:
            assert record["sha256"] == hashlib.sha256(content).hexdigest()
            assert set(record) == {"path", "size", "mtime_ns", "sha256"}
    assert audited.manifest["config.py"]["mtime_ns"] == FIXED_MTIME_NS


@requirement
def test_host_block_reports_hardware_and_only_a_hash_of_the_host_name(audited: Audited) -> None:
    host = audited.inventory["host"]
    assert host["hostname_sha256"] == hashlib.sha256(FAKE_HOSTNAME.encode()).hexdigest()
    assert host["cpu_count"] == os.cpu_count()
    assert host["python_version"] == sys.version.split()[0]
    assert host["machine"]
    assert host["platform"]
    assert host["disk_total_bytes"] == shutil.disk_usage(audited.root).total
    assert host["errors"] == {}
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo", encoding="ascii") as handle:
            kilobytes = next(int(line.split()[1]) for line in handle if line.startswith("MemTotal:"))
        assert host["memory_total_bytes"] == kilobytes * 1024
        assert host["processor"]
    assert isinstance(audited.inventory["run"]["disk_free_bytes"], int)


@requirement
def test_root_summary_and_languages(audited: Audited) -> None:
    summary = audited.inventory["root_summary"]
    assert summary["files"] == len(EXPECTED_PATHS)
    assert summary["total_bytes"] == sum(len(data) for data in VISIBLE.values()) + NON_UTF8_BYTES
    python_files = {rel: data for rel, data in VISIBLE.items() if rel.endswith(".py")}
    assert summary["by_extension"][".py"] == {
        "files": len(python_files),
        "bytes": sum(len(data) for data in python_files.values()),
    }
    assert summary["by_extension"]["(none)"]["files"] == sum(
        1 for rel in VISIBLE if "." not in rel.rsplit("/", 1)[-1].lstrip(".")
    )
    engines = {rel: data for rel, data in VISIBLE.items() if rel.startswith("engines/")}
    assert summary["by_top_level_directory"]["engines"] == {
        "files": len(engines),
        "bytes": sum(len(data) for data in engines.values()),
    }
    assert summary["by_top_level_directory"]["(files in root)"]["files"] == sum(
        1 for rel in EXPECTED_PATHS if "/" not in rel
    )
    assert "venv" not in summary["by_top_level_directory"]
    largest = summary["largest_files"]
    assert [item["size"] for item in largest] == sorted((item["size"] for item in largest), reverse=True)
    assert largest[0]["size"] == max(len(data) for data in VISIBLE.values())

    languages = audited.inventory["languages"]
    text_python = {rel: data for rel, data in python_files.items() if b"\x00" not in data}
    assert languages["Python"]["files"] == len(python_files)
    assert languages["Python"]["files_with_line_count"] == len(text_python)
    assert languages["Python"]["lines"] == sum(data.count(b"\n") for data in text_python.values())
    # the .mq5 source is UTF-16 with a byte order mark: its lines must still be counted
    assert languages["MQL5"] == {
        "files": 1,
        "bytes": len(VISIBLE["engines/gold_breakout/Expert.mq5"]),
        "lines": EXPERT_MQ5.count("\n"),
        "files_with_line_count": 1,
        "extensions": [".mq5"],
    }
    assert languages["MQL4"]["files"] == 1
    assert languages["MQL5 (compiled)"] == {
        "files": 2,
        "bytes": len(VISIBLE["engines/gold_breakout/Expert.ex5"])
        + len(VISIBLE["mt5_terminal/MQL5/Experts/GoldEA.ex5"]),
        "lines": 0,
        "files_with_line_count": 0,
        "extensions": [".ex5"],
    }
    for expected in (
        "MQL4/MQL5 include",
        "Batch",
        "PowerShell",
        "Shell",
        "Jupyter Notebook",
        "YAML",
        "TOML",
        "JSON",
    ):
        assert languages[expected]["files"] >= 1
    required_extensions = [
        ".py",
        ".mq4",
        ".mq5",
        ".mqh",
        ".ex4",
        ".ex5",
        ".c",
        ".cpp",
        ".cs",
        ".java",
        ".js",
        ".ts",
        ".r",
        ".jl",
        ".rs",
        ".go",
        ".sql",
        ".ps1",
        ".bat",
        ".sh",
        ".ipynb",
        ".html",
        ".css",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".ini",
    ]
    assert set(required_extensions) <= set(audit.LANGUAGE_BY_EXTENSION)


@requirement
def test_tests_inventory(audited: Audited) -> None:
    tests = audited.inventory["tests"]
    assert tests["test_files"] == [
        {"path": "tests/legacy_test.py", "test_functions": 1},
        {"path": "tests/test_engine.py", "test_functions": 3},
    ]
    assert tests["test_file_count"] == 2
    assert tests["test_function_count"] == 4
    assert tests["test_directories"] == ["tests"]
    assert tests["conftest_files"] == ["tests/conftest.py"]
    assert tests["config_files"] == [
        {"kind": "pyproject.toml pytest section", "path": "pyproject.toml"},
        {"kind": "pytest.ini", "path": "pytest.ini"},
        {"kind": "setup.cfg pytest section", "path": "setup.cfg"},
    ]
    assert locations(tests["unittest_usage"]) == {
        ("tests/legacy_test.py", line_of(LEGACY_TEST, "import unittest")),
        ("tests/legacy_test.py", line_of(LEGACY_TEST, "unittest.TestCase")),
    }
    assert ("tests/test_engine.py", line_of(TEST_ENGINE, "import pytest")) in locations(tests["pytest_usage"])


@requirement
def test_data_inventory_reports_column_names_only(audited: Audited) -> None:
    data = audited.inventory["data"]
    files = {item["path"]: item for item in data["files"]}
    ticks = files["data/ticks_xauusd.csv"]
    assert ticks["columns"] == ["time", "bid", "ask", "flags"]
    assert ticks["delimiter"] == "comma"
    assert ticks["row_count"] == 5
    assert ticks["row_count_excludes_header"] is True
    assert ticks["size"] == len(TICKS_CSV)
    assert ticks["mtime_ns"] == FIXED_MTIME_NS
    assert ticks["mtime_utc"] == "2023-11-14T22:13:20Z"
    assert files["data/bars.tsv"]["columns"] == ["<DATE>", "<TIME>", "<OPEN>", "<CLOSE>"]
    assert files["data/bars.tsv"]["delimiter"] == "tab"
    assert files["data/bars.tsv"]["row_count"] == 1
    assert files["data/semicolon.csv"]["columns"] == ["time", "bid", "ask"]
    assert files["data/semicolon.csv"]["delimiter"] == "semicolon"
    assert files["data/semicolon.csv"]["row_count"] == 2  # the last line has no trailing newline
    assert files["data/utf16_export.csv"]["columns"] == ["Time", "Bid", "Ask"]
    assert files["data/utf16_export.csv"]["row_count"] == 3
    for name in NON_UTF8_FILES:  # only where a name that is not UTF-8 can exist
        assert files[name]["columns"] == ["time", "bid"]
    # a first line that is data is never emitted
    headerless = files["data/headerless.csv"]
    assert "columns" not in headerless
    assert headerless["header_withheld"] == "first line does not look like column names"
    assert headerless["row_count"] == 2
    assert headerless["row_count_excludes_header"] is False
    # a sensitive-named delimited file is listed but not read
    assert set(files["secrets/prod.csv"]) == {"path", "size", "mtime_ns", "mtime_utc"}
    assert set(files["data/history.sqlite"]) == {"path", "size", "mtime_ns", "mtime_utc"}
    data_paths = {rel for rel in EXPECTED_PATHS if os.path.splitext(rel)[1].lower() in audit.DATA_EXTENSIONS}
    assert set(files) == data_paths
    assert data["file_count"] == len(data_paths)
    assert files["data/big_utf16.csv"]["row_count"] == BIG_UTF16_ROWS
    assert data["by_extension"][".csv"]["files"] == 6 + len(NON_UTF8_FILES)  # the byte-named file is a .csv
    assert data["by_extension"][".sqlite"] == {"files": 1, "bytes": len(VISIBLE["data/history.sqlite"])}
    assert data["largest_files"][0]["size"] == max(item["size"] for item in data["files"])
    required = [
        "csv",
        "tsv",
        "parquet",
        "feather",
        "arrow",
        "h5",
        "hdf5",
        "npy",
        "npz",
        "pkl",
        "pickle",
        "joblib",
        "db",
        "sqlite",
        "sqlite3",
        "duckdb",
        "json",
        "jsonl",
        "xlsx",
        "hst",
        "fxt",
        "tick",
        "bin",
        "zip",
        "gz",
        "7z",
    ]
    assert {"." + name for name in required} <= set(audit.DATA_EXTENSIONS)


@requirement
def test_models_and_notebooks_are_listed(audited: Audited) -> None:
    models = {item["path"]: item for item in audited.inventory["models"]["files"]}
    assert set(models) == {"models/classifier.pkl", "models/model_lgb.txt", "models/net.onnx"}
    assert models["models/model_lgb.txt"]["matched_by"] == "name pattern"
    assert models["models/net.onnx"]["matched_by"] == "extension"
    assert set(models["models/net.onnx"]) == {"path", "size", "mtime_ns", "mtime_utc", "matched_by"}
    assert audited.inventory["notebooks"] == {"count": 1, "paths": ["notebooks/analysis.ipynb"]}


@requirement
def test_broker_interface_locations(audited: Audited) -> None:
    broker = audited.inventory["broker_interfaces"]
    rules = {name: locations(items) for name, items in broker["rules"].items()}
    notebook_import = line_of(NOTEBOOK, "import MetaTrader5")
    assert rules["metatrader5_import"] == {("run_bot.py", 2), ("notebooks/analysis.ipynb", notebook_import)}
    assert rules["order_send"] == {("run_bot.py", line_of(RUN_BOT, "order_send"))}
    assert rules["order_check"] == {("run_bot.py", line_of(RUN_BOT, "order_check"))}
    assert rules["positions_get"] == {("run_bot.py", line_of(RUN_BOT, "positions_get"))}
    assert rules["symbol_info_tick"] == {("run_bot.py", line_of(RUN_BOT, "symbol_info_tick"))}
    assert rules["copy_ticks"] == {("run_bot.py", line_of(RUN_BOT, "copy_ticks_from"))}
    assert rules["copy_rates"] == {("run_bot.py", line_of(RUN_BOT, "copy_rates_from_pos"))}
    assert rules["mt5_login"] == {("run_bot.py", line_of(RUN_BOT, "mt5.login"))}
    assert rules["mt5_initialize"] == {
        ("run_bot.py", line_of(RUN_BOT, "mt5.initialize")),
        ("notebooks/analysis.ipynb", line_of(NOTEBOOK, "mt5.initialize")),
    }
    mq5 = "engines/gold_breakout/Expert.mq5"
    assert rules["mql_OrderSend"] == {(mq5, line_of(EXPERT_MQ5, "OrderSend"))}
    assert rules["mql_CTrade"] == {(mq5, line_of(EXPERT_MQ5, "CTrade"))}
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "import zmq")) in rules["zeromq"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "import requests")) in rules["http_client"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "requests.post")) in rules["http_client"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "import smtplib")) in rules["smtplib"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "import websocket")) in rules["websocket"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, "TELEGRAM_URL")) in rules["telegram"]
    assert ("engines/silver_bullet.py", line_of(SILVER_BULLET, '"discord"')) in rules["discord"]
    assert rules["socket_usage"] == {
        ("engines/orb_vwap.py", line_of(ORB_VWAP, "import socket")),
        ("engines/orb_vwap.py", line_of(ORB_VWAP, "socket.socket()")),
    }
    bridges = ("engines/orb_vwap.py", line_of(ORB_VWAP, "BRIDGES"))
    for name in ("oanda", "ctrader", "dwx", "fix_protocol"):
        assert bridges in rules[name]
    assert ("engines/orb_vwap.py", line_of(ORB_VWAP, "import ccxt")) in rules["ccxt"]
    assert ("engines/big_module.py", BIG_MODULE.count("\n")) in rules[
        "ccxt"
    ]  # the last line of a 12 KiB file
    assert ("engines/orb_vwap.py", line_of(ORB_VWAP, "ib_insync")) in rules["ib_insync"]
    required = {
        "metatrader5_import",
        "metatrader5_reference",
        "mt5_initialize",
        "mt5_login",
        "order_send",
        "order_check",
        "positions_get",
        "copy_ticks",
        "copy_rates",
        "symbol_info_tick",
        "mql_OrderSend",
        "mql_CTrade",
        "zeromq",
        "socket_usage",
        "dwx",
        "ctrader",
        "fix_protocol",
        "oanda",
        "ccxt",
        "ib_insync",
        "http_client",
        "websocket",
        "telegram",
        "smtplib",
        "discord",
    }
    assert set(rules) == required
    for name in required:
        assert rules[name], name  # every rule is exercised by the fixture
        assert broker["rule_totals"][name]["lines"] == len(rules[name])
    # nothing from excluded directories and nothing from a file with NUL bytes
    scanned_paths = {path for found in rules.values() for path, _line in found}
    assert not {
        path for path in scanned_paths if path.startswith(("venv/", "custom_runtime/", "node_modules/"))
    }
    assert "binary_source.py" not in scanned_paths
    assert "engines/late_binary.py" not in scanned_paths
    assert broker["mql_files"] == {
        ".ex5": {
            "files": 2,
            "bytes": len(VISIBLE["engines/gold_breakout/Expert.ex5"])
            + len(VISIBLE["mt5_terminal/MQL5/Experts/GoldEA.ex5"]),
        },
        ".mq4": {"files": 1, "bytes": len(VISIBLE["engines/legacy/old.mq4"])},
        ".mq5": {"files": 1, "bytes": len(VISIBLE["engines/gold_breakout/Expert.mq5"])},
        ".mqh": {"files": 1, "bytes": len(VISIBLE["engines/gold_breakout/Include/helper.mqh"])},
    }
    assert broker["terminal_hints"] == [
        {"hint": "directory named mql5", "path": "mt5_terminal/MQL5"},
        {"hint": "file named terminal64.exe", "path": "mt5_terminal/terminal64.exe"},
    ]


@requirement
def test_keyword_scan_reports_locations_without_content(audited: Audited) -> None:
    inventory = audited.inventory
    for items in inventory["broker_interfaces"]["rules"].values():
        for item in items:
            assert set(item) == {"path", "line"}
            assert isinstance(item["line"], int)
    findings = inventory["credential_scan"]["findings"]
    known_rules = {rule.name for rule in (*audit.CREDENTIAL_RULES, *audit.CREDENTIAL_CONFIG_RULES)}
    for item in findings:
        assert set(item) == {"path", "line", "rule"}
        assert item["rule"] in known_rules
    found = {(item["path"], item["line"], item["rule"]) for item in findings}
    assert ("config.py", line_of(CONFIG, "LOGIN ="), "credential_assignment_number") in found
    assert ("config.py", line_of(CONFIG, "PASSWORD ="), "credential_assignment_literal") in found
    assert ("config.py", line_of(CONFIG, "DATABASE_URL"), "connection_string_with_credentials") in found
    login_line = line_of(RUN_BOT, "mt5.login")
    assert ("run_bot.py", login_line, "mt5_login_or_initialize_literal_arguments") in found
    assert ("run_bot.py", login_line, "credential_assignment_literal") in found
    mq5_line = line_of(EXPERT_MQ5, "InpPassword")
    assert ("engines/gold_breakout/Expert.mq5", mq5_line, "credential_assignment_literal") in found
    index_line = line_of(REQUIREMENTS, "--extra-index-url")
    assert ("requirements.txt", index_line, "connection_string_with_credentials") in found
    assert ("latin1_notes.txt", 2, "credential_assignment_literal") in found
    assert (
        "notebooks/analysis.ipynb",
        line_of(NOTEBOOK, "api_key"),
        "credential_assignment_literal",
    ) in found
    # an empty literal is not a finding, and unread sensitive files produce none
    empty_line = line_of(CONFIG, "EMPTY_PASSWORD")
    assert not {item for item in found if item[0] == "config.py" and item[1] == empty_line}
    assert not {item for item in found if item[0] in SENSITIVE_PATHS}
    assert not {item for item in found if item[0].startswith(("venv/", ".git/"))}


@requirement
def test_process_entry_points(audited: Audited) -> None:
    entry = audited.inventory["process_entry_points"]
    assert entry["python_main_guard"] == [{"line": line_of(RUN_BOT, "if __name__"), "path": "run_bot.py"}]
    assert entry["launchers"] == [
        {"kind": ".sh", "path": "run.sh"},
        {"kind": ".bat", "path": "start_bot.bat"},
        {"kind": ".ps1", "path": "start_bot.ps1"},
    ]
    assert entry["service_and_task_definitions"] == [
        {"line": 2, "path": "deploy/task_export.xml", "rule": "windows_scheduled_task_xml"},
        {"line": 1, "path": "deploy/winsw.xml", "rule": "windows_service_wrapper_xml"},
    ]
    assert entry["containers_and_process_managers"] == [
        {"kind": "dockerfile", "path": "deploy/Dockerfile"},
        {"kind": "procfile", "path": "deploy/Procfile"},
        {"kind": "compose", "path": "deploy/docker-compose.yml"},
        {"kind": "systemd unit", "path": "deploy/gold_bot.service"},
    ]


@requirement
def test_dependency_manifests_emit_names_and_specifiers_only(audited: Audited) -> None:
    parsed = {item["path"]: item for item in audited.inventory["dependency_manifests"]["parsed"]}
    assert set(parsed) == {
        "Pipfile",
        "environment.yml",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
    }

    def entries(path: str) -> set[tuple[str, str, str]]:
        return {
            (item["section"], item["name"], item.get("specifier", "")) for item in parsed[path]["entries"]
        }

    assert entries("requirements.txt") == {
        ("requirements", "numpy", "==1.26.4"),
        ("requirements", "pandas", ">=2.0,<3"),
        ("requirements", "MetaTrader5", "==5.0.45"),
        ("requirements", "pyzmq", ""),
        ("requirements", "requests", ">=2.31"),
    }
    # dropped: the 'token' package, the index URL with credentials and the direct URL reference
    assert parsed["requirements.txt"]["dropped"] == {"sensitive": 3, "options": 1, "unparsed": 1}
    assert entries("pyproject.toml") == {
        ("project.dependencies", "lightgbm", ">=4.0"),
        ("project.dependencies", "scikit-learn", "==1.4.2"),
        ("project.optional-dependencies", "pytest", ">=8"),
        ("project.optional-dependencies", "ruff", ""),
        ("build-system.requires", "setuptools", ">=68"),
        ("tool.poetry.dependencies", "python", "^3.11"),
        ("tool.poetry.dependencies", "ccxt", "^4.2"),
    }
    assert parsed["pyproject.toml"]["dropped"] == {"sensitive": 1, "options": 0, "unparsed": 0}
    assert entries("setup.cfg") == {
        ("options.install_requires", "httpx", ">=0.27"),
        ("options.install_requires", "websockets", ""),
        ("options.extras_require", "python-telegram-bot", "==21.0"),
    }
    assert entries("environment.yml") == {
        ("conda", "python", "=3.11.9"),
        ("conda", "numpy", "=1.26.4=py311h64a7726_0"),
        ("conda", "ta-lib", ""),
        ("pip", "ib_insync", "==0.9.86"),
    }
    assert parsed["environment.yml"]["dropped"] == {"sensitive": 1, "options": 0, "unparsed": 0}
    assert entries("package.json") == {
        ("dependencies", "express", "^4.19.2"),
        ("dependencies", "@types/node", "20.11.0"),
        ("devDependencies", "ws", "~8.17.0"),
    }
    assert parsed["package.json"]["dropped"] == {"sensitive": 1, "options": 0, "unparsed": 0}
    assert entries("Pipfile") == {
        ("packages", "flask", "==3.0.3"),
        ("packages", "oandapyV20", "*"),
        ("dev-packages", "pytest", ">=8"),
    }
    assert parsed["Pipfile"]["dropped"] == {"sensitive": 1, "options": 0, "unparsed": 0}
    for item in parsed.values():
        assert set(item) == {"path", "kind", "entries", "dropped", "truncated"}
        for entry in item["entries"]:
            assert set(entry) <= {"name", "section", "specifier"}


@requirement
def test_python_environments_and_version_files(audited: Audited) -> None:
    section = audited.inventory["python_environments"]
    assert section["environments"] == [
        {
            "detected_by": "pyvenv.cfg",
            "kind": "venv",
            "path": "custom_runtime",
            "python_versions": ["3.12.4.final.0"],
        },
        {"detected_by": "pyvenv.cfg", "kind": "venv", "path": "venv", "python_versions": ["3.11.9"]},
    ]
    assert section["version_files"] == [
        {"kind": ".python-version", "path": ".python-version", "python_versions": ["3.11.9"]},
        {"kind": "runtime.txt", "path": "runtime.txt", "python_versions": ["python-3.11.9"]},
    ]


@requirement
def test_version_control_is_read_from_files(audited: Audited) -> None:
    vcs = audited.inventory["version_control"]
    assert vcs["has_git_directory"] is True
    assert vcs["branch"] == "main"
    assert vcs["head"] == "branch"
    assert vcs["head_commit"] == FAKE_COMMIT
    assert vcs["worktrees"] == ["wt-session-01"]
    assert vcs["has_gitignore"] is True
    assert vcs["gitignore_files"] == [".gitignore"]
    assert "not determined" in vcs["dirty_state"]
    repositories = {item["path"]: item for item in vcs["repositories"]}
    assert set(repositories) == {".git", "vendor/nested_repo/.git", "vendor/packed_repo/.git"}
    assert repositories[".git"]["commit_source"] == "loose ref"
    assert repositories["vendor/nested_repo/.git"] == {
        "branch": None,
        "commit_source": "HEAD",
        "head": "detached",
        "head_commit": FAKE_DETACHED_COMMIT,
        "path": "vendor/nested_repo/.git",
        "worktrees": [],
    }
    assert repositories["vendor/packed_repo/.git"]["branch"] == "dev"
    assert repositories["vendor/packed_repo/.git"]["head_commit"] == FAKE_PACKED_COMMIT
    assert repositories["vendor/packed_repo/.git"]["commit_source"] == "packed-refs"


@requirement
def test_engine_and_baseline_hints_are_paths_only(audited: Audited) -> None:
    hints = audited.inventory["engine_baseline_hints"]
    assert hints["hints"] == list(audit.ENGINE_HINTS)
    required = [
        "cavemen",
        "silver",
        "bullet",
        "liquidity",
        "hunter",
        "orb",
        "vwap",
        "trend",
        "rider",
        "sweep",
        "reversal",
        "gce",
        "gold",
        "breakout",
        "ai_trader",
        "round1",
        "round_1",
        "round2",
        "round_2",
        "baseline",
        "backtest",
        "dashboard",
    ]
    assert set(required) <= set(audit.ENGINE_HINTS)
    listed = {item["path"]: item for item in hints["paths"]}
    assert listed["engines/silver_bullet.py"] == {
        "hints": ["silver", "bullet"],
        "kind": "file",
        "path": "engines/silver_bullet.py",
    }
    assert listed["engines/orb_vwap.py"]["hints"] == ["orb", "vwap"]
    assert listed["engines/gold_breakout"] == {
        "hints": ["gold", "breakout"],
        "kind": "dir",
        "path": "engines/gold_breakout",
    }
    assert listed["baseline_round1"]["hints"] == ["round1", "baseline"]
    assert listed["dashboard"]["kind"] == "dir"
    assert listed["mt5_terminal/MQL5/Experts/GoldEA.ex5"]["hints"] == ["gold"]
    assert "config.py" not in listed
    for path, item in listed.items():
        assert set(item) == {"path", "kind", "hints"}
        base = path.rsplit("/", 1)[-1].lower()
        assert item["hints"]
        for hint in item["hints"]:
            assert hint in base
    assert hints["by_hint"]["gold"] == sum(1 for item in listed.values() if "gold" in item["hints"])
    assert hints["by_hint"]["cavemen"] == 0


@requirement
def test_excluded_directories_are_summarised_not_listed(audited: Audited) -> None:
    section = audited.inventory["exclusions_and_caps"]
    assert section["excluded_directory_names"] == sorted(audit.DEFAULT_EXCLUDED_DIRS)
    assert set(audit.DEFAULT_EXCLUDED_DIRS) == {
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
    }
    excluded = {item["path"]: item for item in section["excluded_directories"]}
    assert set(excluded) == {
        ".git",
        "__pycache__",
        "custom_runtime",
        "node_modules",
        "venv",
        "vendor/nested_repo/.git",
        "vendor/packed_repo/.git",
    }

    def expected(prefix: str) -> tuple[int, int]:
        inside = [data for rel, data in EXCLUDED.items() if rel.startswith(prefix + "/")]
        return len(inside), sum(len(data) for data in inside)

    for path, item in excluded.items():
        assert (item["files"], item["bytes"]) == expected(path), path
        assert item["errors"] == 0
    assert excluded["venv"]["reason"] == "default excluded directory name"
    assert excluded["custom_runtime"]["reason"] == "python environment (pyvenv.cfg found)"
    assert excluded["venv"]["directories"] == 2
    for rel in EXCLUDED:
        assert rel not in audited.manifest
    assert section["sensitive_file_count"] == len(SENSITIVE_PATHS)
    assert section["unreadable"] == []
    assert section["unreadable_count"] == 0
    assert section["changed_while_read"] == []
    assert section["truncated_lists"] == {}
    assert section["scan_skipped_binary_content"] == ["binary_source.py", "engines/late_binary.py"]
    assert section["caps"]["max_hash_bytes"] is None


@requirement
@read_only
def test_symlinks_are_recorded_and_never_followed(audited: Audited) -> None:
    links = audited.inventory["exclusions_and_caps"]["links_not_followed"]
    assert links == [
        {"kind": "symlink", "path": "dangling_link", "target": "missing/target"},
        {"kind": "symlink", "path": "link_to_config.py", "target": "config.py"},
        {"kind": "symlink", "path": "link_to_data", "target": "data"},
    ]
    assert not [path for path in audited.manifest if path.startswith("link_to")]
    everything = json.dumps(audited.inventory)
    assert "link_to_data/" not in everything
    assert audited.inventory["root_summary"]["by_extension"][".py"]["files"] == sum(
        1 for rel in VISIBLE if rel.endswith(".py")
    )


@requirement
def test_non_utf8_names_and_contents_do_not_break_the_audit(audited: Audited) -> None:
    for display, content in NON_UTF8_FILES.items():  # only where a name that is not UTF-8 can exist
        assert audited.manifest[display]["sha256"] == hashlib.sha256(content).hexdigest()
    for name in ("inventory.json", "manifest.jsonl", "audit_log.txt", "processes.json"):
        (audited.out / name).read_bytes().decode("utf-8")  # strict: every output is valid UTF-8
    assert (
        audited.manifest["latin1_notes.txt"]["sha256"]
        == hashlib.sha256(VISIBLE["latin1_notes.txt"]).hexdigest()
    )
    assert audit._display_name("plain.txt") == "plain.txt"
    assert audit._display_name("back\\slash") == "back\\\\slash"
    if HAS_BYTE_NAMES:
        assert audit._display_name(os.fsdecode(NON_UTF8_NAME)) == NON_UTF8_DISPLAY
    # Whatever the operating system hands over for a name it cannot decode (a lone surrogate), the
    # displayed name is plain text that can always be written to the UTF-8 output files.
    audit._display_name("caf\udce9_notes.csv").encode("utf-8")


@requirement
def test_fake_secrets_appear_in_no_output(audited: Audited) -> None:
    blobs = {path.name: path.read_bytes() for path in audited.out.iterdir()}
    blobs["stdout"] = audited.stdout.encode()
    blobs["stderr"] = audited.stderr.encode()
    assert len(blobs) == 6
    # the planted values really are in the audited tree (otherwise this test would prove nothing)
    tree_bytes = b"".join(VISIBLE.values()) + b"".join(EXCLUDED.values())
    for value in (MARKER, FAKE_ACCOUNT, FAKE_ROW_VALUE, *ALL_FAKE_SECRETS):
        assert value.encode() in tree_bytes
    assert FAKE_MQ5_SECRET.encode("utf-16-le") in tree_bytes
    for name, blob in blobs.items():
        lowered = blob.lower()
        for value in FORBIDDEN_OUTPUT_STRINGS:
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                assert value.encode(encoding) not in blob, (name, value, encoding)
            assert value.lower().encode() not in lowered, (name, value)
        assert b"Fake-Server" not in blob, name
        assert b"SQLite format" not in blob, name
        assert b"BEGIN OPENSSH" not in blob, name
        assert b"SYNTHETIC FIXTURE" not in blob, name  # no file content of any kind


@requirement
def test_sensitive_files_are_listed_but_not_hashed_or_opened(audited: Audited) -> None:
    listed = {item["path"]: item for item in audited.inventory["exclusions_and_caps"]["sensitive_files"]}
    assert set(listed) == SENSITIVE_PATHS
    for path, item in listed.items():
        assert item["sensitive"] is True
        assert item["hashed"] is False
        assert item["size"] == len(VISIBLE[path])
        assert item["reason"]
    assert listed["secrets/prod.csv"]["reason"] == "inside a directory named secrets"
    assert audited.inventory["exclusions_and_caps"]["sensitive_files_hashed"] is False


@requirement
@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "prod.env",
        "server.pem",
        "private.key",
        "cert.pfx",
        "cert.p12",
        "id_rsa",
        "id_rsa.pub",
        "my_credentials.json",
        "client_secret.json",
        "passwords.txt",
        "api_token.txt",
        "accounts.json",
        "accounts.ini",
        "accounts_live.cfg",
        "vault.kdbx",
        "TOKEN.TXT",
        "Secret.yaml",
    ],
)
def test_sensitive_name_patterns(name: str) -> None:
    assert audit._sensitive_reason(name, ()) is not None


@requirement
@pytest.mark.parametrize(
    "name", ["config.py", "run_bot.py", "ticks.csv", "account_history.csv", "keyboard.py"]
)
def test_ordinary_names_are_not_sensitive(name: str) -> None:
    assert audit._sensitive_reason(name, ("engines",)) is None


@requirement
@read_only
def test_audit_log_ends_with_the_read_only_check(audited: Audited) -> None:
    lines = (audited.out / "audit_log.txt").read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "READ-ONLY CHECK: no file under root was opened for writing"
    assert lines[-1] == audit.READ_ONLY_OK_LINE
    text = "\n".join(lines)
    total_bytes = sum(len(data) for data in VISIBLE.values()) + NON_UTF8_BYTES
    assert f"files listed in manifest.jsonl: {len(EXPECTED_PATHS)} ({total_bytes} bytes)" in text
    hashed = len(EXPECTED_PATHS) - len(SENSITIVE_PATHS)
    assert f"files hashed (SHA-256): {hashed}; not hashed: {len(SENSITIVE_PATHS)}" in text
    assert f"sensitive-named files: {len(SENSITIVE_PATHS)} (not opened)" in text
    assert "links recorded and not followed: 3" in text
    assert "runtime guard: active" in text
    assert "0 write; other blocked events: 0" in text
    assert "processes.json: written" in text


@requirement
def test_processes_json_on_this_platform_has_no_command_lines(audited: Audited) -> None:
    document = json.loads((audited.out / "processes.json").read_text(encoding="utf-8"))
    assert set(document) == {"tool", "collected_utc", "platform", "note", "processes", "scheduled_tasks"}
    processes = document["processes"]
    if not sys.platform.startswith("linux"):
        pytest.skip(
            "BLOCKED: B-001 - the real process query of this platform cannot be verified in this session"
        )
    assert processes["status"] == "ok"
    assert processes["total"] >= 1
    assert processes["total"] == len(processes["relevant"]) + processes["not_listed"]
    for item in processes["relevant"]:
        assert set(item) == {"image", "pid", "memory_kb", "hints"}
    assert document["scheduled_tasks"]["status"] == "not_collected"


# ---------------------------------------------------------------------------------------------
# Read-only behaviour (acceptance S00-ACC-06)
# ---------------------------------------------------------------------------------------------


@requirement
@read_only
def test_audited_tree_is_byte_identical_with_unchanged_metadata(audited: Audited) -> None:
    assert audited.code == 0
    assert len(audited.before) > len(VISIBLE) + len(EXCLUDED)  # files, directories and links were captured
    assert set(audited.after) == set(audited.before)  # no new or missing file or directory
    for path, fields in audited.before.items():
        assert audited.after[path] == fields, path  # mode, size, mtime_ns, ctime_ns, inode, content
    assert sorted(path.name for path in audited.root.parent.iterdir()) == ["audit_out", "prod"]


@requirement
@read_only
def test_audit_succeeds_on_a_tree_without_write_permission(tree: Path, tmp_path: Path) -> None:
    entries = [tree, *sorted(tree.rglob("*"))]
    try:
        for entry in entries:
            if not entry.is_symlink():
                entry.chmod(0o555 if entry.is_dir() else 0o444)
        before = snapshot(tree)
        code, _out, _err = run_audit(tree, tmp_path / "out", "--count-rows", "--processes")
        after = snapshot(tree)
    finally:
        for entry in entries:
            if not entry.is_symlink():
                entry.chmod(0o755 if entry.is_dir() else 0o644)
    assert code == 0
    assert after == before
    assert (
        load_manifest(tmp_path / "out" / "manifest.jsonl")["config.py"]["sha256"]
        == hashlib.sha256(VISIBLE["config.py"]).hexdigest()
    )
    assert (tmp_path / "out" / "audit_log.txt").read_text().splitlines()[-1] == audit.READ_ONLY_OK_LINE


class _Recorder:
    """Independent interpreter audit hook of the test: records what the process does under a root."""

    WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    MUTATIONS = frozenset(
        {
            "os.remove",
            "os.rename",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.chown",
            "os.utime",
            "os.truncate",
            "os.symlink",
            "os.link",
            "os.chdir",
            "shutil.copyfile",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.copytree",
            "shutil.rmtree",
            "shutil.move",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
        }
    )

    def __init__(self) -> None:
        self.installed = False
        self.root = ""
        self.reads = 0
        self.writes: list[str] = []
        self.mutations: list[str] = []
        self.other: list[str] = []

    def inside(self, candidate: object) -> bool:
        if not self.root or not isinstance(candidate, (str, bytes, os.PathLike)):
            return False
        # The tool addresses files by extended-length paths on Windows; without stripping that
        # prefix the recorder would see neither its reads nor - worse - a write under the root.
        full = os.path.abspath(plain(candidate))
        return full == self.root or full.startswith(self.root + os.sep)

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        if not self.root:
            return
        if event == "open":
            path, mode, flags = args[0], args[1], args[2]
            if self.inside(path):
                writing = (isinstance(flags, int) and flags & self.WRITE_FLAGS) or (
                    isinstance(mode, str) and set(mode) & set("wax+")
                )
                if writing:
                    self.writes.append(f"{mode!r} {flags!r}")
                elif isinstance(mode, str):
                    self.reads += 1
        elif event in self.MUTATIONS:
            if any(self.inside(arg) for arg in args):
                self.mutations.append(event)
        elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork") or (
            event.startswith(("socket.", "urllib.", "http.client.")) and event != "socket.gethostname"
        ):
            self.other.append(event)


_RECORDER = _Recorder()


@contextlib.contextmanager
def recording(root: Path) -> Iterator[_Recorder]:
    if not _RECORDER.installed:
        sys.addaudithook(_RECORDER)
        _RECORDER.installed = True
    _RECORDER.reads, _RECORDER.writes, _RECORDER.mutations, _RECORDER.other = 0, [], [], []
    _RECORDER.root = os.path.abspath(root)
    try:
        yield _RECORDER
    finally:
        _RECORDER.root = ""


@requirement
@read_only
def test_interpreter_sees_only_read_opens_under_root(tree: Path, tmp_path: Path) -> None:
    """Independent of the tool's own guard: the interpreter's audit events show no write, rename,
    delete, chmod, utime, chdir, network use or process start while a full audit runs."""
    with recording(tree) as seen:
        code, _out, _err = run_audit(
            tree, tmp_path / "out", "--count-rows", "--processes", "--hash-sensitive"
        )
    assert code == 0
    assert seen.writes == []
    assert seen.mutations == []
    # With --processes the tool lists the running processes: on Linux by reading /proc, on Windows
    # through its two fixed read-only queries (tasklist, schtasks), which the interpreter reports as
    # process starts.  That the tool's own guard admits no other process start is tested separately.
    process_queries = {"subprocess.Popen"} if os.name == "nt" else set()
    assert set(seen.other) <= process_queries
    assert seen.reads >= len(EXPECTED_PATHS)  # the recorder did observe the audit reading the tree
    # the recorder is not blind: it does see a write under the root made by the test itself
    with recording(tree) as seen:
        (tree / "written_by_the_test.txt").write_text("SYNTHETIC FIXTURE", newline="\n")
        os.remove(tree / "written_by_the_test.txt")
    assert len(seen.writes) == 1
    assert seen.mutations == ["os.remove"]


@requirement
def test_access_times_are_not_updated_where_the_platform_allows(tree: Path, tmp_path: Path) -> None:
    if not getattr(os, "O_NOATIME", 0):
        pytest.skip(
            "BLOCKED: B-001 - this platform has no O_NOATIME; access-time behaviour needs the owner's machine"
        )
    files = [tree / rel for rel in VISIBLE if rel not in SENSITIVE_PATHS]
    old_access = FIXED_MTIME_NS - 10_000_000_000
    for path in files:
        # an access time older than the modification time: an ordinary read would move it ('relatime')
        os.utime(path, ns=(old_access, FIXED_MTIME_NS))
    before = {str(path): os.lstat(path).st_atime_ns for path in files}
    code, _out, _err = run_audit(tree, tmp_path / "out", "--count-rows")
    assert code == 0
    assert {str(path): os.lstat(path).st_atime_ns for path in files} == before
    # the check above only means something where this file system records access times at all
    probe = files[0]
    probe.read_bytes()
    if os.lstat(probe).st_atime_ns == before[str(probe)]:
        pytest.skip(
            "BLOCKED: B-001 - this file system does not record access times (noatime); nothing to observe"
        )


@requirement
@read_only
def test_guard_blocks_every_attempt_to_change_the_root(tree: Path, tmp_path: Path) -> None:
    target = tree / "config.py"
    before = snapshot(tree)
    with audit._guarded(str(tree)) as guard:
        for mode in ("w", "ab", "r+b", "x"):
            with pytest.raises(audit.ReadOnlyViolation):
                (tree / "new_file.txt" if mode in "wx" else target).open(mode)
        with pytest.raises(audit.ReadOnlyViolation):
            os.open(target, os.O_WRONLY)
        with pytest.raises(audit.ReadOnlyViolation):
            os.remove(target)
        with pytest.raises(audit.ReadOnlyViolation):
            os.rename(target, tmp_path / "moved.py")
        with pytest.raises(audit.ReadOnlyViolation):
            os.replace(tmp_path, tree / "replaced")
        with pytest.raises(audit.ReadOnlyViolation):
            os.chmod(target, 0o600)
        with pytest.raises(audit.ReadOnlyViolation):
            os.utime(target, ns=(1, 1))
        with pytest.raises(audit.ReadOnlyViolation):
            os.truncate(target, 0)
        with pytest.raises(audit.ReadOnlyViolation):
            os.mkdir(tree / "new_directory")
        with pytest.raises(audit.ReadOnlyViolation):
            os.symlink("config.py", tree / "new_link")
        with pytest.raises(audit.ReadOnlyViolation):
            shutil.rmtree(tree / "engines")
        with pytest.raises(audit.ReadOnlyViolation):
            shutil.copyfile(target, tree / "copy.py")
        with pytest.raises(audit.ReadOnlyViolation):
            os.chdir(tree)
        with pytest.raises(audit.ReadOnlyViolation):
            audit._create_new_file(str(tree / "created.txt"), str(tree))
        with open(target, "rb") as handle:  # reading stays possible
            assert handle.read() == VISIBLE["config.py"]
        (tmp_path / "outside.txt").write_text("allowed: not under the root", newline="\n")
        assert len(guard.write_violations) == 16
        assert guard.other_violations == []
    assert snapshot(tree) == before
    assert (tmp_path / "outside.txt").exists()


@requirement
@read_only
def test_guard_blocks_network_and_process_starts(tree: Path) -> None:
    with audit._guarded(str(tree)) as guard:
        with pytest.raises(audit.ReadOnlyViolation):
            socket.socket()
        with pytest.raises(audit.ReadOnlyViolation):
            socket.getaddrinfo("localhost", 80)
        with pytest.raises(audit.ReadOnlyViolation):
            subprocess.run([sys.executable, "-c", "pass"], check=False)
        with pytest.raises(audit.ReadOnlyViolation):
            os.system("true")
        assert socket.gethostname()  # a local lookup, used for the host-name hash
        assert guard.write_violations == []
        assert len(guard.other_violations) == 4


@requirement
@read_only
def test_guard_admits_only_the_registered_process_queries(tree: Path) -> None:
    """The interpreter reports a process start as a list on POSIX and as one command line on Windows."""
    tasklist = ["C:\\Windows\\System32\\tasklist.exe", "/FO", "CSV", "/NH"]
    with audit._guarded(str(tree)) as guard:
        guard.allowed_commands.add(subprocess.list2cmdline(tasklist))
        guard.handle("subprocess.Popen", (tasklist[0], list(tasklist), None, None))  # POSIX form
        guard.handle(
            "subprocess.Popen", (tasklist[0], subprocess.list2cmdline(tasklist), None, None)
        )  # Windows
        assert guard.other_violations == []
        for command in (
            [*tasklist, "/V"],
            ["tasklist.exe", "/FO", "CSV", "/NH"],  # not the absolute system path
            "C:\\Windows\\System32\\tasklist.exe /FO CSV /NH & del D:\\ftmo_eurusd",
            ["C:\\Windows\\System32\\schtasks.exe", "/Delete", "/TN", "x"],
            None,
        ):
            with pytest.raises(audit.ReadOnlyViolation):
                guard.handle("subprocess.Popen", ("ignored", command, None, None))
        assert len(guard.other_violations) == 5


@requirement
@read_only
def test_guard_is_inactive_outside_a_scan(tree: Path, tmp_path: Path) -> None:
    code, _out, _err = run_audit(tree, tmp_path / "out")
    assert code == 0
    assert audit._GUARD.active is False
    marker = tree / "written_after_the_audit.txt"
    marker.write_text("SYNTHETIC FIXTURE", newline="\n")  # the test itself may change its own fixture again
    marker.unlink()
    with contextlib.closing(socket.socket()) as probe:
        assert probe.fileno() >= 0


REFUSED_OUTPUTS = {
    "inside_root": lambda root, tmp: root / "audit_out",
    "deep_inside_root": lambda root, tmp: root / "data" / "new" / "out",
    "equal_to_root": lambda root, tmp: root,
    "root_inside_out": lambda root, tmp: tmp,
    "through_a_link_into_root": lambda root, tmp: tmp / "alias_into_root" / "out",
    "relative_spelling_of_root": lambda root, tmp: root / ".." / root.name / "out",
}


@requirement
@read_only
@pytest.mark.parametrize("case", sorted(REFUSED_OUTPUTS))
def test_output_overlapping_the_root_is_refused_and_nothing_is_written(
    tree: Path, tmp_path: Path, case: str
) -> None:
    os.symlink(tree / "data", tmp_path / "alias_into_root", target_is_directory=True)
    out = REFUSED_OUTPUTS[case](tree, tmp_path)
    before = snapshot(tmp_path)
    code, stdout, stderr = run_audit(tree, out)
    assert code == 2
    assert "refused" in stderr
    assert stdout == ""
    assert snapshot(tmp_path) == before


@requirement
@read_only
def test_non_empty_or_non_directory_output_is_refused(tree: Path, tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("existing owner file", newline="\n")
    a_file = tmp_path / "a_file"
    a_file.write_text("existing owner file", newline="\n")
    before = snapshot(tmp_path)
    for out in (occupied, a_file):
        code, _stdout, stderr = run_audit(tree, out)
        assert code == 2
        assert "refused" in stderr
    assert snapshot(tmp_path) == before
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _stdout, _stderr = run_audit(tree, empty)  # an existing empty directory is accepted
    assert code == 0
    assert (empty / "inventory.json").is_file()


@requirement
@pytest.mark.parametrize(
    "argv",
    [
        ["audit", "--root", "{missing}", "--out", "{out}"],
        ["audit", "--root", "{file}", "--out", "{out}"],
        ["audit", "--root", "{root}"],
        ["audit", "--root", "{root}", "--out", "{out}", "--max-hash-bytes", "-1"],
        ["audit", "--root", "{root}", "--out", "{out}", "--max-scan-bytes", "-5"],
        ["audit", "--root", "{root}", "--out", "{out}", "--no-such-option"],
        ["manifest", "--root", "{root}", "--out", "{root}/manifest.jsonl"],
        ["manifest", "--root", "{root}", "--out", "{file}"],
        ["manifest", "--root", "{missing}", "--out", "{out}"],
        ["compare", "{missing}", "{file}"],
        ["compare", "{file}", "{file}"],
        ["unknown-command"],
        [],
    ],
    ids=[
        "audit-missing-root",
        "audit-root-is-a-file",
        "audit-without-out",
        "audit-negative-max-hash-bytes",
        "audit-negative-max-scan-bytes",
        "audit-unknown-option",
        "manifest-out-inside-root",
        "manifest-out-exists",
        "manifest-missing-root",
        "compare-missing-file",
        "compare-not-a-manifest",
        "unknown-command",
        "no-command",
    ],
)
def test_refused_arguments_exit_2_and_change_nothing(tree: Path, tmp_path: Path, argv: list[str]) -> None:
    (tmp_path / "a_file").write_text("not a manifest\n", newline="\n")
    values = {
        "root": str(tree),
        "out": str(tmp_path / "out"),
        "missing": str(tmp_path / "missing"),
        "file": str(tmp_path / "a_file"),
    }
    before = snapshot(tmp_path)
    code, stdout, _stderr = run_main(*[part.format(**values) for part in argv])
    assert code == 2
    assert stdout == ""
    assert snapshot(tmp_path) == before


@requirement
@read_only
def test_tool_located_inside_the_root_is_refused(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(audit, "__file__", str(tree / "tools" / "gie_baseline_audit.py"))
        code, _stdout, stderr = run_audit(tree, tmp_path / "out")
    assert code == 2
    assert "inside --root" in stderr
    with monkeypatch.context() as patch:
        patch.setattr(audit, "__file__", str(tree / "tools" / "gie_baseline_audit.py"))
        patch.delitem(sys.modules, audit.__name__)
        with pytest.raises(audit.AuditRefused, match="must not be located inside --root"):
            audit._refuse_if_tool_inside(str(tree))
    # a module that was imported from the audited tree means code of that tree already ran: refuse
    planted = types.ModuleType("synthetic_fixture_module")
    planted.__file__ = str(tree / "config.py")
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "synthetic_fixture_module", planted)
        code, _stdout, stderr = run_audit(tree, tmp_path / "out")
    assert code == 2
    assert "loaded from inside --root" in stderr
    # the same holds for the interpreter itself, e.g. the virtual environment of the audited system
    for attribute, location in (("executable", tree / "venv" / "bin" / "python"), ("prefix", tree / "venv")):
        with monkeypatch.context() as patch:
            patch.setattr(sys, attribute, str(location))
            code, _stdout, stderr = run_audit(tree, tmp_path / "out")
            manifest_code, _stdout, _stderr = run_main(
                "manifest", "--root", str(tree), "--out", str(tmp_path / "m.jsonl")
            )
        assert code == 2
        assert manifest_code == 2
        assert "running this tool is inside --root" in stderr
    assert snapshot(tmp_path) == before


@requirement
def test_version_and_help_exit_0() -> None:
    code, stdout, _stderr = run_main("--version")
    assert code == 0
    assert stdout.strip() == "gie_baseline_audit " + audit.TOOL_VERSION
    code, stdout, _stderr = run_main("audit", "--help")
    assert code == 0
    assert "--root" in stdout


# ---------------------------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------------------------


@requirement
def test_two_runs_over_the_same_tree_are_identical(tree: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "run1", tmp_path / "run2"
    assert run_audit(tree, first, "--count-rows")[0] == 0
    assert run_audit(tree, second, "--count-rows")[0] == 0
    assert (first / "manifest.jsonl").read_bytes() == (second / "manifest.jsonl").read_bytes()
    # processes.json is only written on request
    assert sorted(path.name for path in first.iterdir()) == [
        "audit_log.txt",
        "inventory.json",
        "manifest.jsonl",
    ]
    assert "processes.json: not requested" in (first / "audit_log.txt").read_text(encoding="utf-8")
    one, two = load_inventory(first), load_inventory(second)
    assert one["tool"]["argv"] != two["tool"]["argv"]  # the --out value differs
    assert set(one["run"]) == {
        "started_utc",
        "finished_utc",
        "duration_seconds",
        "root",
        "out",
        "disk_free_bytes",
        "note",
    }
    for document in (one, two):
        del document["run"]  # the clearly named volatile block: timestamps, paths, free disk space
        del document["tool"]["argv"]
    assert one == two
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


# ---------------------------------------------------------------------------------------------
# manifest / compare
# ---------------------------------------------------------------------------------------------


@requirement
def test_build_manifest_equals_the_audit_manifest(audited: Audited) -> None:
    records = audit.build_manifest(audited.root)
    assert [record["path"] for record in records] == EXPECTED_PATHS
    assert {record["path"]: record for record in records} == audited.manifest
    assert audit.build_manifest(str(audited.root), None) == records
    with pytest.raises(audit.AuditRefused):
        audit.build_manifest(audited.root / "does_not_exist")
    with pytest.raises(audit.AuditRefused):
        audit.build_manifest(audited.root / "config.py")
    assert audit._GUARD.active is False


@requirement
def test_manifest_command_with_include_writes_a_subset(tree: Path, tmp_path: Path) -> None:
    target = tmp_path / "manifests" / "subset.jsonl"
    code, _stdout, _stderr = run_main(
        "manifest",
        "--root",
        str(tree),
        "--out",
        str(target),
        "--include",
        "engines/*",
        "--include",
        "config.py",
        "--progress-every",
        "0",
    )
    assert code == 0
    subset = audit.read_manifest(target)
    expected = sorted(rel for rel in VISIBLE if rel.startswith("engines/") or rel == "config.py")
    assert [record["path"] for record in subset] == expected
    assert subset == audit.build_manifest(tree, ["engines/*", "config.py"])
    full = {record["path"]: record for record in audit.build_manifest(tree)}
    for record in subset:
        assert record == full[record["path"]]  # same record format as the full manifest
        assert record["sha256"] == hashlib.sha256(VISIBLE[record["path"]]).hexdigest()
    assert [r["path"] for r in audit.build_manifest(tree, ["data"])] == sorted(
        r for r in VISIBLE if r.startswith("data/")
    )
    assert [r["path"] for r in audit.build_manifest(tree, ["*.mq5"])] == ["engines/gold_breakout/Expert.mq5"]
    assert audit.build_manifest(tree, ["no/such/path"]) == []
    # the manifest file is never overwritten
    before = target.read_bytes()
    code, _stdout, stderr = run_main("manifest", "--root", str(tree), "--out", str(target))
    assert code == 2
    assert "never overwritten" in stderr
    assert target.read_bytes() == before


@requirement
def test_manifest_options(tree: Path) -> None:
    everything = {
        record["path"] for record in audit.build_manifest(tree, excluded_dirs=(), detect_environments=False)
    }
    assert {"venv/lib/site-packages/leak.py", ".git/HEAD", "custom_runtime/Lib/thing.py"} <= everything
    hashed = {record["path"]: record for record in audit.build_manifest(tree, hash_sensitive=True)}
    assert hashed[".env"]["sha256"] == hashlib.sha256(VISIBLE[".env"]).hexdigest()
    assert hashed[".env"]["sensitive"] is True
    assert "skipped" not in hashed[".env"]
    capped = {record["path"]: record for record in audit.build_manifest(tree, max_hash_bytes=100)}
    for rel, content in VISIBLE.items():
        if rel in SENSITIVE_PATHS:
            continue
        if len(content) > 100:
            assert capped[rel]["sha256"] is None
            assert capped[rel]["skipped"] == "max_hash_bytes"
        else:
            assert capped[rel]["sha256"] == hashlib.sha256(content).hexdigest()
    without_engines = {record["path"] for record in audit.build_manifest(tree, excluded_dirs=("engines",))}
    assert not {path for path in without_engines if path.startswith("engines/")}
    assert ".git/HEAD" in without_engines  # the default names were replaced, not extended


@requirement
def test_write_and_read_manifest_round_trip_and_never_overwrite(tree: Path, tmp_path: Path) -> None:
    records = audit.build_manifest(tree)
    target = tmp_path / "manifest.jsonl"
    assert audit.write_manifest(list(reversed(records)), target) == len(records)
    assert audit.read_manifest(target) == records  # written sorted by path
    assert audit.read_manifest(str(target)) == records
    with pytest.raises(FileExistsError):
        audit.write_manifest(records, target)
    swapped = tmp_path / "swapped.jsonl"
    assert audit.write_manifest(str(swapped), records) == len(records)  # (path, records) is accepted too
    assert swapped.read_bytes() == target.read_bytes()
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"path": "a", "size": 1}\n[1, 2]\n', newline="\n")
    with pytest.raises(ValueError, match="line 2"):
        audit.read_manifest(broken)


@requirement
@read_only
def test_compare_detects_changed_added_removed_and_mtime_only(tree: Path, tmp_path: Path) -> None:
    before = audit.build_manifest(tree)
    assert audit.compare_manifests(before, audit.build_manifest(tree))["exit_code"] == 0

    changed = tree / "run_bot.py"
    data = bytearray(changed.read_bytes())
    data[10] ^= 0x01  # one byte, same size
    changed.write_bytes(bytes(data))
    os.utime(changed, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))  # even with the old modification time
    (tree / "data" / "ticks_xauusd.csv").write_bytes(TICKS_CSV.encode() + b"one more row\n")
    (tree / "engines" / "added_engine.py").write_text("# SYNTHETIC FIXTURE\n", newline="\n")
    (tree / "models" / "net.onnx").unlink()
    os.utime(tree / "README.md", ns=(FIXED_MTIME_NS + 500, FIXED_MTIME_NS + 500))
    os.utime(tree / ".env", ns=(FIXED_MTIME_NS + 700, FIXED_MTIME_NS + 700))
    after = audit.build_manifest(tree)

    result = audit.compare_manifests(before, after)
    assert result["added"] == ["engines/added_engine.py"]
    assert result["removed"] == ["models/net.onnx"]
    assert result["changed"] == [
        {"path": ".env", "reasons": ["mtime_without_sha256"]},
        {"path": "data/ticks_xauusd.csv", "reasons": ["size", "sha256"]},
        {"path": "run_bot.py", "reasons": ["sha256"]},
    ]
    assert result["mtime_only"] == ["README.md"]
    assert result["unverified"] == sorted(SENSITIVE_PATHS - {".env"})
    assert result["content_identical"] is False
    assert result["exit_code"] == 1
    assert result["summary"] == {
        "files_a": len(before),
        "files_b": len(after),
        "added": 1,
        "removed": 1,
        "changed": 3,
        "mtime_only": 1,
        "unverified": len(SENSITIVE_PATHS) - 1,
    }

    first, second = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    audit.write_manifest(before, first)
    audit.write_manifest(after, second)
    assert audit.compare_manifests(first, str(second)) == result
    code, stdout, _stderr = run_main("compare", str(first), str(second))
    assert code == 1
    assert json.loads(stdout) == result


@requirement
def test_compare_exit_codes_for_identical_and_mtime_only_differences(tree: Path, tmp_path: Path) -> None:
    first, second, third = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    audit.write_manifest(audit.build_manifest(tree, hash_sensitive=True), first)
    audit.write_manifest(audit.build_manifest(tree, hash_sensitive=True), second)
    code, stdout, _stderr = run_main("compare", str(first), str(second))
    assert code == 0
    identical = json.loads(stdout)
    assert identical["content_identical"] is True
    assert identical["unverified"] == []
    assert identical["mtime_only"] == []
    assert run_main("compare", str(first), str(second), "--strict-mtime")[0] == 0

    os.utime(tree / "config.py", ns=(FIXED_MTIME_NS + 100, FIXED_MTIME_NS + 100))
    audit.write_manifest(audit.build_manifest(tree, hash_sensitive=True), third)
    code, stdout, _stderr = run_main("compare", str(first), str(third))
    assert code == 0  # reported, but content is identical
    report = json.loads(stdout)
    assert report["mtime_only"] == ["config.py"]
    assert report["changed"] == []
    assert report["content_identical"] is True
    code, stdout, _stderr = run_main("compare", str(first), str(third), "--strict-mtime")
    assert code == 1
    assert json.loads(stdout)["mtime_only"] == ["config.py"]


# ---------------------------------------------------------------------------------------------
# Options, caps and robustness
# ---------------------------------------------------------------------------------------------


@requirement
def test_hash_sensitive_hashes_and_scans_but_still_emits_no_secret(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    code, stdout, stderr = run_audit(tree, out, "--hash-sensitive", "--count-rows")
    assert code == 0
    manifest = load_manifest(out / "manifest.jsonl")
    for rel in SENSITIVE_PATHS:
        assert manifest[rel]["sha256"] == hashlib.sha256(VISIBLE[rel]).hexdigest()
        assert manifest[rel]["sensitive"] is True
    inventory = load_inventory(out)
    section = inventory["exclusions_and_caps"]
    assert section["sensitive_files_hashed"] is True
    assert {item["path"] for item in section["sensitive_files"] if item["hashed"]} == SENSITIVE_PATHS
    found = {(item["path"], item["rule"]) for item in inventory["credential_scan"]["findings"]}
    assert ("secrets/accounts.json", "credential_assignment_literal") in found
    files = {item["path"]: item for item in inventory["data"]["files"]}
    # nothing is extracted from a sensitive file, although its first line looks like column names
    assert VISIBLE["secrets/prod.csv"].startswith(b"user,value\n")
    assert set(files["secrets/prod.csv"]) == {"path", "size", "mtime_ns", "mtime_utc"}
    for blob in [*(path.read_bytes() for path in out.iterdir()), stdout.encode(), stderr.encode()]:
        for value in FORBIDDEN_OUTPUT_STRINGS[:3]:
            assert value.encode() not in blob


@requirement
def test_every_cap_that_is_applied_is_recorded(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    code, _stdout, _stderr = run_audit(
        tree,
        out,
        "--max-hash-bytes",
        "100",
        "--max-line-count-bytes",
        "60",
        "--max-scan-bytes",
        "120",
        "--max-hits-per-file",
        "1",
    )
    assert code == 0
    inventory = load_inventory(out)
    section = inventory["exclusions_and_caps"]
    assert section["caps"]["max_hash_bytes"] == 100
    assert section["caps"]["max_line_count_bytes"] == 60
    assert section["caps"]["max_scan_bytes"] == 120
    assert section["caps"]["config_file_scan_bytes"] == 120
    manifest = load_manifest(out / "manifest.jsonl")
    over = sorted(rel for rel in EXPECTED_PATHS if rel not in SENSITIVE_PATHS and manifest[rel]["size"] > 100)
    assert over
    for rel in over:
        assert manifest[rel]["skipped"] == "max_hash_bytes"
        assert manifest[rel]["sha256"] is None
    assert section["not_hashed_over_max_hash_bytes"] == over
    assert section["truncated_lists"] == {}
    # line counts: only files up to 60 bytes are counted, the others are named
    counted = [
        rel
        for rel, data in VISIBLE.items()
        if rel.endswith(".py") and len(data) <= 60 and b"\x00" not in data
    ]
    assert inventory["languages"]["Python"]["files_with_line_count"] == len(counted)
    assert 0 < len(counted) < inventory["languages"]["Python"]["files"]
    assert "run_bot.py" in section["line_count_skipped_over_cap"]
    assert "tests/conftest.py" not in section["line_count_skipped_over_cap"]
    # scanning stops at 120 bytes: the order_send call near the end of run_bot.py is not reached
    assert "run_bot.py" in section["scan_truncated_at_max_scan_bytes"]
    assert "tests/conftest.py" not in section["scan_truncated_at_max_scan_bytes"]
    assert RUN_BOT.index("order_send") > 120
    assert inventory["broker_interfaces"]["rules"]["order_send"] == []
    # one location per rule and file, and the cap is reported with the real number of hits
    capped = {(item["path"], item["rule"]): item for item in section["hit_lists_capped_per_file"]}
    assert capped[("tests/legacy_test.py", "unittest_usage")] == {
        "path": "tests/legacy_test.py",
        "rule": "unittest_usage",
        "hits": 2,
        "listed": 1,
    }
    assert inventory["tests"]["unittest_usage"] == [{"line": 2, "path": "tests/legacy_test.py"}]

    # long lists are cut at --max-list-entries and the cut is reported with the real total
    short = tmp_path / "short_lists"
    assert run_audit(tree, short, "--max-list-entries", "2", "--max-hash-bytes", "100")[0] == 0
    inventory = load_inventory(short)
    section = inventory["exclusions_and_caps"]
    truncated = section["truncated_lists"]
    assert truncated["exclusions_and_caps.not_hashed"] == {"total": len(over), "listed": 2}
    assert section["not_hashed_over_max_hash_bytes"] == over[:2]
    assert truncated["data.files"] == {"total": inventory["data"]["file_count"], "listed": 2}
    assert len(inventory["data"]["files"]) == 2
    assert truncated["exclusions_and_caps.links_not_followed"] == {"total": 3, "listed": 2}
    assert truncated["exclusions_and_caps.sensitive_files"] == {"total": len(SENSITIVE_PATHS), "listed": 2}
    assert section["sensitive_file_count"] == len(SENSITIVE_PATHS)
    assert len(load_manifest(short / "manifest.jsonl")) == len(EXPECTED_PATHS)  # the manifest is never cut


@requirement
def test_configuration_like_files_are_scanned_up_to_one_mebibyte(tmp_path: Path) -> None:
    root = tmp_path / "prod"
    root.mkdir()
    padding = "# SYNTHETIC FIXTURE padding line\n" * 34000  # a little over 1 MiB
    assert len(padding) > 1 << 20
    (root / "big_export.json").write_text('"mt5.initialize()"\n' + padding + '"order_send()"\n', newline="\n")
    (root / "big_module.py").write_text("mt5.initialize()\n" + padding + "order_send()\n", newline="\n")
    out = tmp_path / "out"
    assert run_audit(root, out)[0] == 0
    inventory = load_inventory(out)
    rules = inventory["broker_interfaces"]["rules"]
    last_line = padding.count("\n") + 2
    assert rules["mt5_initialize"] == [
        {"line": 1, "path": "big_export.json"},
        {"line": 1, "path": "big_module.py"},
    ]
    assert rules["order_send"] == [{"line": last_line, "path": "big_module.py"}]  # source: scanned to the end
    section = inventory["exclusions_and_caps"]
    assert section["scan_truncated_at_max_scan_bytes"] == [
        "big_export.json"
    ]  # and the truncation is recorded
    assert section["caps"]["config_file_scan_bytes"] == 1 << 20
    assert (
        inventory["languages"]["JSON"]["lines"] == last_line
    )  # line counts are not affected by the scan cap


@requirement
def test_trigger_prefilter_never_changes_the_scan_result() -> None:
    """A rule is skipped when none of its trigger words occurs; that must never lose a hit."""
    rules = (
        *audit.BROKER_RULES,
        *audit.CREDENTIAL_RULES,
        *audit.CREDENTIAL_CONFIG_RULES,
        *audit.PYTHON_RULES,
        *audit.XML_RULES,
    )
    assert {rule.name for rule in rules} == set(audit._TRIGGERS)
    variants = "\n".join(
        [
            "import MetaTrader5",
            "MT5.INITIALIZE()",
            "Mt5.Login(12345)",
            "ZMQ",
            "PyZMQ",
            "DARWINEX",
            "DwX_x",
            "cAlgo",
            "QuickFIX",
            "FIX.4.4",
            "fix API",
            "OANDA",
            "wss://host.invalid",
            "WS://host.invalid",
            "TeleBot",
            "SMTP_SSL(",
            "SendMail(",
            "DiscordApp",
            "WSAStartup",
            "System.Net.Sockets",
            "import aiohttp",
            "urllib.request",
            "WebRequest(",
            "PASSWORD = 'FAKE-NOT-A-SECRET'",
            "Api-Key: 'FAKE-NOT-A-SECRET'",
            "APIKEY = 'FAKE-NOT-A-SECRET'",
            "Account = 12345678",
            "PWD='FAKE-NOT-A-SECRET'",
            "Authorization: Bearer FAKE-NOT-A-SECRET-0000",
            "-----BEGIN RSA " + "PRIVATE KEY-----",
            "ftp://fakeuser:FAKE-NOT-A-SECRET@host.invalid/x",
            "IBAPI",
            "ib_async",
            "copy_ticks_range(",
            "OrderSendAsync(",
            "CTrade t;",
            "initialize(login=1)",
            "token: FAKE-NOT-A-SECRET",
            "secret=FAKE-NOT-A-SECRET",
            "export LOGIN=FAKE-NOT-A-SECRET",
            "<service><executable>",
            "unittest.TestCase",
            "import ccxt",
        ]
    )
    corpus = [
        *(data.decode("utf-8", "replace") for data in VISIBLE.values()),
        EXPERT_MQ5,
        TOOL_SOURCE,
        variants,
    ]
    corpus.append(Path(__file__).read_text(encoding="utf-8"))
    hits = 0
    for text in corpus:
        fast = audit._apply_rules(text, rules, 10_000)
        slow = audit._apply_rules(text, rules, 10_000, prefilter=False)
        assert fast == slow
        hits += sum(total for _lines, total in slow.values())
    assert hits > 200
    found = audit._apply_rules(variants, rules, 10_000)
    not_in_variants = {
        "python_main_guard",
        "test_function",
        "pytest_usage",
        "windows_scheduled_task_xml",
        "order_send",
        "order_check",
        "positions_get",
        "copy_rates",
        "symbol_info_tick",
    }
    assert set(found) == set(audit._TRIGGERS) - not_in_variants
    assert audit._apply_rules("nothing of interest here\n" * 100, rules, 100) == {}


@requirement
def test_exclude_options(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    code, _stdout, _stderr = run_audit(tree, out, "--exclude-dir", "engines", "--exclude-dir", "notebooks")
    assert code == 0
    manifest = load_manifest(out / "manifest.jsonl")
    assert not [path for path in manifest if path.startswith(("engines/", "notebooks/"))]
    excluded = {
        item["path"]: item for item in load_inventory(out)["exclusions_and_caps"]["excluded_directories"]
    }
    assert excluded["engines"]["reason"] == "--exclude-dir"
    engine_files = [data for rel, data in VISIBLE.items() if rel.startswith("engines/")]
    assert (excluded["engines"]["files"], excluded["engines"]["bytes"]) == (
        len(engine_files),
        sum(len(data) for data in engine_files),
    )

    everything = tmp_path / "everything"
    code, _stdout, _stderr = run_audit(tree, everything, "--no-default-excludes", "--include-venvs")
    assert code == 0
    manifest = load_manifest(everything / "manifest.jsonl")
    assert sorted(manifest) == sorted([*VISIBLE, *EXCLUDED, *NON_UTF8_FILES])
    inventory = load_inventory(everything)
    assert inventory["exclusions_and_caps"]["excluded_directories"] == []
    assert inventory["version_control"]["branch"] == "main"
    assert {item["path"] for item in inventory["python_environments"]["environments"]} == {
        "venv",
        "custom_runtime",
    }
    # .git/config holds a (fake) remote URL with credentials: hashed like any file, never emitted
    for path in everything.iterdir():
        assert MARKER.encode() not in path.read_bytes()


@requirement
def test_csv_headers_can_be_switched_off_and_rows_are_optional(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_audit(tree, out, "--no-csv-headers")[0] == 0
    files = {item["path"]: item for item in load_inventory(out)["data"]["files"]}
    for item in files.values():
        assert set(item) == {"path", "size", "mtime_ns", "mtime_utc"}
    with_headers = tmp_path / "with_headers"
    assert run_audit(tree, with_headers)[0] == 0
    ticks = {item["path"]: item for item in load_inventory(with_headers)["data"]["files"]}[
        "data/ticks_xauusd.csv"
    ]
    assert ticks["columns"] == ["time", "bid", "ask", "flags"]
    assert "row_count" not in ticks  # only with --count-rows


@requirement
@pytest.mark.parametrize(
    ("first_line", "extension", "expected"),
    [
        (b"time,bid,ask\n1,2,3\n", ".csv", ["time", "bid", "ask"]),
        (b",open,close\n0,1,2\n", ".csv", ["", "open", "close"]),
        (b'"Open Time","Close (USD)"\n', ".csv", ["Open Time", "Close (USD)"]),
        (b"\xef\xbb\xbftime|bid\n", ".csv", ["time", "bid"]),
        ("\u010cas;Cena\n".encode(), ".csv", ["\u010cas", "Cena"]),
        (b"a\tb\n", ".tsv", ["a", "b"]),
        (b"2024.01.02,1.1,1.2\n", ".csv", None),
        (b"fakeuser@mail.invalid,name\n", ".csv", None),
        (b"user,990001112\n", ".csv", None),
        (b"https://host.invalid/x,name\n", ".csv", None),
        (b"login,password_fake\n", ".csv", None),
        (b"\x00\x01\x02binary\n", ".csv", None),
        (b"\n1,2\n", ".csv", None),
    ],
    ids=[
        "plain",
        "unnamed-index-column",
        "quoted-names-with-spaces",
        "utf8-bom-pipe",
        "non-ascii-names-semicolon",
        "tsv",
        "data-row",
        "mail-address",
        "long-number",
        "url",
        "sensitive-word",
        "binary",
        "empty-first-line",
    ],
)
def test_header_is_only_reported_when_it_looks_like_column_names(
    first_line: bytes, extension: str, expected: list[str] | None
) -> None:
    result = audit._delimited_header(first_line, extension)
    if expected is None:
        assert "columns" not in result
        assert result["header_withheld"]
    else:
        assert result["columns"] == expected


@requirement
def test_header_longer_than_the_limit_is_truncated_to_whole_column_names() -> None:
    names = [f"column_{index:04d}" for index in range(400)]  # 4799 characters
    result = audit._delimited_header((",".join(names) + "\n1,2\n").encode(), ".csv")
    assert result["header_truncated"] is True
    assert result["columns"] == names[: len(result["columns"])]
    assert 100 < len(result["columns"]) < 400
    assert len(",".join(result["columns"])) <= 2000


@requirement
@pytest.mark.parametrize(
    ("line", "outcome"),
    [
        ("numpy==1.26.4", ("numpy", "==1.26.4")),
        ("pandas >= 2.0 , < 3", ("pandas", ">=2.0,<3")),
        ("requests[socks]>=2.31", ("requests", ">=2.31")),
        ("scipy  # pinned later", ("scipy", "")),
        ("torch==2.3.0 --hash=sha256:abcdef", ("torch", "==2.3.0")),
        ("pkg @ https://host.invalid/pkg.whl", "sensitive"),
        ("--index-url https://fakeuser:FAKE-NOT-A-SECRET@host.invalid/simple", "sensitive"),
        ("git+ssh://git@host.invalid/repo.git", "sensitive"),
        ("my-token-lib==1.0", "sensitive"),
        ("lib==1.0 # password: FAKE-NOT-A-SECRET", "sensitive"),
        ("--find-links ./wheels", "options"),
        ("-e .", "options"),
        ("./local/path/package", "unparsed"),
        ("FAKE NOT A REQUIREMENT", "unparsed"),
    ],
    ids=[
        "pinned",
        "range-with-spaces",
        "extras",
        "inline-comment",
        "hash-option",
        "direct-url",
        "index-url-with-credentials",
        "vcs-url",
        "sensitive-word-in-name",
        "sensitive-word-in-comment",
        "find-links-option",
        "editable-option",
        "local-path",
        "free-text",
    ],
)
def test_requirement_lines_are_reduced_to_name_and_specifier(
    line: str, outcome: tuple[str, str] | str
) -> None:
    found = audit._Dependencies()
    found.add_requirement(line, "requirements")
    if isinstance(outcome, tuple):
        assert [(item["name"], item.get("specifier", "")) for item in found.entries] == [outcome]
        assert found.dropped == {"sensitive": 0, "options": 0, "unparsed": 0}
    else:
        assert found.entries == []
        assert found.dropped[outcome] == 1
        assert sum(found.dropped.values()) == 1


@requirement
def test_progress_is_written_to_stderr(tree: Path, tmp_path: Path) -> None:
    code, stdout, stderr = run_main(
        "audit", "--root", str(tree), "--out", str(tmp_path / "out"), "--progress-every", "10"
    )
    assert code == 0
    assert stdout == ""
    progress = [line for line in stderr.splitlines() if "files listed" in line]
    assert len(progress) == len(EXPECTED_PATHS) // 10
    assert progress[0].startswith("[gie_baseline_audit] 10 files listed")
    assert "done:" in stderr.splitlines()[-1]


@requirement
def test_unreadable_and_vanished_files_are_recorded_and_do_not_fail_the_run(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_opener = audit._readonly_opener
    failures = {
        str(tree / "config.py"): PermissionError(13, "SYNTHETIC FIXTURE: permission denied"),
        str(tree / "data" / "cache.parquet"): FileNotFoundError(2, "SYNTHETIC FIXTURE: vanished"),
    }

    def failing_opener(path: str, flags: int) -> int:
        if plain(path) in failures:
            raise failures[plain(path)]
        return real_opener(path, flags)

    monkeypatch.setattr(audit, "_readonly_opener", failing_opener)
    out = tmp_path / "out"
    code, _stdout, _stderr = run_audit(tree, out)
    assert code == 0
    manifest = load_manifest(out / "manifest.jsonl")
    assert sorted(manifest) == EXPECTED_PATHS
    assert manifest["config.py"] == {
        "path": "config.py",
        "size": len(CONFIG),
        "mtime_ns": FIXED_MTIME_NS,
        "sha256": None,
        "skipped": "unreadable",
        "error": "PermissionError",
    }
    assert manifest["data/cache.parquet"]["error"] == "FileNotFoundError"
    assert manifest["run_bot.py"]["sha256"] == hashlib.sha256(RUN_BOT.encode()).hexdigest()
    section = load_inventory(out)["exclusions_and_caps"]
    assert section["unreadable"] == [
        {"errno": 13, "error": "PermissionError", "path": "config.py", "stage": "read"},
        {"errno": 2, "error": "FileNotFoundError", "path": "data/cache.parquet", "stage": "read"},
    ]
    assert section["unreadable_count"] == 2
    assert "unreadable paths: 2" in (out / "audit_log.txt").read_text()
    # the manifest-only path reports the same records
    records = {record["path"]: record for record in audit.build_manifest(tree)}
    assert records["config.py"]["skipped"] == "unreadable"


@requirement
def test_unreadable_directory_is_recorded(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir
    blocked = str(tree / "engines" / "legacy")

    def failing_scandir(path: Any) -> Any:
        if plain(path) == blocked:
            raise PermissionError(13, "SYNTHETIC FIXTURE: permission denied")
        return real_scandir(path)

    monkeypatch.setattr(audit.os, "scandir", failing_scandir)
    out = tmp_path / "out"
    code, _stdout, _stderr = run_audit(tree, out)
    assert code == 0
    manifest = load_manifest(out / "manifest.jsonl")
    assert sorted(manifest) == [rel for rel in EXPECTED_PATHS if not rel.startswith("engines/legacy/")]
    assert load_inventory(out)["exclusions_and_caps"]["unreadable"] == [
        {"errno": 13, "error": "PermissionError", "path": "engines/legacy", "stage": "list directory"}
    ]


@requirement
def test_file_that_changes_while_it_is_read_is_flagged(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tree / "data" / "ticks_xauusd.csv"
    listed = os.lstat(target)
    with open(target, "ab") as handle:  # the file grows after it was listed
        handle.write(b"late,row\n")
    result = audit._read_file(str(target), listed, True, False, 0)
    assert result.changed is True
    assert result.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    steady = audit._read_file(str(target), os.lstat(target), True, False, 0)
    assert steady.changed is False

    real_read = audit._read_file

    def unstable_read(path: str, *args: Any) -> Any:
        outcome = real_read(path, *args)
        if plain(path) == str(target):
            outcome.changed = True
        return outcome

    monkeypatch.setattr(audit, "_read_file", unstable_read)
    out = tmp_path / "out"
    assert run_audit(tree, out)[0] == 0
    manifest = load_manifest(out / "manifest.jsonl")
    assert manifest["data/ticks_xauusd.csv"]["unstable"] is True
    assert "unstable" not in manifest["config.py"]
    assert load_inventory(out)["exclusions_and_caps"]["changed_while_read"] == ["data/ticks_xauusd.csv"]


@requirement
def test_files_are_streamed_in_chunks(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = tmp_path / "reference"
    assert run_audit(tree, reference, "--count-rows", "--hash-sensitive")[0] == 0
    assert len(BIG_UTF16_CSV) > 4 * 8192
    assert len(BIG_MODULE) > 8192
    assert 1001 < VISIBLE["engines/late_binary.py"].index(b"\x00") < 8192
    monkeypatch.setattr(audit, "_CHUNK_BYTES", 1001)  # odd on purpose: UTF-16 units straddle chunk borders
    small = tmp_path / "small_chunks"
    assert run_audit(tree, small, "--count-rows", "--hash-sensitive")[0] == 0
    assert (small / "manifest.jsonl").read_bytes() == (reference / "manifest.jsonl").read_bytes()
    one, two = load_inventory(reference), load_inventory(small)
    assert two["languages"] == one["languages"]
    assert two["data"] == one["data"]
    assert two["broker_interfaces"] == one["broker_interfaces"]
    assert two["languages"]["MQL5"]["lines"] == EXPERT_MQ5.count("\n")
    big = {item["path"]: item for item in two["data"]["files"]}["data/big_utf16.csv"]
    assert big["row_count"] == BIG_UTF16_ROWS
    assert {"line": BIG_MODULE.count("\n"), "path": "engines/big_module.py"} in two["broker_interfaces"][
        "rules"
    ]["ccxt"]
    manifest = load_manifest(small / "manifest.jsonl")
    for rel, content in VISIBLE.items():
        assert manifest[rel]["sha256"] == hashlib.sha256(content).hexdigest()


@requirement
def test_fifo_is_recorded_as_special_and_never_opened(tree: Path, tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("BLOCKED: B-001 - named pipes of this platform cannot be created in this session")
    os.mkfifo(tree / "engines" / "pipe_to_nowhere")
    out = tmp_path / "out"
    code, _stdout, _stderr = run_audit(tree, out)  # would block forever if the FIFO were opened
    assert code == 0
    section = load_inventory(out)["exclusions_and_caps"]
    assert section["special_files_not_opened"] == [{"kind": "fifo", "path": "engines/pipe_to_nowhere"}]
    assert "engines/pipe_to_nowhere" not in load_manifest(out / "manifest.jsonl")


@requirement
def test_paths_longer_than_260_characters(tmp_path: Path) -> None:
    root = tmp_path / "prod"
    deep = root
    for index in range(12):
        deep = deep / f"directory_with_a_long_name_{index:02d}"
    deep.mkdir(parents=True)
    content = b"# SYNTHETIC FIXTURE\nimport MetaTrader5\n"
    (deep / "deep_engine.py").write_bytes(content)
    relative = (deep / "deep_engine.py").relative_to(root).as_posix()
    assert len(str(deep / "deep_engine.py")) > 300
    out = tmp_path / "out"
    assert run_audit(root, out)[0] == 0
    manifest = load_manifest(out / "manifest.jsonl")
    assert manifest[relative]["sha256"] == hashlib.sha256(content).hexdigest()
    assert load_inventory(out)["broker_interfaces"]["rules"]["metatrader5_import"] == [
        {"line": 2, "path": relative}
    ]


@requirement
def test_windows_path_helpers() -> None:
    assert audit._extended_path("D:\\ftmo_eurusd") == "\\\\?\\D:\\ftmo_eurusd"
    assert audit._extended_path("\\\\server\\share\\x") == "\\\\?\\UNC\\server\\share\\x"
    assert audit._extended_path("\\\\?\\D:\\x") == "\\\\?\\D:\\x"
    assert audit._strip_extended_prefix("\\\\?\\D:\\ftmo_eurusd") == "D:\\ftmo_eurusd"
    assert audit._strip_extended_prefix("\\\\?\\UNC\\server\\share\\x") == "\\\\server\\share\\x"
    assert audit._strip_extended_prefix("D:\\x") == "D:\\x"
    for path in ("D:\\ftmo_eurusd\\a", "\\\\server\\share\\a"):
        assert audit._strip_extended_prefix(audit._extended_path(path)) == path


@requirement
@read_only
def test_reparse_points_and_offline_files_are_classified_from_stat_only() -> None:
    def fake_stat(mode: int, attributes: int = 0) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            st_mode=mode, st_file_attributes=attributes
        )  # SYNTHETIC FIXTURE stat result

    assert audit._entry_kind(fake_stat(stat.S_IFDIR | 0o755)) == "dir"
    assert (
        audit._entry_kind(fake_stat(stat.S_IFDIR | 0o755, 0x400 | 0x10)) == "link"
    )  # junction: never descended
    assert audit._entry_kind(fake_stat(stat.S_IFLNK | 0o777, 0x400)) == "link"
    assert audit._entry_kind(fake_stat(stat.S_IFREG | 0o644)) == "file"
    assert audit._entry_kind(fake_stat(stat.S_IFREG | 0o644, 0x400000)) == "offline_file"  # cloud placeholder
    assert audit._entry_kind(fake_stat(stat.S_IFREG | 0o644, 0x1000)) == "offline_file"
    assert audit._entry_kind(fake_stat(stat.S_IFIFO | 0o644)) == "special"
    assert audit._entry_kind(types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644)) == "file"


@requirement
@read_only
def test_offline_placeholder_files_are_listed_but_never_opened(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_kind = audit._entry_kind
    placeholder = os.lstat(tree / "data" / "cache.parquet").st_ino

    def kind_with_placeholder(st: Any) -> str:
        return "offline_file" if st.st_ino == placeholder else real_kind(st)

    opened: list[str] = []
    real_opener = audit._readonly_opener

    def recording_opener(path: str, flags: int) -> int:
        opened.append(plain(path))
        return real_opener(path, flags)

    monkeypatch.setattr(audit, "_entry_kind", kind_with_placeholder)
    monkeypatch.setattr(audit, "_readonly_opener", recording_opener)
    out = tmp_path / "out"
    assert run_audit(tree, out)[0] == 0
    record = load_manifest(out / "manifest.jsonl")["data/cache.parquet"]
    assert record["sha256"] is None
    assert record["skipped"] == "offline_placeholder"
    assert load_inventory(out)["exclusions_and_caps"]["offline_placeholders_not_read"] == [
        "data/cache.parquet"
    ]
    assert str(tree / "data" / "cache.parquet") not in opened
    assert str(tree / "config.py") in opened
    for sensitive in SENSITIVE_PATHS:
        assert str(tree / sensitive) not in opened  # sensitive files are not even opened


# ---------------------------------------------------------------------------------------------
# Processes and scheduled tasks (parsers and fixed queries; SYNTHETIC FIXTURE inputs)
# ---------------------------------------------------------------------------------------------

SCHTASKS_HEADER = [
    "HostName",
    "TaskName",
    "Next Run Time",
    "Status",
    "Logon Mode",
    "Last Run Time",
    "Last Result",
    "Author",
    "Task To Run",
    "Start In",
    "Comment",
    "Scheduled Task State",
    "Idle Time",
    "Power Management",
    "Run As User",
    "Delete Task If Not Rescheduled",
    "Stop Task If Runs X Hours and X Mins",
    "Schedule",
    "Schedule Type",
    "Start Time",
    "Start Date",
    "End Date",
    "Days",
    "Months",
    "Repeat: Every",
    "Repeat: Until: Time",
    "Repeat: Until: Duration",
    "Repeat: Stop If Still Running",
]
GOLD_BOT_COMMAND = (
    '"C:\\Python311\\python.exe" D:\\ftmo_eurusd\\run_bot.py'
    + " --login 99000111222333 --password FAKE-NOT-A-SECRET-taskarg-e1e1"
)
UPDATER_COMMAND = "C:\\Program Files\\Fake App\\updater.exe /silent /apikey=FAKE-NOT-A-SECRET-taskarg-e1e1"


def schtasks_row(name: str, command: str, schedule_type: str = "Daily") -> list[str]:
    row = ["N/A"] * len(SCHTASKS_HEADER)
    row[0] = "FAKE-HOST"
    row[1] = name
    row[2] = "28.11.2026 07:00:00"
    row[3] = "Ready"
    row[5] = "27.11.2026 07:00:00"
    row[7] = "FAKE-HOST\\fakeuser"
    row[8] = command
    row[9] = "D:\\ftmo_eurusd" if "ftmo" in command else "N/A"
    row[14] = "fakeuser"
    row[18] = schedule_type
    return row


def schtasks_csv(header: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerow(schtasks_row("\\GoldBot Start", GOLD_BOT_COMMAND))
    writer.writerow(schtasks_row("\\GoldBot Start", GOLD_BOT_COMMAND, "At logon time"))  # second trigger
    writer.writerow(header)  # schtasks repeats the header for every folder
    writer.writerow(
        schtasks_row("\\Microsoft\\Windows\\Defrag\\ScheduledDefrag", "%windir%\\system32\\defrag.exe -c")
    )
    writer.writerow(schtasks_row("\\Updater", UPDATER_COMMAND))
    return buffer.getvalue()


TASKLIST_CSV = (
    '"System Idle Process","0","Services","0","8 K"\r\n'
    '"python.exe","4321","Console","1","45,120 K"\r\n'
    '"terminal64.exe","5000","Console","1","512 000 K"\r\n'
    '"chrome.exe","6000","Console","1","300,000 K"\r\n'
    '"pythonw.exe","4400","Services","0","N/A"\r\n'
)
EXPECTED_TASKS = [
    {
        "task_name": "\\GoldBot Start",
        "status": "Ready",
        "next_run_time": "28.11.2026 07:00:00",
        "last_run_time": "27.11.2026 07:00:00",
        "task_to_run": "C:\\Python311\\python.exe <redacted>",
        "mentions_root": True,
        "trading_hint": True,
    },
    {
        "task_name": "\\Updater",
        "status": "Ready",
        "next_run_time": "28.11.2026 07:00:00",
        "last_run_time": "27.11.2026 07:00:00",
        "task_to_run": "C:\\Program Files\\Fake App\\updater.exe <redacted>",
        "mentions_root": False,
        "trading_hint": False,
    },
]


@requirement
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (GOLD_BOT_COMMAND, "C:\\Python311\\python.exe <redacted>"),
        (UPDATER_COMMAND, "C:\\Program Files\\Fake App\\updater.exe <redacted>"),
        (
            '"C:\\Program Files\\MetaTrader 5\\terminal64.exe"',
            "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        ),
        ("C:\\bot\\start.bat", "C:\\bot\\start.bat"),
        ("C:\\bot\\start.bat --key FAKE-NOT-A-SECRET", "C:\\bot\\start.bat <redacted>"),
        ("powershell -File C:\\bot\\run.ps1 -Password FAKE-NOT-A-SECRET", "powershell <redacted>"),
        # no closing quote: nothing after the opening quote can be trusted to be part of the path
        ('"C:\\unterminated\\path.exe -password FAKE-NOT-A-SECRET', "C:\\unterminated\\path.exe <redacted>"),
        ('"  C:\\bot\\run.exe  FAKE-NOT-A-SECRET', "C:\\bot\\run.exe <redacted>"),
        # a switch before the first executable extension: only the first token is kept
        ("C:\\My Tools\\run /key=FAKE-NOT-A-SECRET.exe", "C:\\My <redacted>"),
        ("C:\\My Tools\\run -x C:\\other\\tool.exe FAKE-NOT-A-SECRET", "C:\\My <redacted>"),
        ("python D:\\ftmo_eurusd\\run_bot.py FAKE-NOT-A-SECRET", "python <redacted>"),
        ("COM handler", "COM <redacted>"),
        ("", ""),
    ],
    ids=[
        "quoted-python-with-arguments",
        "unquoted-path-with-spaces",
        "quoted-without-arguments",
        "batch-file",
        "batch-file-with-arguments",
        "bare-command-with-script",
        "unterminated-quote",
        "unterminated-quote-with-spaces",
        "switch-before-extension",
        "option-before-second-path",
        "bare-python-with-script",
        "com-handler",
        "empty",
    ],
)
def test_scheduled_task_arguments_are_redacted(command: str, expected: str) -> None:
    assert audit._redact_task_command(command) == expected
    assert MARKER not in audit._redact_task_command(command)


@requirement
def test_tasklist_and_schtasks_output_is_reduced_to_the_documented_fields() -> None:
    processes = audit._parse_tasklist(TASKLIST_CSV)
    assert processes == {
        "total": 5,
        "not_listed": 2,
        "relevant": [
            {"image": "python.exe", "pid": 4321, "memory_kb": 45120, "hints": ["python"]},
            {"image": "pythonw.exe", "pid": 4400, "memory_kb": None, "hints": ["python"]},
            {"image": "terminal64.exe", "pid": 5000, "memory_kb": 512000, "hints": ["metatrader_terminal"]},
        ],
    }
    tasks = audit._parse_schtasks(schtasks_csv(SCHTASKS_HEADER), "D:\\ftmo_eurusd")
    assert tasks == {
        "total": 3,
        "microsoft_system_tasks_not_listed": 1,
        "column_mapping": "by_name",
        "tasks": EXPECTED_TASKS,
    }
    dumped = json.dumps([processes, tasks])
    for forbidden in (MARKER, FAKE_ACCOUNT, "FAKE-HOST", "fakeuser", "run_bot.py", "/silent"):
        assert forbidden not in dumped


@requirement
def test_schtasks_with_translated_column_names_is_read_by_position() -> None:
    translated = [f"Sloupec {index}" for index in range(len(SCHTASKS_HEADER))]  # SYNTHETIC non-English header
    tasks = audit._parse_schtasks(schtasks_csv(translated), "D:\\ftmo_eurusd")
    assert tasks["column_mapping"] == "by_position"
    assert tasks["tasks"] == EXPECTED_TASKS
    unknown = audit._parse_schtasks('"a","b","c"\r\n"1","2","3"\r\n', "D:\\ftmo_eurusd")
    assert unknown == {
        "total": 0,
        "tasks": [],
        "microsoft_system_tasks_not_listed": 0,
        "column_mapping": "unrecognised",
    }
    assert audit._parse_schtasks("", "D:\\ftmo_eurusd")["column_mapping"] == "none"
    # 28 unknown columns whose "task name" column does not hold task paths: nothing is trusted
    shifted = io.StringIO()
    writer = csv.writer(shifted, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(translated)
    writer.writerow([GOLD_BOT_COMMAND] * len(translated))
    assert audit._parse_schtasks(shifted.getvalue(), "D:\\ftmo_eurusd") == unknown
    assert MARKER not in json.dumps(audit._parse_schtasks(shifted.getvalue(), "D:\\ftmo_eurusd"))


@requirement
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ready", "Ready"),
        (" 28.11.2026 07:00:00 ", "28.11.2026 07:00:00"),
        ("11/27/2026 7:00:00 AM", "11/27/2026 7:00:00 AM"),
        ("N/A", "N/A"),
        ("P\u0159ipraveno", "P\u0159ipraveno"),
        (GOLD_BOT_COMMAND, "<withheld>"),
        ("--password=FAKE-NOT-A-SECRET", "<withheld>"),
        ("C:\\bot\\run.bat", "<withheld>"),
        ("fakeuser@host.invalid", "<withheld>"),
    ],
    ids=[
        "status",
        "date-with-spaces",
        "us-date",
        "not-available",
        "translated-status",
        "command-line",
        "option-with-value",
        "path",
        "mail-address",
    ],
)
def test_scheduled_task_status_and_time_fields_never_carry_commands(value: str, expected: str) -> None:
    assert audit._short_field(value) == expected


class FakeRunner:
    """SYNTHETIC stand-in for ``subprocess.run``: records the call and returns canned output."""

    def __init__(self, outputs: dict[str, Any]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(args), kwargs))
        outcome = self.outputs[args[0].rsplit("\\", 1)[-1]]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return subprocess.CompletedProcess(args, outcome, stdout=b"", stderr=b"ERROR: access denied")
        if isinstance(outcome, tuple):
            return subprocess.CompletedProcess(args, outcome[0], stdout=outcome[1].encode(), stderr=b"ERROR")
        return subprocess.CompletedProcess(args, 0, stdout=outcome.encode(), stderr=b"")


@requirement
def test_windows_process_queries_use_fixed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner({"tasklist.exe": TASKLIST_CSV, "schtasks.exe": schtasks_csv(SCHTASKS_HEADER)})
    monkeypatch.setattr(audit.subprocess, "run", runner)
    monkeypatch.setattr(audit, "_windows_system_tool", lambda name, root: "C:\\Windows\\System32\\" + name)
    guard = audit._Guard()
    result = audit._collect_processes_windows("D:\\ftmo_eurusd", guard)
    assert [call[0] for call in runner.calls] == [
        ["C:\\Windows\\System32\\tasklist.exe", "/FO", "CSV", "/NH"],
        ["C:\\Windows\\System32\\schtasks.exe", "/Query", "/FO", "CSV", "/V"],
    ]
    for _args, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] > 0
        assert kwargs["capture_output"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["check"] is False
    assert guard.allowed_commands == {subprocess.list2cmdline(call[0]) for call in runner.calls}
    assert guard.allowed_commands == {
        "C:\\Windows\\System32\\tasklist.exe /FO CSV /NH",
        "C:\\Windows\\System32\\schtasks.exe /Query /FO CSV /V",
    }
    assert result["processes"]["status"] == "ok"
    assert result["processes"]["query"] == "tasklist /FO CSV /NH"
    assert [item["image"] for item in result["processes"]["relevant"]] == [
        "python.exe",
        "pythonw.exe",
        "terminal64.exe",
    ]
    assert result["scheduled_tasks"]["status"] == "ok"
    assert result["scheduled_tasks"]["query"] == "schtasks /Query /FO CSV /V"
    assert result["scheduled_tasks"]["tasks"] == EXPECTED_TASKS
    assert MARKER not in json.dumps(result)
    assert FAKE_ACCOUNT not in json.dumps(result)


@requirement
def test_failed_process_queries_are_recorded_and_the_other_query_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "_windows_system_tool", lambda name, root: "C:\\Windows\\System32\\" + name)
    timeout = subprocess.TimeoutExpired(cmd="tasklist", timeout=1)
    runner = FakeRunner({"tasklist.exe": timeout, "schtasks.exe": schtasks_csv(SCHTASKS_HEADER)})
    monkeypatch.setattr(audit.subprocess, "run", runner)
    result = audit._collect_processes_windows("D:\\ftmo_eurusd", audit._Guard())
    assert result["processes"]["status"] == "error"
    assert result["processes"]["error"] == "TimeoutExpired"
    assert result["scheduled_tasks"]["status"] == "ok"
    assert len(runner.calls) == 2

    runner = FakeRunner({"tasklist.exe": TASKLIST_CSV, "schtasks.exe": 1})
    monkeypatch.setattr(audit.subprocess, "run", runner)
    result = audit._collect_processes_windows("D:\\ftmo_eurusd", audit._Guard())
    assert result["processes"]["status"] == "ok"
    assert result["scheduled_tasks"]["status"] == "error"
    assert result["scheduled_tasks"]["error"] == "ExitCode1"
    assert result["scheduled_tasks"]["tasks"] == []

    # schtasks reports an error for one damaged task but still prints the others: keep them, say so
    runner = FakeRunner({"tasklist.exe": (1, ""), "schtasks.exe": (1, schtasks_csv(SCHTASKS_HEADER))})
    monkeypatch.setattr(audit.subprocess, "run", runner)
    result = audit._collect_processes_windows("D:\\ftmo_eurusd", audit._Guard())
    assert result["processes"]["status"] == "error"
    assert result["scheduled_tasks"]["status"] == "partial"
    assert result["scheduled_tasks"]["error"] == "ExitCode1"
    assert result["scheduled_tasks"]["tasks"] == EXPECTED_TASKS

    def missing_tool(name: str, root: str) -> str:
        raise FileNotFoundError(name)

    monkeypatch.setattr(audit, "_windows_system_tool", missing_tool)
    runner = FakeRunner({})
    monkeypatch.setattr(audit.subprocess, "run", runner)
    result = audit._collect_processes_windows("D:\\ftmo_eurusd", audit._Guard())
    assert result["processes"]["error"] == "FileNotFoundError"
    assert result["scheduled_tasks"]["error"] == "FileNotFoundError"
    assert runner.calls == []


@requirement
@read_only
def test_system_tools_are_never_taken_from_the_audited_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "FakeWindows" / "System32"
    system.mkdir(parents=True)
    (system / "tasklist.exe").write_bytes(b"SYNTHETIC FIXTURE - not an executable")
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "FakeWindows"))
    assert audit._windows_system_tool("tasklist.exe", str(tmp_path / "prod")) == str(system / "tasklist.exe")
    with pytest.raises(FileNotFoundError):
        audit._windows_system_tool("schtasks.exe", str(tmp_path / "prod"))  # missing
    with pytest.raises(FileNotFoundError):
        audit._windows_system_tool("tasklist.exe", str(tmp_path))  # the tool would come from inside the root


@requirement
def test_proc_scan_reads_names_and_memory_but_never_command_lines(tmp_path: Path) -> None:
    proc = tmp_path / "fake_proc"  # SYNTHETIC FIXTURE process table
    entries = {
        "101": ("python3.11\n", "Name:\tpython3.11\nVmRSS:\t   20480 kB\n"),
        "202": ("terminal64.exe\n", "Name:\tterminal64.exe\nVmRSS:\t  512000 kB\n"),
        "303": ("bash\n", "Name:\tbash\nVmRSS:\t    4096 kB\n"),
        "404": ("node\n", "Name:\tnode\n"),
    }
    for pid, (comm, status) in entries.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "comm").write_text(comm, newline="\n")
        (proc / pid / "status").write_text(status, newline="\n")
        (proc / pid / "cmdline").write_bytes(
            b"python\0run_bot.py\0--password\0FAKE-NOT-A-SECRET-taskarg-e1e1"
        )
    (proc / "505").mkdir()  # a process that ended: no files left
    (proc / "self").mkdir()
    (proc / "meminfo").write_text("MemTotal: 1 kB\n", newline="\n")
    result = audit._collect_processes_proc(str(proc))
    assert result["processes"] == {
        "status": "ok",
        "error": None,
        "query": "scan of " + str(proc),
        "total": 4,
        "not_listed": 1,
        "relevant": [
            {"image": "node", "pid": 404, "memory_kb": None, "hints": ["node"]},
            {"image": "python3.11", "pid": 101, "memory_kb": 20480, "hints": ["python"]},
            {"image": "terminal64.exe", "pid": 202, "memory_kb": 512000, "hints": ["metatrader_terminal"]},
        ],
    }
    assert result["scheduled_tasks"]["status"] == "not_collected"
    assert MARKER not in json.dumps(result)
    missing = audit._collect_processes_proc(str(tmp_path / "no_such_proc"))
    assert missing["processes"]["status"] == "error"
    assert missing["processes"]["error"] == "FileNotFoundError"


# ---------------------------------------------------------------------------------------------
# Static guarantees about the tool source
# ---------------------------------------------------------------------------------------------

ALLOWED_IMPORTS = {
    "__future__",
    "argparse",
    "bisect",
    "configparser",
    "contextlib",
    "csv",
    "ctypes",
    "dataclasses",
    "datetime",
    "fnmatch",
    "hashlib",
    "json",
    "msvcrt",
    "os",
    "platform",
    "re",
    "shutil",
    "stat",
    "subprocess",
    "sys",
    "time",
    "typing",
    "winreg",
}
FORBIDDEN_OS_CALLS = {
    "remove",
    "unlink",
    "rmdir",
    "removedirs",
    "rename",
    "renames",
    "replace",
    "chmod",
    "lchmod",
    "chown",
    "lchown",
    "utime",
    "truncate",
    "ftruncate",
    "link",
    "symlink",
    "mkfifo",
    "mknod",
    "chdir",
    "fchdir",
    "write",
    "system",
    "popen",
    "startfile",
    "fdopen",
    "execv",
    "execve",
    "execl",
    "execvp",
    "spawnv",
    "spawnl",
    "fork",
    "kill",
    "putenv",
    "unsetenv",
    "setxattr",
    "removexattr",
}
FORBIDDEN_METHODS = {
    "unlink",
    "rmdir",
    "rename",
    "chmod",
    "lchmod",
    "touch",
    "write_text",
    "write_bytes",
    "mkdir",
    "symlink_to",
    "hardlink_to",
    "link_to",
    "rmtree",
    "move",
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "copymode",
    "copystat",
    "make_archive",
    "unpack_archive",
    "truncate",
}
NEWER_THAN_PYTHON_38 = {
    "removeprefix",
    "removesuffix",
    "is_relative_to",
    "file_digest",
    "bit_count",
    "BooleanOptionalAction",
    "unparse",
    "pairwise",
    "batched",
    "UTC",
    "readlink_to",
    "hardlink_to",
    "lcm",
    "TaskGroup",
    "to_thread",
    "aclosing",
}
BUILTIN_GENERICS = {"list", "dict", "set", "frozenset", "tuple", "type"}


def enclosing_functions() -> dict[int, str]:
    """id(node) -> name of the innermost function that contains it ('' at module level)."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        owner[id(node)] = current
        inner = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(TOOL_AST, "")
    return owner


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


@requirement
def test_tool_source_is_valid_python_38_syntax() -> None:
    ast.parse(TOOL_SOURCE, feature_version=(3, 8))
    assert "from __future__ import annotations" in TOOL_SOURCE
    lines = TOOL_SOURCE.splitlines()
    annotations: set[int] = set()
    for node in ast.walk(TOOL_AST):
        assert not isinstance(node, ast.Match), "match statements need Python 3.10"
        assert not isinstance(node, ast.TryStar), "except* needs Python 3.11"
        if isinstance(node, (ast.With, ast.AsyncWith)):
            # feature_version does not reject parenthesised context managers (Python 3.9+)
            after_keyword = lines[node.lineno - 1][node.col_offset :].split("with", 1)[1].lstrip()
            assert not after_keyword.startswith("("), f"line {node.lineno}: parenthesised with statement"
        if isinstance(node, ast.Attribute):
            assert node.attr not in NEWER_THAN_PYTHON_38, (
                f"line {node.lineno}: .{node.attr} is newer than 3.8"
            )
        if isinstance(node, ast.keyword) and node.arg == "strict":
            parent_calls = [
                call for call in ast.walk(TOOL_AST) if isinstance(call, ast.Call) and node in call.keywords
            ]
            assert all(dotted(call.func) != "zip" for call in parent_calls), "zip(strict=) needs Python 3.10"
        for annotation in (
            getattr(node, "annotation", None),
            getattr(node, "returns", None),
        ):
            if isinstance(annotation, ast.AST):
                annotations.update(id(child) for child in ast.walk(annotation))
    for node in ast.walk(TOOL_AST):
        if isinstance(node, ast.Subscript) and id(node) not in annotations:
            # outside annotations a subscripted built-in or stdlib class is evaluated at run time
            assert dotted(node.value) not in BUILTIN_GENERICS, f"line {node.lineno}"
            assert dotted(node.value) not in {"re.Pattern", "os.PathLike", "subprocess.CompletedProcess"}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and id(node) not in annotations:
            for side in (node.left, node.right):
                assert not (isinstance(side, ast.Constant) and side.value is None), (
                    f"line {node.lineno}: X | None"
                )
                assert dotted(side) not in {"str", "int", "bytes", "bool", "float"}, f"line {node.lineno}"
    for name in ("list[", "dict[", "tuple[", " | None"):
        assert name not in TOOL_SOURCE, name + " is not used even inside annotations"


@requirement
def test_tool_uses_the_standard_library_only_and_no_network_module() -> None:
    imported: set[str] = set()
    for node in ast.walk(TOOL_AST):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call):
            assert dotted(node.func) not in {
                "__import__",
                "importlib.import_module",
                "eval",
                "exec",
                "compile",
            }
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    for forbidden in (
        "socket",
        "urllib",
        "http",
        "requests",
        "ftplib",
        "smtplib",
        "ssl",
        "asyncio",
        "importlib",
    ):
        assert forbidden not in imported
    assert {"os", "hashlib", "json", "subprocess"} <= imported
    # Windows-only modules are imported only under an explicit platform check
    for node in TOOL_AST.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.name for alias in node.names} | {getattr(node, "module", None) or ""}
            assert not names & {"ctypes", "msvcrt", "winreg"}


@requirement
@read_only
def test_tool_source_contains_no_call_that_could_modify_files() -> None:
    owner = enclosing_functions()
    calls = [node for node in ast.walk(TOOL_AST) if isinstance(node, ast.Call)]
    assert len(calls) > 300  # the scan below really covers the whole tool
    for call in calls:
        name = dotted(call.func)
        if name.startswith("os.") and name.count(".") == 1:
            assert name.split(".")[1] not in FORBIDDEN_OS_CALLS, f"line {call.lineno}: {name}"
        if name.startswith("shutil."):
            assert name == "shutil.disk_usage", f"line {call.lineno}: {name}"
        if isinstance(call.func, ast.Attribute) and call.func.attr in FORBIDDEN_METHODS:
            raise AssertionError(f"line {call.lineno}: .{call.func.attr}() could modify a file")
        if name in {"os.makedirs", "os.mkdir"}:
            assert owner[id(call)] in {"_prepare_output_directory", "_command_manifest"}
        if name == "os.open":
            assert owner[id(call)] == "_readonly_opener"
        assert name not in {"io.open", "os.fdopen", "codecs.open", "tempfile.mkstemp", "open_code"}
    for node in ast.walk(TOOL_AST):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = {alias.name for alias in node.names} | {getattr(node, "module", None) or ""}
            assert not modules & {
                "pathlib",
                "tempfile",
                "io",
                "codecs",
                "pickle",
                "shelve",
                "sqlite3",
                "zipfile",
            }
        if isinstance(node, ast.Attribute) and node.attr in {
            "O_WRONLY",
            "O_RDWR",
            "O_APPEND",
            "O_CREAT",
            "O_TRUNC",
        }:
            # write flags are only named where the guard decides what a write is
            assert owner[id(node)] == "", f"line {node.lineno}: {node.attr}"


@requirement
@read_only
def test_files_are_opened_only_with_mode_rb_and_outputs_with_exclusive_create() -> None:
    owner = enclosing_functions()
    opens = [
        node
        for node in ast.walk(TOOL_AST)
        if isinstance(node, ast.Call) and dotted(node.func) in {"open", "io.open"}
    ]
    modes = {}
    for call in opens:
        assert len(call.args) == 2
        mode = call.args[1]
        assert isinstance(mode, ast.Constant), f"line {call.lineno}: the open mode must be a literal"
        modes[owner[id(call)]] = mode.value
    assert modes == {"_open_readonly": "rb", "_create_new_file": "xb"}
    assert len(opens) == 2
    # reading: nothing but _open_readonly; writing: every caller passes the root that must stay untouched
    creators = [
        node
        for node in ast.walk(TOOL_AST)
        if isinstance(node, ast.Call) and dotted(node.func) == "_create_new_file"
    ]
    assert {owner[id(call)] for call in creators} == {"write", "_write_manifest_file"}
    for call in creators:
        assert len(call.args) == 2


@requirement
@read_only
def test_every_subprocess_call_is_a_fixed_list_without_shell() -> None:
    calls = [
        node
        for node in ast.walk(TOOL_AST)
        if isinstance(node, ast.Call) and dotted(node.func).startswith("subprocess.")
    ]
    fixed: set[tuple[str, ...]] = set()
    # list2cmdline only formats a list as text (for the guard); it starts nothing
    assert {dotted(call.func) for call in calls} == {"subprocess.run", "subprocess.list2cmdline"}
    calls = [call for call in calls if dotted(call.func) != "subprocess.list2cmdline"]
    for call in calls:
        assert dotted(call.func) == "subprocess.run", f"line {call.lineno}"
        command = call.args[0]
        assert isinstance(command, ast.List), f"line {call.lineno}: the command must be a list literal"
        assert isinstance(command.elts[0], ast.Name)
        arguments = command.elts[1:]
        assert all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in arguments)
        fixed.add(tuple(item.value for item in arguments if isinstance(item, ast.Constant)))
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        shell = keywords["shell"]
        assert isinstance(shell, ast.Constant)
        assert shell.value is False
        assert "timeout" in keywords
        assert "cwd" in keywords
    assert fixed == {("/FO", "CSV", "/NH"), ("/Query", "/FO", "CSV", "/V")}
    assert len(calls) == 2
    for node in ast.walk(TOOL_AST):
        if isinstance(node, ast.Attribute):
            assert dotted(node) not in {"subprocess.Popen", "subprocess.call", "subprocess.check_output"}
            assert dotted(node) != "subprocess.getoutput"
    assert "git" not in {item for command in fixed for item in command}
    assert '"git"' not in TOOL_SOURCE  # no git process: .git is read as plain files
    assert "shell=True" not in TOOL_SOURCE


# ---------------------------------------------------------------------------------------------
# The tool as the owner runs it: a stand-alone script
# ---------------------------------------------------------------------------------------------


@requirement
@read_only
def test_tool_runs_as_a_standalone_script_and_leaves_the_tree_untouched(tree: Path, tmp_path: Path) -> None:
    before = snapshot(tree)
    out = tmp_path / "run1"
    completed = subprocess.run(
        [
            *(sys.executable, "-I", "-X", "dev", "-W", "error", str(TOOL_PATH)),
            *("audit", "--root", str(tree), "--out", str(out), "--processes", "--count-rows"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=tree,  # even when started from inside the audited tree
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert snapshot(tree) == before
    assert sorted(path.name for path in out.iterdir()) == [
        "audit_log.txt",
        "inventory.json",
        "manifest.jsonl",
        "processes.json",
    ]
    assert (out / "audit_log.txt").read_text().splitlines()[-1] == audit.READ_ONLY_OK_LINE
    in_process = tmp_path / "run2"
    assert run_audit(tree, in_process, "--count-rows")[0] == 0
    assert (out / "manifest.jsonl").read_bytes() == (in_process / "manifest.jsonl").read_bytes()

    refused = subprocess.run(
        [sys.executable, "-I", str(TOOL_PATH), "audit", "--root", str(tree), "--out", str(tree / "out")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert refused.returncode == 2
    assert "refused" in refused.stderr
    assert snapshot(tree) == before
