# GIE implementation status

> GENERATED - do not edit. Rendered by `python -m tools.gie_registers render` from
> `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`.
> Change the source and render again; `render --check` (run in CI) rejects a stale copy.

## Requirements by session

A requirement is counted for a session when the session is listed in its `sessions` or has recorded
a contribution; the status is the overall status of the requirement. Requirements shared by several
sessions appear in several rows; the last row counts every requirement once.

| session | requirements | not_started | in_progress | partial | implemented | verified | blocked | not_applicable |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| S00 | 171 | 11 | 19 | 85 | 1 | 42 | 11 | 2 |
| S01 | 126 | 46 | 17 | 61 | 0 | 0 | 0 | 2 |
| S02 | 107 | 30 | 17 | 56 | 0 | 0 | 2 | 2 |
| S03 | 121 | 47 | 17 | 55 | 0 | 0 | 0 | 2 |
| S04 | 115 | 38 | 17 | 58 | 0 | 0 | 0 | 2 |
| S05 | 126 | 43 | 17 | 64 | 0 | 0 | 0 | 2 |
| S06 | 122 | 40 | 17 | 63 | 0 | 0 | 0 | 2 |
| S07 | 135 | 48 | 17 | 68 | 0 | 0 | 0 | 2 |
| S08 | 142 | 61 | 17 | 62 | 0 | 0 | 0 | 2 |
| S09 | 117 | 41 | 17 | 57 | 0 | 0 | 0 | 2 |
| S10 | 141 | 66 | 17 | 54 | 0 | 0 | 2 | 2 |
| S11 | 104 | 31 | 17 | 54 | 0 | 0 | 0 | 2 |
| S12 | 103 | 27 | 17 | 57 | 0 | 0 | 0 | 2 |
| **all** | 584 | 424 | 19 | 85 | 1 | 42 | 11 | 2 |

## Acceptance tests by session

| session | acceptance tests | planned | passing | failing | blocked | partial |
|---|---:|---:|---:|---:|---:|---:|
| S00 | 6 | 0 | 4 | 0 | 0 | 2 |
| S01 | 10 | 10 | 0 | 0 | 0 | 0 |
| S02 | 5 | 5 | 0 | 0 | 0 | 0 |
| S03 | 5 | 5 | 0 | 0 | 0 | 0 |
| S04 | 6 | 6 | 0 | 0 | 0 | 0 |
| S05 | 7 | 7 | 0 | 0 | 0 | 0 |
| S06 | 8 | 8 | 0 | 0 | 0 | 0 |
| S07 | 8 | 8 | 0 | 0 | 0 | 0 |
| S08 | 8 | 8 | 0 | 0 | 0 | 0 |
| S09 | 6 | 6 | 0 | 0 | 0 | 0 |
| S10 | 9 | 9 | 0 | 0 | 0 | 0 |
| S11 | 5 | 5 | 0 | 0 | 0 | 0 |
| S12 | 8 | 8 | 0 | 0 | 0 | 0 |
| **all** | 91 | 85 | 4 | 0 | 0 | 2 |

## Requirements that are blocked, partial, in progress or implemented

Everything that is started but not verified, with what each session delivered and what blocks it.

### blocked (11)

- **G02-08** (S00, S02, S10; review: unverified) - Preserve GCE-B.
  - S00 [blocked]: The baselines are not accessible (B-001) and were therefore neither modified nor frozen. The freeze / verify harness is ready and self-tested on a labelled fixture. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)
  - Blockers: B-001.
  - Note: GCE-B is a baseline of the existing system.
- **G02-09** (S00, S02, S10; review: unverified) - Preserve exact Round 1 baselines.
  - S00 [blocked]: The baselines are not accessible (B-001) and were therefore neither modified nor frozen. The freeze / verify harness is ready and self-tested on a labelled fixture. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-01** (S00; review: unverified) - Audit existing D:\ftmo_eurusd without modifying production.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-02** (S00; review: unverified) - Inventory languages.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-03** (S00; review: unverified) - Inventory processes.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-04** (S00; review: unverified) - Inventory data.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-05** (S00; review: unverified) - Inventory tests.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-06** (S00; review: unverified) - Inventory hardware.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ASG-07** (S00; review: unverified) - Inventory broker interfaces.
  - S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-ACC-05** (S00; review: unverified) - Baseline regression.
  - S00 [blocked]: NOT EXECUTED on real baselines: GCE-B, Round 1, Round 2 and the Cavemen trade sets are on the owner's machine (B-001). The freeze / verify harness exists and is self-tested on a labelled synthetic fixture; the real regression test reports BLOCKED and never passes vacuously. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/baseline/test_baseline_regression.py`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)
  - Blockers: B-001.
- **S00-DEL-04** (S00; review: unverified) - Baseline audit.
  - S00 [blocked]: NOT DELIVERED: the baseline audit could not be performed (B-001). The document states exactly what is and is not known and how the audit can be completed. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/README.md`; blockers: B-001)
  - Blockers: B-001.

### partial (85)

- **M-AUTH-02** (all (S00-S12); review: unverified) - The owner approves material changes.
  - S00 [verified]: Session 00: all material decisions are PROPOSED for the owner; the package is prepared for the supervisor's review (U-13). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **M-AUTH-03** (all (S00-S12); review: unverified) - ChatGPT provides architecture and independent review recommendations.
  - S00 [verified]: Session 00: all material decisions are PROPOSED for the owner; the package is prepared for the supervisor's review (U-13). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **M-AUTH-04** (all (S00-S12); review: unverified) - Read the approved master specification, CLAUDE.md, contracts and decision log before editing.
  - S00 [verified]: Session 00 read the full specification before any work and created CLAUDE.md, the contracts and the decision log that later sessions must read. (evidence: `CLAUDE.md`, `specifications/SHARED_CONTRACTS.md`, `docs/DECISION_LOG.md`)
- **M-AUTH-05** (all (S00-S12); review: unverified) - Raise conflicts or missing dependencies as change requests.
  - S00 [verified]: Conflicts and missing dependencies are recorded as B-001, CR-001..CR-008 and the contract extensions CX-01..CX-19. (evidence: `docs/CHANGE_REQUESTS.md`)
- **M-AUTH-06** (all (S00-S12); review: unverified) - Never silently simplify.
  - S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`)
