"""Self-tests of the baseline freeze / regression harness ``tools/baseline_freeze.py``.

Requirements: G02-08 (preserve GCE-B), G02-09 (preserve exact Round 1 baselines) and S00-ACC-05
(baseline regression).  These tests prove the *harness*; they are not the acceptance test, which is
``tests/gie/baseline/test_baseline_regression.py`` and stays BLOCKED until the real baselines are
frozen (B-001).

SYNTHETIC FIXTURE: the baseline tree built by `_build_tree` (file names, CSV rows, JSON values),
the decision-log entry D-900 and the production manifests below are invented for these tests.
They are not the existing system's baselines, not market data and not evidence of performance.
Every test works on a copy of the registry under ``tmp_path``; the repository is never written.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools import baseline_freeze as bf
from tools.audit.gie_baseline_audit import read_manifest, write_manifest

pytestmark = pytest.mark.requirement("G02-08", "G02-09", "S00-ACC-05")

REPO = Path(__file__).resolve().parents[3]
INCLUDES = ["gce/*", "round1"]
FIXED_MTIME_NS = 1_700_000_000_000_000_000  # fixed modification time of every fixture file

# SYNTHETIC FIXTURE content (relative path -> bytes).
FIXTURE_FILES: dict[str, bytes] = {
    "gce/trades.csv": b"# SYNTHETIC FIXTURE\nid,side,r\n1,long,0.5\n2,short,-1.0\n",
    "gce/summary.json": b'{"origin": "SYNTHETIC_FIXTURE", "rows": 2}\n',
    "gce/accounts_export.txt": b"SYNTHETIC FIXTURE - a name the audit tool treats as sensitive\n",
    "gce/__pycache__/cached.bin": b"\x00\x01SYNTHETIC FIXTURE\x02",
    "round1/report.txt": b"SYNTHETIC FIXTURE round 1 report\nline two\n",
    "round1/deep/nested/data.bin": bytes(range(256)) * 4,
    "unrelated/notes.txt": b"SYNTHETIC FIXTURE - not part of any include pattern\n",
}


def _build_tree(root: Path) -> Path:
    for rel, data in FIXTURE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return root


def _snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    """Every entry below ``root`` with content, mode and modification time (links not followed)."""
    snap: dict[str, tuple[bytes, int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            st = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            snap[path.relative_to(root).as_posix()] = (data, st.st_mode, st.st_mtime_ns)
    return snap


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository copy: the real registry and matrix, a synthetic decision log."""
    copy = tmp_path / "repo"
    for rel in (bf.REGISTRY_REL, "specifications/requirements_matrix.yaml"):
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, target)
    log = copy / bf.DECISION_LOG_REL
    log.parent.mkdir(parents=True)
    log.write_bytes(b"# Decision log (SYNTHETIC FIXTURE)\n\n## D-900 - synthetic replacement approval\n")
    return copy


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _build_tree(tmp_path / "baseline_tree")


def _frozen_repo(repo: Path, tree: Path) -> dict[str, Any]:
    return bf.freeze("gce-b", tree, INCLUDES, repo_root=repo)


def _verify(repo: Path, tree: Path | None, baseline_id: str | None = "gce-b") -> dict[str, Any]:
    return bf.verify(repo_root=repo, baseline_id=baseline_id, root=tree, environ={})


def _only(report: dict[str, Any]) -> dict[str, Any]:
    assert len(report["results"]) == 1
    result: dict[str, Any] = report["results"][0]
    return result


# --------------------------------------------------------------------------------------------
# freeze + verify
# --------------------------------------------------------------------------------------------


