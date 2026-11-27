"""Tests for ``tools/check_docs_paths.py`` - documents name only paths that exist (DP000, DP001).

Requirements: M-AUTH-08 (never mark placeholders complete), OWN-21 (do not declare incomplete
components finished): a document that names a file claims that the file exists.

The expected behaviour is taken from the module documentation of the tool (what is scanned, what
counts as a repository path, the ignore list) - not from the implementation.

SYNTHETIC FIXTURE: every document, directory and dependency graph in this module is invented for
testing and lives below pytest's ``tmp_path``.  The real repository is only read; its findings are
reported, not asserted to be empty, because its documents are still being written.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools import check_docs_paths as dp

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.requirement("M-AUTH-08", "OWN-21")

GRAPH = dp.GRAPH_PATH

Found = set[tuple[str, str, int]]


# ============================================================================== synthetic fixtures
def write(root: Path, relative: str, text: str = "SYNTHETIC FIXTURE\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def make_tree(root: Path, readme: str, *, ignore: object | None = None) -> Path:
    """SYNTHETIC FIXTURE repository: a few existing files, a README with *readme*, optionally a graph."""
    for existing in (
        "docs/GUIDE.md",
        "docs/sub/nested/deep.json",
        "tools/run.py",
        "tools/audit/helper.py",
        "pyproject.toml",
        "gie/core/clock.py",
    ):
        write(root, existing)
    write(root, "README.md", readme)
    if ignore is not None:
        write(root, GRAPH, yaml.safe_dump({"schema_version": 1, dp.IGNORE_KEY: ignore}))
    return root


def found(violations: list[dp.Violation]) -> Found:
    return {(violation.rule, violation.path, violation.line) for violation in violations}


def spans(text: str) -> list[tuple[int, str]]:
    return list(dp.inline_code_spans(text))


# ============================================================================== existing and missing paths
def test_paths_that_exist_pass(tmp_path: Path) -> None:
    readme = (
        "# Synthetic fixture\n\n"
        "See `docs/GUIDE.md` and `tools/run.py`; configuration is in `pyproject.toml`.\n"
        "Directories: `docs/`, `docs`, `tools/audit/`, `docs/sub/nested`.\n"
        "Deep: `docs/sub/nested/deep.json`. Relative spelling: `./tools/run.py`.\n"
    )
    root = make_tree(tmp_path, readme)
    report = dp.scan(root)
    assert report.violations == ()
    assert report.documents == ("README.md", "docs/GUIDE.md")  # the fixture guide is a document too
    # Nine spans; `docs` alone has neither a slash nor a known extension and is not taken for a path.
    assert report.paths_checked == 8
    assert report.notes == ()


def test_dp001_missing_file_is_reported_with_document_line_and_span(tmp_path: Path) -> None:
    readme = "# Synthetic fixture\n\nThe tool `tools/run.py` exists.\nThe tool `tools/missing.py` does not.\n"
    root = make_tree(tmp_path, readme)
    violations = dp.check(root)
    assert violations == [
        dp.Violation(
            "DP001", "README.md", 4, "`tools/missing.py` is named but does not exist in the repository"
        )
    ]
    assert violations[0].render().startswith("README.md:4: DP001 `tools/missing.py`")


@pytest.mark.parametrize(
    "span",
    [
        "docs/sub/missing/deep.json",  # a directory in the middle is missing
        "docs/sub/nested/other.json",  # only the file is missing
        "notes.txt",  # a bare file name with a known extension is resolved against the root
        "GUIDE.md",  # exists in docs/, not at the root
        "README.md/",  # a trailing slash needs a directory
        "tools/run.py/",
        "docs/guide.md",  # upper and lower case are compared exactly on every operating system
        "Docs/GUIDE.md",
        "gie/core/clock",  # the extension is part of the name
        "planned/",
        "and/or",  # anything with a slash is taken for a path
    ],
)
def test_dp001_named_path_that_does_not_exist(tmp_path: Path, span: str) -> None:
    root = make_tree(tmp_path, f"Line one.\n\nSee `{span}` for details.\n")
    violations = dp.check(root)
    assert found(violations) == {("DP001", "README.md", 3)}
    assert f"`{span}`" in violations[0].message


@pytest.mark.parametrize(
    "span",
    [
        "tests/gie/<area>/",
        "<root>/gie/core/clock.py",
        "docs/*.md",
        "*.py",
        "reviews/{session}/review.pdf",
        "a|b/missing.py",
        "python -m tools.missing",
        "cd docs/missing && ls",
        "D:\\legacy_tree\\missing.py",
        "C:/legacy_tree/missing.py",
        "https://example.org/docs/missing.md",
        "http://localhost:8000/missing.json",
        "www.example.org/missing.md",
        "gie.core.clock",
        "tools.missing_module",
        "gie.contracts.interfaces.RecordSource",
        "/etc/missing.cfg",
        "../sibling/missing.py",
        "docs/../missing.md",
        "tools/missing.py:82",
        "missing.py::test_name",
        "2026/11/27",
        "1/2",
        ".py",
        ".yaml",
        "Start",
        "S00-ASG-22",
        "--gie-evidence=out.json",
        "path/with space.md",
        "",
    ],
)
def test_spans_that_are_not_repository_paths_are_ignored(tmp_path: Path, span: str) -> None:
    assert not dp.looks_like_path(span)
    root = make_tree(tmp_path, f"Text with `{span}` in it.\n")
    report = dp.scan(root)
    assert report.violations == ()
    assert report.paths_checked == 0


@pytest.mark.parametrize(
    "span",
    [
        "tools/run.py",
        "docs/",
        "a/b",
        "pyproject.toml",
        "x.jsonl",
        "report.pdf",
        "setup.cfg",
        "tox.ini",
        "a.yml",
    ],
)
def test_spans_with_a_slash_or_a_known_extension_are_paths(span: str) -> None:
    assert dp.looks_like_path(span)


# ============================================================================== Markdown reading
def test_fenced_code_blocks_are_ignored(tmp_path: Path) -> None:
    readme = (
        "Before `docs/GUIDE.md`.\n"  # 1
        "\n"  # 2
        "```bash\n"  # 3
        "cat `tools/missing_in_backtick_fence.py`\n"  # 4
        "```\n"  # 5
        "\n"  # 6
        "~~~\n"  # 7
        "`tools/missing_in_tilde_fence.py`\n"  # 8
        "```\n"  # 9  (a backtick fence does not close a tilde fence)
        "`tools/still_inside_tilde_fence.py`\n"  # 10
        "~~~\n"  # 11
        "\n"  # 12
        "1. A list item with an indented block:\n"  # 13
        "\n"  # 14
        "    ````text\n"  # 15
        "    `tools/missing_in_indented_fence.py`\n"  # 16
        "    ```\n"  # 17 (shorter than the opening fence: still inside)
        "    `tools/still_inside_long_fence.py`\n"  # 18
        "    ````\n"  # 19
        "\n"  # 20
        "After the fences `tools/missing_after.py` is text again.\n"  # 21
    )
    root = make_tree(tmp_path, readme)
    violations = dp.check(root)
    assert [(v.line, v.message.split("`")[1]) for v in violations] == [(21, "tools/missing_after.py")]


def test_unclosed_fence_runs_to_the_end_of_the_document(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "Text.\n\n```\n`tools/missing.py`\n\nstill code `docs/missing.md`\n")
    assert dp.check(root) == []


def test_three_backticks_on_one_line_are_an_inline_span_not_a_fence(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "```tools/missing.py```\n\nNext `docs/missing.md`.\n")
    assert found(dp.check(root)) == {("DP001", "README.md", 1), ("DP001", "README.md", 3)}


def test_inline_spans_follow_the_backtick_delimiter_rule() -> None:
    assert spans("plain `one` and ``two`` and ```three```") == [(1, "one"), (1, "two"), (1, "three")]
    assert spans("`` a ` inside ``") == [(1, "a ` inside")]
    assert spans("` padded `") == [(1, "padded")]
    assert spans("no closing ` backtick here") == []
    assert spans("escaped \\` backtick and `real`") == [(1, "real")]
    assert spans("first line `spans\ntwo lines` then `next`") == [(1, "spans two lines"), (2, "next")]
    assert spans("empty `` `` pair") == [(1, " ")]


def test_an_unmatched_backtick_does_not_swallow_later_blocks(tmp_path: Path) -> None:
    readme = (
        "A paragraph with a lone ` backtick.\n"  # 1
        "\n"  # 2
        "Next paragraph names `tools/missing_one.py`.\n"  # 3
        "\n"  # 4
        "| table | row with a lone ` backtick |\n"  # 5
        "| `docs/GUIDE.md` | `tools/missing_two.py` |\n"  # 6
        "- item with a lone ` backtick\n"  # 7
        "- item naming `tools/missing_three.py`\n"  # 8
        "## Heading with a lone ` backtick\n"  # 9
        "Text right after naming `tools/missing_four.py`.\n"  # 10
        "1. numbered with a lone ` backtick\n"  # 11
        "2. numbered naming `tools/missing_five.py`\n"  # 12
    )
    root = make_tree(tmp_path, readme)
    violations = dp.check(root)
    assert [(v.line, v.message.split("`")[1]) for v in violations] == [
        (3, "tools/missing_one.py"),
        (6, "tools/missing_two.py"),
        (8, "tools/missing_three.py"),
        (10, "tools/missing_four.py"),
        (12, "tools/missing_five.py"),
    ]


def test_span_inside_a_paragraph_reports_the_line_where_it_starts(tmp_path: Path) -> None:
    readme = "First line of a paragraph,\nsecond line names `tools/missing.py`,\nthird line.\n"
    root = make_tree(tmp_path, readme)
    assert found(dp.check(root)) == {("DP001", "README.md", 2)}
    write(root, "README.md", readme.replace("\n", "\r\n"))  # the same document with CRLF line ends
    (root / "README.md").write_bytes(readme.replace("\n", "\r\n").encode("utf-8"))
    assert found(dp.check(root)) == {("DP001", "README.md", 2)}


# ============================================================================== which documents
def test_only_the_listed_documents_are_scanned(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "Names `tools/missing.py`.\n")
    scanned = [
        "CLAUDE.md",
        "docs/ARCHITECTURE.md",
        "specifications/SHARED.md",
        "specifications/baselines/NOTES.md",
        "tools/audit/README.md",
    ]
    not_scanned = [
        "docs/sub/DEEP.md",
        "docs/notes.txt",
        "specifications/contracts/FIELDS.md",
        "tools/README.md",
        "reviews/session_00/README.md",
        "OTHER.md",
    ]
    for document in (*scanned, *not_scanned):
        write(root, document, "Names `tools/missing.py`.\n")
    report = dp.scan(root)
    assert list(report.documents) == sorted(["README.md", "docs/GUIDE.md", *scanned])
    assert {violation.path for violation in report.violations} == {"README.md", *scanned}
    assert all(violation.line == 1 for violation in report.violations)


def test_document_that_cannot_be_decoded_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "Fine.\n")
    (root / "docs" / "BROKEN.md").write_bytes(b"\xff\xfe invalid `tools/missing.py`\n")
    violations = dp.check(root)
    assert found(violations) == {("DP001", "docs/BROKEN.md", 0)}
    assert "cannot be read" in violations[0].message


# ============================================================================== the ignore list
def test_ignore_list_is_honoured_for_exact_strings_only(tmp_path: Path) -> None:
    readme = (
        "Planned: `gie/app/` (not created yet), also written `gie/app`.\n"  # 1
        "Planned file: `planned/module.py`.\n"  # 2
        "Below an ignored directory: `gie/app/main.py`.\n"  # 3
        "Not on the list: `gie/market_data/`.\n"  # 4
    )
    root = make_tree(tmp_path, readme, ignore=["gie/app/", "planned/module.py"])
    report = dp.scan(root)
    assert found(list(report.violations)) == {("DP001", "README.md", 3), ("DP001", "README.md", 4)}
    assert report.paths_checked == 2  # ignored spans are not counted as checked
    assert report.notes == ()


@pytest.mark.parametrize(
    "graph", [None, "schema_version: 1\n", f"{dp.IGNORE_KEY}: []\n", f"{dp.IGNORE_KEY}:\n"]
)
def test_missing_graph_or_missing_key_means_an_empty_ignore_list(tmp_path: Path, graph: str | None) -> None:
    root = make_tree(tmp_path, "Planned: `gie/app/`.\n")
    if graph is not None:
        write(root, GRAPH, graph)
    assert found(dp.check(root)) == {("DP001", "README.md", 1)}


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        (f"{dp.IGNORE_KEY}: gie/app/\n", "must be a list of non-empty strings"),
        (f"{dp.IGNORE_KEY}:\n  - gie/app/\n  - 42\n", "must be a list of non-empty strings"),
        (f"{dp.IGNORE_KEY}:\n  gie/app/: planned\n", "must be a list of non-empty strings"),
        (f"{dp.IGNORE_KEY}: [unclosed\n", "not valid YAML"),
    ],
)
def test_dp000_unusable_ignore_list_fails_closed(tmp_path: Path, graph: str, expected: str) -> None:
    root = make_tree(tmp_path, "Planned: `gie/app/`.\n")
    write(root, GRAPH, graph)
    violations = dp.check(root)
    # The list is treated as empty, so nothing is hidden by a list that could not be read.
    assert {(v.rule, v.path) for v in violations} == {("DP000", GRAPH), ("DP001", "README.md")}
    assert expected in next(v.message for v in violations if v.rule == "DP000")


def test_ignore_entry_that_exists_by_now_is_reported_as_a_note(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path, "Built: `tools/run.py` and `docs/`.\n", ignore=["tools/run.py", "docs/", "gie/app/"]
    )
    report = dp.scan(root)
    assert report.violations == ()
    assert len(report.notes) == 2
    assert "docs" in report.notes[0]
    assert "tools/run.py" in report.notes[1]
    assert all("exists by now" in note for note in report.notes)


# ============================================================================== command line
def test_cli_output_and_exit_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree(tmp_path, "See `docs/GUIDE.md`.\n")
    assert dp.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip().startswith("check_docs_paths: OK - 2 documents scanned, 1 named paths exist")

    write(root, "README.md", "See `docs/GUIDE.md`.\n\nAnd `docs/MISSING.md`, `tools/missing.py`.\n")
    assert dp.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "README.md:3: DP001 `docs/MISSING.md` is named but does not exist in the repository",
        "README.md:3: DP001 `tools/missing.py` is named but does not exist in the repository",
    ]
    assert "FAILED - 2 violation(s)" in captured.err


def test_cli_json_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree(tmp_path, "See `docs/MISSING.md`.\n", ignore=["docs/"])
    assert dp.main(["--root", str(root), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["tool"] == "tools/check_docs_paths.py"
    assert document["documents"] == ["README.md", "docs/GUIDE.md"]
    assert document["paths_checked"] == 1
    assert document["violation_count"] == 1
    assert [(v["rule"], v["path"], v["line"]) for v in document["violations"]] == [("DP001", "README.md", 1)]
    assert sorted(document["rules"]) == ["DP000", "DP001"]
    assert len(document["notes"]) == 1  # 'docs/' is on the ignore list although it exists


def test_cli_rejects_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dp.main(["--root", str(tmp_path / "missing")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_module_entry_point_runs_as_documented(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "See `tools/missing.py`.\n")
    completed = subprocess.run(
        [sys.executable, "-m", "tools.check_docs_paths", "--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout.startswith("README.md:1: DP001 `tools/missing.py`")


def test_the_scan_writes_nothing(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "See `tools/missing.py`.\n", ignore=["docs/"])
    before = {
        path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert dp.main(["--root", str(root)]) == 1
    after = {
        path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


# ============================================================================== the real repository
def test_real_repository_is_scanned_and_findings_are_well_formed() -> None:
    """Runs on the real documents and reports; it does not assert that they are free of findings.

    The documents of the repository are still being written by the coordinator, so the list of
    findings is evidence for the review (``python -m tools.check_docs_paths``), not a test verdict.
    What is asserted is that the tool reads the real documents and returns a well-formed list.
    """
    report = dp.scan(REPO_ROOT)
    violations = dp.check(REPO_ROOT)
    assert isinstance(violations, list)
    assert violations == list(report.violations)  # deterministic: two runs, one result
    assert {"CLAUDE.md", "README.md", "docs/ARCHITECTURE.md"} <= set(report.documents)
    assert report.paths_checked > 0, "no path was checked: the scan would be vacuous"
    for violation in violations:
        assert violation.rule in dp.RULES
        assert violation.path in report.documents or violation.path == GRAPH
        assert violation.line >= 0
        assert violation.message
