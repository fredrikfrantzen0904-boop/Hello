"""Write the published contract schemas and their lock (deliverable S00-DEL-02).

``python -m tools.write_schemas`` regenerates ``gie/contracts/schemas/*.schema.json`` and
``LOCK.json`` from the code.  The content comes from ``gie.contracts.schema_gen.planned_files``;
this tool only writes it - code below ``gie/`` does not write files (rule LL015 of
``tools/check_live_lock.py``).  Files are written as bytes with LF line ends on every platform.
A released schema of an approved contract set is never rewritten (``planned_files`` refuses).
After writing, the result is checked like ``python -m gie.contracts.schema_gen --check`` does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gie.contracts import schema_gen
from gie.contracts.registry import all_contracts


def write(schema_dir: Path = schema_gen.SCHEMA_DIR) -> list[str]:
    """Publish the current schemas.  Returns the names of the files written (the lock last)."""
    files = schema_gen.planned_files(schema_dir)
    schema_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, data in files.items():
        path = schema_dir / name
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)  # bytes: no newline translation on any platform
            written.append(name)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the contract schemas and their lock.")
    parser.parse_args(argv)
    for name in write():
        print(f"wrote {name}")
    problems = schema_gen.check()
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if not problems:
        print(f"contract schemas consistent: {len(all_contracts())} contracts")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
