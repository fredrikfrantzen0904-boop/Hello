# GIE requirements matrix

> GENERATED - do not edit. Rendered by `python -m tools.gie_registers render` from
> `specifications/requirements_matrix.yaml`.
> Change the source and render again; `render --check` (run in CI) rejects a stale copy.

Specification: V1.0 (27 Nov 2026).

## Totals by status

| status | requirements | meaning |
|---|---:|---|
| not_started | 424 | No implementation work yet. |
| in_progress | 19 | Work started, not complete. |
| partial | 85 | A foundation portion is delivered; the remainder is owned by a later session (see contributions). |
| implemented | 1 | Code/document exists but automated or review evidence is incomplete. |
| verified | 42 | Implemented and backed by recorded, reproducible evidence. |
| blocked | 11 | Cannot proceed; see the linked blocker / change request. |
| not_applicable | 2 | Informational clause with nothing to implement (justified in notes). |
| **total** | 584 | |

## Totals by review status

| review status | requirements |
|---|---:|
| unverified | 584 |
| approved | 0 |
| conditional | 0 |
| rejected | 0 |
| **total** | 584 |

## MASTER.pdf - Authority and scope

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| M-AUTH-01 | You are a specialist implementation engineer for Gold Intelligence Engine (GIE). | all (S00-S12) | not_applicable | unverified | S00 [not_applicable]: Role statement; nothing to implement. (evidence: `CLAUDE.md`)<br>Note: Informational: role statement. |
| M-AUTH-02 | The owner approves material changes. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: all material decisions are PROPOSED for the owner; the package is prepared for the supervisor's review (U-13). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| M-AUTH-03 | ChatGPT provides architecture and independent review recommendations. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: all material decisions are PROPOSED for the owner; the package is prepared for the supervisor's review (U-13). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| M-AUTH-04 | Read the approved master specification, CLAUDE.md, contracts and decision log before editing. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 read the full specification before any work and created CLAUDE.md, the contracts and the decision log that later sessions must read. (evidence: `CLAUDE.md`, `specifications/SHARED_CONTRACTS.md`, `docs/DECISION_LOG.md`) |
| M-AUTH-05 | Raise conflicts or missing dependencies as change requests. | all (S00-S12) | partial | unverified | S00 [verified]: Conflicts and missing dependencies are recorded as B-001, CR-001..CR-008 and the contract extensions CX-01..CX-19. (evidence: `docs/CHANGE_REQUESTS.md`) |
| M-AUTH-06 | Never silently simplify. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`) |
| M-AUTH-07 | Never invent data. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`) |
| M-AUTH-08 | Never mark placeholders complete. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: nothing simplified silently; no data invented (all sample data is labelled SYNTHETIC_FIXTURE, the quoted baseline figures are recorded as unverified claims); no placeholder marked complete (documents may only name paths that exist, checked in CI). (evidence: `tests/gie/samples.py`, `specifications/baselines/claims.yaml`, `tools/check_docs_paths.py`, `docs/CHANGE_REQUESTS.md`) |
| M-AUTH-09 | Use a dedicated Git branch/worktree. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`) |
| M-AUTH-10 | Do not overwrite other sessions. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`) |
| M-AUTH-11 | Do not merge without owner approval. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 worked only on the development branch; nothing was merged; main is unchanged; the worktree procedure for parallel sessions is defined. (evidence: `docs/WORKTREES.md`, `tools/check_protected_paths.py`) |
| M-AUTH-12 | Do not modify existing Gold Bot, AI trader, breakout bot or GCE baselines. | all (S00-S12) | partial | unverified | S00 [verified]: No existing bot, trader or baseline was modified: none was accessible (B-001) and the pre-existing repository content is byte-identical. (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `reviews/session_00/evidence/ci_results.json`)<br>Note: Covers four named things (Gold Bot, AI trader, breakout bot, GCE baselines); status is the minimum over all four. Same rule: OWN-13. |
| M-AUTH-13 | Paper/replay only. | all (S00-S12) | partial | unverified | S00 [verified]: Only REPLAY and PAPER run modes exist; 'paper' is in-process simulation, broker demo accounts are treated as real routing (D-011, to be confirmed: U-06). (evidence: `gie/core/routing_lock.py`, `tests/gie/core/test_routing_lock.py`) |
| M-AUTH-14 | Build execution interfaces. | S00, S07 | partial | unverified | S00 [partial]: The execution interfaces exist (ExecutionAdapter, OrderGateway, order lifecycle contract, order gateway); the simulated adapter and the design of a live adapter interface belong to session 07. (evidence: `gie/contracts/interfaces.py`, `gie/contracts/guarded.py`, `tests/gie/contracts/test_guarded_adapter.py`)<br>Note: Split from 'build execution interfaces but keep real order routing disabled'; see M-AUTH-19. |
| M-AUTH-15 | Do not expose credentials. | all (S00-S12) | partial | unverified | S00 [verified]: No credentials exist in the repository (secret scan in CI); the audit tool never emits credential values and does not hash or read files with secret-like names. (evidence: `tools/check_no_secrets.py`, `tests/gie/tools/test_check_no_secrets.py`, `reviews/session_00/evidence/ci_results.json`) |
| M-AUTH-16 | Record exact commit. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-17 | Submit a PDF review. | all (S00-S12) | partial | unverified | S00 [verified]: PDF review of Session 00. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| M-AUTH-18 | Stop at the review gate. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 stops at the review gate. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| M-AUTH-19 | Keep real order routing disabled. | all (S00-S12) | partial | unverified | S00 [verified]: Real order routing is disabled by the four-layer lock (acceptance test S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `tools/check_live_lock.py`, `tests/gie/core/test_routing_lock.py`, `reviews/session_00/evidence/test_results.json`)<br>Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. |
| M-AUTH-20 | Record files. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-21 | Record tests. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-22 | Record commands. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-23 | Record results. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-24 | Record limitations. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-25 | Record requirement IDs. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |
| M-AUTH-26 | Submit machine-readable evidence. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package records the exact commit, the files (SHA-256 manifest), the tests, the commands, the results, the limitations and the requirement ids, as machine-readable evidence. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/file_manifest.json`) |

## MASTER.pdf - G01 | Objective

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G01-01 | XAUUSD exclusively. | S00, S01, S05, S07, S08 | partial | unverified | S00 [verified]: Plans, GIE orders and GIE positions can only be XAUUSD (contract invariants); other symbols can only be observed. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/trade_plan.py`, `gie/contracts/order_event.py`, `gie/contracts/position.py`, `tests/gie/contracts/test_trade_plan.py`, `tests/gie/contracts/test_order_event.py`)<br>Note: Same rule: G06-07. |
| G01-02 | Hybrid scalping and intraday. | S05, S06, S10 | not_started | unverified |  |
| G01-03 | Continuous monitoring during actual broker sessions. | S01, S06, S07 | not_started | unverified |  |
| G01-04 | Cautious Asian-session selection. | S05, S08, S10 | not_started | unverified |  |
| G01-05 | Overnight positions when permitted and monitored. | S07, S08, S06 | not_started | unverified |  |
| G01-06 | No weekend exposure. | S08, S07, S01 | not_started | unverified |  |
| G01-07 | Target at least one valid trade per eligible day. | S05, S10 | not_started | unverified | Note: A research goal, not established performance (G01-11). |
| G01-08 | Target at least 5% average daily net ROI. | S10, S05, S08 | not_started | unverified | Note: A research goal, not established performance (G01-11). |
| G01-09 | Report misses. | S10, S09, S11 | not_started | unverified | Note: Misses of the trade-frequency and ROI targets. |
| G01-10 | Never force trades. | S05, S08 | not_started | unverified |  |
| G01-11 | These are research goals, not established performance. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Informational clause that qualifies G01-07 and G01-08. |
| G01-12 | Never force leverage. | S08, S05 | not_started | unverified | Note: Split from 'never force trades or leverage'. |

## MASTER.pdf - G02 | Intelligence

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G02-01 | Six existing engines provide directional scores and evidence: Cavemen, Silver Bullet, Liquidity Hunter, ORB + VWAP, VWAP Trend Rider, Sweep & Reversal. | S02, S00 | partial | unverified | S00 [partial]: The Evidence contract admits exactly the six engines as strategy producers with bullish / bearish scores. The engines themselves could not be inspected (B-001); their adapters belong to session 02. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/evidence.py`, `tests/gie/contracts/test_evidence.py`; blockers: B-001)<br>Blockers: B-001. |
| G02-02 | Deduplicate correlated events. | S02, S00 | partial | unverified | S00 [verified]: Exact de-duplication is defined by the contract: records of different engines about the same market event share one market_event_id and are grouped deterministically. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/evidence.py`, `gie/contracts/identity.py`, `tests/gie/contracts/test_evidence.py`) |
| G02-03 | Build setup-specific expected net R. | S04, S05, S00 | partial | unverified | S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`) |
| G02-04 | Build setup-specific uncertainty. | S04, S05, S00 | partial | unverified | S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)<br>Note: 'setup-specific' is read as governing the whole list (expected net R, uncertainty, price destinations, flexible trade plans); the narrower reading would apply it to expected net R only. |
| G02-05 | Build setup-specific price destinations. | S04, S05, S00 | partial | unverified | S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)<br>Note: See note on G02-04. |
| G02-06 | Build setup-specific flexible trade plans. | S05, S00 | partial | unverified | S00 [verified]: The Candidate and TradePlan contracts carry expected net R, both kinds of uncertainty (open item U-16), destinations and alternative plans per opportunity. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/candidate.py`, `gie/contracts/trade_plan.py`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_trade_plan.py`)<br>Note: See note on G02-04. |
| G02-07 | Actual market behavior overrides historical expectations. | S05, S06 | not_started | unverified |  |
| G02-08 | Preserve GCE-B. | S00, S02, S10 | blocked | unverified | S00 [blocked]: The baselines are not accessible (B-001) and were therefore neither modified nor frozen. The freeze / verify harness is ready and self-tested on a labelled fixture. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)<br>Blockers: B-001.<br>Note: GCE-B is a baseline of the existing system. |
| G02-09 | Preserve exact Round 1 baselines. | S00, S02, S10 | blocked | unverified | S00 [blocked]: The baselines are not accessible (B-001) and were therefore neither modified nor frozen. The freeze / verify harness is ready and self-tested on a labelled fixture. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)<br>Blockers: B-001. |

## MASTER.pdf - G03 | Timing

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G03-01 | Anticipate, prepare, react, protect. | S05, S06, S03 | not_started | unverified | Note: Four verbs (anticipate, prepare, react, protect); status is the minimum over all four. |
| G03-02 | Reflex every tick. | S06 | not_started | unverified |  |
| G03-03 | Tactical every tick or second. | S05, S06 | not_started | unverified |  |
| G03-04 | Strategic on completed candles and asynchronous background updates. | S02, S04, S05 | not_started | unverified | Note: Two triggers (completed candles; asynchronous background updates); status is the minimum over both. |
| G03-05 | No network LLM in critical path. | all (S00-S12) | partial | unverified | S00 [verified]: The foundation contains no network, LLM or training call; the architecture and live-lock scans forbid network clients in decision and protection packages. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `tools/check_architecture.py`, `tools/check_live_lock.py`, `tests/gie/tools/test_check_architecture.py`) |
| G03-06 | No training in critical path. | all (S00-S12) | partial | unverified | S00 [verified]: The foundation contains no network, LLM or training call; the architecture and live-lock scans forbid network clients in decision and protection packages. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `tools/check_architecture.py`, `tools/check_live_lock.py`, `tests/gie/tools/test_check_architecture.py`) |
| G03-07 | Proposed local target: tick processing <1 ms. | S01, S06, S00 | partial | unverified | S00 [partial]: The target is a research parameter. Measured cost of the foundation primitives on the development container is reported; whether tick processing meets the target must be measured by sessions 01 / 06 on the owner's hardware with real ticks. (evidence: `specifications/research_parameters.toml`, `reviews/session_00/evidence/benchmark.json`) |
| G03-08 | Proposed local target: protection decision <5 ms. | S06 | not_started | unverified |  |
| G03-09 | Proposed local target: tactical <20 ms. | S05 | not_started | unverified |  |
| G03-10 | Measure p50/p95/p99/max. | S00, S01, S05, S06, S07, S12 | partial | unverified | S00 [verified]: Latency recorder with exact nearest-rank p50 / p95 / p99 / max; event, receive and decision time on every record make end-to-end delays measurable. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/latency.py`, `tests/gie/core/test_latency.py`, `reviews/session_00/evidence/benchmark.json`) |
| G03-11 | Measure end-to-end delays. | S00, S01, S06, S07, S12 | partial | unverified | S00 [verified]: Latency recorder with exact nearest-rank p50 / p95 / p99 / max; event, receive and decision time on every record make end-to-end delays measurable. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/latency.py`, `tests/gie/core/test_latency.py`, `reviews/session_00/evidence/benchmark.json`) |
| G03-12 | Reject stale signals. | S00, S01, S03, S05, S06, S07, S08 | partial | unverified | S00 [verified]: Freshness and expiry helpers fail closed at the boundary; the order gateway refuses an entry whose plan or approval has expired. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/freshness.py`, `gie/contracts/authorization.py`, `tests/gie/core/test_freshness.py`, `tests/gie/contracts/test_authorization.py`)<br>Note: Same rule family: G06-04. |
| G03-13 | No guarantee of zero latency. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Informational: a statement of limits; nothing may be presented as zero latency. |