- **M-AUTH-07** (all (S00-S12); review: unverified) - Never invent data.
  - S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`)
- **M-AUTH-08** (all (S00-S12); review: unverified) - Never mark placeholders complete.
  - S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`)
- **M-AUTH-09** (all (S00-S12); review: unverified) - Use a dedicated Git branch/worktree.
  - S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`)
- **M-AUTH-10** (all (S00-S12); review: unverified) - Do not overwrite other sessions.
  - S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`)
- **M-AUTH-11** (all (S00-S12); review: unverified) - Do not merge without owner approval.
  - S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`)
- **M-AUTH-12** (all (S00-S12); review: unverified) - Do not modify existing Gold Bot, AI trader, breakout bot or GCE baselines.
  - S00 [verified]: No existing bot, trader or baseline was modified: none was accessible (B-001) and the pre-existing repository content is byte-identical. (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `reviews/session_00/evidence/ci_results.json`)
  - Note: Covers four named things (Gold Bot, AI trader, breakout bot, GCE baselines); status is the minimum over all four. Same rule: OWN-13.
- **M-AUTH-13** (all (S00-S12); review: unverified) - Paper/replay only.
  - S00 [verified]: Only REPLAY and PAPER run modes exist; 'paper' is in-process simulation, broker demo accounts are treated as real routing (D-011, to be confirmed: U-06). (evidence: `gie/core/routing_lock.py`, `tests/gie/core/test_routing_lock.py`)
- **M-AUTH-14** (S00, S07; review: unverified) - Build execution interfaces.
  - S00 [partial]: The execution interfaces exist (ExecutionAdapter, OrderGateway, order lifecycle contract, order gateway); the simulated adapter and the design of a live adapter interface belong to session 07. (evidence: `gie/contracts/interfaces.py`, `gie/contracts/guarded.py`, `tests/gie/contracts/test_guarded_adapter.py`)
  - Note: Split from 'build execution interfaces but keep real order routing disabled'; see M-AUTH-19.
- **M-AUTH-15** (all (S00-S12); review: unverified) - Do not expose credentials.
  - S00 [verified]: No credentials exist in the repository (secret scan in CI); the audit tool never emits credential values and does not hash or read files with secret-like names. (evidence: `tools/check_no_secrets.py`, `tests/gie/tools/test_check_no_secrets.py`, `reviews/session_00/evidence/ci_results.json`)
- **M-AUTH-16** (all (S00-S12); review: unverified) - Record exact commit.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-17** (all (S00-S12); review: unverified) - Submit a PDF review.
  - S00 [verified]: PDF review of Session 00. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **M-AUTH-18** (all (S00-S12); review: unverified) - Stop at the review gate.
  - S00 [verified]: Session 00 stops at the review gate. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **M-AUTH-19** (all (S00-S12); review: unverified) - Keep real order routing disabled.
  - S00 [verified]: Real order routing is disabled by the four-layer lock (acceptance test S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `tools/check_live_lock.py`, `tests/gie/core/test_routing_lock.py`, `reviews/session_00/evidence/test_results.json`)
  - Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11.
- **M-AUTH-20** (all (S00-S12); review: unverified) - Record files.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-21** (all (S00-S12); review: unverified) - Record tests.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-22** (all (S00-S12); review: unverified) - Record commands.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-23** (all (S00-S12); review: unverified) - Record results.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-24** (all (S00-S12); review: unverified) - Record limitations.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-25** (all (S00-S12); review: unverified) - Record requirement IDs.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **M-AUTH-26** (all (S00-S12); review: unverified) - Submit machine-readable evidence.
  - S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`)
- **G01-01** (S00, S01, S05, S07, S08; review: unverified) - XAUUSD exclusively.
  - S00 [verified]: Plans, GIE orders and GIE positions can only be XAUUSD (contract invariants); other symbols can only be observed. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/trade_plan.py`, `gie/contracts/order_event.py`, `gie/contracts/position.py`, `tests/gie/contracts/test_trade_plan.py`, `tests/gie/contracts/test_order_event.py`)
  - Note: Same rule: G06-07.
- **G01-11** (all (S00-S12); review: unverified) - These are research goals, not established performance.
  - S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Informational clause that qualifies G01-07 and G01-08.
- **G02-01** (S02, S00; review: unverified) - Six existing engines provide directional scores and evidence: Cavemen, Silver Bullet, Liquidity Hunter, ORB + VWAP, VWAP Trend Rider, Sweep & Reversal.
  - S00 [partial]: The Evidence contract admits exactly the six engines as strategy producers with bullish / bearish scores. The engines themselves could not be inspected (B-001); their adapters belong to session 02. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/evidence.py`, `tests/gie/contracts/test_evidence.py`; blockers: B-001)
  - Blockers: B-001.
