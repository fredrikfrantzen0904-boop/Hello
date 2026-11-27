"""Write the file manifest of a commit (requirements M-AUTH-16, M-AUTH-20, S12-ASG-07).

``python -m tools.make_file_manifest --commit <rev> --out FILE`` lists every file of
the given commit with its size and SHA-256, read from the git object database -
not from the working tree - so the manifest describes exactly the reviewed commit
and can be checked later with ``--check FILE``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> bytes:
    return subprocess.run(("git", *args), cwd=REPO_ROOT, capture_output=True, check=True).stdout


def build(commit: str) -> dict[str, Any]:
    resolved = _git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    files: list[dict[str, Any]] = []
    for line in _git("ls-tree", "-r", "-z", resolved).split(b"\0"):
        if not line:
            continue
        meta, _, raw_path = line.partition(b"\t")
        mode, kind, blob = meta.decode().split()
        if kind != "blob":
            continue
        data = _git("cat-file", "blob", blob)
        files.append(
            {
                "path": raw_path.decode("utf-8"),
                "mode": mode,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "git_blob": blob,
            }
        )
    files.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "generated_by": "tools/make_file_manifest.py",
        "commit": resolved,
        "tree": _git("rev-parse", f"{resolved}^{{tree}}").decode().strip(),
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check the file manifest of a commit.")
    parser.add_argument("--commit", default="HEAD")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="write the manifest of --commit to this file")
    group.add_argument("--check", type=Path, help="verify a manifest against the commit it names")
    args = parser.parse_args(argv)
    if args.out:
        payload = build(args.commit)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{payload['file_count']} files, {payload['total_bytes']} bytes, commit {payload['commit']}")
        return 0
    recorded = json.loads(args.check.read_text(encoding="utf-8"))
    current = build(recorded["commit"])
    if current["files"] != recorded["files"]:
        print(f"MISMATCH: {args.check} does not describe commit {recorded['commit']}")
        return 1
    print(f"manifest matches commit {recorded['commit']} ({current['file_count']} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