## MASTER.pdf - G04 | Risk

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G04-01 | No arbitrary position count. | S08, S07 | not_started | unverified |  |
| G04-02 | Aggregate gross/net risk. | S08 | not_started | unverified |  |
| G04-03 | Aggregate correlated risk. | S08 | not_started | unverified |  |
| G04-04 | Aggregate pending orders. | S08, S07 | not_started | unverified | Note: Item of the list 'aggregate gross/net and correlated risk, pending orders, margin, costs and gap stress'. |
| G04-05 | Aggregate margin. | S08 | not_started | unverified | Note: See note on G04-04. |
| G04-06 | Aggregate costs. | S08, S07 | not_started | unverified | Note: See note on G04-04. |
| G04-07 | Aggregate gap stress. | S08 | not_started | unverified | Note: See note on G04-04. |
| G04-08 | Adaptive sizing only from validated quality. | S08, S04 | not_started | unverified |  |
| G04-09 | No martingale. | S08 | not_started | unverified |  |
| G04-10 | No averaging down. | S08, S05 | not_started | unverified |  |
| G04-11 | No widening protective stops. | S06, S07, S08, S00 | partial | unverified | S00 [verified]: Every plan and position has a hard stop on the losing side; a stop can only be tightened and never removed (transition validators, ledger, journal, order gateway); interpretation to confirm: U-12. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/order_event.py`, `gie/contracts/ledger.py`, `tests/gie/contracts/test_position.py`, `tests/gie/contracts/test_order_event.py`, `tests/gie/contracts/test_ledger.py`) |
| G04-12 | Hard protective stops. | S05, S06, S07, S00 | partial | unverified | S00 [verified]: Every plan and position has a hard stop on the losing side; a stop can only be tightened and never removed (transition validators, ledger, journal, order gateway); interpretation to confirm: U-12. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/order_event.py`, `gie/contracts/ledger.py`, `tests/gie/contracts/test_position.py`, `tests/gie/contracts/test_order_event.py`, `tests/gie/contracts/test_ledger.py`) |
| G04-13 | Profit ratchet based on executable net P&L. | S06, S00 | partial | unverified | S00 [verified]: Position carries executable net P&L with a peak that can never decrease across revisions. The ratchet itself belongs to session 06. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `tests/gie/contracts/test_position.py`) |
| G04-14 | Compare against fixed exits. | S06, S10 | not_started | unverified | Note: The comparison is of the profit ratchet / dynamic protection against fixed exits. |
| G04-15 | Monotonic profit ratchet. | S06, S00 | partial | unverified | S00 [verified]: Position carries executable net P&L with a peak that can never decrease across revisions. The ratchet itself belongs to session 06. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `tests/gie/contracts/test_position.py`)<br>Note: Split from 'fast monotonic profit ratchet based on executable net P&L'. |
| G04-16 | Fast profit ratchet. | S06 | not_started | unverified | Note: Split from 'fast monotonic profit ratchet based on executable net P&L'. |

## MASTER.pdf - G05 | Daily states

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G05-01 | Normal above -2% until +5%. | S08, S00 | partial | unverified | S00 [verified]: Daily states and the four levels are contract vocabulary and research parameters (ordering halt < defensive < recovery < 0 < profit protection enforced by the loader); limits are decided on exact amounts and the sequence rule of a trading day is enforced by the run ledger (D-028); open items U-03, U-14. The state machine belongs to session 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/risk_decision.py`, `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/contracts/test_risk_decision.py`, `tests/gie/core/test_params.py`) |
| G05-02 | Defensive at -2%. | S08 | not_started | unverified |  |
| G05-03 | Proposed recovery at -1.75%. | S08 | not_started | unverified | Note: Meaning of 'recovery' is open item U-03. Configurability: G05-05. |
| G05-04 | Emergency halt at -3%. | S08 | not_started | unverified | Note: 'Proposed' and configurable: G05-05. |
| G05-05 | Recovery and emergency halt: configurable and subject to validation. | S08, S00 | partial | unverified | S00 [verified]: Daily states and the four levels are contract vocabulary and research parameters (ordering halt < defensive < recovery < 0 < profit protection enforced by the loader); limits are decided on exact amounts and the sequence rule of a trading day is enforced by the run ledger (D-028); open items U-03, U-14. The state machine belongs to session 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/risk_decision.py`, `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/contracts/test_risk_decision.py`, `tests/gie/core/test_params.py`)<br>Note: The source attaches 'configurable and subject to validation' to the recovery and emergency-halt levels only. Whether -2% and +5% are also adjustable is open item U-14; M-DIR-08 says 'Proposed numerical settings are research parameters, not approved production limits'. |
| G05-06 | Above +5% protect a nondecreasing daily profit floor. | S08, S06 | not_started | unverified |  |
| G05-07 | Above +5%: exceptional setups only with surplus risk budget after costs and open exposure. | S08, S05 | not_started | unverified |  |
| G05-08 | At exactly the floor no new risk. | S08 | not_started | unverified |  |
| G05-09 | Slippage can breach floors. | S08, S07, S10 | not_started | unverified |  |
| G05-10 | Track Prague day. | S08, S00, S01 | partial | unverified | S00 [verified]: Trading day = Europe/Prague calendar date with exact DST handling; RiskDecision verifies its trading day. The account's real reset instant is open item U-04. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/timeutil.py`, `tests/gie/core/test_timeutil.py`, `tests/gie/contracts/test_risk_decision.py`) |
| G05-11 | Track actual account rules. | S08 | not_started | unverified |  |
| G05-12 | Track other positions. | S08, S07, S00 | partial | unverified | S00 [verified]: Positions of other systems on the account are representable (owner EXTERNAL) as observations; GIE never sends orders for them. Tracking them belongs to sessions 07 / 08. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/position.py`, `gie/contracts/authorization.py`, `tests/gie/contracts/test_position.py`)<br>Note: 'Track Prague day and actual account rules, including other positions' - other positions on the same account. |
| G05-13 | Defensive: reduced risk. | S08 | not_started | unverified | Note: Split from 'defensive at -2%, reduced risk and stricter quality'. |
| G05-14 | Defensive: stricter quality. | S05, S08 | not_started | unverified | Note: Split from 'defensive at -2%, reduced risk and stricter quality'. |

## MASTER.pdf - G06 | Volatility and external data

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G06-01 | Scheduled news mode. | S03, S06 | not_started | unverified | Note: Split from 'Scheduled news and unexpected volatility mode'; see G06-18. |
| G06-02 | Prepare scenarios. | S03, S05 | not_started | unverified |  |
| G06-03 | Assess spread, acceleration and persistence. | S03, S06, S01 | not_started | unverified |  |
| G06-04 | Reject stale entries. | S05, S07, S00 | partial | unverified | S00 [verified]: Freshness and expiry helpers fail closed at the boundary; the order gateway refuses an entry whose plan or approval has expired. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/core/freshness.py`, `gie/contracts/authorization.py`, `tests/gie/core/test_freshness.py`, `tests/gie/contracts/test_authorization.py`)<br>Note: Same rule family: G03-12. |
| G06-05 | Verify account news restrictions. | S08, S03 | not_started | unverified |  |
| G06-06 | Observe XAGUSD where licensed and available. | S01, S03 | not_started | unverified | Note: First of nine observed sources; the others are G06-10 .. G06-17. |
| G06-07 | Trade only gold. | S00, S05, S07, S08 | partial | unverified | S00 [verified]: Plans, GIE orders and GIE positions can only be XAUUSD (contract invariants); other symbols can only be observed. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/trade_plan.py`, `gie/contracts/order_event.py`, `gie/contracts/position.py`, `tests/gie/contracts/test_trade_plan.py`, `tests/gie/contracts/test_order_event.py`)<br>Note: Same rule: G01-01. |
| G06-08 | Learn conditional lead/lag. | S03 | not_started | unverified |  |
| G06-09 | Never assume fixed correlations. | S03, S04 | not_started | unverified |  |
| G06-10 | Observe DXY where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-11 | Observe GC futures where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-12 | Observe Treasury yields where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-13 | Observe real yields where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-14 | Observe S&P 500 where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-15 | Observe VIX where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-16 | Observe USDJPY where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-17 | Observe timestamped news where licensed and available. | S01, S03 | not_started | unverified |  |
| G06-18 | Unexpected volatility mode. | S03, S06 | not_started | unverified | Note: Split from 'Scheduled news and unexpected volatility mode'. |

## MASTER.pdf - G07 | Learning

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G07-01 | Historical replay. | S01, S10, S09 | not_started | unverified |  |
| G07-02 | Setup-specific return models. | S04, S09 | not_started | unverified |  |
| G07-03 | New pattern discovery. | S09 | not_started | unverified |  |
| G07-04 | Rejected and missed opportunities. | S09, S05 | not_started | unverified | Note: Item of the learning list. |
| G07-05 | Execution and exit learning. | S09, S06, S07 | not_started | unverified |  |
| G07-06 | No manual-trade learning. | S09 | not_started | unverified |  |
| G07-07 | Separate decision quality from outcomes. | S09 | not_started | unverified |  |
| G07-08 | Background research isolated from active model. | S09, S04 | not_started | unverified |  |
| G07-09 | Shadow validation. | S09 | not_started | unverified |  |
| G07-10 | Versioned promotion with owner approval. | S09, S00 | partial | unverified | S00 [verified]: ModelVersion: ACTIVE only through SHADOW and with a recorded owner approval and evidence; rollback and re-activation rules; one ACTIVE version per family; a run pins its models and an entry needs a model status the role of the run allows (D-026, open item U-17). The registry belongs to session 09. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`) |
| G07-11 | Rollback. | S09, S00 | partial | unverified | S00 [verified]: ModelVersion: ACTIVE only through SHADOW and with a recorded owner approval and evidence; rollback and re-activation rules; one ACTIVE version per family; a run pins its models and an entry needs a model status the role of the run allows (D-026, open item U-17). The registry belongs to session 09. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`) |
| G07-12 | No untested online changes. | S09 | not_started | unverified |  |
| G07-13 | Market memory. | S04, S09 | not_started | unverified | Note: Split from 'Historical replay and market memory'. |

## MASTER.pdf - G08 | Reliability

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G08-01 | Build autonomous order lifecycle foundation now. | S07, S00 | partial | unverified | S00 [partial]: Order lifecycle contract with state machine, replacement protocol, idempotency and the order gateway. The lifecycle engine belongs to session 07. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/order_event.py`, `gie/contracts/guarded.py`, `tests/gie/contracts/test_order_event.py`) |
| G08-02 | Simulated adapter. | S07 | not_started | unverified |  |
| G08-03 | Live lock. | S00, S07, S12 | partial | unverified | S00 [verified]: Live lock with four layers; acceptance test S00-ACC-04. Later sessions must keep it intact; CI enforces that. (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `specifications/lock_pins.json`, `tests/gie/core/test_routing_lock.py`, `tests/gie/tools/test_check_live_lock.py`, `reviews/session_00/evidence/test_results.json`) |
| G08-04 | Persistent state. | S07, S08 | not_started | unverified |  |
| G08-05 | Restart reconciliation. | S07 | not_started | unverified |  |
| G08-06 | Disconnect/stale-feed fail closed. | S01, S06, S07, S00 | partial | unverified | S00 [verified]: Feed silence is measurable from receive time; timers for silence detection come from the injected scheduler; stale data raises and forbids new risk. Detection and reaction belong to sessions 01 / 06 / 07. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/tick.py`, `gie/core/clock.py`, `gie/core/freshness.py`, `tests/gie/contracts/test_tick.py`) |
| G08-07 | Idempotent orders. | S07, S00 | partial | unverified | S00 [verified]: Orders are idempotent by a client_order_id derived from the risk decision or position; the journal treats an identical repeat as a duplicate and refuses conflicting content; the gateway journals a request before the adapter sees it and forwards a repeated request again (the adapter is idempotent per order event). Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `gie/contracts/identity.py`, `gie/contracts/guarded.py`, `gie/contracts/journal.py`, `tests/gie/contracts/test_guarded_adapter.py`, `tests/gie/contracts/test_journal.py`) |
| G08-08 | Broker-side protection design. | S07 | not_started | unverified |  |
| G08-09 | Overnight monitoring. | S06, S07, S08 | not_started | unverified |  |
| G08-10 | Never enable real execution during this project phase. | all (S00-S12) | partial | unverified | S00 [verified]: Live lock with four layers; acceptance test S00-ACC-04. Later sessions must keep it intact; CI enforces that. (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `specifications/lock_pins.json`, `tests/gie/core/test_routing_lock.py`, `tests/gie/tools/test_check_live_lock.py`, `reviews/session_00/evidence/test_results.json`)<br>Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. |

## MASTER.pdf - G09 | Evidence

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G09-01 | Round 2: 23,481 setups. | S10, S00 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001.<br>Note: Existing evidence quoted by the specification; unverified here (specifications/baselines/claims.yaml). |
| G09-02 | Round 2: GCE-B 134 trades. | S10, S00, S02 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001.<br>Note: Existing evidence quoted by the specification; unverified here. The figures that follow in the same source sentence are separate requirements: G09-11 (+0.462R), G09-12 (18% day coverage), G09-13 (4.9% drawdown at 0.5% risk). |
| G09-03 | Round 2: no corrected statistical significance. | S10 | not_started | unverified | Note: A statement about the existing Round 2 results. |
| G09-04 | Learned score 96% coverage but negative recent period. | S10, S04 | not_started | unverified |  |
| G09-05 | Existing history is research only. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 presents the specification's numbers only as research parameters and unverified claims; no performance and no zero latency is claimed anywhere. (evidence: `specifications/research_parameters.toml`, `specifications/baselines/claims.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| G09-06 | Raw ticks not stored in Round 2. | S01, S10 | not_started | unverified |  |
| G09-07 | Reconcile 429 vs 473 Cavemen trades. | S02, S10, S00 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001. |
| G09-08 | Reconcile daily breach denominators. | S10, S08 | not_started | unverified |  |
| G09-09 | Reconcile live delay. | S02, S10, S01 | not_started | unverified |  |
| G09-10 | Use actual tick data for fast execution claims. | S01, S06, S07, S10 | not_started | unverified |  |
| G09-11 | Round 2: GCE-B +0.462R. | S10, S00, S02 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001.<br>Note: Unverified claim; definition is open item U-05. |
| G09-12 | Round 2: GCE-B 18% day coverage. | S10, S00, S02 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001.<br>Note: Unverified claim; denominator is open item U-05. |
| G09-13 | Round 2: GCE-B 4.9% drawdown at 0.5% risk. | S10, S00, S02 | not_started | unverified | S00 [implemented]: Recorded as an unverified claim with its open questions (U-05); nothing in the repository treats it as verified. Reconciliation belongs to sessions 02 and 10 and needs the baselines (B-001). (evidence: `specifications/baselines/claims.yaml`; blockers: B-001)<br>Blockers: B-001.<br>Note: Unverified claim; convention is open item U-05. |