- **G02-02** (S02, S00; review: unverified) - Deduplicate correlated events.
  - S00 [verified]: Exact de-duplication is defined by the contract: records of different engines about the same market event share one market_event_id and are grouped deterministically. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/evidence.py`, `gie/contracts/identity.py`, `tests/gie/contracts/test_evidence.py`)
- **G02-03** (S04, S05, S00; review: unverified) - Build setup-specific expected net R.
  - S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)
- **G02-04** (S04, S05, S00; review: unverified) - Build setup-specific uncertainty.
  - S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)
  - Note: 'setup-specific' is read as governing the whole list (expected net R, uncertainty, price destinations, flexible trade plans); the narrower reading would apply it to expected net R only.
- **G02-05** (S04, S05, S00; review: unverified) - Build setup-specific price destinations.
  - S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)
  - Note: See note on G02-04.
- **G02-06** (S05, S00; review: unverified) - Build setup-specific flexible trade plans.
  - S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)
  - Note: See note on G02-04.
- **G03-05** (all (S00-S12); review: unverified) - No network LLM in critical path.
  - S00 [verified]: The foundation contains no network, LLM or training call; the architecture and live-lock scans forbid network clients in decision and protection packages. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `tools/check_architecture.py`, `tools/check_live_lock.py`, `tests/gie/tools/test_check_architecture.py`)
- **G03-06** (all (S00-S12); review: unverified) - No training in critical path.
  - S00 [verified]: The foundation contains no network, LLM or training call; the architecture and live-lock scans forbid network clients in decision and protection packages. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `tools/check_architecture.py`, `tools/check_live_lock.py`, `tests/gie/tools/test_check_architecture.py`)
- **G03-07** (S01, S06, S00; review: unverified) - Proposed local target: tick processing <1 ms.
  - S00 [partial]: The target is a research parameter. Measured cost of the foundation primitives on the development container is reported; whether tick processing meets the target must be measured by sessions 01 / 06 on the owner's hardware with real ticks. (evidence: `specifications/research_parameters.toml`, `reviews/session_00/evidence/benchmark.json`)
- **G03-10** (S00, S01, S05, S06, S07, S12; review: unverified) - Measure p50/p95/p99/max.
  - S00 [verified]: Latency recorder with exact nearest-rank p50 / p95 / p99 / max; event, receive and decision time on every record make end-to-end delays measurable. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/latency.py`, `tests/gie/core/test_latency.py`, `reviews/session_00/evidence/benchmark.json`)
- **G03-11** (S00, S01, S06, S07, S12; review: unverified) - Measure end-to-end delays.
  - S00 [verified]: Latency recorder with exact nearest-rank p50 / p95 / p99 / max; event, receive and decision time on every record make end-to-end delays measurable. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/latency.py`, `tests/gie/core/test_latency.py`, `reviews/session_00/evidence/benchmark.json`)
- **G03-12** (S00, S01, S03, S05, S06, S07, S08; review: unverified) - Reject stale signals.
  - S00 [verified]: Freshness and expiry helpers fail closed at the boundary; the order gateway refuses an entry whose plan or approval has expired. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/freshness.py`, `gie/contracts/authorization.py`, `tests/gie/core/test_freshness.py`, `tests/gie/contracts/test_authorization.py`)
  - Note: Same rule family: G06-04.
- **G03-13** (all (S00-S12); review: unverified) - No guarantee of zero latency.
  - S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Informational: a statement of limits; nothing may be presented as zero latency.
- **G04-11** (S06, S07, S08, S00; review: unverified) - No widening protective stops.
  - S00 [verified]: Every plan and position has a hard stop on the losing side; a stop can only be tightened and never removed (transition validators, ledger, journal, order gateway); interpretation to confirm: U-12. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/order_event.py`, `gie/contracts/ledger.py`, `tests/gie/contracts/test_position.py`, `tests/gie/contracts/test_order_event.py`, `tests/gie/contracts/test_ledger.py`)
- **G04-12** (S05, S06, S07, S00; review: unverified) - Hard protective stops.
  - S00 [verified]: Every plan and position has a hard stop on the losing side; a stop can only be tightened and never removed (transition validators, ledger, journal, order gateway); interpretation to confirm: U-12. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/order_event.py`, `gie/contracts/ledger.py`, `tests/gie/contracts/test_position.py`, `tests/gie/contracts/test_order_event.py`, `tests/gie/contracts/test_ledger.py`)
- **G04-13** (S06, S00; review: unverified) - Profit ratchet based on executable net P&L.
  - S00 [verified]: Position carries executable net P&L with a peak that can never decrease across revisions. The ratchet itself belongs to session 06. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `tests/gie/contracts/test_position.py`)
- **G04-15** (S06, S00; review: unverified) - Monotonic profit ratchet.
  - S00 [verified]: Position carries executable net P&L with a peak that can never decrease across revisions. The ratchet itself belongs to session 06. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `tests/gie/contracts/test_position.py`)
  - Note: Split from 'fast monotonic profit ratchet based on executable net P&L'.
- **G05-01** (S08, S00; review: unverified) - Normal above -2% until +5%.
  - S00 [verified]: Daily states and the four levels are contract vocabulary and research parameters (ordering halt < defensive < recovery < 0 < profit protection enforced by the loader); limits are decided on exact amounts and the sequence rule of a trading day is enforced by the run ledger (D-028); open items U-03, U-14. The state machine belongs to session 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/risk_decision.py`, `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/contracts/test_risk_decision.py`, `tests/gie/core/test_params.py`)
- **G05-05** (S08, S00; review: unverified) - Recovery and emergency halt: configurable and subject to validation.
  - S00 [verified]: Daily states and the four levels are contract vocabulary and research parameters (ordering halt < defensive < recovery < 0 < profit protection enforced by the loader); limits are decided on exact amounts and the sequence rule of a trading day is enforced by the run ledger (D-028); open items U-03, U-14. The state machine belongs to session 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/risk_decision.py`, `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/contracts/test_risk_decision.py`, `tests/gie/core/test_params.py`)
  - Note: The source attaches 'configurable and subject to validation' to the recovery and emergency-halt levels only. Whether -2% and +5% are also adjustable is open item U-14; M-DIR-08 says 'Proposed numerical settings are research parameters, not approved production limits'.
