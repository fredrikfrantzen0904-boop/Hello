# GIE blockers and change requests

Persistent record of everything that prevents a requirement from being completed (**B-nnn**) and
of every proposed deviation from, or addition to, the specification (**CR-nnn**).
Requirements M-AUTH-05, M-GATE-07, M-GATE-11, OWN-10, COM-10, COM-12. Entries are never deleted.

Status values: `OPEN`, `RESOLVED`, `APPROVED`, `REJECTED`.
The heading and `- Status:` format is parsed by `tools/gie_registers.py`.

---

# Blockers

## B-001 - The existing system `D:\ftmo_eurusd` is not reachable from this session

- Status: OPEN
- Raised: 2026-11-27 (Session 00)
- Requirements: S00-ASG-01, S00-ASG-02, S00-ASG-03, S00-ASG-04, S00-ASG-05, S00-ASG-06, S00-ASG-07, S00-ACC-05, S00-ACC-06, S00-DEL-04, G02-08, G02-09

**Fact.** Session 00 must "Audit existing D:\ftmo_eurusd without modifying production. Inventory
languages, processes, data, tests, hardware and broker interfaces." This session runs in an
isolated cloud container. The only repository attached is `fredrikfrantzen0904-boop/Hello`, which
contained a single file (`Start`, 203 bytes of git bootstrap notes) before this work. No computer of
the owner is connected (`list_computer_folders` returned no folders), so the production tree, the
six engines, the Gold Bot / AI trader / breakout bot, the GCE-B and Round 1/2 baselines, the
historical data and the dashboard cannot be read.

**Consequences (reported, not worked around).**

| requirement | state |
|---|---|
| S00-ASG-01 .. 07 audit and inventory of languages, processes, data, tests, hardware, broker interfaces | not performed; tool delivered |
| S00-DEL-04 baseline audit | not delivered; `docs/BASELINE_AUDIT.md` states exactly what is and is not known |
| S00-ACC-05 baseline regression | harness built and self-tested on a labelled synthetic fixture; the real test reports BLOCKED |
| S00-ACC-06 no production changes | proven for this repository (protected path check) and for the audit tool on a fixture; cannot be demonstrated on the production tree itself |
| D-003, D-014 | provisional until the audit |

Nothing about the existing system has been assumed or invented in its place.

**Proposed solutions (owner to choose - open item U-01).**

1. *Owner runs the read-only audit tool* (recommended). `tools/audit/gie_baseline_audit.py` is a
   single standard-library Python file. It only reads, never writes inside the audited tree, uses no
   network and emits no credentials. It does not copy file contents, with three narrow exceptions
   that the inventory needs: the names and version specifiers of packages in dependency files, the
   column names of CSV / TSV files (only when the first line looks like a header) and Python
   version strings. It produces `inventory.json`, `manifest.jsonl` (size, modification time and
   SHA-256 per file - the proof that nothing changed, and the basis for freezing the baselines)
   and optionally `processes.json`. Not hashed, and listed as such: files with secret-like names,
   files in excluded directories (version control, virtual environments, caches), files above the
   size cap and files that cannot be read; a baseline is only frozen from files that were hashed.
   Instructions: `tools/audit/README.md`. The owner reviews the output and uploads the folder to
   this session.
2. *Connect the machine through Claude Remote Control* so that this session can read the folder
   directly. Faster, but commands would then run on the production machine; only acceptable with
   read-only discipline and the owner's explicit approval.
3. *Provide a copy* of the code (without credentials and without the terminal data folder), for
   example as a private GitHub repository attached to this session.

**What happens after access exists.** Complete the inventory, write the baseline audit, freeze the
baselines (`tools/baseline_freeze.py freeze`), re-run the regression test, revisit D-003 and D-014,
and submit an addendum to the Session 00 review package.

---

# Change requests

## CR-001 - Single coordinating session instead of one Claude Code session per brief

- Status: APPROVED
- Raised: 2026-11-27 (Session 00)
- Requirements: COM-01, OWN-01, OWN-06

