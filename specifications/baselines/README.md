# Baselines of the existing system

Requirements: G02-08 "Preserve GCE-B", G02-09 "Preserve exact Round 1 baselines", M-AUTH-12 (the
GCE baselines are not modified), acceptance tests S00-ACC-05 "Baseline regression" and S00-ACC-06
"No production changes".

| file | content |
|---|---|
| `specifications/baselines/registry.yaml` | one entry per baseline the master specification names (G02, G09): `gce-b`, `round-1`, `round-2`, `cavemen-trades`; written only by `tools/baseline_freeze.py` |
| `specifications/baselines/claims.yaml` | the numbers the specification quotes about these baselines, recorded as **unverified claims** (coordinator's file) |
| frozen manifests | `<id>.manifest.jsonl` and `<id>.summary.json` in the sub-directory `frozen` - created by the first `freeze`; it does not exist yet |

## Current state: BLOCKED (B-001)

The baseline artifacts are on the owner's machine, in the production tree named in rule 2 of
`CLAUDE.md`; this development session cannot reach it (blocker B-001 in `docs/CHANGE_REQUESTS.md`).
Therefore every registry entry is `not_frozen`, with `include` and `manifest` null: which files make
up each baseline, where they are and what they are called is **unknown until the audit** and is not
guessed. The regression test `tests/gie/baseline/test_baseline_regression.py` reports every baseline
as skipped with the reason `BLOCKED: B-001 - ...`, and `verify` exits with status 3 (`UNVERIFIED`).
BLOCKED means "not verified", never "passed". The harness itself is proven on a labelled synthetic
fixture by `tests/gie/tools/test_baseline_freeze.py`; that proves the tool, not any baseline.

## Procedure

1. **Audit** the production tree read-only with `tools/audit/gie_baseline_audit.py` (see
   `tools/audit/README.md`) and decide, from its inventory, which files form each baseline.
2. **Freeze once**, with owner approval, on the machine that has the artifacts. The baseline tree is
   only read; the root is given on the command line, never written into the repository:

   ```bash
   python -m tools.baseline_freeze freeze --id gce-b --root <directory> --include "<glob>" [--include "<glob>" ...]
   ```

   Every matching file is hashed (SHA-256, names that suggest secrets included, no size cap). The
   command refuses when a pattern matches no file, a file cannot be hashed, a link or special file
   is reachable by a pattern, the root lies inside the frozen directory, or any output would be
   written inside the root. It writes the manifest and the summary (file count, total bytes,
   SHA-256 of the manifest, include patterns) and marks the entry `frozen`. The result is committed
   and reviewed like any other change.
3. **Verify in CI** on the machine that has the artifacts, with the environment variable named by
   `root_env` (default `GIE_BASELINE_ROOT`) pointing to the root:

   ```bash
   python -m tools.baseline_freeze verify            # exit 0 identical, 1 mismatch, 3 unverified
   python -m pytest tests/gie/baseline -q
   ```

   Byte identity means the same paths, sizes and SHA-256; a modification-time-only change is
   reported but is not a mismatch. A baseline that is not frozen, or whose root is not available,
   is `UNVERIFIED` (exit 3), never success.
4. **Replacing** a frozen manifest is a change to a baseline (M-AUTH-12): it needs an owner-approved
   decision in `docs/DECISION_LOG.md` and `--replace --decision D-nnn`; the decision id and the
   SHA-256 of the replaced manifest are recorded.

`python -m tools.baseline_freeze status` prints a JSON summary of the registry.

## No production changes (S00-ACC-06)

The owner takes a manifest of the production tree before and after a session with
the `manifest` command of `tools/audit/gie_baseline_audit.py` (preferably with `--hash-sensitive`, otherwise those files cannot
be proven identical) and points `GIE_PRODUCTION_MANIFEST_BEFORE` and `GIE_PRODUCTION_MANIFEST_AFTER`
at the two files. The acceptance test then requires content identity and lists added, removed,
changed and unhashed paths; without the two files it is skipped as `BLOCKED: B-001`.

## The numbers quoted in the specification

The performance and count figures that G09 quotes about Round 2, GCE-B, the learned score and the
Cavemen trade sets are **unverified claims** pending reconciliation in sessions 02 and 10. They are
recorded only in `specifications/baselines/claims.yaml`; they are not restated here, not part of any baseline entry, and no
test or tool treats them as facts or as targets.