- **G05-10** (S08, S00, S01; review: unverified) - Track Prague day.
  - S00 [verified]: Trading day = Europe/Prague calendar date with exact DST handling; RiskDecision verifies its trading day. The account's real reset instant is open item U-04. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/timeutil.py`, `tests/gie/core/test_timeutil.py`, `tests/gie/contracts/test_risk_decision.py`)
- **G05-12** (S08, S07, S00; review: unverified) - Track other positions.
  - S00 [verified]: Positions of other systems on the account are representable (owner EXTERNAL) as observations; GIE never sends orders for them. Tracking them belongs to sessions 07 / 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/authorization.py`, `tests/gie/contracts/test_position.py`)
  - Note: 'Track Prague day and actual account rules, including other positions' - other positions on the same account.
- **G06-04** (S05, S07, S00; review: unverified) - Reject stale entries.
  - S00 [verified]: Freshness and expiry helpers fail closed at the boundary; the order gateway refuses an entry whose plan or approval has expired. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/freshness.py`, `gie/contracts/authorization.py`, `tests/gie/core/test_freshness.py`, `tests/gie/contracts/test_authorization.py`)
  - Note: Same rule family: G03-12.
- **G06-07** (S00, S05, S07, S08; review: unverified) - Trade only gold.
  - S00 [verified]: Plans, GIE orders and GIE positions can only be XAUUSD (contract invariants); other symbols can only be observed. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/trade_plan.py`, `gie/contracts/order_event.py`, `gie/contracts/position.py`, `tests/gie/contracts/test_trade_plan.py`, `tests/gie/contracts/test_order_event.py`)
  - Note: Same rule: G01-01.
- **G07-10** (S09, S00; review: unverified) - Versioned promotion with owner approval.
  - S00 [verified]: ModelVersion: ACTIVE only through SHADOW and with a recorded owner approval and evidence; rollback and re-activation rules; one ACTIVE version per family; a run pins its models and an entry needs a model status the role of the run allows (D-026, open item U-17). The registry belongs to session 09. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`)
- **G07-11** (S09, S00; review: unverified) - Rollback.
  - S00 [verified]: ModelVersion: ACTIVE only through SHADOW and with a recorded owner approval and evidence; rollback and re-activation rules; one ACTIVE version per family; a run pins its models and an entry needs a model status the role of the run allows (D-026, open item U-17). The registry belongs to session 09. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`)
- **G08-01** (S07, S00; review: unverified) - Build autonomous order lifecycle foundation now.
  - S00 [partial]: Order lifecycle contract with state machine, replacement protocol, idempotency and the order gateway. The lifecycle engine belongs to session 07. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/order_event.py`, `gie/contracts/guarded.py`, `tests/gie/contracts/test_order_event.py`)
- **G08-03** (S00, S07, S12; review: unverified) - Live lock.
  - S00 [verified]: Live lock with four layers; acceptance test S00-ACC-04. Later sessions must keep it intact; CI enforces that. (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `specifications/lock_pins.json`, `tests/gie/core/test_routing_lock.py`, `tests/gie/tools/test_check_live_lock.py`, `reviews/session_00/evidence/test_results.json`)
- **G08-06** (S01, S06, S07, S00; review: unverified) - Disconnect/stale-feed fail closed.
  - S00 [verified]: Feed silence is measurable from receive time; timers for silence detection come from the injected scheduler; stale data raises and forbids new risk. Detection and reaction belong to sessions 01 / 06 / 07. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/tick.py`, `gie/core/clock.py`, `gie/core/freshness.py`, `tests/gie/contracts/test_tick.py`)
- **G08-07** (S07, S00; review: unverified) - Idempotent orders.
  - S00 [verified]: Orders are idempotent by a client_order_id derived from the risk decision or position; the journal treats an identical repeat as a duplicate and refuses conflicting content; the gateway journals a request before the adapter sees it and forwards a repeated request again (the adapter is idempotent per order event). Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/identity.py`, `gie/contracts/guarded.py`, `gie/contracts/journal.py`, `tests/gie/contracts/test_guarded_adapter.py`, `tests/gie/contracts/test_journal.py`)
- **G08-10** (all (S00-S12); review: unverified) - Never enable real execution during this project phase.
  - S00 [verified]: Live lock with four layers; acceptance test S00-ACC-04. Later sessions must keep it intact; CI enforces that. (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `specifications/lock_pins.json`, `tests/gie/core/test_routing_lock.py`, `tests/gie/tools/test_check_live_lock.py`, `reviews/session_00/evidence/test_results.json`)
  - Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11.
- **G09-05** (all (S00-S12); review: unverified) - Existing history is research only.
  - S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **G10-17** (all (S00-S12); review: unverified) - No cherry-picking.
  - S00 [verified]: Every test outcome and every blocked item of Session 00 is reported; evidence is generated, never selected by hand. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `reviews/session_00/evidence/test_results.json`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **M-DIR-06** (all (S00-S12); review: unverified) - Coordinator alone integrates reviewed branches.
  - S00 [verified]: Session 00: only the coordinator integrated work; the rule and the procedure are in the worktree instructions. (evidence: `docs/WORKTREES.md`, `specifications/dependency_graph.yaml`)
  - Note: A duty of the coordinator and a prohibition for every other session.
