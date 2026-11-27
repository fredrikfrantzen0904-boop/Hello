# Session 00 review package

Submitted for the owner's decision at the architectural review gate. Review status: **unverified**
until the owner sets it to approved, conditional or rejected. Paper / research only - no live trading.

| file | content |
|---|---|
| `GIE_Session_00_Foundation_Review.pdf` | the review document; every table in it is generated from the files below and from the registers |
| `evidence/ci_results.json` | every quality gate of `python -m tools.ci` with command, exit code, duration and output, and the commit it ran on |
| `evidence/test_results.json` | outcome of every test with the requirement and acceptance ids it verifies |
| `evidence/coverage.json` | statement and branch coverage of the `gie` package from the same run |
| `evidence/benchmark.json` | micro-benchmarks of the foundation primitives and of the enforcement path (synthetic records, development container) |
| `evidence/file_manifest.json` | SHA-256 of every file of the reviewed commit, read from git |
| `evidence/review_round1_findings.json`, `review_round1_dispositions.yaml` | first independent review inside the session: findings verbatim, and what was done about each |
| `evidence/review_round1_verification.json` | independent verification that each round-1 disposition was really carried out |
| `evidence/review_round2_findings.json`, `review_round2_dispositions.yaml` | second review round on the reworked contracts |
| `rework_2_change_notice.md` | every change of behaviour or interface made in the second rework |
| `evidence/final_verification.json` | verdicts of verifiers who checked the round-2 dispositions and the open round-1 items against the final code (present only if that verification was run; the PDF says so) |
| `evidence/test_author_reports.json` | what the engineers who wrote the tests reported: files, counts, defects found and their resolution, limitations |
| `evidence/independent_review.json` | counts derived from the review files (`python -m tools.summarize_reviews`) |
| `status_patch.yaml` | the Session 00 status of every requirement it touches, as applied to the requirements matrix |

## How the package was produced

The registers are brought up to date first (last block below) and committed; that commit is the
reviewed commit. From a clean checkout of it (the PDF names it), with
`python -m pip install -e ".[dev]"`:

```bash
python -m tools.ci --evidence-dir out                 # all gates; writes ci_results.json, test_results.json, junit.xml, coverage.json
python -m tools.benchmark_foundation --out out/benchmark.json
python -m tools.make_file_manifest --commit HEAD --out out/file_manifest.json
python -m tools.summarize_reviews --out out/independent_review.json
python -m tools.build_review --evidence-dir out --out review.pdf --commit "$(git rev-parse HEAD)"
```

The evidence files in this folder are the output of exactly these commands; they were copied here
and committed in the commit that follows the reviewed one, so that the reviewed commit and its
evidence can be told apart. `python -m tools.make_file_manifest --check evidence/file_manifest.json`
verifies the manifest against the git history.

The acceptance test register and the generated registers are updated from the test evidence with

```bash
python -m tools.gie_registers sync-evidence --evidence out/test_results.json
python -m tools.gie_registers apply --patch reviews/session_00/status_patch.yaml
python -m tools.gie_registers render
```

The JUnit file of the test run is written by the CI as well but is not committed: it repeats the
fake fixtures of the secret scanner's own tests in forms that the secret scan rightly reports. It
is kept as a workflow artifact of the hosted CI.

## What is not in this package

The audit of the existing system, its inventories and the baseline regression on real baselines:
the production tree could not be reached from the session (blocker B-001 in
`docs/CHANGE_REQUESTS.md`). No file here describes the existing system and no performance of any
trading strategy is claimed.
