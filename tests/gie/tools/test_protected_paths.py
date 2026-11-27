"""Tests for ``tools/check_protected_paths.py`` - acceptance test S00-ACC-06 "no production changes".

Requirements: S00-ACC-06, OWN-13 (preserve all existing trading systems), M-AUTH-12 (do not modify
the files of the existing systems).

The expected behaviour is taken from the rule texts PP000-PP006 in the module documentation of the
tool, rule 2 of ``CLAUDE.md`` (the pre-existing file ``Start`` stays byte-identical) and
``docs/WORKTREES.md`` (``main`` is never updated) - not from the implementation.  Blob ids are
cross-checked against git itself (``git hash-object``, ``git rev-parse``), never against the tool.

SYNTHETIC FIXTURE: every repository, commit, file and record in this module is invented for
testing.  The repositories are created with ``git init`` below pytest's ``tmp_path`` and only
there; the identity of the fixture committer is passed with ``-c`` options, so no configuration is
written anywhere.  The real repository is only read: no test changes its files or its git state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tools import check_protected_paths as pp

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.requirement("S00-ACC-06", "OWN-13", "M-AUTH-12"),
    pytest.mark.acceptance("S00-ACC-06"),
]

GRAPH = pp.GRAPH_PATH
MAIN = "refs/heads/main"
ORIGIN_MAIN = "refs/remotes/origin/main"
#: SYNTHETIC FIXTURE: content of an invented pre-existing file (LF line ends, no final blank line).
START_BYTES = b'echo "# fixture" >> README.md\ngit init\ngit add README.md\n'
#: The blob id git gives an empty file - a published constant, independent of this project.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

Found = set[tuple[str, str, int]]


# ============================================================================== synthetic fixtures
def git_bytes(cwd: Path, *arguments: str, data: bytes | None = None) -> bytes:
    """Run git inside a temporary fixture repository (never in the real one)."""
    assert REPO_ROOT not in (cwd, *cwd.parents), "fixture git commands must stay below tmp_path"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=GIE Synthetic Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "advice.detachedHead=false",
            *arguments,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        input=data,
        env=environment,
    )
    return completed.stdout


def git(cwd: Path, *arguments: str) -> str:
    return git_bytes(cwd, *arguments).decode("utf-8").strip()


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--allow-empty", "-m", f"SYNTHETIC FIXTURE: {message}")
    return git(root, "rev-parse", "HEAD")


def write_record(root: Path, base_commit: object, paths: object) -> None:
    """Write a SYNTHETIC FIXTURE dependency graph that holds only the record of protected paths."""
    document = {"schema_version": 1, "protected_paths": {"base_commit": base_commit, "paths": paths}}
    target = root / GRAPH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class Repository:
    root: Path
    #: Commit id of the state "before the project" (the tip of main).
    base: str
    #: Blob id of ``Start`` at the base commit, as git reports it.
    blob: str


def build_repository(root: Path, *, extra: dict[str, bytes] | None = None) -> Repository:
    """SYNTHETIC FIXTURE repository: ``main`` holds the pre-existing files, a second branch the project."""
    root.mkdir(parents=True)
    git(root, "init", "--quiet")
    (root / "Start").write_bytes(START_BYTES)
    for relative, content in (extra or {}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(content)
    base = commit_all(root, "state before the project")
    git(root, "branch", "-M", "main")
    git(root, "checkout", "--quiet", "-b", "fixture-development")
    blob = git(root, "rev-parse", f"{base}:Start")
    paths = {
        "Start": blob,
        **{relative: git(root, "rev-parse", f"{base}:{relative}") for relative in extra or {}},
    }
    write_record(root, base, paths)
    commit_all(root, "project work")
    return Repository(root=root, base=base, blob=blob)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")
    return build_repository(tmp_path / "repository")


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """SYNTHETIC FIXTURE directory without git: ``Start`` and a record that matches it."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "Start").write_bytes(START_BYTES)
    write_record(root, "a" * 40, {"Start": pp.blob_sha1(START_BYTES)})
    return root


def found(violations: list[pp.Violation]) -> Found:
    return {(violation.rule, violation.path, violation.line) for violation in violations}


def record_line(root: Path, path: str) -> int:
    """The line of the entry of *path* in the fixture graph."""
    lines = (root / GRAPH).read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, start=1) if line.strip().startswith(f"{path}:"))