- **M-DIR-07** (all (S00-S12); review: unverified) - No live activation.
  - S00 [verified]: No live activation is possible (routing lock, S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `reviews/session_00/evidence/test_results.json`)
  - Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11.
- **M-DIR-08** (all (S00-S12); review: unverified) - Proposed numerical settings are research parameters, not approved production limits.
  - S00 [verified]: All numerical settings of the specification are in one file labelled PROPOSED_RESEARCH_PARAMETERS, loaded strictly; none is hard-coded or treated as an approved limit. (evidence: `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/core/test_params.py`)
- **M-GATE-01** (all (S00-S12); review: unverified) - Every session delivers requirement IDs.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
- **M-GATE-02** (all (S00-S12); review: unverified) - Every session delivers code paths.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
- **M-GATE-03** (all (S00-S12); review: unverified) - Every session delivers test commands and outputs.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
- **M-GATE-04** (all (S00-S12); review: unverified) - Every session delivers commit hash.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
- **M-GATE-05** (all (S00-S12); review: unverified) - Every session delivers data provenance.
  - S00 [verified]: Data provenance of Session 00: no market data was used; every sample record is labelled SYNTHETIC_FIXTURE; every record carries its data origin and every run a manifest. (evidence: `tests/gie/samples.py`, `gie/contracts/run.py`, `gie/contracts/common.py`)
- **M-GATE-06** (all (S00-S12); review: unverified) - Every session delivers measured performance.
  - S00 [verified]: Measured performance of the foundation primitives and of the enforcement path (p50 / p95 / p99 / max), labelled as micro-benchmarks on synthetic records on the development container. (evidence: `reviews/session_00/evidence/benchmark.json`, `tools/benchmark_foundation.py`)
- **M-GATE-07** (all (S00-S12); review: unverified) - Every session delivers blockers.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
- **M-GATE-08** (all (S00-S12); review: unverified) - Status: approved, conditional, rejected or unverified.
  - S00 [verified]: Session 00 is submitted with review status 'unverified'; the owner sets approved, conditional or rejected. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `specifications/requirements_matrix.yaml`)
- **M-GATE-10** (S00, S04, S09; review: unverified) - Owner authorizes model promotion.
  - S00 [partial]: The ModelVersion contract makes ACTIVE unreachable without a recorded owner approval, evaluation evidence and shadow runs; the promotion process itself belongs to session 09. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`)
  - Note: Split from 'Owner authorizes merge and model promotion'.
- **M-GATE-11** (all (S00-S12); review: unverified) - Every session delivers change requests.
  - S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)
  - Note: Split from 'blockers and change requests'.
- **M-DOC-01** (all (S00-S12); review: unverified) - GIE / ENGINEERING SPECIFICATION / V1.0 ... GIE | 27 Nov 2026 | Paper / research only
  - S00 [verified]: Specification version V1.0 of 27 Nov 2026 is recorded with the hashes of its text; the review PDF carries the 'Paper / research only' label on every page. (evidence: `specifications/requirements_matrix.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Header (before '...') and footer (after '...') printed on every page of the master specification and of every brief; the '...' joins the two and is not part of the source. Specification version V1.0; label 'Paper / research only'.
- **COM-01** (all (S00-S12); review: unverified) - Open a dedicated Claude Code session and worktree.
  - S00 [verified]: Replaced for the whole project by the owner's directive: one coordinating session with sub-agents and worktrees (D-001, CR-001, approved). (evidence: `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`)
  - Note: Replaced for this project by the owner's launch directive (OWN-01, OWN-06): decision D-001, change request CR-001. Identical text in every session brief (00-12); the source names the first.