def test_freeze_then_verify_is_identical(repo: Path, tree: Path) -> None:
    summary = _frozen_repo(repo, tree)
    included = [rel for rel in FIXTURE_FILES if not rel.startswith("unrelated/")]
    assert summary["file_count"] == len(included) == 6
    assert summary["total_bytes"] == sum(len(FIXTURE_FILES[rel]) for rel in included)
    assert summary["includes"] == INCLUDES
    assert summary["decision"] is None

    manifest_path = repo / bf.FROZEN_REL / "gce-b.manifest.jsonl"
    manifest_bytes = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == summary["manifest_sha256"]
    assert b"\r" not in manifest_bytes
    assert manifest_bytes.endswith(b"\n")
    summary_bytes = (repo / bf.FROZEN_REL / "gce-b.summary.json").read_bytes()
    assert b"\r" not in summary_bytes
    assert json.loads(summary_bytes) == summary

    records = read_manifest(manifest_path)
    assert [r["path"] for r in records] == sorted(included)
    for record in records:
        expected = hashlib.sha256(FIXTURE_FILES[record["path"]]).hexdigest()
        assert record["sha256"] == expected, record["path"]  # sensitive name and __pycache__ hashed too
    assert next(r for r in records if r["path"] == "gce/accounts_export.txt").get("sensitive") is True

    registry = bf.load_registry(repo)
    entry = next(e for e in registry["baselines"] if e["id"] == "gce-b")
    assert entry["status"] == "frozen"
    assert entry["manifest"] == "specifications/baselines/frozen/gce-b.manifest.jsonl"
    assert entry["include"] == INCLUDES
    assert b"\r" not in (repo / bf.REGISTRY_REL).read_bytes()

    report = _verify(repo, tree)
    result = _only(report)
    assert report["exit_code"] == bf.EXIT_OK
    assert result["status"] == "OK", result
    assert result["files"] == 6


def _mutate_and_verify(repo: Path, tree: Path, mutate: Any) -> dict[str, Any]:
    _frozen_repo(repo, tree)
    mutate(tree)
    report = _verify(repo, tree)
    assert report["exit_code"] == bf.EXIT_MISMATCH, report
    result = _only(report)
    assert result["status"] == "MISMATCH"
    return result


def test_one_changed_byte_is_detected(repo: Path, tree: Path) -> None:
    def flip(root: Path) -> None:
        path = root / "round1/deep/nested/data.bin"
        data = bytearray(path.read_bytes())
        data[100] ^= 0x01
        path.write_bytes(bytes(data))
        os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))  # same size, same time: only the hash differs

    result = _mutate_and_verify(repo, tree, flip)
    assert result["changed"] == ["round1/deep/nested/data.bin (sha256)"]
    assert result["added"] == []
    assert result["removed"] == []


def test_appended_newline_is_detected(repo: Path, tree: Path) -> None:
    def append(root: Path) -> None:
        with open(root / "gce/trades.csv", "ab") as handle:
            handle.write(b"\n")

    result = _mutate_and_verify(repo, tree, append)
    assert result["changed"] == ["gce/trades.csv (size+sha256)"]


def test_line_ending_change_only_is_detected(repo: Path, tree: Path) -> None:
    def crlf(root: Path) -> None:
        path = root / "round1/report.txt"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    result = _mutate_and_verify(repo, tree, crlf)
    assert result["changed"] == ["round1/report.txt (size+sha256)"]


def test_added_matching_file_is_detected(repo: Path, tree: Path) -> None:
    def add(root: Path) -> None:
        (root / "gce/extra.csv").write_bytes(b"# SYNTHETIC FIXTURE\n")

    result = _mutate_and_verify(repo, tree, add)
    assert result["added"] == ["gce/extra.csv"]
    assert result["removed"] == []
    assert result["changed"] == []