## MASTER.pdf - G10 | Validation

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| G10-01 | Chronological purged walk-forward. | S10, S04 | not_started | unverified |  |
| G10-02 | Embargo. | S10, S04 | not_started | unverified |  |
| G10-03 | Training-only transforms. | S04, S10 | not_started | unverified |  |
| G10-04 | Block bootstrap. | S10 | not_started | unverified |  |
| G10-05 | Multiple-testing correction. | S10, S09 | not_started | unverified |  |
| G10-06 | Cost/latency stress. | S10, S04, S07 | not_started | unverified |  |
| G10-07 | Untouched forward paper. | S10, S12 | not_started | unverified |  |
| G10-08 | Report all eligible days. | S10 | not_started | unverified |  |
| G10-09 | Report net R/day. | S10 | not_started | unverified |  |
| G10-10 | Report mean/median ROI. | S10 | not_started | unverified |  |
| G10-11 | Report >=5% days. | S10 | not_started | unverified |  |
| G10-12 | Report coverage. | S10 | not_started | unverified |  |
| G10-13 | Report drawdown. | S10 | not_started | unverified |  |
| G10-14 | Report breaches. | S10, S08 | not_started | unverified |  |
| G10-15 | Report phase-specific challenge outcomes. | S10, S08 | not_started | unverified |  |
| G10-16 | Report failures. | S10, S09 | not_started | unverified |  |
| G10-17 | No cherry-picking. | all (S00-S12) | partial | unverified | S00 [verified]: Every test outcome and every blocked item of Session 00 is reported; evidence is generated, never selected by hand. Foundation part only; the behaviour belongs to the sessions named for this requirement. (evidence: `reviews/session_00/evidence/test_results.json`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| G10-18 | Report uncertainty. | S10, S09 | not_started | unverified | Note: Split from 'failures and uncertainty'. |

## MASTER.pdf - Implementation directive

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| M-DIR-01 | Start with session 00 only. | S00 | verified | unverified | S00 [verified]: Started with Session 00 only. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Same rule from the owner's directive: OWN-07. |
| M-DIR-02 | Owner approves contracts before parallel work. | S00 | in_progress | unverified | S00 [implemented]: The contracts are submitted for approval (lock status PROPOSED); no parallel work was started. The owner's approval is the open step. (evidence: `gie/contracts/schemas/LOCK.json`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| M-DIR-03 | Wave 2: 01, 02, 07, 08. | S00 | not_started | unverified | S00 [implemented]: The waves are encoded in the dependency graph with the start gate; no wave has been opened. (evidence: `specifications/dependency_graph.yaml`) |
| M-DIR-04 | Wave 3: 03, 04, 05, 06 as dependencies become ready. | S00 | not_started | unverified | S00 [implemented]: The waves are encoded in the dependency graph with the start gate; no wave has been opened. (evidence: `specifications/dependency_graph.yaml`) |
| M-DIR-05 | Wave 4: 09, 10, 11, 12. | S00 | not_started | unverified | S00 [implemented]: The waves are encoded in the dependency graph with the start gate; no wave has been opened. (evidence: `specifications/dependency_graph.yaml`) |
| M-DIR-06 | Coordinator alone integrates reviewed branches. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00: only the coordinator integrated work; the rule and the procedure are in the worktree instructions. (evidence: `docs/WORKTREES.md`, `specifications/dependency_graph.yaml`)<br>Note: A duty of the coordinator and a prohibition for every other session. |
| M-DIR-07 | No live activation. | all (S00-S12) | partial | unverified | S00 [verified]: No live activation is possible (routing lock, S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `reviews/session_00/evidence/test_results.json`)<br>Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. |
| M-DIR-08 | Proposed numerical settings are research parameters, not approved production limits. | all (S00-S12) | partial | unverified | S00 [verified]: All numerical settings of the specification are in one file labelled PROPOSED_RESEARCH_PARAMETERS, loaded strictly; none is hard-coded or treated as an approved limit. (evidence: `specifications/research_parameters.toml`, `gie/core/params.py`, `tests/gie/core/test_params.py`) |
| M-DIR-09 | Research and QA start scaffolding earlier. | S00, S10, S12 | not_started | unverified | S00 [implemented]: Sessions 10 and 12 are marked early_scaffolding in the dependency graph; neither has been started. (evidence: `specifications/dependency_graph.yaml`)<br>Note: Split from the Wave 4 clause. |

## MASTER.pdf - Review gate

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| M-GATE-01 | Every session delivers requirement IDs. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`) |
| M-GATE-02 | Every session delivers code paths. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`) |
| M-GATE-03 | Every session delivers test commands and outputs. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`) |
| M-GATE-04 | Every session delivers commit hash. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`) |
| M-GATE-05 | Every session delivers data provenance. | all (S00-S12) | partial | unverified | S00 [verified]: Data provenance of Session 00: no market data was used; every sample record is labelled SYNTHETIC_FIXTURE; every record carries its data origin and every run a manifest. (evidence: `tests/gie/samples.py`, `gie/contracts/run.py`, `gie/contracts/common.py`) |
| M-GATE-06 | Every session delivers measured performance. | all (S00-S12) | partial | unverified | S00 [verified]: Measured performance of the foundation primitives and of the enforcement path (p50 / p95 / p99 / max), labelled as micro-benchmarks on synthetic records on the development container. (evidence: `reviews/session_00/evidence/benchmark.json`, `tools/benchmark_foundation.py`) |
| M-GATE-07 | Every session delivers blockers. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`) |
| M-GATE-08 | Status: approved, conditional, rejected or unverified. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 is submitted with review status 'unverified'; the owner sets approved, conditional or rejected. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `specifications/requirements_matrix.yaml`) |
| M-GATE-09 | Owner authorizes merge. | all (S00-S12) | not_started | unverified | S00 [not_started]: No merge has been requested; nothing is merged without the owner's authorisation. (evidence: `docs/WORKTREES.md`) |
| M-GATE-10 | Owner authorizes model promotion. | S00, S04, S09 | partial | unverified | S00 [partial]: The ModelVersion contract makes ACTIVE unreachable without a recorded owner approval, evaluation evidence and shadow runs; the promotion process itself belongs to session 09. (evidence: `gie/contracts/model_version.py`, `tests/gie/contracts/test_model_version.py`)<br>Note: Split from 'Owner authorizes merge and model promotion'. |
| M-GATE-11 | Every session delivers change requests. | all (S00-S12) | partial | unverified | S00 [verified]: The Session 00 review package delivers requirement ids, code paths, test commands and outputs, the commit hash, blockers and change requests. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/test_results.json`, `docs/CHANGE_REQUESTS.md`)<br>Note: Split from 'blockers and change requests'. |