- **COM-02** (all (S00-S12); review: unverified) - Read MASTER.pdf or its extracted text.
  - S00 [verified]: MASTER.pdf and its extracted text were read in full. (evidence: `specifications/extracted/MASTER.txt`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-05** (all (S00-S12); review: unverified) - Apply the master requirements G01-G10 relevant to this subsystem.
  - S00 [verified]: Session 00 applied the master requirements relevant to the foundation (see the G-rows of this matrix). (evidence: `specifications/requirements_matrix.yaml`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-06** (all (S00-S12); review: unverified) - Do not redefine shared contracts unilaterally.
  - S00 [verified]: Shared contracts can only be defined in gie.contracts (the framework refuses any other module), are locked by hash, and change only under the version rules; missing shared types are routed through the coordinator (CR-003, CR-005). (evidence: `gie/contracts/_spec.py`, `gie/contracts/schemas/LOCK.json`, `tests/gie/contracts/test_schema_lock.py`, `docs/CHANGE_REQUESTS.md`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-07** (all (S00-S12); review: unverified) - If a dependency is unavailable, use explicitly labeled test fixtures only.
  - S00 [verified]: Session 00 used only explicitly labelled synthetic fixtures (DataOrigin.SYNTHETIC_FIXTURE); a derived record cannot claim a better origin than its inputs. (evidence: `tests/gie/samples.py`, `gie/contracts/causality.py`, `tests/gie/contracts/test_causality.py`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-08** (all (S00-S12); review: unverified) - Do not claim integration complete.
  - S00 [verified]: Session 00 claims no integration: no subsystem exists. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Applies when a dependency is unavailable and labelled fixtures are used (COM-07). Identical text in every session brief (00-12); the source names the first.
- **COM-09** (all (S00-S12); review: unverified) - Submit the review package and await owner approval before merging.
  - S00 [verified]: Session 00 review package submitted; nothing merged. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-10** (all (S00-S12); review: unverified) - Report failed tests explicitly.
  - S00 [verified]: Every test that did not pass (failed, skipped, blocked) is listed in the review package. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/test_results.json`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **COM-11** (all (S00-S12); review: unverified) - No live trading.
  - S00 [verified]: No live trading (routing lock, S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `reviews/session_00/evidence/test_results.json`)
  - Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. Identical text in every session brief (00-12); the source names the first.
- **COM-12** (all (S00-S12); review: unverified) - Report unimplemented requirements explicitly.
  - S00 [verified]: Unimplemented and blocked requirements of Session 00 are listed explicitly in the review package. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `specifications/requirements_matrix.yaml`)
  - Note: Split from 'Report failed tests and unimplemented requirements explicitly'. Identical text in every session brief (00-12); the source names the first.
- **OWN-11** (S00; review: unverified) - Complete Session 00.
  - S00 [partial]: Everything in the Session 00 brief that does not need the production tree is complete and verified. The audit, the inventories, the baseline audit document and the real baseline regression are blocked (B-001). (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `docs/CHANGE_REQUESTS.md`; blockers: B-001)
  - Blockers: B-001.
- **S00-ACC-06** (S00; review: unverified) - No production changes.
  - S00 [partial]: Shown for this repository: the pre-existing file Start is byte-identical to the base commit and branch main was not advanced (checked in CI); the audit tool is tested to leave an audited tree byte-identical. NOT shown for the production tree itself, which was never reached (B-001) - so it was also never written to; the test that compares manifests of the production tree taken before and after reports BLOCKED until the owner supplies them. (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `tests/gie/tools/test_baseline_audit.py`, `tests/gie/baseline/test_baseline_regression.py`, `reviews/session_00/evidence/ci_results.json`; blockers: B-001)
  - Blockers: B-001.

### in_progress (19)

- **M-DIR-02** (S00; review: unverified) - Owner approves contracts before parallel work.
  - S00 [implemented]: The contracts are submitted for approval (lock status PROPOSED); no parallel work was started. The owner's approval is the open step. (evidence: `gie/contracts/schemas/LOCK.json`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **COM-03** (all (S00-S12); review: unverified) - Paths are proposed until coordinator approves actual repository layout.
  - S00 [implemented]: Repository layout proposed (D-002) with the mapping from brief paths; awaits the owner's approval (CR-002). (evidence: `specifications/dependency_graph.yaml`, `docs/DECISION_LOG.md`)
  - Note: Identical text in every session brief (00-12); the source names the first.
- **OWN-01** (S00; review: unverified) - You will coordinate the entire project within this session.
  - S00 [verified]: Session 00 was coordinated and implemented in this session (D-001). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
- **OWN-04** (all (S00-S12); review: unverified) - Treat ChatGPT as the architectural supervisor and independent reviewer.
  - S00 [implemented]: The review package is written for the supervisor's independent review; the supervisor cannot be contacted from this session, so the owner is asked to forward it (open item U-13). An internal independent review (two rounds) was performed in addition, not instead. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `docs/DECISION_LOG.md`, `reviews/session_00/evidence/independent_review.json`)
- **OWN-05** (all (S00-S12); review: unverified) - You are responsible for implementation, not independently reducing the agreed scope.
  - S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`)
- **OWN-06** (all (S00-S12); review: unverified) - You may use specialized subagents and parallel development where appropriate, provided all components follow the approved shared architecture.
  - S00 [verified]: Sub-agents were used for independent tests, tooling and reviews under the shared rules (D-001, D-023); integration was done by the coordinator only. (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/evidence/independent_review.json`)
- **OWN-08** (all (S00-S12); review: unverified) - Maintain a persistent requirements matrix throughout development.
  - S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)
  - Note: Artefact: specifications/requirements_matrix.yaml.
- **OWN-09** (all (S00-S12); review: unverified) - Do not silently simplify functionality.
  - S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`)
- **OWN-10** (all (S00-S12); review: unverified) - If you encounter technical limitations, document them and propose solutions for approval.
  - S00 [verified]: The one technical limitation met (no access to the production tree) is documented as B-001 with three proposed solutions for approval. (evidence: `docs/CHANGE_REQUESTS.md`, `docs/BASELINE_AUDIT.md`)
- **OWN-13** (all (S00-S12); review: unverified) - Preserve all existing trading systems.
  - S00 [verified]: Nothing of the existing systems was touched: the production tree was never accessed; in this repository the pre-existing file Start is byte-identical and branch main is unchanged (checked in CI). (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `reviews/session_00/evidence/ci_results.json`)
  - Note: Same rule: M-AUTH-12.
- **OWN-14** (all (S00-S12); review: unverified) - Live execution must remain disabled throughout development and validation.
  - S00 [verified]: Live execution is impossible in the foundation: four-layer routing lock, tested and scanned in CI (acceptance test S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `tests/gie/core/test_routing_lock.py`, `reviews/session_00/evidence/test_results.json`)
  - Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11.
- **OWN-16** (all (S00-S12); review: unverified) - Maintain a persistent implementation status throughout development.
  - S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)
  - Note: Artefact: docs/IMPLEMENTATION_STATUS.md.
- **OWN-17** (all (S00-S12); review: unverified) - Maintain a persistent decision log throughout development.
  - S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)
  - Note: Artefact: docs/DECISION_LOG.md.
- **OWN-18** (all (S00-S12); review: unverified) - Maintain a persistent acceptance test register throughout development.
  - S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)
  - Note: Artefact: specifications/acceptance_tests.yaml.