def test_added_file_outside_includes_is_not_part_of_the_baseline(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    (tree / "unrelated/new.txt").write_bytes(b"SYNTHETIC FIXTURE\n")
    report = _verify(repo, tree)
    assert report["exit_code"] == bf.EXIT_OK, report


def test_deleted_file_is_detected(repo: Path, tree: Path) -> None:
    result = _mutate_and_verify(repo, tree, lambda root: (root / "gce/summary.json").unlink())
    assert result["removed"] == ["gce/summary.json"]
    assert result["added"] == []


def test_renamed_file_is_detected(repo: Path, tree: Path) -> None:
    def rename(root: Path) -> None:
        (root / "round1/report.txt").rename(root / "round1/report_renamed.txt")

    result = _mutate_and_verify(repo, tree, rename)
    assert result["removed"] == ["round1/report.txt"]
    assert result["added"] == ["round1/report_renamed.txt"]
    assert result["changed"] == []


def test_modification_time_only_is_not_a_mismatch(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    later = FIXED_MTIME_NS + 86_400 * 10**9
    os.utime(tree / "gce/trades.csv", ns=(later, later))
    report = _verify(repo, tree)
    result = _only(report)
    assert report["exit_code"] == bf.EXIT_OK
    assert result["status"] == "OK"
    assert result["mtime_only"] == ["gce/trades.csv"]


def test_symlink_reachable_by_include_is_a_mismatch(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    try:
        (tree / "gce/link.csv").symlink_to(tree / "unrelated/notes.txt")
    except OSError as exc:  # pragma: no cover - platform without symlink permission
        pytest.skip(f"BLOCKED: platform - symbolic links cannot be created here ({type(exc).__name__})")
    result = _only(_verify(repo, tree))
    assert result["status"] == "MISMATCH"
    assert result["problems"] == ["gce/link.csv (symbolic link)"]


def test_tampered_frozen_manifest_is_invalid(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    manifest = repo / bf.FROZEN_REL / "gce-b.manifest.jsonl"
    manifest.write_bytes(manifest.read_bytes().replace(b'"size":', b'"size": ', 1))
    report = _verify(repo, tree)
    assert report["exit_code"] == bf.EXIT_MISMATCH
    assert _only(report)["status"] == "INVALID"


def test_baseline_tree_is_never_modified(repo: Path, tree: Path) -> None:
    before = _snapshot(tree)
    _frozen_repo(repo, tree)
    _verify(repo, tree)
    bf.status(repo_root=repo)
    with pytest.raises(bf.BaselineRefused):
        _frozen_repo(repo, tree)
    assert _snapshot(tree) == before


# --------------------------------------------------------------------------------------------
# freeze refusals
# --------------------------------------------------------------------------------------------


def test_freeze_refuses_overwrite_without_replace_and_decision(repo: Path, tree: Path) -> None:
    first = _frozen_repo(repo, tree)
    manifest = repo / bf.FROZEN_REL / "gce-b.manifest.jsonl"
    frozen_bytes = manifest.read_bytes()
    (tree / "gce/trades.csv").write_bytes(b"# SYNTHETIC FIXTURE changed\n")

    with pytest.raises(bf.BaselineRefused, match="already frozen"):
        bf.freeze("gce-b", tree, INCLUDES, repo_root=repo)
    with pytest.raises(bf.BaselineRefused, match="already frozen"):
        bf.freeze("gce-b", tree, INCLUDES, repo_root=repo, replace=True)
    with pytest.raises(bf.BaselineRefused, match="D-nnn"):
        bf.freeze("gce-b", tree, INCLUDES, repo_root=repo, replace=True, decision="approved")
    with pytest.raises(bf.BaselineRefused, match=r"not in docs/DECISION_LOG\.md"):
        bf.freeze("gce-b", tree, INCLUDES, repo_root=repo, replace=True, decision="D-901")
    assert manifest.read_bytes() == frozen_bytes

    second = bf.freeze("gce-b", tree, INCLUDES, repo_root=repo, replace=True, decision="D-900")
    assert second["decision"] == "D-900"
    assert second["replaces_manifest_sha256"] == first["manifest_sha256"]
    assert second["manifest_sha256"] != first["manifest_sha256"]
    entry = next(e for e in bf.load_registry(repo)["baselines"] if e["id"] == "gce-b")
    assert entry["decision"] == "D-900"
    assert _verify(repo, tree)["exit_code"] == bf.EXIT_OK


def test_replace_of_a_baseline_that_is_not_frozen_is_refused(repo: Path, tree: Path) -> None:
    with pytest.raises(bf.BaselineRefused, match="not frozen"):
        bf.freeze("gce-b", tree, INCLUDES, repo_root=repo, replace=True, decision="D-900")


def test_freeze_refuses_output_inside_the_baseline_root(repo: Path, tree: Path, tmp_path: Path) -> None:
    before = _snapshot(tmp_path / "baseline_tree")
    registry_before = (repo / bf.REGISTRY_REL).read_bytes()
    # tmp_path contains both the repository copy and the tree: outputs would land inside the root.
    with pytest.raises(bf.BaselineRefused, match="inside --root"):
        bf.freeze("gce-b", tmp_path, ["baseline_tree/gce/*"], repo_root=repo)
    with pytest.raises(bf.BaselineRefused, match="inside --root"):
        bf.freeze("gce-b", repo, ["specifications/*"], repo_root=repo)
    assert not (repo / bf.FROZEN_REL).exists()
    assert (repo / bf.REGISTRY_REL).read_bytes() == registry_before
    assert _snapshot(tmp_path / "baseline_tree") == before


def test_freeze_refuses_root_inside_the_frozen_directory(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    with pytest.raises(bf.BaselineRefused, match="inside specifications/baselines/frozen"):
        bf.freeze("round-1", repo / bf.FROZEN_REL, ["*"], repo_root=repo)


@pytest.mark.parametrize(
    ("includes", "message"),
    [
        ([], "at least one"),
        (["nothing/*"], "match no file"),
        (["gce/*", "missing/*"], "missing/"),
        (["/gce/*"], "absolute"),
        (["../outside/*"], r"\.\."),
        (["gce/*", "gce/*"], "twice"),
    ],
)
def test_freeze_refuses_bad_include_patterns(
    repo: Path, tree: Path, includes: list[str], message: str
) -> None:
    with pytest.raises(bf.BaselineRefused, match=message):
        bf.freeze("gce-b", tree, includes, repo_root=repo)
    assert not (repo / bf.FROZEN_REL).exists()


def test_freeze_refuses_unknown_id_and_missing_root(repo: Path, tree: Path, tmp_path: Path) -> None:
    with pytest.raises(bf.BaselineRefused, match=r"not in specifications/baselines/registry\.yaml"):
        bf.freeze("no-such-baseline", tree, INCLUDES, repo_root=repo)
    with pytest.raises(bf.BaselineRefused, match="not a directory"):
        bf.freeze("gce-b", tmp_path / "absent", INCLUDES, repo_root=repo)


def test_freeze_refuses_a_link_the_includes_reach(repo: Path, tree: Path) -> None:
    try:
        (tree / "round1/alias").symlink_to(tree / "unrelated", target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform without symlink permission
        pytest.skip(f"BLOCKED: platform - symbolic links cannot be created here ({type(exc).__name__})")
    with pytest.raises(bf.BaselineRefused, match=r"round1/alias \(symbolic link\)"):
        _frozen_repo(repo, tree)
    assert not (repo / bf.FROZEN_REL / "gce-b.manifest.jsonl").exists()


def test_freeze_refuses_an_unhashed_record(monkeypatch: pytest.MonkeyPatch, repo: Path, tree: Path) -> None:
    """A file the audit tool did not hash (for example unreadable) makes freeze fail."""
    original = bf.build_baseline_manifest

    def with_unreadable(root: Any, includes: Any) -> list[dict[str, Any]]:
        records = original(root, includes)
        records[0] = {**records[0], "sha256": None, "skipped": "unreadable"}
        return records

    monkeypatch.setattr(bf, "build_baseline_manifest", with_unreadable)
    with pytest.raises(bf.BaselineRefused, match="not hashed: unreadable"):
        _frozen_repo(repo, tree)
    assert not (repo / bf.FROZEN_REL).exists()


# --------------------------------------------------------------------------------------------
# verify without a root, not-frozen baselines
# --------------------------------------------------------------------------------------------


def test_verify_without_root_is_unverified(repo: Path, tree: Path) -> None:
    _frozen_repo(repo, tree)
    report = bf.verify(repo_root=repo, baseline_id="gce-b", environ={})
    assert report["exit_code"] == bf.EXIT_UNVERIFIED
    result = _only(report)
    assert result["status"] == "UNVERIFIED"
    assert "GIE_BASELINE_ROOT is not set" in result["reason"]

    missing = bf.verify(
        repo_root=repo, baseline_id="gce-b", environ={"GIE_BASELINE_ROOT": str(tree / "nope")}
    )
    assert missing["exit_code"] == bf.EXIT_UNVERIFIED

    from_env = bf.verify(repo_root=repo, baseline_id="gce-b", environ={"GIE_BASELINE_ROOT": str(tree)})
    assert from_env["exit_code"] == bf.EXIT_OK


def test_verify_all_reports_not_frozen_as_unverified(repo: Path, tree: Path) -> None:
    report = bf.verify(repo_root=repo, environ={})
    assert report["exit_code"] == bf.EXIT_UNVERIFIED
    assert {r["id"] for r in report["results"]} == {"gce-b", "round-1", "round-2", "cavemen-trades"}
    assert all(r["status"] == "UNVERIFIED" and "B-001" in r["reason"] for r in report["results"])

    _frozen_repo(repo, tree)
    env = {"GIE_BASELINE_ROOT": str(tree)}
    mixed = bf.verify(repo_root=repo, environ=env)
    assert mixed["exit_code"] == bf.EXIT_UNVERIFIED  # gce-b OK, three others still not frozen
    (tree / "gce/summary.json").unlink()
    assert bf.verify(repo_root=repo, environ=env)["exit_code"] == bf.EXIT_MISMATCH  # mismatch wins


def test_verify_root_without_id_is_refused(repo: Path, tree: Path) -> None:
    with pytest.raises(bf.BaselineRefused, match="--root needs --id"):
        bf.verify(repo_root=repo, root=tree, environ={})


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def test_command_line(
    repo: Path, tree: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GIE_BASELINE_ROOT", raising=False)
    rr = ["--repo-root", str(repo)]
    args = ["freeze", *rr, "--id", "gce-b", "--root", str(tree), "--include", "gce/*", "--include", "round1"]
    assert bf.main(args) == bf.EXIT_OK
    assert "frozen gce-b: 6 files" in capsys.readouterr().out
    assert bf.main(args) == bf.EXIT_REFUSED
    assert "already frozen" in capsys.readouterr().err

    assert bf.main(["verify", *rr, "--id", "gce-b", "--root", str(tree)]) == bf.EXIT_OK
    assert "OK gce-b" in capsys.readouterr().out
    assert bf.main(["verify", *rr, "--id", "gce-b"]) == bf.EXIT_UNVERIFIED
    assert "UNVERIFIED gce-b" in capsys.readouterr().out

    (tree / "gce/summary.json").unlink()
    assert bf.main(["verify", *rr, "--id", "gce-b", "--root", str(tree)]) == bf.EXIT_MISMATCH
    assert "removed: gce/summary.json" in capsys.readouterr().out

    assert bf.main(["status", *rr]) == bf.EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["counts"] == {"frozen": 1, "not_frozen": 3}
    row = next(r for r in status["baselines"] if r["id"] == "gce-b")
    assert row["file_count"] == 6

    assert bf.main(["verify", *rr, "--id", "unknown"]) == bf.EXIT_REFUSED


# --------------------------------------------------------------------------------------------
# Determinism and registry form
# --------------------------------------------------------------------------------------------


def test_manifest_is_deterministic(tmp_path: Path) -> None:
    first = _build_tree(tmp_path / "a")
    second = _build_tree(tmp_path / "b")
    manifests = []
    for number, root in enumerate((first, first, second)):
        records = bf.build_baseline_manifest(root, INCLUDES)
        target = tmp_path / f"m{number}.jsonl"
        write_manifest(records, target)
        manifests.append(target.read_bytes())
    assert manifests[0] == manifests[1] == manifests[2]


def test_freeze_is_deterministic_across_repository_copies(tmp_path: Path) -> None:
    summaries = []
    for name in ("one", "two"):
        root = _build_tree(tmp_path / f"tree_{name}")
        repo = tmp_path / f"repo_{name}"
        (repo / "specifications/baselines").mkdir(parents=True)
        shutil.copyfile(REPO / bf.REGISTRY_REL, repo / bf.REGISTRY_REL)
        summaries.append(bf.freeze("round-1", root, ["round1"], repo_root=repo))
        summaries[-1]["registry"] = (repo / bf.REGISTRY_REL).read_bytes()
        summaries[-1]["manifest"] = (repo / bf.FROZEN_REL / "round-1.manifest.jsonl").read_bytes()
    assert summaries[0] == summaries[1]


def test_real_registry_is_canonical_and_valid() -> None:
    raw = (REPO / bf.REGISTRY_REL).read_bytes()
    document = bf.load_registry(REPO)
    assert bf.render_registry(document) == raw
    assert b"\r" not in raw
    assert bf.validate_registry(document, bf.known_requirement_ids(REPO)) == []


def test_registry_validation_rejects_inconsistent_entries() -> None:
    document = yaml.safe_load((REPO / bf.REGISTRY_REL).read_bytes())
    entry = document["baselines"][0]
    entry["status"] = "frozen"  # frozen without manifest and include
    entry["requirement_ids"] = ["NOPE-99"]
    problems = bf.validate_registry(document, bf.known_requirement_ids(REPO))
    assert any("manifest must be" in p for p in problems)
    assert any("include must be" in p for p in problems)
    assert any("NOPE-99" in p for p in problems)


# --------------------------------------------------------------------------------------------
# Production-tree comparison helper used by the S00-ACC-06 acceptance test
# --------------------------------------------------------------------------------------------


def _production_manifest(tmp_path: Path, name: str, root: Path) -> Path:
    target = tmp_path / f"{name}.jsonl"
    write_manifest(bf.build_baseline_manifest(root, ["*"]), target)
    return target


def test_production_comparison_identical_trees_pass(tmp_path: Path) -> None:
    root = _build_tree(tmp_path / "synthetic_production")
    before = _production_manifest(tmp_path, "before", root)
    after = _production_manifest(tmp_path, "after", root)
    result = bf.compare_production_manifests(before, after)
    assert result["identical"] is True, result["message"]
    assert result["message"] == f"production tree content identical: {len(FIXTURE_FILES)} files"


def test_production_comparison_changed_tree_fails(tmp_path: Path) -> None:
    root = _build_tree(tmp_path / "synthetic_production")
    before = _production_manifest(tmp_path, "before", root)
    (root / "unrelated/notes.txt").write_bytes(b"SYNTHETIC FIXTURE changed\n")
    (root / "gce/summary.json").unlink()
    (root / "added.txt").write_bytes(b"SYNTHETIC FIXTURE\n")
    after = _production_manifest(tmp_path, "after", root)
    result = bf.compare_production_manifests(before, after)
    assert result["identical"] is False
    assert result["added"] == ["added.txt"]
    assert result["removed"] == ["gce/summary.json"]
    assert result["changed"] == ["unrelated/notes.txt (size+sha256)"]
    for path in ("added.txt", "gce/summary.json", "unrelated/notes.txt"):
        assert path in result["message"]


def test_production_comparison_never_passes_without_proof() -> None:
    record = {"path": "a.txt", "size": 1, "mtime_ns": 1, "sha256": None, "skipped": "sensitive"}
    unhashed = bf.compare_production_manifests([record], [record])
    assert unhashed["identical"] is False
    assert "a.txt" in unhashed["message"]
    empty = bf.compare_production_manifests([], [])
    assert empty["identical"] is False
    assert "lists no file" in empty["message"]


def test_resolve_production_manifests(tmp_path: Path) -> None:
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_bytes(b"")
    names = (bf.PRODUCTION_MANIFEST_BEFORE_ENV, bf.PRODUCTION_MANIFEST_AFTER_ENV)
    assert "is not set" in str(bf.resolve_production_manifests({}))
    missing = bf.resolve_production_manifests({names[0]: str(before), names[1]: str(after)})
    assert isinstance(missing, str)
    assert "does not exist" in missing
    after.write_bytes(b"")
    assert bf.resolve_production_manifests({names[0]: str(before), names[1]: str(after)}) == (before, after)