## MASTER.pdf - Document header and footer

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| M-DOC-01 | GIE / ENGINEERING SPECIFICATION / V1.0 ... GIE \| 27 Nov 2026 \| Paper / research only | all (S00-S12) | partial | unverified | S00 [verified]: Specification version V1.0 of 27 Nov 2026 is recorded with the hashes of its text; the review PDF carries the 'Paper / research only' label on every page. (evidence: `specifications/requirements_matrix.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Header (before '...') and footer (after '...') printed on every page of the master specification and of every brief; the '...' joins the two and is not part of the source. Specification version V1.0; label 'Paper / research only'. |

## 00_Project_Coordinator.pdf - Common brief clauses

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| COM-01 | Open a dedicated Claude Code session and worktree. | all (S00-S12) | partial | unverified | S00 [verified]: Replaced for the whole project by the owner's directive: one coordinating session with sub-agents and worktrees (D-001, CR-001, approved). (evidence: `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`)<br>Note: Replaced for this project by the owner's launch directive (OWN-01, OWN-06): decision D-001, change request CR-001. Identical text in every session brief (00-12); the source names the first. |
| COM-02 | Read MASTER.pdf or its extracted text. | all (S00-S12) | partial | unverified | S00 [verified]: MASTER.pdf and its extracted text were read in full. (evidence: `specifications/extracted/MASTER.txt`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-03 | Paths are proposed until coordinator approves actual repository layout. | all (S00-S12) | in_progress | unverified | S00 [implemented]: Repository layout proposed (D-002) with the mapping from brief paths; awaits the owner's approval (CR-002). (evidence: `specifications/dependency_graph.yaml`, `docs/DECISION_LOG.md`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-04 | Integrated prompt - paste or attach to Claude. | all (S00-S12) | not_applicable | unverified | S00 [not_applicable]: Instruction to the owner on how to hand over a brief; nothing to implement. (evidence: `CLAUDE.md`)<br>Note: The prompt text is identical to the master 'Authority and scope' clause in all 13 briefs; its atoms are M-AUTH-01 .. M-AUTH-26. Identical text in every session brief (00-12); the source names the first. |
| COM-05 | Apply the master requirements G01-G10 relevant to this subsystem. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 applied the master requirements relevant to the foundation (see the G-rows of this matrix). (evidence: `specifications/requirements_matrix.yaml`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-06 | Do not redefine shared contracts unilaterally. | all (S00-S12) | partial | unverified | S00 [verified]: Shared contracts can only be defined in gie.contracts (the framework refuses any other module), are locked by hash, and change only under the version rules; missing shared types are routed through the coordinator (CR-003, CR-005). (evidence: `gie/contracts/_spec.py`, `gie/contracts/schemas/LOCK.json`, `tests/gie/contracts/test_schema_lock.py`, `docs/CHANGE_REQUESTS.md`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-07 | If a dependency is unavailable, use explicitly labeled test fixtures only. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 used only explicitly labelled synthetic fixtures (DataOrigin.SYNTHETIC_FIXTURE); a derived record cannot claim a better origin than its inputs. (evidence: `tests/gie/samples.py`, `gie/contracts/causality.py`, `tests/gie/contracts/test_causality.py`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-08 | Do not claim integration complete. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 claims no integration: no subsystem exists. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Applies when a dependency is unavailable and labelled fixtures are used (COM-07). Identical text in every session brief (00-12); the source names the first. |
| COM-09 | Submit the review package and await owner approval before merging. | all (S00-S12) | partial | unverified | S00 [verified]: Session 00 review package submitted; nothing merged. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-10 | Report failed tests explicitly. | all (S00-S12) | partial | unverified | S00 [verified]: Every test that did not pass (failed, skipped, blocked) is listed in the review package. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `reviews/session_00/evidence/test_results.json`)<br>Note: Identical text in every session brief (00-12); the source names the first. |
| COM-11 | No live trading. | all (S00-S12) | partial | unverified | S00 [verified]: No live trading (routing lock, S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `reviews/session_00/evidence/test_results.json`)<br>Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. Identical text in every session brief (00-12); the source names the first. |
| COM-12 | Report unimplemented requirements explicitly. | all (S00-S12) | partial | unverified | S00 [verified]: Unimplemented and blocked requirements of Session 00 are listed explicitly in the review package. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `specifications/requirements_matrix.yaml`)<br>Note: Split from 'Report failed tests and unimplemented requirements explicitly'. Identical text in every session brief (00-12); the source names the first. |

## specifications/source/OWNER_DIRECTIVE_2026-11-27.md - Owner launch instructions

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| OWN-01 | You will coordinate the entire project within this session. | S00 | in_progress | unverified | S00 [verified]: Session 00 was coordinated and implemented in this session (D-001). (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| OWN-02 | Read every PDF in full before beginning. | S00 | verified | unverified | S00 [verified]: All 14 PDFs (master specification and 13 briefs) were read in full; their text is extracted and hashed in the repository. (evidence: `specifications/source/SHA256SUMS`, `specifications/extracted/MASTER.txt`, `specifications/requirements_matrix.yaml`) |
| OWN-03 | Extract and preserve all requirements in permanent project documentation so nothing is lost during context compaction. | S00 | verified | unverified | S00 [verified]: 584 atomised requirements with verbatim source clauses in the permanent matrix; decisions, blockers and the owner's directive are permanent documents; CI detects a deleted requirement or a changed specification text. (evidence: `specifications/requirements_matrix.yaml`, `specifications/source/OWNER_DIRECTIVE_2026-11-27.md`, `tools/gie_registers.py`) |
| OWN-04 | Treat ChatGPT as the architectural supervisor and independent reviewer. | all (S00-S12) | in_progress | unverified | S00 [implemented]: The review package is written for the supervisor's independent review; the supervisor cannot be contacted from this session, so the owner is asked to forward it (open item U-13). An internal independent review (two rounds) was performed in addition, not instead. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `docs/DECISION_LOG.md`, `reviews/session_00/evidence/independent_review.json`) |
| OWN-05 | You are responsible for implementation, not independently reducing the agreed scope. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`) |
| OWN-06 | You may use specialized subagents and parallel development where appropriate, provided all components follow the approved shared architecture. | all (S00-S12) | in_progress | unverified | S00 [verified]: Sub-agents were used for independent tests, tooling and reviews under the shared rules (D-001, D-023); integration was done by the coordinator only. (evidence: `docs/DECISION_LOG.md`, `reviews/session_00/evidence/independent_review.json`) |
| OWN-07 | Begin with Session 00 only. | S00 | verified | unverified | S00 [verified]: Only Session 00 was performed. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `specifications/dependency_graph.yaml`)<br>Note: Same rule: M-DIR-01. |
| OWN-08 | Maintain a persistent requirements matrix throughout development. | all (S00-S12) | in_progress | unverified | S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)<br>Note: Artefact: specifications/requirements_matrix.yaml. |
| OWN-09 | Do not silently simplify functionality. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`) |
| OWN-10 | If you encounter technical limitations, document them and propose solutions for approval. | all (S00-S12) | in_progress | unverified | S00 [verified]: The one technical limitation met (no access to the production tree) is documented as B-001 with three proposed solutions for approval. (evidence: `docs/CHANGE_REQUESTS.md`, `docs/BASELINE_AUDIT.md`) |
| OWN-11 | Complete Session 00. | S00 | partial | unverified | S00 [partial]: Everything in the Session 00 brief that does not need the production tree is complete and verified. The audit, the inventories, the baseline audit document and the real baseline regression are blocked (B-001). (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`, `docs/CHANGE_REQUESTS.md`; blockers: B-001)<br>Blockers: B-001. |
| OWN-12 | Stop at the architectural review gate before implementing the remaining subsystems. | S00 | verified | unverified | S00 [verified]: Work stops at the architectural review gate; no remaining subsystem was started. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| OWN-13 | Preserve all existing trading systems. | all (S00-S12) | in_progress | unverified | S00 [verified]: Nothing of the existing systems was touched: the production tree was never accessed; in this repository the pre-existing file Start is byte-identical and branch main is unchanged (checked in CI). (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `reviews/session_00/evidence/ci_results.json`)<br>Note: Same rule: M-AUTH-12. |
| OWN-14 | Live execution must remain disabled throughout development and validation. | all (S00-S12) | in_progress | unverified | S00 [verified]: Live execution is impossible in the foundation: four-layer routing lock, tested and scanned in CI (acceptance test S00-ACC-04). (evidence: `gie/core/routing_lock.py`, `gie/contracts/guarded.py`, `tools/check_live_lock.py`, `tests/gie/core/test_routing_lock.py`, `reviews/session_00/evidence/test_results.json`)<br>Note: Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. |
| OWN-15 | The ZIP contains the complete Gold Intelligence Engine project specification and all 13 specialist development briefs. | S00 | verified | unverified | S00 [verified]: The ZIP held MASTER.pdf and the 13 briefs 00-12; all are preserved with SHA-256 sums. (evidence: `specifications/source/SHA256SUMS`)<br>Note: Informational: defines the agreed scope. All 13 briefs and the master specification are stored with SHA-256 in specifications/source/. |
| OWN-16 | Maintain a persistent implementation status throughout development. | all (S00-S12) | in_progress | unverified | S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)<br>Note: Artefact: docs/IMPLEMENTATION_STATUS.md. |
| OWN-17 | Maintain a persistent decision log throughout development. | all (S00-S12) | in_progress | unverified | S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)<br>Note: Artefact: docs/DECISION_LOG.md. |
| OWN-18 | Maintain a persistent acceptance test register throughout development. | all (S00-S12) | in_progress | unverified | S00 [verified]: The four registers exist, are validated in CI and their readable forms are generated; they continue through all sessions. (evidence: `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/ACCEPTANCE_TEST_REGISTER.md`, `tools/gie_registers.py`)<br>Note: Artefact: specifications/acceptance_tests.yaml. |
| OWN-19 | Do not substitute placeholders. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`) |
| OWN-20 | Do not omit requirements. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`) |
| OWN-21 | Do not declare incomplete components finished. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00: no requirement was dropped or reduced; what could not be done is reported as blocked (B-001); additions and deviations are change requests (CR-001..CR-008); no placeholder package exists; statuses are derived from recorded evidence. The rule continues for all later sessions. (evidence: `specifications/requirements_matrix.yaml`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tools/check_docs_paths.py`) |
| OWN-22 | Treat me as the project owner with final authority. | all (S00-S12) | in_progress | unverified | S00 [verified]: Every decision of the coordinator is PROPOSED until the owner approves it; the contract lock is PROPOSED; nothing is merged or started beyond Session 00. (evidence: `docs/DECISION_LOG.md`, `gie/contracts/schemas/LOCK.json`)<br>Note: 'me' is the project owner who wrote the directive. Split from OWN-04. |
| OWN-23 | Submit the foundation review PDF. | S00 | verified | unverified | S00 [verified]: Foundation review PDF. (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Split from OWN-11. |
| OWN-24 | Submit the shared contracts. | S00 | verified | unverified | S00 [verified]: Shared contracts: code, normative rules, field reference, schemas and lock. (evidence: `gie/contracts/schemas/LOCK.json`, `specifications/SHARED_CONTRACTS.md`, `specifications/contracts/FIELDS.md`)<br>Note: Split from OWN-11. |
| OWN-25 | Submit the source files. | S00 | verified | unverified | S00 [verified]: Source files: pushed to the development branch; SHA-256 manifest of the reviewed commit. (evidence: `reviews/session_00/evidence/file_manifest.json`)<br>Note: Split from OWN-11. |
| OWN-26 | Submit the test evidence. | S00 | verified | unverified | S00 [verified]: Test evidence: machine-readable results of every test with requirement ids, CI results, coverage, benchmark. (evidence: `reviews/session_00/evidence/test_results.json`, `reviews/session_00/evidence/ci_results.json`, `reviews/session_00/evidence/coverage.json`, `reviews/session_00/evidence/benchmark.json`)<br>Note: Split from OWN-11. |
| OWN-27 | You will implement the entire project within this session. | all (S00-S12) | in_progress | unverified | S00 [verified]: Session 00 implemented in this session; sessions 01-12 not started (review gate). (evidence: `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`)<br>Note: Split from OWN-01; stays open until the last session is approved. |

## 00_Project_Coordinator.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S00-DEP-01 | Dependencies: None; start here. | S00 | verified | unverified | S00 [verified]: Session 00 was started first and alone; no other session has been started and no other package exists. (evidence: `specifications/dependency_graph.yaml`, `docs/ARCHITECTURE.md`) |
| S00-DEP-02 | Owned scope: core/, contracts/, specifications/ | S00 | verified | unverified | S00 [verified]: Session 00 owns gie/core, gie/contracts and specifications (brief paths core/, contracts/, specifications/ mapped by D-002; layout awaits approval, CR-002). Ownership of every file is checked in CI. (evidence: `specifications/dependency_graph.yaml`, `tools/check_architecture.py`, `tests/gie/tools/test_check_architecture.py`)<br>Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 00_Project_Coordinator.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S00-ASG-01 | Audit existing D:\ftmo_eurusd without modifying production. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-02 | Inventory languages. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-03 | Inventory processes. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-04 | Inventory data. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-05 | Inventory tests. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-06 | Inventory hardware. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-07 | Inventory broker interfaces. | S00 | blocked | unverified | S00 [blocked]: NOT PERFORMED. The production tree D:\ftmo_eurusd cannot be reached from this cloud session (blocker B-001); nothing about it was assumed or invented. Delivered instead: a read-only, standard-library audit tool for the owner to run, tested to leave the audited tree byte-identical and to emit no credentials and no file contents beyond package names and versions, CSV column names and Python version strings. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/gie_baseline_audit.py`, `tools/audit/README.md`, `tests/gie/tools/test_baseline_audit.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ASG-08 | Establish repository/worktrees. | S00 | implemented | unverified | S00 [implemented]: Repository structure, development branch and the branch / worktree procedure with start gate and integration gate are defined. No session worktree has been created yet because only Session 00 has run; the procedure is therefore documented but not yet exercised. (evidence: `docs/WORKTREES.md`, `specifications/dependency_graph.yaml`, `README.md`) |
| S00-ASG-09 | Establish ownership. | S00 | verified | unverified | S00 [verified]: Every path has exactly one owning session (owns lists); shared files are coordinator-only; CI fails on an unowned or doubly owned file. (evidence: `specifications/dependency_graph.yaml`, `tools/check_architecture.py`, `tests/gie/tools/test_check_architecture.py`) |
| S00-ASG-10 | Establish dependency graph. | S00 | verified | unverified | S00 [verified]: Dependency graph of the 13 sessions with waves, hard / contract-only / optional dependencies, gates, import rules and the shared types still to be proposed (contract_extensions); validated in CI (no cycle, wave order, import rules enforced on the code). (evidence: `specifications/dependency_graph.yaml`, `tools/check_architecture.py`, `tests/gie/tools/test_check_architecture.py`, `docs/ARCHITECTURE.md`) |
| S00-ASG-11 | Establish versioned contract for Tick. | S00 | verified | unverified | S00 [verified]: Tick 1.0.0 (PROPOSED, awaiting owner approval): raw bid/ask quote with event and receive time, per-stream sequence number, data origin and quality flags. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/tick.py`, `gie/contracts/schemas/Tick-1.0.0.schema.json`, `tests/gie/contracts/test_tick.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-12 | Establish versioned contract for Evidence. | S00 | verified | unverified | S00 [verified]: Evidence 1.0.0 (PROPOSED, awaiting owner approval): engine evidence with bullish / bearish scores, market-event identity for exact de-duplication, stages, levels, invalidation, freshness, missing / inactive / contra states; vocabularies are provisional until the six engines can be inspected (D-014, B-001). Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/evidence.py`, `gie/contracts/schemas/Evidence-1.0.0.schema.json`, `tests/gie/contracts/test_evidence.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-13 | Establish versioned contract for Candidate. | S00 | verified | unverified | S00 [verified]: Candidate 1.0.0 (PROPOSED, awaiting owner approval): scored or unscored trading opportunity with expected net R, uncertainty, quantiles, destinations and contributions. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/candidate.py`, `gie/contracts/schemas/Candidate-1.0.0.schema.json`, `tests/gie/contracts/test_candidate.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-14 | Establish versioned contract for TradePlan. | S00 | verified | unverified | S00 [verified]: TradePlan 1.0.0 (PROPOSED, awaiting owner approval): executable plan with entry style, hard stop, targets, expiry, expected net R and uncertainty, alternative group. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/trade_plan.py`, `gie/contracts/schemas/TradePlan-1.0.0.schema.json`, `tests/gie/contracts/test_trade_plan.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-15 | Establish versioned contract for RiskDecision. | S00 | verified | unverified | S00 [verified]: RiskDecision 1.0.0 (PROPOSED, awaiting owner approval): approval or rejection of one plan with granted volume and risk, daily state, profit floor and reservation. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/risk_decision.py`, `gie/contracts/schemas/RiskDecision-1.0.0.schema.json`, `tests/gie/contracts/test_risk_decision.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-16 | Establish versioned contract for OrderEvent. | S00 | verified | unverified | S00 [verified]: OrderEvent 1.0.0 (PROPOSED, awaiting owner approval): one step in the life of a simulated order: requests, reports, fills, replacement protocol, reconciliation. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/order_event.py`, `gie/contracts/schemas/OrderEvent-1.0.0.schema.json`, `tests/gie/contracts/test_order_event.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-17 | Establish versioned contract for Position. | S00 | verified | unverified | S00 [verified]: Position 1.0.0 (PROPOSED, awaiting owner approval): snapshot of a GIE or external position with stop, executable net P&L, peak and trough. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/position.py`, `gie/contracts/schemas/Position-1.0.0.schema.json`, `tests/gie/contracts/test_position.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-18 | Establish versioned contract for ModelVersion. | S00 | verified | unverified | S00 [verified]: ModelVersion 1.0.0 (PROPOSED, awaiting owner approval): registry record of a model with training provenance, status lifecycle, owner approval and rollback. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/contracts/model_version.py`, `gie/contracts/schemas/ModelVersion-1.0.0.schema.json`, `tests/gie/contracts/test_model_version.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-19 | Establish versioned contract for Clock. | S00 | verified | unverified | S00 [verified]: Clock 1.0.0 (PROPOSED, awaiting owner approval): injected Clock and Scheduler with step time in replay and paper (ReplayClock; PaperClock over SystemClock with PollingScheduler; read-only views for components) and the ClockReading record. Declared once; validation, canonical encoding, strict decoding and the JSON Schema are derived from the declaration; locked by hash; golden messages; tests written by engineers who did not write the contract. (evidence: `gie/core/clock.py`, `tests/gie/core/test_clock.py`, `gie/contracts/clock.py`, `gie/contracts/schemas/Clock-1.0.0.schema.json`, `tests/gie/contracts/test_clock_reading.py`, `tests/gie/contracts/test_roundtrip.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ASG-20 | Define units. | S00 | verified | unverified | S00 [verified]: Units defined and enforced: int64 nanoseconds UTC; prices, volumes, money and risk fractions as exact decimals (strings on the wire); fractions not percent; R multiples net of costs; unknown is null, never 0. (evidence: `specifications/SHARED_CONTRACTS.md`, `specifications/contracts/FIELDS.md`, `gie/core/canonical.py`, `tests/gie/core/test_canonical.py`, `tests/gie/contracts/test_spec_framework.py`) |
| S00-ASG-21 | Define timestamps. | S00 | verified | unverified | S00 [verified]: Event time, receive time, decision time, market-data watermark and observable time defined; all time from an injected clock; trading day = Europe/Prague calendar date (D-013; account reset instant to be confirmed, U-04). (evidence: `specifications/SHARED_CONTRACTS.md`, `gie/core/timeutil.py`, `gie/core/clock.py`, `tests/gie/core/test_timeutil.py`, `tests/gie/core/test_clock.py`) |
| S00-ASG-22 | Define IDs. | S00 | verified | unverified | S00 [verified]: Deterministic content-derived identifiers (prefix + 128 bits of SHA-256 over documented identity parts); records verify their own identifier; no random or clock-derived ids. (evidence: `specifications/SHARED_CONTRACTS.md`, `gie/core/ids.py`, `gie/contracts/identity.py`, `tests/gie/core/test_ids.py`, `tests/gie/contracts/test_identity.py`) |
| S00-ASG-23 | Define freshness. | S00 | verified | unverified | S00 [verified]: Freshness and expiry rules defined and fail closed at the boundary; future data is an error; expiry propagates to derived records and orders. The numeric limits are research parameters requested by the consuming sessions; none was invented here. (evidence: `specifications/SHARED_CONTRACTS.md`, `gie/core/freshness.py`, `gie/contracts/causality.py`, `tests/gie/core/test_freshness.py`, `tests/gie/contracts/test_causality.py`) |
| S00-ASG-24 | Define error semantics. | S00 | verified | unverified | S00 [verified]: Typed errors with stable codes; fail closed; invalid input is never repaired; unknown vocabulary is refused; normal rejections are records with reason codes. (evidence: `specifications/SHARED_CONTRACTS.md`, `gie/core/errors.py`, `tests/gie/core/test_errors.py`) |
| S00-ASG-25 | Define event ordering. | S00 | verified | unverified | S00 [verified]: Input order (availability time, stream rank, stream, sequence) and the total order of a run (journal order, run-to-completion step) defined; duplicate / gap / conflict detection; reference journal with look-ahead guard. (evidence: `specifications/SHARED_CONTRACTS.md`, `gie/core/ordering.py`, `gie/contracts/ordering.py`, `gie/contracts/journal.py`, `tests/gie/core/test_ordering.py`, `tests/gie/contracts/test_contract_ordering.py`, `tests/gie/contracts/test_journal.py`) |
| S00-ASG-26 | Create CLAUDE.md. | S00 | verified | unverified | S00 [verified]: CLAUDE.md created: binding working rules for every later session. (evidence: `CLAUDE.md`) |
| S00-ASG-27 | Create decision log. | S00 | verified | unverified | S00 [verified]: Decision log with decisions (D), open items for the owner (U); blockers and change requests in a second log; both parsed and cross-checked by the register tool. (evidence: `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`, `tools/gie_registers.py`, `tests/gie/tools/test_gie_registers.py`) |
| S00-ASG-28 | Create requirements matrix. | S00 | verified | unverified | S00 [verified]: Requirements matrix with 584 atomised requirements, each with the verbatim source clause (checked against the extracted specification text); every requirement is locked by hash, so none can be deleted or reworded unnoticed; validated in CI. (evidence: `specifications/requirements_matrix.yaml`, `specifications/requirements_lock.json`, `specifications/REQUIREMENTS_MATRIX.md`, `tools/gie_registers.py`, `tests/gie/tools/test_gie_registers.py`) |
| S00-ASG-29 | Create integration CI. | S00 | verified | unverified | S00 [verified]: Integration CI: one entry point (python -m tools.ci) running lint, format, strict typing, schema lock, generated docs, golden messages, live-lock scan, architecture, protected paths, documentation paths, secret scan, tests with evidence, register validation and baseline verification; the same entry point runs in the GitHub workflow on Linux and Windows. (evidence: `tools/ci.py`, `.github/workflows/gie-ci.yml`, `reviews/session_00/evidence/ci_results.json`) |

## 00_Project_Coordinator.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S00-ACC-01 | Contract round-trip tests. | S00 | verified | unverified | S00 [verified]: Round-trip of every contract and every sample variant, property-based round trips, strict decoding, golden messages byte for byte, schema validation of the wire form. (evidence: `tests/gie/contracts/test_roundtrip.py`, `tests/gie/contracts/test_roundtrip_property.py`, `tests/gie/contracts/test_golden.py`, `tests/gie/golden`, `reviews/session_00/evidence/test_results.json`) |
| S00-ACC-02 | Contract compatibility tests. | S00 | verified | unverified | S00 [verified]: Reader rules for other versions, classification of schema changes (PATCH / MINOR / MAJOR), schema lock, routing vocabulary can never change silently. (evidence: `tests/gie/contracts/test_compatibility.py`, `tests/gie/contracts/test_schema_lock.py`, `gie/contracts/compat.py`, `gie/contracts/schemas/LOCK.json`, `reviews/session_00/evidence/test_results.json`) |
| S00-ACC-03 | Replay clock. | S00 | verified | unverified | S00 [verified]: Replay clock: virtual time, never backwards, deterministic timers; the same component code observes the same instants and timer order under the replay clock and under the paper step clock with its scheduler. (evidence: `tests/gie/core/test_clock.py`, `gie/core/clock.py`, `reviews/session_00/evidence/test_results.json`) |
| S00-ACC-04 | Paper-only routing lock. | S00 | verified | unverified | S00 [verified]: Paper-only routing lock: no live vocabulary, literal constant, guard and order gateway, static scan with pinned hashes; environment variables and files cannot unlock it. (evidence: `tests/gie/core/test_routing_lock.py`, `tests/gie/contracts/test_guarded_adapter.py`, `tests/gie/tools/test_check_live_lock.py`, `tools/check_live_lock.py`, `specifications/lock_pins.json`, `reviews/session_00/evidence/test_results.json`) |
| S00-ACC-05 | Baseline regression. | S00 | blocked | unverified | S00 [blocked]: NOT EXECUTED on real baselines: GCE-B, Round 1, Round 2 and the Cavemen trade sets are on the owner's machine (B-001). The freeze / verify harness exists and is self-tested on a labelled synthetic fixture; the real regression test reports BLOCKED and never passes vacuously. (evidence: `tools/baseline_freeze.py`, `specifications/baselines/registry.yaml`, `tests/gie/baseline/test_baseline_regression.py`, `tests/gie/tools/test_baseline_freeze.py`; blockers: B-001)<br>Blockers: B-001. |
| S00-ACC-06 | No production changes. | S00 | partial | unverified | S00 [partial]: Shown for this repository: the pre-existing file Start is byte-identical to the base commit and branch main was not advanced (checked in CI); the audit tool is tested to leave an audited tree byte-identical. NOT shown for the production tree itself, which was never reached (B-001) - so it was also never written to; the test that compares manifests of the production tree taken before and after reports BLOCKED until the owner supplies them. (evidence: `tools/check_protected_paths.py`, `tests/gie/tools/test_protected_paths.py`, `tests/gie/tools/test_baseline_audit.py`, `tests/gie/baseline/test_baseline_regression.py`, `reviews/session_00/evidence/ci_results.json`; blockers: B-001)<br>Blockers: B-001. |

## 00_Project_Coordinator.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S00-DEL-01 | Architecture report. | S00 | verified | unverified | S00 [verified]: Architecture report. (evidence: `docs/ARCHITECTURE.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| S00-DEL-02 | Contract schemas. | S00 | verified | unverified | S00 [verified]: Contract schemas (JSON Schema 2020-12) for ten contracts at 1.0.0, generated from the code, locked by SHA-256 (lock status PROPOSED), with the normative rules and the generated field reference. (evidence: `gie/contracts/schemas/LOCK.json`, `specifications/SHARED_CONTRACTS.md`, `specifications/contracts/FIELDS.md`, `tests/gie/contracts/test_schema_lock.py`) |
| S00-DEL-03 | Dependency graph. | S00 | verified | unverified | S00 [verified]: Dependency graph (machine-readable, checked in CI) and its diagram in the review PDF. (evidence: `specifications/dependency_graph.yaml`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |
| S00-DEL-04 | Baseline audit. | S00 | blocked | unverified | S00 [blocked]: NOT DELIVERED: the baseline audit could not be performed (B-001). The document states exactly what is and is not known and how the audit can be completed. (evidence: `docs/BASELINE_AUDIT.md`, `tools/audit/README.md`; blockers: B-001)<br>Blockers: B-001. |
| S00-DEL-05 | Worktree instructions. | S00 | verified | unverified | S00 [verified]: Worktree instructions. (evidence: `docs/WORKTREES.md`) |
| S00-DEL-06 | Unresolved decisions. | S00 | verified | unverified | S00 [verified]: Unresolved decisions: open items U-01..U-18 for the owner, blocker B-001, change requests CR-002..CR-008, decisions D-002..D-028 awaiting approval. (evidence: `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`, `reviews/session_00/GIE_Session_00_Foundation_Review.pdf`) |

## 01_Market_Data.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S01-DEP-01 | Dependencies: 00. | S01 | not_started | unverified |  |
| S01-DEP-02 | Owned scope: market_data/ | S01 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 01_Market_Data.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S01-ASG-01 | Implement timestamped raw bid/ask tick ingestion. | S01 | not_started | unverified |  |
| S01-ASG-02 | Implement timestamped raw bid/ask tick storage. | S01 | not_started | unverified |  |
| S01-ASG-03 | Implement historical replay. | S01 | not_started | unverified |  |
| S01-ASG-04 | Implement candle construction. | S01 | not_started | unverified |  |
| S01-ASG-05 | Implement broker calendar. | S01 | not_started | unverified |  |
| S01-ASG-06 | Implement quality flags. | S01 | not_started | unverified |  |
| S01-ASG-07 | Implement sequence IDs. | S01 | not_started | unverified |  |
| S01-ASG-08 | Implement feed freshness. | S01 | not_started | unverified |  |
| S01-ASG-09 | Audit missing history. | S01 | not_started | unverified |  |
| S01-ASG-10 | Acquire only authorized data. | S01 | not_started | unverified |  |
| S01-ASG-11 | Distinguish event time, receive time and decision time. | S01 | not_started | unverified |  |
| S01-ASG-12 | Provide synchronized external feeds to 03. | S01 | not_started | unverified |  |

## 01_Market_Data.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S01-ACC-01 | Out-of-order tick tests. | S01 | not_started | unverified |  |
| S01-ACC-02 | Duplicate tick tests. | S01 | not_started | unverified |  |
| S01-ACC-03 | Missing tick tests. | S01 | not_started | unverified |  |
| S01-ACC-04 | Stale tick tests. | S01 | not_started | unverified |  |
| S01-ACC-05 | DST tests. | S01 | not_started | unverified |  |
| S01-ACC-06 | Holiday tests. | S01 | not_started | unverified |  |
| S01-ACC-07 | Rollover tests. | S01 | not_started | unverified |  |
| S01-ACC-08 | Deterministic replay. | S01 | not_started | unverified |  |
| S01-ACC-09 | Throughput distributions. | S01 | not_started | unverified |  |
| S01-ACC-10 | Latency distributions. | S01 | not_started | unverified |  |

## 01_Market_Data.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S01-DEL-01 | Data inventory. | S01 | not_started | unverified |  |
| S01-DEL-02 | Coverage/gap report. | S01 | not_started | unverified |  |
| S01-DEL-03 | Schema. | S01 | not_started | unverified |  |
| S01-DEL-04 | Replay samples. | S01 | not_started | unverified |  |
| S01-DEL-05 | Feed benchmarks. | S01 | not_started | unverified |  |

## 02_Strategy_Intelligence.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S02-DEP-01 | Dependencies: 00; data contracts from 01. | S02 | not_started | unverified |  |
| S02-DEP-02 | Owned scope: strategy_intelligence/ | S02 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 02_Strategy_Intelligence.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S02-ASG-01 | Adapt all six existing engines into causal evidence producers. | S02 | not_started | unverified |  |
| S02-ASG-02 | Evidence with bullish/bearish scores. | S02 | not_started | unverified |  |
| S02-ASG-03 | Evidence with event IDs. | S02 | not_started | unverified |  |
| S02-ASG-04 | Evidence with freshness. | S02 | not_started | unverified |  |
| S02-ASG-05 | Evidence with levels. | S02 | not_started | unverified |  |
| S02-ASG-06 | Evidence with stages. | S02 | not_started | unverified |  |
| S02-ASG-07 | Evidence with invalidation. | S02 | not_started | unverified |  |
| S02-ASG-08 | Evidence with missing/contra states. | S02 | not_started | unverified | Note: Two states (missing; contra); the status is the minimum over both. |
| S02-ASG-09 | Expose Cavemen pending limit at breakout close. | S02 | not_started | unverified |  |
| S02-ASG-10 | Deduplicate shared sweep/FVG/breakout evidence. | S02 | not_started | unverified |  |
| S02-ASG-11 | Keep original engines unchanged. | S02 | not_started | unverified |  |

## 02_Strategy_Intelligence.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S02-ACC-01 | Prefix invariance. | S02 | not_started | unverified |  |
| S02-ACC-02 | Exact event deduplication. | S02 | not_started | unverified |  |
| S02-ACC-03 | Inactive vs missing evidence. | S02 | not_started | unverified |  |
| S02-ACC-04 | Baseline parity. | S02 | not_started | unverified |  |
| S02-ACC-05 | No five-minute notification delay. | S02 | not_started | unverified |  |

## 02_Strategy_Intelligence.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S02-DEL-01 | Feature dictionary. | S02 | not_started | unverified |  |
| S02-DEL-02 | Adapter tests. | S02 | not_started | unverified |  |
| S02-DEL-03 | Baseline reconciliation. | S02 | not_started | unverified |  |
| S02-DEL-04 | Annotated examples. | S02 | not_started | unverified |  |

## 03_Intermarket_Intelligence.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S03-DEP-01 | Dependencies: 00, 01. | S03 | not_started | unverified |  |
| S03-DEP-02 | Owned scope: intermarket/ | S03 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 03_Intermarket_Intelligence.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S03-ASG-01 | Synchronize XAGUSD when available. | S03 | not_started | unverified | Note: First of nine sources; the others are S03-ASG-10 .. S03-ASG-17. It is undecided whether 'when available' qualifies only news or all sources; the broader reading is used. |
| S03-ASG-02 | Estimate conditional lead/lag at seconds/minutes/hours. | S03 | not_started | unverified |  |
| S03-ASG-03 | Using actual availability timestamps. | S03 | not_started | unverified | Note: Qualifies the lead/lag estimation of S03-ASG-02. |
| S03-ASG-04 | Handle futures rolls. | S03 | not_started | unverified |  |
| S03-ASG-05 | Handle stale feeds. | S03 | not_started | unverified |  |
| S03-ASG-06 | Detect scheduled volatility. | S03 | not_started | unverified |  |
| S03-ASG-07 | Detect unscheduled volatility. | S03 | not_started | unverified |  |
| S03-ASG-08 | Prepare scenarios. | S03 | not_started | unverified |  |
| S03-ASG-09 | Respect news restrictions. | S03 | not_started | unverified |  |
| S03-ASG-10 | Synchronize DXY when available. | S03 | not_started | unverified |  |
| S03-ASG-11 | Synchronize GC when available. | S03 | not_started | unverified |  |
| S03-ASG-12 | Synchronize yields when available. | S03 | not_started | unverified |  |
| S03-ASG-13 | Synchronize real yields when available. | S03 | not_started | unverified |  |
| S03-ASG-14 | Synchronize S&P when available. | S03 | not_started | unverified |  |
| S03-ASG-15 | Synchronize VIX when available. | S03 | not_started | unverified |  |
| S03-ASG-16 | Synchronize USDJPY when available. | S03 | not_started | unverified |  |
| S03-ASG-17 | Synchronize news when available. | S03 | not_started | unverified |  |

## 03_Intermarket_Intelligence.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S03-ACC-01 | No future data leakage. | S03 | not_started | unverified |  |
| S03-ACC-02 | No revised release leakage. | S03 | not_started | unverified |  |
| S03-ACC-03 | Delayed-feed rejection. | S03 | not_started | unverified |  |
| S03-ACC-04 | Missing-source degradation. | S03 | not_started | unverified |  |
| S03-ACC-05 | Incremental out-of-period value versus gold-only. | S03 | not_started | unverified | Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. |

## 03_Intermarket_Intelligence.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S03-DEL-01 | Source/licensing inventory. | S03 | not_started | unverified |  |
| S03-DEL-02 | Timestamp audit. | S03 | not_started | unverified |  |
| S03-DEL-03 | Event studies. | S03 | not_started | unverified |  |
| S03-DEL-04 | Feature exports. | S03 | not_started | unverified |  |
| S03-DEL-05 | Limitations. | S03 | not_started | unverified |  |

## 04_Predictive_Intelligence.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S04-DEP-01 | Dependencies: 00, 01, 02; 03 optional until available. | S04 | not_started | unverified |  |
| S04-DEP-02 | Owned scope: prediction/, models/research/ | S04 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 04_Predictive_Intelligence.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S04-ASG-01 | Train regularized net-R models. | S04 | not_started | unverified |  |
| S04-ASG-02 | Train regularized payoff models. | S04 | not_started | unverified |  |
| S04-ASG-03 | Setup-specific interactions. | S04 | not_started | unverified |  |
| S04-ASG-04 | Historical similarity retrieval. | S04 | not_started | unverified |  |
| S04-ASG-05 | Scenario destinations. | S04 | not_started | unverified |  |
| S04-ASG-06 | Use prior-only neighbors. | S04 | not_started | unverified |  |
| S04-ASG-07 | Use training-only transforms. | S04 | not_started | unverified |  |
| S04-ASG-08 | Estimate uncertainty. | S04 | not_started | unverified |  |
| S04-ASG-09 | Estimate holding time. | S04 | not_started | unverified |  |
| S04-ASG-10 | Estimate expected costs. | S04 | not_started | unverified |  |
| S04-ASG-11 | Compare against GCE-B. | S04 | not_started | unverified |  |
| S04-ASG-12 | Compare against simple baselines. | S04 | not_started | unverified |  |

## 04_Predictive_Intelligence.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S04-ACC-01 | Purged chronological splits. | S04 | not_started | unverified |  |
| S04-ACC-02 | Calibration by setup and session. | S04 | not_started | unverified |  |
| S04-ACC-03 | Leakage mutation tests. | S04 | not_started | unverified |  |
| S04-ACC-04 | Cost stress. | S04 | not_started | unverified |  |
| S04-ACC-05 | Model inference latency. | S04 | not_started | unverified |  |
| S04-ACC-06 | Reproducibility. | S04 | not_started | unverified |  |

## 04_Predictive_Intelligence.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S04-DEL-01 | Model cards. | S04 | not_started | unverified |  |
| S04-DEL-02 | Frozen artifacts. | S04 | not_started | unverified |  |
| S04-DEL-03 | Predictions. | S04 | not_started | unverified |  |
| S04-DEL-04 | Splits. | S04 | not_started | unverified |  |
| S04-DEL-05 | Metrics. | S04 | not_started | unverified |  |
| S04-DEL-06 | Unsuccessful models. | S04 | not_started | unverified |  |

## 05_Decision_Engine.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S05-DEP-01 | Dependencies: 00, 02, 04; risk contract 08. | S05 | not_started | unverified |  |
| S05-DEP-02 | Owned scope: decision/ | S05 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 05_Decision_Engine.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S05-ASG-01 | Construct alternative market/limit/retest/pullback plans. | S05 | not_started | unverified | Note: Four plan types (market, limit, retest, pullback); status is the minimum over all four. |
| S05-ASG-02 | Plans with executable entry. | S05 | not_started | unverified |  |
| S05-ASG-03 | Plans with hard invalidation. | S05 | not_started | unverified |  |
| S05-ASG-04 | Plans with realistic destination. | S05 | not_started | unverified |  |
| S05-ASG-05 | Plans with expected net R. | S05 | not_started | unverified |  |
| S05-ASG-06 | Hybrid scalp/intraday. | S05 | not_started | unverified |  |
| S05-ASG-07 | Prepare both directions. | S05 | not_started | unverified |  |
| S05-ASG-08 | Actual behavior invalidates plans. | S05 | not_started | unverified |  |
| S05-ASG-09 | Rank only currently available candidates. | S05 | not_started | unverified |  |
| S05-ASG-10 | Reject stale opportunities. | S05 | not_started | unverified |  |
| S05-ASG-11 | Reject negative expected opportunities. | S05 | not_started | unverified |  |
| S05-ASG-12 | One unified decision stream. | S05 | not_started | unverified |  |
| S05-ASG-13 | Plans with uncertainty. | S05 | not_started | unverified | Note: Split from 'expected net R and uncertainty'. |

## 05_Decision_Engine.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S05-ACC-01 | No future candidate ranking. | S05 | not_started | unverified |  |
| S05-ACC-02 | Expiry tests. | S05 | not_started | unverified |  |
| S05-ACC-03 | Contradiction tests. | S05 | not_started | unverified |  |
| S05-ACC-04 | Duplicate plan handling. | S05 | not_started | unverified |  |
| S05-ACC-05 | Positive expectancy gate. | S05 | not_started | unverified |  |
| S05-ACC-06 | Session coverage. | S05 | not_started | unverified | Note: An evaluation: 'passing' means coverage was measured reproducibly and recorded. |
| S05-ACC-07 | Explainability. | S05 | not_started | unverified |  |

## 05_Decision_Engine.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S05-DEL-01 | Plan schema examples. | S05 | not_started | unverified |  |
| S05-DEL-02 | Scoring mapping. | S05 | not_started | unverified |  |
| S05-DEL-03 | Accepted/rejected cases. | S05 | not_started | unverified |  |
| S05-DEL-04 | Decision benchmarks. | S05 | not_started | unverified |  |

## 06_Reflex_Engine.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S06-DEP-01 | Dependencies: 00, 01, 07, 08. | S06 | not_started | unverified |  |
| S06-DEP-02 | Owned scope: reflex/ | S06 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 06_Reflex_Engine.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S06-ASG-01 | Implement deterministic per-tick protective decisions. | S06 | not_started | unverified |  |
| S06-ASG-02 | Executable net P&L peak tracking. | S06 | not_started | unverified |  |
| S06-ASG-03 | Monotonic stops. | S06 | not_started | unverified |  |
| S06-ASG-04 | Fast giveback protection. | S06 | not_started | unverified |  |
| S06-ASG-05 | Trade-health reactions. | S06 | not_started | unverified |  |
| S06-ASG-06 | Separate from tactical/strategic tasks. | S06 | not_started | unverified |  |
| S06-ASG-07 | Never wait for model or dashboard. | S06 | not_started | unverified |  |
| S06-ASG-08 | Test volatility-aware protection versus fixed exits. | S06 | not_started | unverified |  |

## 06_Reflex_Engine.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S06-ACC-01 | Burst replay. | S06 | not_started | unverified |  |
| S06-ACC-02 | Gap/slippage. | S06 | not_started | unverified | Note: Two scenarios (gap; slippage): each needs its own marked test; the status is the worse of the two. |
| S06-ACC-03 | Stale tick. | S06 | not_started | unverified |  |
| S06-ACC-04 | Slow-model isolation. | S06 | not_started | unverified |  |
| S06-ACC-05 | No stop widening. | S06 | not_started | unverified |  |
| S06-ACC-06 | p99/max latency. | S06 | not_started | unverified | Note: Two measures (p99; max): each needs its own marked test and both must be reported; the status is the worse of the two. |
| S06-ACC-07 | Profit giveback comparisons. | S06 | not_started | unverified | Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. |
| S06-ACC-08 | Premature-exit comparisons. | S06 | not_started | unverified | Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. |

## 06_Reflex_Engine.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S06-DEL-01 | Tick traces. | S06 | not_started | unverified |  |
| S06-DEL-02 | Timing profiles. | S06 | not_started | unverified |  |
| S06-DEL-03 | Protection tests. | S06 | not_started | unverified |  |
| S06-DEL-04 | Fixed-vs-dynamic exit results. | S06 | not_started | unverified |  |

## 07_Execution_Engine.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S07-DEP-01 | Dependencies: 00, 01; risk contract 08. | S07 | not_started | unverified |  |
| S07-DEP-02 | Owned scope: execution/ | S07 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 07_Execution_Engine.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S07-ASG-01 | Implement paper/replay order state machine. | S07 | not_started | unverified |  |
| S07-ASG-02 | Market orders. | S07 | not_started | unverified |  |
| S07-ASG-03 | Partial fills. | S07 | not_started | unverified |  |
| S07-ASG-04 | Cancel/replace. | S07 | not_started | unverified | Note: Two operations (cancel; replace); status is the minimum over both. |
| S07-ASG-05 | Multiple positions. | S07 | not_started | unverified |  |
| S07-ASG-06 | Idempotency. | S07 | not_started | unverified |  |
| S07-ASG-07 | Persistence. | S07 | not_started | unverified |  |
| S07-ASG-08 | Restart reconciliation. | S07 | not_started | unverified |  |
| S07-ASG-09 | Simulated broker-side protection. | S07 | not_started | unverified |  |
| S07-ASG-10 | Design live adapter interface. | S07 | not_started | unverified |  |
| S07-ASG-11 | Model bid/ask. | S07 | not_started | unverified |  |
| S07-ASG-12 | Model costs. | S07 | not_started | unverified |  |
| S07-ASG-13 | Model slippage. | S07 | not_started | unverified |  |
| S07-ASG-14 | Model latency. | S07 | not_started | unverified |  |
| S07-ASG-15 | Model broker constraints. | S07 | not_started | unverified |  |
| S07-ASG-16 | No enabled real routing. | S07 | not_started | unverified | Note: Split from 'Design live adapter interface but no enabled real routing'. Same rule under other ids: M-AUTH-19, G08-10, M-DIR-07, OWN-14, COM-11. |
| S07-ASG-17 | Limit orders. | S07 | not_started | unverified | Note: Split from 'market/limit/stop orders'. |
| S07-ASG-18 | Stop orders. | S07 | not_started | unverified | Note: Split from 'market/limit/stop orders'. |

## 07_Execution_Engine.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S07-ACC-01 | Duplicate messages. | S07 | not_started | unverified |  |
| S07-ACC-02 | Partial fill races. | S07 | not_started | unverified |  |
| S07-ACC-03 | Rejected orders. | S07 | not_started | unverified |  |
| S07-ACC-04 | Reconnect. | S07 | not_started | unverified |  |
| S07-ACC-05 | Crash recovery. | S07 | not_started | unverified |  |
| S07-ACC-06 | Stale plans. | S07 | not_started | unverified |  |
| S07-ACC-07 | No real orders. | S07 | not_started | unverified |  |
| S07-ACC-08 | Deterministic replay. | S07 | not_started | unverified |  |

## 07_Execution_Engine.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S07-DEL-01 | State diagrams. | S07 | not_started | unverified |  |
| S07-DEL-02 | Order traces. | S07 | not_started | unverified |  |
| S07-DEL-03 | Execution parity. | S07 | not_started | unverified |  |
| S07-DEL-04 | Failure-injection results. | S07 | not_started | unverified |  |

## 08_Risk_Intelligence.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S08-DEP-01 | Dependencies: 00; execution/data contracts. | S08 | not_started | unverified |  |
| S08-DEP-02 | Owned scope: risk/ | S08 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 08_Risk_Intelligence.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S08-ASG-01 | Implement aggregate exposure. | S08 | not_started | unverified |  |
| S08-ASG-02 | Implement margin. | S08 | not_started | unverified |  |
| S08-ASG-03 | Implement pending risk. | S08 | not_started | unverified |  |
| S08-ASG-04 | Implement gap stress. | S08 | not_started | unverified |  |
| S08-ASG-05 | Implement adaptive sizing with validated quality. | S08 | not_started | unverified |  |
| S08-ASG-06 | Implement daily state machine. | S08 | not_started | unverified |  |
| S08-ASG-07 | Implement monotonic profit floor. | S08 | not_started | unverified |  |
| S08-ASG-08 | Account-wide Prague reset. | S08 | not_started | unverified |  |
| S08-ASG-09 | Actual FTMO product rules. | S08 | not_started | unverified |  |
| S08-ASG-10 | Overnight/rollover restrictions. | S08 | not_started | unverified |  |
| S08-ASG-11 | Recovery threshold configurable. | S08 | not_started | unverified |  |
| S08-ASG-12 | No risk increase to recover losses. | S08 | not_started | unverified |  |
| S08-ASG-13 | Emergency threshold configurable. | S08 | not_started | unverified | Note: Split from 'Recovery and emergency thresholds configurable'. |

## 08_Risk_Intelligence.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S08-ACC-01 | Boundary tests at -2%. | S08 | not_started | unverified | Note: Tests read the threshold from specifications/research_parameters.toml. |
| S08-ACC-02 | Boundary tests at proposed -1.75%. | S08 | not_started | unverified | Note: The meaning of the -1.75% level is open item U-03; tests read thresholds from specifications/research_parameters.toml. |
| S08-ACC-03 | Boundary tests at -3%. | S08 | not_started | unverified | Note: The -3% level is proposed and configurable (G05-05); tests read it from specifications/research_parameters.toml. |
| S08-ACC-04 | Boundary tests at +5%. | S08 | not_started | unverified | Note: Tests read the threshold from specifications/research_parameters.toml. |
| S08-ACC-05 | Exact-floor no new risk. | S08 | not_started | unverified |  |
| S08-ACC-06 | Simultaneous reservations atomic. | S08 | not_started | unverified |  |
| S08-ACC-07 | Gap stress. | S08 | not_started | unverified |  |
| S08-ACC-08 | Other-account-position handling. | S08 | not_started | unverified |  |

## 08_Risk_Intelligence.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S08-DEL-01 | Risk formulas. | S08 | not_started | unverified |  |
| S08-DEL-02 | Rule provenance. | S08 | not_started | unverified |  |
| S08-DEL-03 | State traces. | S08 | not_started | unverified |  |
| S08-DEL-04 | Stress results. | S08 | not_started | unverified |  |
| S08-DEL-05 | Unresolved account assumptions. | S08 | not_started | unverified |  |

## 09_Learning_Laboratory.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S09-DEP-01 | Dependencies: 00, 04, 05, 10. | S09 | not_started | unverified |  |
| S09-DEP-02 | Owned scope: learning/ | S09 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 09_Learning_Laboratory.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S09-ASG-01 | Collect decision and outcome logs. | S09 | not_started | unverified | Note: Two logs (decision; outcome); status is the minimum over both. |
| S09-ASG-02 | Collect rejected/missed setups. | S09 | not_started | unverified | Note: Two kinds (rejected; missed); status is the minimum over both. |
| S09-ASG-03 | Collect latency. | S09 | not_started | unverified |  |
| S09-ASG-04 | Collect profit giveback. | S09 | not_started | unverified |  |
| S09-ASG-05 | Collect execution errors. | S09 | not_started | unverified |  |
| S09-ASG-06 | Discover new patterns beyond six engines with constrained search. | S09 | not_started | unverified |  |
| S09-ASG-07 | Separate research compute. | S09 | not_started | unverified |  |
| S09-ASG-08 | Version models. | S09 | not_started | unverified |  |
| S09-ASG-09 | Shadow compare. | S09 | not_started | unverified |  |
| S09-ASG-10 | Require owner-approved promotion. | S09 | not_started | unverified |  |
| S09-ASG-11 | Rollback. | S09 | not_started | unverified | Note: Source: 'require owner-approved promotion and rollback' - rollback is required, and 'owner-approved' may apply to it as well; the stricter reading is used. |
| S09-ASG-12 | No manual trade learning. | S09 | not_started | unverified |  |

## 09_Learning_Laboratory.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S09-ACC-01 | Active model immutable during training. | S09 | not_started | unverified |  |
| S09-ACC-02 | Resource isolation. | S09 | not_started | unverified |  |
| S09-ACC-03 | Leakage tests. | S09 | not_started | unverified |  |
| S09-ACC-04 | Promotion rejection for weak evidence. | S09 | not_started | unverified |  |
| S09-ACC-05 | Rollback. | S09 | not_started | unverified |  |
| S09-ACC-06 | Reproducibility. | S09 | not_started | unverified |  |

## 09_Learning_Laboratory.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S09-DEL-01 | Experiment registry. | S09 | not_started | unverified |  |
| S09-DEL-02 | Candidate models. | S09 | not_started | unverified |  |
| S09-DEL-03 | Shadow results. | S09 | not_started | unverified |  |
| S09-DEL-04 | Promotion criteria. | S09 | not_started | unverified |  |
| S09-DEL-05 | Resource benchmarks. | S09 | not_started | unverified |  |

## 10_Research_and_Backtesting.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S10-DEP-01 | Dependencies: 00; begin harness early; integrated modules later. | S10 | not_started | unverified | Note: 'integrated modules later': the replay harness starts early but its package cannot be final before the modules it drives are integrated. |
| S10-DEP-02 | Owned scope: backtesting/ | S10 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 10_Research_and_Backtesting.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S10-ASG-01 | Build chronological tick replay using same decision/risk/execution interfaces as paper. | S10 | not_started | unverified |  |
| S10-ASG-02 | Reconcile Round 1/2 baselines. | S10 | not_started | unverified | Note: Two baselines (Round 1; Round 2); status is the minimum over both. |
| S10-ASG-03 | Reconcile daily metrics. | S10 | not_started | unverified |  |
| S10-ASG-04 | Test hybrid. | S10 | not_started | unverified | Note: Source: 'Test hybrid, multiple positions, sizing, exits, news and intermarket ablations.' Whether 'ablations' applies to all six items is open item U-15; the broader reading (an ablation study of this item too) is not to be lost. |
| S10-ASG-05 | Test multiple positions. | S10 | not_started | unverified | Note: Source: 'Test hybrid, multiple positions, sizing, exits, news and intermarket ablations.' Whether 'ablations' applies to all six items is open item U-15; the broader reading (an ablation study of this item too) is not to be lost. |
| S10-ASG-06 | Test sizing. | S10 | not_started | unverified | Note: Source: 'Test hybrid, multiple positions, sizing, exits, news and intermarket ablations.' Whether 'ablations' applies to all six items is open item U-15; the broader reading (an ablation study of this item too) is not to be lost. |
| S10-ASG-07 | Test exits. | S10 | not_started | unverified | Note: Source: 'Test hybrid, multiple positions, sizing, exits, news and intermarket ablations.' Whether 'ablations' applies to all six items is open item U-15; the broader reading (an ablation study of this item too) is not to be lost. |
| S10-ASG-08 | Test news ablations. | S10 | not_started | unverified |  |
| S10-ASG-09 | Test intermarket ablations. | S10 | not_started | unverified |  |
| S10-ASG-10 | Report unconstrained research and halted challenge simulations separately. | S10 | not_started | unverified |  |

## 10_Research_and_Backtesting.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S10-ACC-01 | Deterministic replay. | S10 | not_started | unverified |  |
| S10-ACC-02 | No lookahead. | S10 | not_started | unverified |  |
| S10-ACC-03 | Tick ordering. | S10 | not_started | unverified |  |
| S10-ACC-04 | Costs. | S10 | not_started | unverified |  |
| S10-ACC-05 | Overlapping positions. | S10 | not_started | unverified |  |
| S10-ACC-06 | Daily denominator checks. | S10 | not_started | unverified |  |
| S10-ACC-07 | Purged walk-forward. | S10 | not_started | unverified |  |
| S10-ACC-08 | Block bootstrap. | S10 | not_started | unverified |  |
| S10-ACC-09 | Multiplicity. | S10 | not_started | unverified |  |

## 10_Research_and_Backtesting.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S10-DEL-01 | PDF with all metrics G09/G10. | S10 | not_started | unverified |  |
| S10-DEL-02 | Trade/candidate CSVs. | S10 | not_started | unverified |  |
| S10-DEL-03 | Configs. | S10 | not_started | unverified |  |
| S10-DEL-04 | Seeds. | S10 | not_started | unverified |  |
| S10-DEL-05 | Equity curves. | S10 | not_started | unverified |  |
| S10-DEL-06 | Frequency-expectancy frontier. | S10 | not_started | unverified |  |

## 11_Dashboard.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S11-DEP-01 | Dependencies: 00; read-only contracts from all modules. | S11 | not_started | unverified |  |
| S11-DEP-02 | Owned scope: dashboard/gie/ | S11 | not_started | unverified | Note: Actual repository paths: decision D-002 and path_mapping in specifications/dependency_graph.yaml. |

## 11_Dashboard.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S11-ASG-01 | Show live chart. | S11 | not_started | unverified |  |
| S11-ASG-02 | Show BUY/SELL quality. | S11 | not_started | unverified |  |
| S11-ASG-03 | Show expected R. | S11 | not_started | unverified |  |
| S11-ASG-04 | Show uncertainty. | S11 | not_started | unverified |  |
| S11-ASG-05 | Show evidence contributions. | S11 | not_started | unverified |  |
| S11-ASG-06 | Show prepared plans. | S11 | not_started | unverified |  |
| S11-ASG-07 | Show open positions. | S11 | not_started | unverified |  |
| S11-ASG-08 | Show risk states. | S11 | not_started | unverified |  |
| S11-ASG-09 | Show profit floor. | S11 | not_started | unverified |  |
| S11-ASG-10 | Show data freshness. | S11 | not_started | unverified |  |
| S11-ASG-11 | Show latency. | S11 | not_started | unverified |  |
| S11-ASG-12 | Show model versions. | S11 | not_started | unverified |  |
| S11-ASG-13 | Show learning. | S11 | not_started | unverified | Note: Source: 'model versions, learning and paper results' - may also be read as 'learning results'. |
| S11-ASG-14 | Show paper results. | S11 | not_started | unverified |  |
| S11-ASG-15 | Clearly label simulated values. | S11 | not_started | unverified |  |
| S11-ASG-16 | No direct order route. | S11 | not_started | unverified |  |
| S11-ASG-17 | Clearly label unavailable values. | S11 | not_started | unverified | Note: Split from 'simulated, unavailable and unvalidated values'. |
| S11-ASG-18 | Clearly label unvalidated values. | S11 | not_started | unverified | Note: Split from 'simulated, unavailable and unvalidated values'. |

## 11_Dashboard.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S11-ACC-01 | Replay UI with real event payloads. | S11 | not_started | unverified |  |
| S11-ACC-02 | Stale/disconnect state. | S11 | not_started | unverified | Note: Two states (stale; disconnect): each needs its own marked test; the status is the worse of the two. |
| S11-ACC-03 | No hardcoded metrics. | S11 | not_started | unverified |  |
| S11-ACC-04 | No blocking of reflex. | S11 | not_started | unverified |  |
| S11-ACC-05 | Existing tabs unaffected. | S11 | not_started | unverified |  |

## 11_Dashboard.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S11-DEL-01 | Screenshots. | S11 | not_started | unverified |  |
| S11-DEL-02 | UI tests. | S11 | not_started | unverified |  |
| S11-DEL-03 | Event mappings. | S11 | not_started | unverified |  |
| S11-DEL-04 | Usability notes. | S11 | not_started | unverified |  |

## 12_Quality_Assurance.pdf - Launch and dependencies

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S12-DEP-01 | Dependencies: 00; ongoing through all waves. | S12 | not_started | unverified | Note: 'ongoing through all waves': QA is active in every wave, not only in wave 4. |
| S12-DEP-02 | Owned scope: tests/gie/, reviews/qa/ | S12 | not_started | unverified | Note: The repository layout proposes tests/gie/qa/ for session 12 and tests/gie/<subsystem>/ for the implementing sessions: decision D-002, change request CR-002 (open). |

## 12_Quality_Assurance.pdf - Your assignment

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S12-ASG-01 | Independently verify requirements. | S12 | not_started | unverified |  |
| S12-ASG-02 | Inspect code and tests. | S12 | not_started | unverified | Note: Two objects (code; tests); the status is the minimum over both. |
| S12-ASG-03 | Run mutation/failure injection. | S12 | not_started | unverified | Note: Two techniques (mutation; failure injection); the status is the minimum over both. |
| S12-ASG-04 | Stress concurrency and latency. | S12 | not_started | unverified | Note: Two properties (concurrency; latency); the status is the minimum over both. |
| S12-ASG-05 | Check data leakage. | S12 | not_started | unverified |  |
| S12-ASG-06 | Check live lock. | S12 | not_started | unverified |  |
| S12-ASG-07 | Verify reports against raw exports and commits. | S12 | not_started | unverified | Note: Two references (raw exports; commits); the status is the minimum over both. |
| S12-ASG-08 | Do not approve own implementation. | S12 | not_started | unverified |  |
| S12-ASG-09 | Do not silently fix other branches. | S12 | not_started | unverified |  |

## 12_Quality_Assurance.pdf - Acceptance tests

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S12-ACC-01 | Full integration replay. | S12 | not_started | unverified |  |
| S12-ACC-02 | Restart/disconnect. | S12 | not_started | unverified | Note: Two scenarios (restart; disconnect): each needs its own marked test; the status is the worse of the two. |
| S12-ACC-03 | Stale feed. | S12 | not_started | unverified |  |
| S12-ACC-04 | Burst load. | S12 | not_started | unverified |  |
| S12-ACC-05 | Risk races. | S12 | not_started | unverified |  |
| S12-ACC-06 | Overnight reset. | S12 | not_started | unverified |  |
| S12-ACC-07 | Regression. | S12 | not_started | unverified |  |
| S12-ACC-08 | Paper-only end-to-end tests. | S12 | not_started | unverified |  |

## 12_Quality_Assurance.pdf - Review deliverables

| id | requirement | sessions | status | review | contributions/notes |
|---|---|---|---|---|---|
| S12-DEL-01 | Requirement-by-requirement verdict. | S12 | not_started | unverified |  |
| S12-DEL-02 | Reproducible commands. | S12 | not_started | unverified |  |
| S12-DEL-03 | Failures. | S12 | not_started | unverified |  |
| S12-DEL-04 | Evidence paths. | S12 | not_started | unverified |  |
| S12-DEL-05 | Release blockers. | S12 | not_started | unverified |  |