def snapshot(root: Path) -> dict[str, str]:
    """Every file, link and directory below *root* - the ``.git`` directory included."""
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = f"link to {os.readlink(path)}"
        elif path.is_dir():
            state[relative] = "directory"
        else:
            state[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def make_link(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("BLOCKED: this platform or account cannot create symbolic links")


# ============================================================================== the blob hash
def test_blob_sha1_of_the_empty_file_is_the_published_constant() -> None:
    assert pp.blob_sha1(b"") == EMPTY_BLOB


@pytest.mark.parametrize(
    "data",
    [b"", b"a", START_BYTES, START_BYTES.replace(b"\n", b"\r\n"), b"no final newline", bytes(range(256)) * 3],
    ids=["empty", "one-byte", "start", "start-crlf", "no-final-newline", "binary"],
)
def test_blob_sha1_is_the_identity_git_itself_computes(repository: Repository, data: bytes) -> None:
    assert (
        pp.blob_sha1(data) == git_bytes(repository.root, "hash-object", "--stdin", data=data).decode().strip()
    )


# ============================================================================== unchanged
def test_unchanged_repository_passes_and_says_what_was_compared(repository: Repository) -> None:
    report = pp.scan(repository.root)
    assert report.violations == ()
    assert report.protected == ("Start",)
    assert report.base_commit == repository.base
    assert report.history_checked is True
    # main exists and is the base commit; origin/main does not exist in this fixture: a note, no violation.
    assert len(report.notes) == 1
    assert ORIGIN_MAIN in report.notes[0]
    assert "not a violation" in report.notes[0]
    assert pp.check(repository.root) == []


# ============================================================================== PP001, PP002, PP004
@pytest.mark.parametrize(
    "changed",
    [START_BYTES + b"\n", START_BYTES[:-1], START_BYTES.replace(b"e", b"E", 1), b"", START_BYTES + b" "],
    ids=["one-byte-appended", "one-byte-removed", "one-byte-replaced", "emptied", "trailing-space"],
)
def test_pp002_content_changed_by_a_single_byte(repository: Repository, changed: bytes) -> None:
    (repository.root / "Start").write_bytes(changed)
    violations = pp.check(repository.root)
    assert found(violations) == {("PP002", "Start", 0)}
    actual = git_bytes(repository.root, "hash-object", "--stdin", data=changed).decode().strip()
    assert actual in violations[0].message
    assert repository.blob in violations[0].message


def test_pp002_end_of_line_conversion_is_a_change(repository: Repository) -> None:
    converted = START_BYTES.replace(b"\n", b"\r\n")
    assert converted != START_BYTES
    (repository.root / "Start").write_bytes(converted)
    assert found(pp.check(repository.root)) == {("PP002", "Start", 0)}
    # Writing the original bytes back restores the pass: nothing but the content is compared.
    (repository.root / "Start").write_bytes(START_BYTES)
    assert pp.check(repository.root) == []


def test_pp001_protected_path_deleted(repository: Repository) -> None:
    (repository.root / "Start").unlink()
    violations = pp.check(repository.root)
    assert found(violations) == {("PP001", "Start", 0)}
    assert "missing" in violations[0].message


def test_pp004_protected_path_replaced_by_a_symbolic_link(repository: Repository, tmp_path: Path) -> None:
    copy = tmp_path / "copy_of_start"
    copy.write_bytes(START_BYTES)  # identical content: only the kind of file differs
    (repository.root / "Start").unlink()
    make_link(repository.root / "Start", copy)
    violations = pp.check(repository.root)
    assert found(violations) == {("PP004", "Start", 0)}
    assert "symbolic link" in violations[0].message


def test_pp004_protected_path_replaced_by_a_directory(repository: Repository) -> None:
    (repository.root / "Start").unlink()
    (repository.root / "Start").mkdir()
    (repository.root / "Start" / "Start").write_bytes(START_BYTES)
    violations = pp.check(repository.root)
    assert found(violations) == {("PP004", "Start", 0)}
    assert "directory" in violations[0].message


def test_pp004_directory_on_the_way_is_a_symbolic_link(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")
    legacy = "legacy/bot.py"
    repository = build_repository(tmp_path / "repository", extra={legacy: b"print('synthetic fixture')\n"})
    assert pp.check(repository.root) == []
    moved = tmp_path / "moved_legacy"
    shutil.move(str(repository.root / "legacy"), str(moved))
    make_link(repository.root / "legacy", moved, directory=True)
    violations = pp.check(repository.root)
    assert found(violations) == {("PP004", legacy, 0)}
    assert "legacy" in violations[0].message


# ============================================================================== PP003, PP006
def test_pp003_recorded_hash_differs_from_the_base_commit(repository: Repository) -> None:
    other = pp.blob_sha1(b"SYNTHETIC FIXTURE: some other content\n")
    write_record(repository.root, repository.base, {"Start": other})
    violations = pp.check(repository.root)
    line = record_line(repository.root, "Start")
    # The record is wrong (PP003) and therefore the untouched file no longer matches it (PP002).
    assert found(violations) == {("PP003", GRAPH, line), ("PP002", "Start", 0)}
    message = next(v.message for v in violations if v.rule == "PP003")
    assert other in message
    assert repository.blob in message


def test_pp003_catches_a_change_made_together_with_its_record(repository: Repository) -> None:
    tampered = START_BYTES + b"rm -rf production\n"
    (repository.root / "Start").write_bytes(tampered)
    write_record(repository.root, repository.base, {"Start": pp.blob_sha1(tampered)})
    violations = pp.check(repository.root)
    assert found(violations) == {("PP003", GRAPH, record_line(repository.root, "Start"))}
    # Without git history this particular change cannot be seen - and the output says which rules
    # were not evaluated instead of claiming a full pass.
    report = pp.scan(repository.root, use_git=False)
    assert report.violations == ()
    assert report.history_checked is False
    assert any("PP003" in note for note in report.notes)


def test_pp003_recorded_path_does_not_exist_at_the_base_commit(repository: Repository) -> None:
    added = b"SYNTHETIC FIXTURE: created by the project, not pre-existing\n"
    (repository.root / "Later").write_bytes(added)
    write_record(repository.root, repository.base, {"Start": repository.blob, "Later": pp.blob_sha1(added)})
    violations = pp.check(repository.root)
    assert found(violations) == {("PP003", GRAPH, record_line(repository.root, "Later"))}
    assert "does not exist at base commit" in violations[0].message


def test_pp006_every_file_of_the_base_commit_must_be_protected(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")
    legacy = "legacy/bot.py"
    repository = build_repository(tmp_path / "repository", extra={legacy: b"print('synthetic fixture')\n"})
    assert pp.check(repository.root) == []
    write_record(repository.root, repository.base, {"Start": repository.blob})  # the entry is dropped
    (repository.root / legacy).write_bytes(b"print('changed by the project')\n")
    violations = pp.check(repository.root)
    assert found(violations) == {("PP006", legacy, 0)}
    assert "not listed" in violations[0].message


# ============================================================================== missing base commit
def test_missing_base_commit_is_a_note_not_a_violation(repository: Repository, tmp_path: Path) -> None:
    clone = tmp_path / "shallow_clone"
    git(
        tmp_path,
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--branch",
        "fixture-development",
        repository.root.as_uri(),
        str(clone),
    )
    # The fixture really lacks the base commit and both main refs.
    absent = subprocess.run(
        ["git", "cat-file", "-e", f"{repository.base}^{{commit}}"],
        cwd=clone,
        capture_output=True,
        check=False,
    )
    assert absent.returncode != 0
    report = pp.scan(clone)
    assert report.violations == ()
    assert report.history_checked is False
    notes = "\n".join(report.notes)
    assert f"base commit {repository.base} is not present in this clone" in notes
    assert "not a violation" in notes
    assert "PP003" in notes
    assert MAIN in notes
    assert ORIGIN_MAIN in notes
    # The comparison with the record does not need history and still works.
    (clone / "Start").write_bytes(START_BYTES + b"#")
    assert found(pp.check(clone)) == {("PP002", "Start", 0)}


# ============================================================================== PP005
def test_pp005_main_was_advanced(repository: Repository) -> None:
    root = repository.root
    git(root, "checkout", "--quiet", "main")
    (root / "added_on_main.txt").write_bytes(b"SYNTHETIC FIXTURE: main must never receive commits\n")
    advanced = commit_all(root, "a commit on main")
    git(root, "checkout", "--quiet", "fixture-development")
    violations = pp.check(root)
    assert found(violations) == {("PP005", MAIN, 0)}
    assert advanced in violations[0].message
    assert repository.base in violations[0].message


def test_pp005_origin_main_is_compared_when_it_exists(repository: Repository) -> None:
    root = repository.root
    git(root, "update-ref", ORIGIN_MAIN, repository.base)
    report = pp.scan(root)
    assert report.violations == ()
    assert report.notes == ()  # both refs exist and equal the base commit: nothing left to say
    git(root, "update-ref", ORIGIN_MAIN, git(root, "rev-parse", "fixture-development"))
    assert found(pp.check(root)) == {("PP005", ORIGIN_MAIN, 0)}


def test_pp005_main_behind_the_base_commit_was_not_advanced(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "Start").write_bytes(START_BYTES)
    older = commit_all(root, "older pre-existing state")
    git(root, "branch", "-M", "main")
    git(root, "checkout", "--quiet", "-b", "fixture-development")
    (root / "Second").write_bytes(b"SYNTHETIC FIXTURE: second pre-existing file\n")
    base = commit_all(root, "newest pre-existing state, the base commit")
    paths = {name: git(root, "rev-parse", f"{base}:{name}") for name in ("Start", "Second")}
    write_record(root, base, paths)
    commit_all(root, "project work")
    assert git(root, "rev-parse", MAIN) == older
    report = pp.scan(root)
    assert report.violations == ()
    assert any(MAIN in note and "ancestor" in note for note in report.notes)


def test_pp005_fails_closed_when_ancestry_cannot_be_shown(repository: Repository) -> None:
    unknown = "f" * 40  # SYNTHETIC FIXTURE: a well-formed commit id that no repository here contains
    write_record(repository.root, unknown, {"Start": repository.blob})
    report = pp.scan(repository.root)
    assert found(list(report.violations)) == {("PP005", MAIN, 0)}
    assert "cannot be shown" in report.violations[0].message
    assert any("is not present in this clone" in note for note in report.notes)


# ============================================================================== PP000
VALID_HASH = pp.blob_sha1(START_BYTES)
#: SYNTHETIC FIXTURE: the first lines of a well-formed record, up to the entries of 'paths'.
RECORD_HEAD = f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n"

UNUSABLE_RECORDS: list[tuple[str, str | None, str]] = [
    ("graph-missing", None, "is missing"),
    ("not-yaml", "protected_paths: [unclosed\n", "not valid YAML"),
    ("no-record", "schema_version: 1\n", "must be a mapping"),
    ("record-is-a-list", "protected_paths: [Start]\n", "must be a mapping"),
    ("no-base-commit", f"protected_paths:\n  paths:\n    Start: {VALID_HASH}\n", "'base_commit' must be"),
    (
        "short-base-commit",
        f"protected_paths:\n  base_commit: abc1234\n  paths:\n    Start: {VALID_HASH}\n",
        "'base_commit' must be",
    ),
    (
        "upper-case-base-commit",
        f"protected_paths:\n  base_commit: {'A' * 40}\n  paths:\n    Start: {VALID_HASH}\n",
        "'base_commit' must be",
    ),
    (
        "base-commit-is-a-branch",
        f"protected_paths:\n  base_commit: main\n  paths:\n    Start: {VALID_HASH}\n",
        "'base_commit' must be",
    ),
    ("no-paths", f"protected_paths:\n  base_commit: {'a' * 40}\n", "'paths' must map"),
    (
        "paths-is-a-list",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths: [Start]\n",
        "'paths' must map",
    ),
    (
        "nothing-protected",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths: {{}}\n",
        "no path is protected",
    ),
    (
        "absolute-path",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    /srv/Start: {VALID_HASH}\n",
        "not a relative POSIX path",
    ),
    (
        "parent-path",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    ../Start: {VALID_HASH}\n",
        "not a relative POSIX path",
    ),
    (
        "backslash-path",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    'legacy\\Start': {VALID_HASH}\n",
        "not a relative POSIX path",
    ),
    (
        "path-inside-git-directory",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    .git/config: {VALID_HASH}\n",
        "not a relative POSIX path",
    ),
    (
        "hash-not-hexadecimal",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: unchanged\n",
        "recorded hash of Start",
    ),
    (
        "hash-too-short",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: {VALID_HASH[:39]}\n",
        "recorded hash of Start",
    ),
    (
        "hash-upper-case",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: {VALID_HASH.upper()}X\n",
        "recorded hash of Start",
    ),
    (
        "path-listed-twice",
        RECORD_HEAD + f"    Start: {VALID_HASH}\n    Start: {VALID_HASH}\n",
        "listed twice",
    ),
    (
        "record-given-twice",
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: {VALID_HASH}\n"
        f"protected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: {VALID_HASH}\n",
        "duplicate key 'protected_paths'",
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), [case[1:] for case in UNUSABLE_RECORDS], ids=[case[0] for case in UNUSABLE_RECORDS]
)
def test_pp000_unusable_record_fails_closed(plain: Path, text: str | None, expected: str) -> None:
    assert pp.check(plain, use_git=False) == []  # the fixture is valid before the record is replaced
    if text is None:
        (plain / GRAPH).unlink()
    else:
        (plain / GRAPH).write_text(text, encoding="utf-8", newline="\n")
    report = pp.scan(plain, use_git=False)
    assert report.violations, "an unusable record was accepted"
    assert {violation.rule for violation in report.violations} == {"PP000"}
    assert {violation.path for violation in report.violations} == {GRAPH}
    assert any(expected in violation.message for violation in report.violations), report.violations
    assert report.protected == ()


def test_pp000_points_at_the_line_of_the_bad_entry(plain: Path) -> None:
    text = f"schema_version: 1\nprotected_paths:\n  base_commit: {'a' * 40}\n  paths:\n    Start: nonsense\n"
    (plain / GRAPH).write_text(text, encoding="utf-8", newline="\n")
    assert found(pp.check(plain, use_git=False)) == {("PP000", GRAPH, 5)}


# ============================================================================== without git
def test_without_git_history_the_working_tree_is_still_compared(plain: Path) -> None:
    for use_git in (True, False):  # a directory that is not a work tree, and git switched off
        report = pp.scan(plain, use_git=use_git)
        assert report.violations == ()
        assert report.history_checked is False
        assert len(report.notes) == 1
        assert "PP003, PP005 and PP006 were not evaluated" in report.notes[0]
        assert "not a violation" in report.notes[0]
    (plain / "Start").write_bytes(START_BYTES.replace(b"\n", b"\r\n"))
    assert found(pp.check(plain)) == {("PP002", "Start", 0)}
    (plain / "Start").unlink()
    assert found(pp.check(plain)) == {("PP001", "Start", 0)}


def test_root_below_the_top_level_of_a_work_tree_is_not_that_repository(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")
    outer = tmp_path / "outer"
    inner = outer / "nested"
    inner.mkdir(parents=True)
    (inner / "Start").write_bytes(START_BYTES)
    write_record(inner, "a" * 40, {"Start": VALID_HASH})
    git(outer, "init", "--quiet")
    commit_all(outer, "outer repository")
    git(outer, "branch", "-M", "main")
    report = pp.scan(inner)
    assert report.violations == ()  # main of the outer repository is not judged against this record
    assert "not the top level" in report.notes[0]


# ============================================================================== never writes
def test_the_check_never_writes_anything(repository: Repository) -> None:
    root = repository.root
    (root / "Start").write_bytes(START_BYTES + b"changed\n")
    git(root, "update-ref", ORIGIN_MAIN, git(root, "rev-parse", "fixture-development"))
    before = snapshot(root)
    assert ".git/HEAD" in before  # the snapshot really covers the repository data
    assert {violation.rule for violation in pp.check(root)} == {"PP002", "PP005"}
    assert pp.main(["--root", str(root)]) == 1
    assert pp.main(["--root", str(root), "--json"]) == 1
    assert snapshot(root) == before
    (root / "Start").write_bytes(START_BYTES)
    git(root, "update-ref", ORIGIN_MAIN, repository.base)
    before = snapshot(root)
    assert pp.main(["--root", str(root)]) == 0
    assert snapshot(root) == before


# ============================================================================== command line
def test_cli_output_and_exit_status(repository: Repository, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository.root
    assert pp.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[-1].startswith("check_protected_paths: OK - 1 protected path(s) byte-identical")
    assert repository.base in lines[-1]
    assert lines[0].startswith(f"check_protected_paths: note: {ORIGIN_MAIN}")

    (root / "Start").write_bytes(START_BYTES + b"x")
    assert pp.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("Start:0: PP002 ")
    assert "OK" not in captured.out
    assert "FAILED - 1 violation(s)" in captured.err


def test_cli_json_document(repository: Repository, capsys: pytest.CaptureFixture[str]) -> None:
    (repository.root / "Start").unlink()
    assert pp.main(["--root", str(repository.root), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["tool"] == "tools/check_protected_paths.py"
    assert document["base_commit"] == repository.base
    assert document["protected_paths"] == ["Start"]
    assert document["history_checked"] is True
    assert document["violation_count"] == 1
    assert [(v["rule"], v["path"], v["line"]) for v in document["violations"]] == [("PP001", "Start", 0)]
    assert sorted(document["rules"]) == [f"PP00{number}" for number in range(7)]
    assert len(document["notes"]) == 1


def test_cli_rejects_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pp.main(["--root", str(tmp_path / "missing")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_module_entry_point_runs_as_documented(plain: Path) -> None:
    (plain / "Start").write_bytes(START_BYTES + b"x")
    completed = subprocess.run(
        [sys.executable, "-m", "tools.check_protected_paths", "--root", str(plain)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "Start:0: PP002 " in completed.stdout


# ============================================================================== the real repository
def test_real_repository_has_no_production_changes() -> None:
    report = pp.scan(REPO_ROOT)
    assert "Start" in report.protected, "the pre-existing file named in CLAUDE.md rule 2 is not protected"
    assert [violation.render() for violation in report.violations] == []
    assert pp.check(REPO_ROOT) == []