The briefs ask for a dedicated Claude Code session per work package; the owner's launch directive
asks for one session that coordinates everything, with sub-agents. Recorded as decision D-001
(approved by the owner's directive). Worktrees are still used for parallel work.

## CR-002 - Repository layout differs from the proposed paths

- Status: OPEN
- Raised: 2026-11-27 (Session 00)
- Requirements: COM-03, S00-DEP-02, S12-DEP-02

Proposed in D-002: all code under the `gie/` package; tests under `tests/gie/<subsystem>/` with
session 12 owning `tests/gie/qa/` (the brief gives session 12 all of `tests/gie/`, which would make
it the owner of every other session's unit tests). Needs owner approval.

## CR-003 - Additional shared contracts are needed by later sessions

- Status: OPEN
- Raised: 2026-11-27 (Session 00); extended 2026-11-27 after review round 1
- Requirements: COM-06, S01-ASG-04, S01-ASG-05, S01-ASG-12, S07-ASG-15, S08-ASG-01, S08-ASG-02, S08-ACC-08, S02-DEP-01, S08-DEP-01, S09-DEP-01, S11-DEP-01

The brief for Session 00 lists nine contracts. Other briefs refer to further shared data ("data
contracts from 01", "execution/data contracts", "read-only contracts from all modules"), and the
independent review of the Session 00 contracts (136 findings, `reviews/session_00/`) confirmed for
each consuming session which shared types are still missing. They were **not** defined in
Session 00: their content depends on the audit of the existing system (B-001) and on broker and
account facts (U-04, U-09, U-10); defining them now would mean inventing data.

Proposal (the rule is `gates.extension_gate` in `specifications/dependency_graph.yaml`; the
machine-readable form of the table is `contract_extensions` there): each type is the **first
deliverable** of the session that owns the knowledge. The coordinator integrates it into
`gie/contracts/` with the Session 00 framework (field declarations, registry, schema lock, golden
messages, independent tests); it stays PROPOSED until the owner approves it at that session's
review gate. A consuming session starts with its wave but writes no code against the type before it
is integrated, never defines a local stand-in (COM-06), and cannot be integrated itself before the
types it consumes are.

| id | shared type | proposed by | consumed by | why it cannot be defined now |
|---|---|---|---|---|
| CX-01 | `Candle` (completed bar with its construction rule) | 01 | 02, 03, 04, 10 | candle construction and timeframes of the six engines are unknown (B-001) |
| CX-02 | Broker calendar and session classifier | 01 | 02, 04, 05, 06, 08, 10 | broker trading hours, rollover and the Prague reset are account facts (U-04) |
| CX-04 | `InstrumentSpec` (contract size, digits, volume step, minimum stop distance, margin, swap, cost schedule) and the shared valuation function | 07 | 04, 05, 06, 08, 10 | broker data (U-10) |
| CX-05 | `AccountSnapshot` (equity, margin, account-wide positions including other systems) and the daily risk state as a record | 08 | 05, 06, 07, 10, 11, 12 | account currency, reference equity and the other systems on the account are unknown (U-04) |
| CX-06 | Single-valued observations with release vintage; news and calendar events; futures data (last, volume, contract month, expiry and roll) | 01 | 03 | licensed feeds are unknown (U-09) |
| CX-07 | Market condition / trading restriction record | 03 | 05, 08 | news and rollover restrictions are account rules (U-04) |
| CX-08 | `Decision` (the unified decision stream of session 05) | 05 | 09, 10, 11 | its content is the design task of session 05 |
| CX-09 | Outcome / label record | 04 | 09, 10 | label definitions are a research deliverable of session 04 |
| CX-10 | Step driver: the one run loop of replay and paper, with a replay / paper conformance test | 00 | 01, 06, 07, 10 | needs the first tick source and adapter to be proven against; required before any subsystem is integrated (D-024) |
| CX-11 | Health / error event and heartbeat rule | 00 | 01, 06, 07, 11, 12 | defined together with the step driver |
| CX-12 | Run configuration document, per-stream data digests, run catalogue, experiment / trial / holdout bookkeeping, split specification | 10 | 04, 09, 11, 12 | research procedure is the design task of session 10 (D-027) |
| CX-13 | Feed status record | 01 | 06, 07, 08, 11 | depends on the real feed (U-09) |
| CX-14 | Latency summary record | 01 | 06, 07, 11, 12 | measured quantities are defined with the feed benchmark of session 01 |
| CX-15 | Run result records (trades, daily metrics, comparison with baselines) | 10 | 09, 11 | metric definitions depend on the baseline reconciliation (U-05, U-07) |
| CX-16 | Learning result records (experiments, candidate models, promotion requests) | 09 | 11 | design task of session 09 |
| CX-17 | Display projection of records for browsers | 11 | 12 | design task of session 11 |
| CX-18 | Reservation life-cycle record | 08 | 07 | reservation rules are the design task of session 08 |
| CX-19 | Adapter queries for reconciliation, journal checkpoints, ledger eviction, storage of raw ticks | 07 | 01, 10 | depends on the persistence design of sessions 07 and 01 |
| CX-20 | Feature dictionary and per-kind identity table of evidence (identity levels, anchor) | 02 | 03, 04, 09 | the six engines could not be inspected (B-001); no engine emits evidence before it is integrated |

(CX-03 is CR-005.) Open item U-11 asks the owner to confirm this procedure.

What each proposal must settle is recorded with it in `specifications/dependency_graph.yaml`
(`must_define`), so that the findings of the review are not lost: for example the candle proposal
fixes open and close instants, the availability instant, the price series, the rank relative to
ticks and order events and the rule for a quiet interval; the decision record holds one ranking
pass with candidate-level rejections; the outcome record separates realised from counterfactual
outcomes and states when a label becomes available.

## CR-004 - Interpretations built into the contracts need confirmation

- Status: OPEN
- Raised: 2026-11-27 (Session 00)
- Requirements: G04-10, G04-11, G05-03, M-AUTH-13

See D-011 (paper excludes demo accounts), D-012 (position volume never increases; stops never
removed) and U-03 (meaning of the -1.75 % recovery level). They are stricter readings of the
specification, chosen so that nothing unsafe is representable; the owner may relax them.

## CR-005 - One shared policy for what may be done with a flagged tick

- Status: OPEN
- Raised: 2026-11-27 (Session 00, review round 1, finding DI-14)
- Requirements: S01-ASG-06, S01-ASG-08, G03-12, G08-06, S06-ACC-03

The `Tick` contract carries quality flags (crossed quote, out of order, duplicate, gap before,
delayed, outlier, off session, modelled receive time). What a consumer may do with a flagged tick
differs by purpose - for example a delayed quote may still tighten a stop but must not open a
position - and must be the same everywhere. Session 00 defines the flags and the rule that order
streams tolerate no gap while tick consumers must tolerate gaps, but not the per-flag, per-purpose
table: its thresholds are owned by session 01 and depend on the real feed.
Proposal (CX-03 in the dependency graph): session 01 proposes the table as its first deliverable;
the coordinator adds it to `gie.contracts` as one function used by sessions 02, 06, 07 and 08;
no consumer implements its own reading of the flags.

## CR-006 - Additions to the shared contracts beyond the nine named by the brief

- Status: OPEN
- Raised: 2026-11-27 (Session 00, review round 1)
- Requirements: S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-ASG-25, COM-06

The brief says: "Establish versioned contracts for Tick, Evidence, Candidate, TradePlan,
RiskDecision, OrderEvent, Position, ModelVersion and Clock." Session 00 delivers these nine and, in
addition, the following shared elements that the independent review found necessary for the nine
to be usable and enforceable. They are additions, not substitutions; each is a decision awaiting
approval and can be rejected individually.

| addition | where | decision |
|---|---|---|
| `RunManifest`, a tenth contract describing a run | `gie/contracts/run.py` | D-019 |
| Reference journal (order of a run, look-ahead guard, torn-write detection) | `gie/contracts/journal.py` | D-020 |
| Lifecycle ledger and authorisation checks | `gie/contracts/ledger.py`, `gie/contracts/authorization.py` | D-021 |
| Order gateway in front of every execution adapter | `gie/contracts/guarded.py` | D-021 |
| Derivation check (causality, expiry and data-origin propagation) | `gie/contracts/causality.py` | D-010, D-020 |
| Scheduler interface and polling scheduler | `gie/core/clock.py` | D-022 |

## CR-007 - Evidence fields of a risk decision proposed by session 08

- Status: OPEN
- Raised: 2026-11-27 (Session 00, review round 2, finding L2E-19)
- Requirements: S08-ASG-01, S08-ASG-05, G04-02, G04-03, G04-08

The `RiskDecision` contract states the outcome, the granted volume and risk, the aggregates the
approval rests on and the daily state. The review from the viewpoint of session 08 found that four
values a reviewer needs to reproduce a sizing decision have no field yet: the aggregate exposure
(gross and net), the correlated risk, the base size before adaptation and the quality value that
was used for adaptive sizing. Their definitions are the design task of session 08 and depend on the
instrument and account facts (U-04, U-10), so they were not invented here.
Proposal: session 08 proposes them with its first review package as new **optional** fields of
`RiskDecision` (a MINOR change under `specifications/SHARED_CONTRACTS.md` section 8); the
coordinator integrates them. Until then session 08 reads the candidate of a plan through the run
ledger and reports these values in its own review evidence.

## CR-008 - Attribution of an estimate is recorded but not yet checked

- Status: OPEN
- Raised: 2026-11-27 (Session 00, verification of review round 1, items PD-23, PD-09, DI-25)
- Requirements: S05-ACC-07, S11-ASG-05, S02-DEL-01, S04-ASG-08

Three points of the prediction and learning review are recorded here rather than built, because
their content belongs to session 04:
1. A candidate or plan may carry `baseline_r` and per-feature `contributions`; no rule relates them
   to `expected_net_r` yet (an additive attribution with a stated tolerance, or another method).
   Session 04 proposes the rule together with its first model.
2. The feature dictionary (a deliverable of session 02, used by session 04) that
   `ModelVersion.feature_schema_sha256` refers to has no agreed location and canonical format. It
   is extension CX-20, the first deliverable of session 02, together with the per-kind identity
   table of evidence; the hash is only then meaningful, and until then the contract cannot detect
   two engines that disagree about which levels identify an event.
3. A scoring pass in a paper run reads the model registry once: the models of a run are pinned in
   its manifest (D-026), which gives replay and paper the same consistent view; a registry
   snapshot API beyond that is added only if session 04 or 05 shows the need.
