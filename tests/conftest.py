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
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_RESULTS: dict[str, dict[str, Any]] = {}
_STARTED = time.time()


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
    elif report.when == "setup" and report.skipped:
        entry["outcome"] = "skipped"
        longrepr = report.longrepr
        entry["detail"] = str(longrepr[2] if isinstance(longrepr, tuple) else longrepr)[:500]
    elif report.failed:
        entry["outcome"] = "error"
        entry["detail"] = str(report.longrepr)[-2000:]
    elif report.when == "call" and report.skipped:
        entry["outcome"] = "skipped"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = session.config.getoption("--gie-evidence")
    if not target:
        return
    tests = [_RESULTS[key] for key in sorted(_RESULTS)]
    counts: dict[str, int] = {}
    for entry in tests:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    payload = {
        "schema_version": 1,
        "generated_by": "tests/conftest.py",
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
