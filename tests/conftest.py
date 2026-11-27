"""Shared pytest configuration: requirement traceability and machine-readable evidence.

Tests declare what they verify with markers::

    @pytest.mark.requirement("S00-ASG-22", "G08-07")
    @pytest.mark.acceptance("S00-ACC-01")

Marker ids must exist in ``specifications/requirements_matrix.yaml`` (and acceptance
ids must be ``-ACC-`` requirements); an unknown id aborts the run, so a typo can
never silently detach a test from its requirement.

When ``--gie-evidence=<path>`` is given, a JSON file is written with the outcome
of every test together with its requirement and acceptance ids.  The registers
(`tools/gie_registers.py`) and the review package are built from that file, so
no test result is ever typed by hand.

The file also binds the results to what was tested: the git commit, whether the
working tree had uncommitted changes when the run started (the evidence file
itself is ignored), and every module that could not be collected - a test that
could not even be imported is reported, never silently absent.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_RESULTS: dict[str, dict[str, Any]] = {}
_COLLECTION_ERRORS: list[str] = []
_STARTED = time.time()
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", *args), cwd=_REPO_ROOT, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _tree_state(evidence_file: Path | None) -> tuple[str | None, bool | None]:
    """Commit of the tested code and whether the tree differed from it (ignoring the evidence file)."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        return (commit.strip() if commit else None), None
    ignored = None
    if evidence_file is not None:
        try:
            ignored = evidence_file.resolve().parent.relative_to(_REPO_ROOT).as_posix() + "/"
        except ValueError:
            ignored = None
    dirty = any(
        line[3:].strip('"') and not (ignored and line[3:].strip('"').startswith(ignored))
        for line in status.splitlines()
    )
    return (commit.strip() if commit else None), dirty


_TREE_AT_START: tuple[str | None, bool | None] = (None, None)


def pytest_configure(config: pytest.Config) -> None:
    global _TREE_AT_START  # noqa: PLW0603 - recorded once, before any test can touch the tree
    target = config.getoption("--gie-evidence", default=None)
    if target:
        _TREE_AT_START = _tree_state(Path(target))


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _COLLECTION_ERRORS.append(report.nodeid or "<unknown module>")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--gie-evidence",
        action="store",
        default=None,
        help="write per-test results with requirement ids to this JSON file",
    )


def _marker_ids(item: pytest.Item, name: str) -> list[str]:
    ids: list[str] = []
    for marker in item.iter_markers(name=name):
        ids.extend(str(arg) for arg in marker.args)
    return sorted(set(ids))


def _known_requirement_ids() -> set[str]:
    matrix = Path(__file__).resolve().parents[1] / "specifications" / "requirements_matrix.yaml"
    document = yaml.safe_load(matrix.read_text(encoding="utf-8"))
    return {entry["id"] for entry in document["requirements"]}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    known = _known_requirement_ids()
    unknown: list[str] = []
    for item in items:
        for name in ("requirement", "acceptance"):
            for marker_id in _marker_ids(item, name):
                if marker_id not in known:
                    unknown.append(f"{item.nodeid}: @{name}({marker_id!r}) is not in the requirements matrix")
                elif name == "acceptance" and "-ACC-" not in marker_id:
                    unknown.append(
                        f"{item.nodeid}: @acceptance({marker_id!r}) is not an acceptance requirement"
                    )
    if unknown:
        raise pytest.UsageError(
            "tests reference unknown requirement ids (traceability would be lost):\n  " + "\n  ".join(unknown)
        )
    for item in items:
        _RESULTS[item.nodeid] = {
            "nodeid": item.nodeid,
            "requirements": _marker_ids(item, "requirement"),
            "acceptance": _marker_ids(item, "acceptance"),
            "outcome": "not_run",
            "duration_s": 0.0,
            "detail": "",
        }


def _skip_reason(longrepr: object) -> str:
    """The reason given to ``pytest.skip`` (for example ``BLOCKED: B-001 - ...``), never lost."""
    reason = str(longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else longrepr)
    return reason.removeprefix("Skipped: ")[:500]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    entry = _RESULTS.setdefault(
        item.nodeid,
        {
            "nodeid": item.nodeid,
            "requirements": [],
            "acceptance": [],
            "outcome": "not_run",
            "duration_s": 0.0,
            "detail": "",
        },
    )
    entry["duration_s"] = round(entry["duration_s"] + report.duration, 6)
    if report.when == "call":
        if hasattr(report, "wasxfail"):
            entry["outcome"] = "xpassed" if report.passed else "xfailed"
            entry["detail"] = str(report.wasxfail)[:500]
        else:
            entry["outcome"] = report.outcome
            if report.failed:
                entry["detail"] = str(report.longrepr)[-2000:]
            elif report.skipped:
                entry["detail"] = _skip_reason(report.longrepr)
    elif report.when == "setup" and report.skipped:
        entry["outcome"] = "skipped"
        entry["detail"] = _skip_reason(report.longrepr)
    elif report.failed:
        entry["outcome"] = "error"
        entry["detail"] = str(report.longrepr)[-2000:]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = session.config.getoption("--gie-evidence")
    if not target:
        return
    tests = [_RESULTS[key] for key in sorted(_RESULTS)]
    counts: dict[str, int] = {}
    for entry in tests:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    commit, dirty = _TREE_AT_START
    payload = {
        "schema_version": 1,
        "generated_by": "tests/conftest.py",
        "commit": commit,
        "tree_dirty": dirty,
        "collection_errors": sorted(set(_COLLECTION_ERRORS)),
        "exit_status": int(exitstatus),
        "started_unix_s": round(_STARTED, 3),
        "duration_s": round(time.time() - _STARTED, 3),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "counts": dict(sorted(counts.items())),
        "tests": tests,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