- **OWN-19** (all (S00-S12); review: unverified) - Do not substitute placeholders.
  - S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`)
- **OWN-20** (all (S00-S12); review: unverified) - Do not omit requirements.
  - S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`)
- **OWN-21** (all (S00-S12); review: unverified) - Do not declare incomplete components finished.
  - S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`)
- **OWN-22** (all (S00-S12); review: unverified) - Treat me as the project owner with final authority.
  - S00 [verified]: Every decision of the coordinator is PROPOSED until the owner approves it; the contract lock is PROPOSED; nothing is merged or started beyond Session 00. (evidence: `docs/DECISION_LOG.md`, `gie/contracts/schemas/LOCK.json`)
  - Note: 'me' is the project owner who wrote the directive. Split from OWN-04.
- **OWN-27** (all (S00-S12); review: unverified) - You will implement the entire project within this session.
  - S00 [verified]: Session 00 implemented in this session; sessions 01-12 not started (review gate). (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)
  - Note: Split from OWN-01; stays open until the last session is approved.

### implemented (1)

- **S00-ASG-08** (S00; review: unverified) - Establish repository/worktrees.
  - S00 [implemented]: Repository structure, development branch and the branch / worktree procedure with start gate and integration gate are defined. No session worktree has been created yet because only Session 00 has run; the procedure is therefore documented but not yet exercised. (evidence: `docs/WORKTREES.md`, `specifications/dependency_graph.yaml`, `README.md`)

## Open blockers, change requests and open items

- blockers: 1 open of 1
- change requests: 7 open of 8
- open items: 18 open of 18

| id | type | status | title | requirements |
|---|---|---|---|---|
| B-001 | blocker | OPEN | The existing system `D:\ftmo_eurusd` is not reachable from this session | S00-ASG-01, S00-ASG-02, S00-ASG-03, S00-ASG-04, S00-ASG-05, S00-ASG-06, S00-ASG-07, S00-ACC-05, S00-ACC-06, S00-DEL-04, G02-08, G02-09 |
| CR-002 | change request | OPEN | Repository layout differs from the proposed paths | COM-03, S00-DEP-02, S12-DEP-02 |
| CR-003 | change request | OPEN | Additional shared contracts are needed by later sessions | COM-06, S01-ASG-04, S01-ASG-05, S01-ASG-12, S07-ASG-15, S08-ASG-01, S08-ASG-02, S08-ACC-08, S02-DEP-01, S08-DEP-01, S09-DEP-01, S11-DEP-01 |
| CR-004 | change request | OPEN | Interpretations built into the contracts need confirmation | G04-10, G04-11, G05-03, M-AUTH-13 |
| CR-005 | change request | OPEN | One shared policy for what may be done with a flagged tick | S01-ASG-06, S01-ASG-08, G03-12, G08-06, S06-ACC-03 |
| CR-006 | change request | OPEN | Additions to the shared contracts beyond the nine named by the brief | S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-ASG-25, COM-06 |
| CR-007 | change request | OPEN | Evidence fields of a risk decision proposed by session 08 | S08-ASG-01, S08-ASG-05, G04-02, G04-03, G04-08 |
| CR-008 | change request | OPEN | Attribution of an estimate is recorded but not yet checked | S05-ACC-07, S11-ASG-05, S02-DEL-01, S04-ASG-08 |
| U-01 | open item | OPEN | Access to the existing system `D:\ftmo_eurusd` | S00-ASG-01, S00-DEL-04 |
| U-02 | open item | OPEN | Where will GIE finally live? | COM-03, S11-ACC-05 |
| U-03 | open item | OPEN | Meaning of "proposed recovery at -1.75%" | G05-03, S08-ASG-11, S08-ACC-02 |
| U-04 | open item | OPEN | Actual account rules | G05-10, G05-11, S08-ASG-08, S08-ASG-09, S08-ASG-10, G06-05 |
| U-05 | open item | OPEN | Definitions behind the quoted baseline figures | G09-02, G09-11, G09-12, G09-13, G09-07, G09-08 |
| U-06 | open item | OPEN | Confirm that "paper" excludes broker demo accounts | M-AUTH-13, G08-10 |
| U-07 | open item | OPEN | Definition of an "eligible day" | G01-07, G10-08, S10-ACC-06 |
| U-08 | open item | OPEN | Runtime environment | S00-ASG-06, G03-07 |
| U-09 | open item | OPEN | Authorised market data | S01-ASG-10, G06-06, G06-10, G06-11, G06-12, G06-13, G06-14, G06-15, G06-16, G06-17, G09-06, G09-10 |
| U-10 | open item | OPEN | Instrument specification of XAUUSD at the broker | S07-ASG-15, S08-ASG-02 |
| U-11 | open item | OPEN | Further shared contracts proposed by later sessions | COM-06, S02-DEP-01, S08-DEP-01, S11-DEP-01 |
| U-12 | open item | OPEN | Confirm the two interpretations in D-012 | G04-10, G04-11 |
| U-13 | open item | OPEN | How supervisor review reaches the coordinator | OWN-04, M-AUTH-03 |
| U-14 | open item | OPEN | Are the -2 % and +5 % levels adjustable research parameters as well? | G05-01, G05-02, G05-05, G05-06, M-DIR-08 |
| U-15 | open item | OPEN | Scope of "ablations" in brief 10 | S10-ASG-04, S10-ASG-05, S10-ASG-06, S10-ASG-07, S10-ASG-08, S10-ASG-09 |
| U-16 | open item | OPEN | Which "uncertainty" gates a decision | G02-04, S04-ASG-08, S05-ASG-13, S05-ACC-05, S11-ASG-04, G10-18 |
| U-17 | open item | OPEN | Which model statuses may lead to paper orders | G07-08, G07-09, G07-10, G07-12, G04-08 |
| U-18 | open item | OPEN | Read-only access for the paper data connection | M-AUTH-13, M-AUTH-15, G08-10, S01-ASG-01 |

## Decisions

- PROPOSED: 26
- APPROVED: 1
- REJECTED: 0
- SUPERSEDED: 1

| id | status | title | requirements |
|---|---|---|---|
| D-001 | APPROVED | One coordinating session with sub-agents instead of thirteen separate sessions | OWN-01, OWN-06, COM-01, M-AUTH-09, M-DIR-06 |
| D-002 | PROPOSED | Repository layout: everything importable lives in the `gie/` package | COM-03, S00-ASG-08, S00-DEP-02, S12-DEP-02 |
| D-003 | PROPOSED | Implementation language: Python 3.11+ | S00-ASG-02, G03-07, G03-08, G03-09 |
| D-004 | PROPOSED | Contracts are frozen standard-library dataclasses declared once | S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-DEL-02 |
| D-005 | PROPOSED | Numeric representation | S00-ASG-20, S00-ASG-21, G05-08 |
| D-006 | PROPOSED | Subsystems never import each other | S00-ASG-10, COM-06, M-DIR-06, M-AUTH-10 |
| D-007 | PROPOSED | Contract set 1.0.0 and its change procedure | M-DIR-02, S00-ACC-02, COM-06 |
| D-008 | PROPOSED | Canonical JSON and deterministic identifiers | S00-ASG-22, S00-ACC-01, G08-07, G02-02 |
| D-009 | SUPERSEDED | Total order by availability time | S00-ASG-25, S10-ACC-02, S10-ACC-03 |
| D-010 | PROPOSED | Freshness fails closed at the boundary | S00-ASG-23, G03-12, G06-04 |
| D-011 | PROPOSED | Paper-only routing lock; "paper" means in-process simulation | G08-03, G08-10, M-AUTH-13, M-AUTH-19, OWN-14, COM-11, M-DIR-07 |
| D-012 | PROPOSED | Master-specification safety rules are structural contract invariants | G04-09, G04-10, G04-11, G04-12, G04-13, G05-04, G05-08, G07-09, G07-10 |
| D-013 | PROPOSED | Trading day is the Europe/Prague calendar date | G05-10, S08-ASG-08 |
| D-014 | PROPOSED | Provisional vocabularies pending the audit | S02-ASG-06, S02-ASG-08, S01-ASG-06 |
| D-015 | PROPOSED | Requirement traceability and evidence are generated, never typed | OWN-08, OWN-16, OWN-17, OWN-18, M-AUTH-16, M-AUTH-17, M-AUTH-26, M-GATE-01, M-GATE-03 |
| D-016 | PROPOSED | Branching | M-AUTH-09, M-AUTH-10, M-AUTH-11, M-DIR-06 |
| D-017 | PROPOSED | Baselines are frozen by hash; quoted results are unverified claims | G02-08, G02-09, G09-01, G09-02, G09-07, S00-ACC-05 |
| D-018 | PROPOSED | Research parameters live in one labelled file | M-DIR-08, G05-05, G01-11 |
| D-019 | PROPOSED | `RunManifest` is added as a tenth shared contract | S10-DEL-03, S10-DEL-04, S10-ASG-10, S10-ACC-01, M-AUTH-16, M-GATE-05, S11-ASG-15 |
| D-020 | PROPOSED | The order of a run is its journal; inputs are ordered by availability | S00-ASG-25, S10-ASG-01, S10-ACC-01, S10-ACC-02, S10-ACC-03, S07-ACC-08, G08-04 |
| D-021 | PROPOSED | One order gateway enforces authorisation, lifecycle and the routing lock | G08-03, G08-07, G08-10, G04-09, G04-10, G04-11, G04-12, G04-13, S07-ACC-06, S08-ACC-06, M-AUTH-19, OWN-14 |
| D-022 | PROPOSED | Timers come from an injected scheduler; there are no timer threads | S00-ASG-19, S00-ACC-03, G03-12, G08-06, S06-ASG-06, S10-ASG-01 |
| D-023 | PROPOSED | Tests and reviews are written by someone other than the implementer | OWN-04, OWN-09, M-AUTH-06, M-AUTH-08, S12-ASG-08, COM-10 |
| D-024 | PROPOSED | Step time: a component sees the instant of the input it is processing | S00-ASG-19, S00-ASG-21, S00-ACC-03, S10-ASG-01, S10-ACC-01, S10-ACC-02, S06-ASG-01, G03-10, G03-11 |
| D-025 | PROPOSED | Order events have one writer; the broker may create protective orders | S07-ASG-01, S07-ASG-06, S07-ASG-09, S07-ACC-02, S06-ASG-03, G08-07, G08-08, G04-12 |
| D-026 | PROPOSED | Which model may lead to a paper order depends on the role of the run | G07-08, G07-09, G07-10, G07-12, G04-08 |
| D-027 | PROPOSED | A run is identified by the content of its manifest | S10-ACC-01, S10-DEL-03, S10-DEL-04, S10-ASG-10, M-AUTH-16, M-GATE-05, S08-ASG-09 |
| D-028 | PROPOSED | Amounts decide, fractions report | S08-ASG-06, S08-ASG-07, S08-ACC-01, S08-ACC-02, S08-ACC-03, S08-ACC-04, S08-ACC-05, G05-02, G05-04, G05-06 |
