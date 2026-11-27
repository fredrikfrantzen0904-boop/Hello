"""Integration CI entry point (requirement S00-ASG-29).

``python -m tools.ci [--evidence-dir DIR]`` runs every quality gate in a fixed
order, never stops at the first failure, and writes ``ci_results.json`` with the
exact command, exit code, duration and output of each step - the machine-readable
evidence that review packages are built from.  The GitHub workflow runs the same
entry point, so local and hosted results are comparable.

Step outcomes:

* ``passed``  - the step ran and succeeded;
* ``failed``  - the step ran and reported a problem (the run exits 1);
* ``blocked`` - the step cannot be completed for a recorded external reason
  (for example baselines that are not accessible).  It is reported, never
  counted as passed, and does not fail the run on its own.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_TAIL_CHARS = 6000


@dataclass(frozen=True)
class Step:
    name: str
    args: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    #: exit code -> explanation, for outcomes that mean "blocked", not "failed".
    blocked_exit_codes: dict[int, str] = field(default_factory=dict)


@dataclass
class StepResult:
    name: str
    command: str
    requirement_ids: list[str]
    exit_code: int
    status: str
    duration_s: float
    output_tail: str
    note: str = ""


def build_steps(evidence_dir: Path) -> list[Step]:
    py = sys.executable
    return [
        Step("lint (ruff check)", (py, "-m", "ruff", "check", "."), ("S00-ASG-29",)),
        Step("format (ruff format --check)", (py, "-m", "ruff", "format", "--check", "."), ("S00-ASG-29",)),
        Step("types (mypy --strict)", (py, "-m", "mypy"), ("S00-ASG-29",)),
        Step(
            "contract schemas, lock and evolution rules",
            (py, "-m", "gie.contracts.schema_gen", "--check"),
            ("S00-ACC-02", "S00-DEL-02", "COM-06"),
        ),
        Step(
            "generated contract reference is current",
            (py, "-m", "tools.render_contract_docs", "--check"),
            ("S00-DEL-02",),
        ),
        Step(
            "golden contract messages still decode and re-encode byte for byte",
            (py, "-m", "tools.make_golden", "--check"),
            ("S00-ACC-01", "S00-ACC-02"),
        ),
        Step(
            "paper-only routing lock (static scan)",
            (py, "-m", "tools.check_live_lock"),
            ("S00-ACC-04", "G08-03", "G08-10", "M-AUTH-19", "M-DIR-07", "COM-11", "OWN-14"),
        ),
        Step(
            "architecture and dependency rules",
            (py, "-m", "tools.check_architecture"),
            ("S00-ASG-10", "COM-06"),
        ),
        Step(
            "protected paths unchanged (no production changes)",
            (py, "-m", "tools.check_protected_paths"),
            ("S00-ACC-06", "OWN-13", "M-AUTH-12"),
        ),
        Step(
            "documents name only paths that exist",
            (py, "-m", "tools.check_docs_paths"),
            ("M-AUTH-08", "OWN-21"),
        ),
        Step(
            "no credentials in the repository",
            (py, "-m", "tools.check_no_secrets"),
            ("M-AUTH-15",),
        ),
        Step(
            "tests with requirement evidence",
            (
                py,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--gie-evidence={evidence_dir / 'test_results.json'}",
                f"--junitxml={evidence_dir / 'junit.xml'}",
                "--cov=gie",
                "--cov-branch",
                f"--cov-report=json:{evidence_dir / 'coverage.json'}",
                "--cov-report=term",
            ),
            ("S00-ACC-01", "S00-ACC-02", "S00-ACC-03", "S00-ACC-04", "S00-ACC-05", "S00-ACC-06"),
        ),
        Step(
            "acceptance register equals what this test run shows",
            (
                py,
                "-m",
                "tools.gie_registers",
                "sync-evidence",
                "--evidence",
                str(evidence_dir / "test_results.json"),
                "--check",
            ),
            ("OWN-18", "M-AUTH-26", "S12-ASG-07"),
        ),
        Step(
            "every independent review finding has a disposition",
            (py, "-m", "tools.summarize_reviews", "--check"),
            ("OWN-09", "COM-10"),
        ),
        Step(
            "registers are valid",
            (py, "-m", "tools.gie_registers", "validate"),
            ("OWN-08", "S00-ASG-28"),
        ),
        Step(
            "generated registers are current",
            (py, "-m", "tools.gie_registers", "render", "--check"),
            ("OWN-08", "S00-ASG-28"),
        ),
        Step(
            "baseline regression (frozen baselines byte-identical)",
            (py, "-m", "tools.baseline_freeze", "verify"),
            ("S00-ACC-05", "G02-08", "G02-09"),
            blocked_exit_codes={3: "baselines are not frozen or not accessible (blocker B-001)"},
        ),
    ]


def run_step(step: Step) -> StepResult:
    started = time.perf_counter()
    completed = subprocess.run(
        step.args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    duration = time.perf_counter() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    note = ""
    if completed.returncode == 0:
        status = "passed"
    elif completed.returncode in step.blocked_exit_codes:
        status = "blocked"
        note = step.blocked_exit_codes[completed.returncode]
    else:
        status = "failed"
    display = " ".join(arg.replace(str(REPO_ROOT), ".") for arg in step.args)
    display = display.replace(sys.executable, "python")
    return StepResult(
        name=step.name,
        command=display,
        requirement_ids=list(step.requirement_ids),
        exit_code=completed.returncode,
        status=status,
        duration_s=round(duration, 3),
        output_tail=output[-OUTPUT_TAIL_CHARS:],
        note=note,
    )


def _git(*args: str) -> str:
    completed = subprocess.run(("git", *args), cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _tree_clean(evidence_dir: Path) -> bool:
    """True when the working tree equals the commit, not counting files in the evidence directory."""
    try:
        ignored = evidence_dir.resolve().relative_to(REPO_ROOT).as_posix() + "/"
    except ValueError:
        ignored = None
    completed = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False  # unknown is not clean
    paths = [line[3:].strip('"') for line in completed.stdout.splitlines() if line.strip()]
    return not [path for path in paths if not (ignored and path.startswith(ignored))]


_VOLATILE = re.compile(r"(pytest-of-[\w.-]+[\\/]+pytest-\d+[\\/]+[\w.\[\]-]+|0x[0-9a-fA-F]+|\b\d{6,}\b)")


def failure_digest(evidence_dir: Path, limit: int = 14) -> list[str]:
    """Compact summary of the tests that neither passed nor were skipped (for logs that are read remotely).

    Per test module the number of failures and errors, then the most frequent last lines of the
    failure texts with one example each.  Volatile parts (temporary paths, addresses, long
    numbers) are masked so that equal causes group together.  Empty when every test passed.
    """
    path = evidence_dir / "test_results.json"
    try:
        tests = json.loads(path.read_text(encoding="utf-8"))["tests"]
    except (OSError, ValueError, KeyError):
        return []
    bad = [item for item in tests if item.get("outcome") not in ("passed", "skipped", "xfailed")]
    if not bad:
        return []
    modules: dict[str, dict[str, int]] = {}
    reasons: dict[str, list[str]] = {}
    for item in bad:
        module = str(item.get("nodeid", "")).split("::")[0]
        bucket = modules.setdefault(module, {})
        outcome = str(item.get("outcome"))
        bucket[outcome] = bucket.get(outcome, 0) + 1
        text_lines = [line.strip() for line in str(item.get("detail", "")).splitlines() if line.strip()]
        explained = [line for line in text_lines if line.startswith("E ")]
        reason = _VOLATILE.sub("~", text_lines[-1] if text_lines else "(no text)")[:170]
        if explained:  # the first line pytest marks as the explanation of the failure
            reason += " | " + _VOLATILE.sub("~", explained[0])[:260]
        reasons.setdefault(reason, []).append(str(item.get("nodeid", "")))
    lines = [f"test failure digest: {len(bad)} test(s) neither passed nor skipped"]
    for module in sorted(modules, key=lambda name: -sum(modules[name].values())):
        counts = ", ".join(f"{count} {outcome}" for outcome, count in sorted(modules[module].items()))
        lines.append(f"  {module}: {counts}")
    lines.append("  most frequent reasons (last line of the failure text; one example each):")
    for reason, nodeids in sorted(reasons.items(), key=lambda pair: -len(pair[1]))[:limit]:
        lines.append(f"  {len(nodeids):4d} x {reason}")
        lines.append(f"         e.g. {nodeids[0][:150]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GIE integration CI.")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=REPO_ROOT / ".ci-evidence",
        help="directory for ci_results.json, test_results.json, junit.xml, coverage.json",
    )
    args = parser.parse_args(argv)
    evidence_dir: Path = (
        args.evidence_dir if args.evidence_dir.is_absolute() else REPO_ROOT / args.evidence_dir
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Recorded before anything runs: the evidence directory itself must not make the tree "dirty".
    commit_at_start = _git("rev-parse", "HEAD")
    clean_at_start = _tree_clean(evidence_dir)

    results: list[StepResult] = []
    for step in build_steps(evidence_dir):
        print(f"==> {step.name}", flush=True)
        result = run_step(step)
        results.append(result)
        print(f"    {result.status.upper()} (exit {result.exit_code}, {result.duration_s:.1f} s)", flush=True)
        if result.status != "passed":
            print(result.output_tail, flush=True)

    digest = failure_digest(evidence_dir)
    counts = {
        status: sum(1 for r in results if r.status == status) for status in ("passed", "failed", "blocked")
    }
    overall = (
        "failed" if counts["failed"] else ("passed_with_blocked_steps" if counts["blocked"] else "passed")
    )
    payload = {
        "schema_version": 1,
        "generated_by": "tools/ci.py",
        "overall": overall,
        "counts": counts,
        "commit": commit_at_start,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "working_tree_clean": clean_at_start,
        "working_tree_clean_after": _tree_clean(evidence_dir),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "steps": [asdict(result) for result in results],
        "test_failure_digest": digest,
    }
    (evidence_dir / "ci_results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print()
    for result in results:
        print(f"{result.status.upper():8s} {result.name}")
    print(f"\noverall: {overall}  ({counts})  evidence: {evidence_dir}")
    if digest:
        print()
        print("\n".join(digest))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
