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

## CR-003 - Additional shared contracts are needed before wave 2 can finish

- Status: OPEN
- Raised: 2026-11-27 (Session 00)
- Requirements: COM-06, S01-ASG-04, S01-ASG-05, S07-ASG-15, S08-ASG-02, S02-DEP-01, S08-DEP-01

The brief for Session 00 lists nine contracts. Other briefs refer to further shared data: "data
contracts from 01" (session 02), "execution/data contracts" (session 08), candle construction and
the broker calendar (session 01), broker constraints and margin (sessions 07, 08), account-wide
state including other positions (session 08). Proposal: sessions 01, 07 and 08 each propose their
additional shared types (`Candle`, `BrokerCalendar`/session data, `InstrumentSpec`,
`AccountSnapshot`) as their first deliverable; the coordinator adds them to `gie/contracts/` with
the same framework, schema lock and tests before the dependent work starts. They were not defined
in Session 00 because their content depends on the audit (B-001) and on broker facts (U-04, U-10),
and inventing them now would mean inventing data.

## CR-004 - Interpretations built into the contracts need confirmation

- Status: OPEN
- Raised: 2026-11-27 (Session 00)
- Requirements: G04-10, G04-11, G05-03, M-AUTH-13

See D-011 (paper excludes demo accounts), D-012 (position volume never increases; stops never
removed) and U-03 (meaning of the -1.75 % recovery level). They are stricter readings of the
specification, chosen so that nothing unsafe is representable; the owner may relax them.
