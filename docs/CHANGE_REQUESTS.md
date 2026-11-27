# GIE blockers and change requests

Persistent record of everything that prevents a requirement from being completed (**B-nnn**) and
of every proposed deviation from, or addition to, the specification (**CR-nnn**).
Requirements M-AUTH-05, M-GATE-07, OWN-10, COM-10. Entries are never deleted.

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
   network, emits no file contents and no credentials, and produces `inventory.json`,
   `manifest.jsonl` (SHA-256 of every file - the proof that nothing changed, and the basis for
   freezing the baselines) and optionally `processes.json`. Instructions: `tools/audit/README.md`.
   The owner reviews the output and uploads the folder to this session.
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

## CR-003 - Additional shared contracts are needed before later sessions can start

- Status: OPEN
- Raised: 2026-11-27 (Session 00); extended 2026-11-27 after review round 1
- Requirements: COM-06, S01-ASG-04, S01-ASG-05, S01-ASG-12, S07-ASG-15, S08-ASG-01, S08-ASG-02, S08-ACC-08, S02-DEP-01, S08-DEP-01, S09-DEP-01, S11-DEP-01

The brief for Session 00 lists nine contracts. Other briefs refer to further shared data ("data
contracts from 01", "execution/data contracts", "read-only contracts from all modules"), and the
independent review of the Session 00 contracts (136 findings, `reviews/session_00/`) confirmed for
each consuming session which shared types are still missing. They were **not** defined in
Session 00: their content depends on the audit of the existing system (B-001) and on broker and
account facts (U-04, U-09, U-10); defining them now would mean inventing data.

Proposal: each type is proposed by the named session as its **first deliverable**, integrated into
`gie/contracts/` by the coordinator with the Session 00 framework (field declarations, registry,
schema lock, golden messages, independent tests), and must exist before the listed sessions start.
The machine-readable form of this table is `contract_extensions` in
`specifications/dependency_graph.yaml`; the start gate there refers to it.

| id | shared type | proposed by | needed before the start of | why it cannot be defined now |
|---|---|---|---|---|
| CX-01 | `Candle` (completed bar with its construction rule) | 01 | 02, 03, 04 | candle construction and timeframes of the six engines are unknown (B-001) |
| CX-02 | Broker calendar and session classifier | 01 | 02, 06, 08 | broker trading hours, rollover and the Prague reset are account facts (U-04) |
| CX-04 | `InstrumentSpec` (contract size, digits, volume step, stop distance, margin, swap) | 07 | 06, 08 | broker data (U-10) |
| CX-05 | `AccountSnapshot` (equity, margin, account-wide positions including other systems) | 08 | 05, 06 | account currency, reference equity and the other systems on the account are unknown (U-04) |
| CX-06 | Single-valued observations with release vintage; news and calendar events | 01 and 03 | 03 | licensed feeds are unknown (U-09) |
| CX-07 | Market condition / trading restriction record | 03 | 05 | news and rollover restrictions are account rules (U-04) |
| CX-08 | `Decision` (the unified decision stream of session 05) | 05 | 09, 11 | its content is the design task of session 05 |
| CX-09 | Outcome / label record | 04 and 09 | 09 | label definitions are a research deliverable of session 04 |

(CX-03 is CR-005.) Until a type exists, a session that needs it works on explicitly labelled test
fixtures and may not claim integration complete (COM-07, COM-08). Open item U-11 asks the owner to
confirm this procedure.

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
