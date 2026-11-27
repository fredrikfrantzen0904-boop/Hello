"""Acceptance tests S00-ACC-05 "Baseline regression" and S00-ACC-06 "No production changes".

S00-ACC-05 checks every baseline of the REAL registry ``specifications/baselines/registry.yaml``
against its frozen manifest, on the machine that holds the artifacts (the directory named by the
entry's ``root_env``).  S00-ACC-06 compares two manifests of the production tree taken before and
after the session (``GIE_PRODUCTION_MANIFEST_BEFORE`` / ``GIE_PRODUCTION_MANIFEST_AFTER``).

Neither the baselines nor the production tree are reachable from the development session (blocker
B-001).  A baseline that is not frozen, a root that is not available and a missing manifest are
reported as ``pytest.skip("BLOCKED: ...")`` - never as a pass.  No baseline content or number is
assumed here; the harness itself is proven on a synthetic fixture in
``tests/gie/tools/test_baseline_freeze.py``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tools import baseline_freeze as bf

EXPECTED_BASELINES = ("gce-b", "round-1", "round-2", "cavemen-trades")


def _registry_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = list(bf.load_registry(bf.REPO_ROOT)["baselines"])
    return entries


@pytest.mark.acceptance("S00-ACC-05")
@pytest.mark.requirement("G02-08", "G02-09", "M-AUTH-12")
def test_registry_is_well_formed_and_names_all_baselines() -> None:
    document = bf.load_registry(bf.REPO_ROOT)
    assert bf.validate_registry(document, bf.known_requirement_ids(bf.REPO_ROOT)) == []
    assert tuple(entry["id"] for entry in document["baselines"]) == EXPECTED_BASELINES
    for entry in document["baselines"]:
        if entry["status"] == "not_frozen":
            assert entry["blocker"], f"{entry['id']} is not frozen but names no blocker"
            assert entry["manifest"] is None


@pytest.mark.acceptance("S00-ACC-05")
@pytest.mark.requirement("G02-08", "G02-09", "M-AUTH-12")
@pytest.mark.parametrize("entry", _registry_entries(), ids=lambda entry: str(entry["id"]))
def test_baseline_is_byte_identical(entry: dict[str, Any]) -> None:
    blocker = entry["blocker"] or "none recorded"
    if entry["status"] != "frozen":
        pytest.skip(
            f"BLOCKED: {blocker} - baseline {entry['id']} is not frozen yet: its files are unknown "
            "until the audit of the production tree"
        )
    if not os.environ.get(entry["root_env"]):
        pytest.skip(
            f"BLOCKED: {blocker} - environment variable {entry['root_env']} is not set: the artifacts "
            f"of baseline {entry['id']} are not available on this machine"
        )
    result = bf.verify_baseline(entry, repo_root=bf.REPO_ROOT)
    if result["status"] == "UNVERIFIED":
        pytest.skip(f"BLOCKED: {blocker} - baseline {entry['id']}: {result['reason']}")
    assert result["status"] == "OK", (
        f"baseline {entry['id']} differs from its frozen manifest ({result['reason']}): "
        f"added={result['added']} removed={result['removed']} changed={result['changed']} "
        f"problems={result['problems']}"
    )


@pytest.mark.acceptance("S00-ACC-06")
@pytest.mark.requirement("S00-ACC-06", "S00-ASG-01", "M-AUTH-12")
def test_no_production_changes() -> None:
    paths = bf.resolve_production_manifests()
    if isinstance(paths, str):
        pytest.skip(
            f"BLOCKED: B-001 - {paths}: manifests of the production tree taken before and after the "
            "session are needed (tools/audit/gie_baseline_audit.py manifest, run by the owner)"
        )
    before, after = paths
    result = bf.compare_production_manifests(before, after)
    assert result["identical"], f"production tree changed or not proven unchanged: {result['message']}"
