"""Summarise the independent in-session reviews of a session (requirements OWN-09, COM-10, M-AUTH-24).

``python -m tools.summarize_reviews --session 00 --out FILE`` reads, for every review round
``N`` found under ``reviews/session_<NN>/``:

* ``evidence/review_round<N>_findings.json`` - the reviewers' findings, verbatim;
* ``review_round<N>_dispositions.yaml``      - what was done about each finding
  (``fix``: corrected in this session; ``record``: recorded as an open item or change request;
  ``decline``: not accepted, with the reason);

and, when present, ``evidence/test_author_reports.json`` (defects reported by the engineers who
wrote the tests).  It writes the counts the review PDF shows.

``--check`` (used by CI) fails when a finding has no disposition, a disposition names no
finding, a disposition has an unknown kind or no explanation: a finding can never be dropped
silently.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
KINDS = ("fix", "record", "decline")


def _rounds(folder: Path) -> list[int]:
    numbers = set()
    for path in (folder / "evidence").glob("review_round*_findings.json"):
        match = re.fullmatch(r"review_round(\d+)_findings\.json", path.name)
        if match:
            numbers.add(int(match[1]))
    for path in folder.glob("review_round*_dispositions.yaml"):
        match = re.fullmatch(r"review_round(\d+)_dispositions\.yaml", path.name)
        if match:
            numbers.add(int(match[1]))
    return sorted(numbers)


def summarize(session: str, root: Path = REPO_ROOT) -> tuple[dict[str, Any], list[str]]:
    folder = root / "reviews" / f"session_{session}"
    problems: list[str] = []
    rounds: list[dict[str, Any]] = []
    for number in _rounds(folder):
        findings_path = folder / "evidence" / f"review_round{number}_findings.json"
        dispositions_path = folder / f"review_round{number}_dispositions.yaml"
        if not findings_path.is_file():
            problems.append(f"round {number}: {findings_path.relative_to(root)} is missing")
            continue
        if not dispositions_path.is_file():
            problems.append(f"round {number}: {dispositions_path.relative_to(root)} is missing")
            continue
        findings = json.loads(findings_path.read_text(encoding="utf-8"))["findings"]
        dispositions = yaml.safe_load(dispositions_path.read_text(encoding="utf-8"))["dispositions"]
        finding_ids = [item["id"] for item in findings]
        if len(set(finding_ids)) != len(finding_ids):
            problems.append(f"round {number}: duplicate finding ids")
        for finding_id in finding_ids:
            if finding_id not in dispositions:
                problems.append(f"round {number}: finding {finding_id} has no disposition")
        for finding_id, entry in dispositions.items():
            if finding_id not in finding_ids:
                problems.append(f"round {number}: disposition {finding_id} names no finding")
            if not isinstance(entry, dict) or entry.get("disposition") not in KINDS:
                problems.append(f"round {number}: {finding_id} has no valid disposition ({', '.join(KINDS)})")
            elif not str(entry.get("action", "")).strip():
                problems.append(f"round {number}: {finding_id} does not say what was done")
        by_kind = dict.fromkeys(KINDS, 0)
        by_severity: dict[str, int] = {}
        by_reviewer: dict[str, int] = {}
        for item in findings:
            kind = (dispositions.get(item["id"]) or {}).get("disposition")
            if kind in by_kind:
                by_kind[kind] += 1
            by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
            by_reviewer[item["reviewer"]] = by_reviewer.get(item["reviewer"], 0) + 1
        rounds.append(
            {
                "round": number,
                "source": f"review round {number}: {len(by_reviewer)} independent reviewers",
                "findings": len(findings),
                "fixed": by_kind["fix"],
                "recorded": by_kind["record"],
                "declined": by_kind["decline"],
                "by_severity": dict(sorted(by_severity.items())),
                "by_reviewer": dict(sorted(by_reviewer.items())),
                "findings_file": str(findings_path.relative_to(root)),
                "dispositions_file": str(dispositions_path.relative_to(root)),
            }
        )
    reports_path = folder / "evidence" / "test_author_reports.json"
    test_authors: dict[str, Any] | None = None
    if reports_path.is_file():
        reports = json.loads(reports_path.read_text(encoding="utf-8"))["reports"]
        defects = [defect for report in reports for defect in report.get("defects_found", [])]
        resolutions: dict[str, int] = {}
        for defect in defects:
            resolution = defect.get("resolution")
            if resolution not in KINDS:
                problems.append(
                    f"test author defect without a resolution ({', '.join(KINDS)}): "
                    f"{defect.get('file')}: {str(defect.get('description'))[:80]}"
                )
            else:
                resolutions[resolution] = resolutions.get(resolution, 0) + 1
        test_authors = {
            "source": f"defect reports of {len(reports)} independent test authors",
            "findings": len(defects),
            "fixed": resolutions.get("fix", 0),
            "recorded": resolutions.get("record", 0),
            "declined": resolutions.get("decline", 0),
            "reports_file": str(reports_path.relative_to(root)),
        }
    summary = (
        "Inside the session, independence was obtained by separation of roles (decision D-023): "
        "the foundation code was written by the coordinator; the tests were derived from the "
        "specification by engineers who did not write the code and who reported defects instead of "
        "fixing them; reviewers examined the contracts against the owner's specification from the "
        "point of view of the sessions that must build on them, and of safety and determinism. "
        "Every finding is kept verbatim with its disposition."
    )
    payload = {
        "schema_version": 1,
        "generated_by": "tools/summarize_reviews.py",
        "session": f"S{session}",
        "summary": summary,
        "rounds": [*rounds, *([test_authors] if test_authors else [])],
    }
    return payload, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise independent review rounds of a session.")
    parser.add_argument("--session", default="00", help="two-digit session number (default 00)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    payload, problems = summarize(args.session)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if args.out and not problems:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row in payload["rounds"]:
        print(
            f"{row['source']}: {row['findings']} findings - fixed {row['fixed']}, "
            f"recorded {row['recorded']}, declined {row['declined']}"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
