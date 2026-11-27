# GIE decision log

Persistent record of architectural and procedural decisions (requirements S00-ASG-27, OWN-08).

* **D-nnn** are decisions. Status: `PROPOSED` (made by the coordinator, awaiting owner approval),
  `APPROVED`, `REJECTED`, `SUPERSEDED`. Nothing marked PROPOSED is binding until the owner approves
  it at a review gate. Entries are never deleted; a changed decision gets a new entry that
  supersedes the old one.
* **U-nn** are open items: questions only the owner (or the supervisor) can answer.
* Blockers (B-nnn) and change requests (CR-nnn) are in `docs/CHANGE_REQUESTS.md`.

The format of the headings and of the `- Status:` lines is parsed by `tools/gie_registers.py`.

---

## D-001 - One coordinating session with sub-agents instead of thirteen separate sessions

- Status: APPROVED
- Date: 2026-11-27
- Requirements: OWN-01, OWN-06, COM-01, M-AUTH-09, M-DIR-06
- Decided by: owner (launch directive of 2026-11-27)

Every brief says "Open a dedicated Claude Code session and worktree". The owner's launch directive
replaces this: "You will coordinate and implement the entire project within this session" and "You
may use specialized subagents and parallel development where appropriate, provided all components
follow the approved shared architecture". Each session brief is therefore executed as a work
package of this one session; parallel packages run as sub-agents in separate git worktrees
(`docs/WORKTREES.md`); the coordinator alone integrates. The per-session review gate is unchanged.

## D-002 - Repository layout: everything importable lives in the `gie/` package

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: COM-03, S00-ASG-08, S00-DEP-02, S12-DEP-02

The briefs propose top-level directories (`core/`, `contracts/`, `risk/`, `execution/`, ...) and
state that "paths are proposed until coordinator approves actual repository layout". Decision:
all Python code lives in one package `gie/` (`gie/core`, `gie/contracts`, `gie/market_data`, ...).
Reasons: generic top-level names such as `core`, `risk` or `execution` collide with other packages
as soon as GIE is installed next to the existing trading systems; one namespace makes the import
rules checkable; it can be dropped into another repository as a single directory.
Tests live in `tests/gie/<subsystem>/` (owned by the implementing session) and `tests/gie/qa/`
(owned by session 12); review packages in `reviews/session_NN/`. The full mapping from brief paths
to repository paths is in `specifications/dependency_graph.yaml` (`path_mapping`).
Alternative considered: top-level directories exactly as in the briefs - rejected for the collision
risk; can be restored by a pure rename if the owner prefers.

## D-003 - Implementation language: Python 3.11+

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-02, G03-07, G03-08, G03-09

Python >= 3.11, type-checked with `mypy --strict`. This is **provisional**: the languages of the
existing system could not be inventoried (blocker B-001). Python is assumed because the FTMO /
MetaTrader ecosystem and the described engines are typically Python driven. Measured cost of the
foundation on the development container is reported in the review package; whether Python meets
the proposed latency targets on the owner's hardware must be measured there by sessions 01/06
with real ticks (G09-10). If the audit shows a different primary language, the contracts remain
valid because their normative form is language neutral (JSON Schema + canonical JSON).

## D-004 - Contracts are frozen standard-library dataclasses declared once

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-DEL-02

`gie.core` and `gie.contracts` depend on the Python standard library only. Each contract is a
frozen, slotted dataclass whose fields are declared once with a field specification
(`gie/contracts/_spec.py`); validation on construction, canonical encoding, strict decoding and the
published JSON Schema are all derived from that single declaration, and cross-field invariants are
declared next to the fields. Every record that exists in memory is valid.
Alternative considered: pydantic v2 (less code, faster validation in Rust) - not chosen so that the
foundation has zero third-party dependencies in the tick path, full control of the canonical form,
and no schema drift when a library version changes. Can be revisited by change request if measured
validation cost becomes the bottleneck.

## D-005 - Numeric representation

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-20, S00-ASG-21, G05-08

Instants are 64-bit integer nanoseconds since the Unix epoch (UTC). Prices, volumes, money and
every fraction that is compared against a risk limit are exact decimals (strings on the wire).
Statistical quantities are IEEE-754 doubles; NaN/Infinity are invalid. Unknown is `null`, never 0.
Reason: boundary rules such as "at exactly the floor no new risk" and the -2 % / -3 % / +5 %
thresholds must be decided exactly and reproducibly; binary floats cannot guarantee that.
Consequence: JSON readers must parse integers exactly (JavaScript needs BigInt-aware parsing).

## D-006 - Subsystems never import each other

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-10, COM-06, M-DIR-06, M-AUTH-10

A subsystem package may import only `gie.core`, `gie.contracts` and itself. Subsystems exchange
contract records and call each other only through the `Protocol` interfaces in
`gie/contracts/interfaces.py`. Implementations are wired together in a composition root `gie.app`
that only the coordinator writes at integration time. Enforced in CI by
`tools/check_architecture.py`. Reason: this is what allows waves 2-4 to be built in parallel without
overwriting or depending on each other's unfinished code, and what lets replay (session 10) drive
exactly the same components as paper mode.

## D-007 - Contract set 1.0.0 and its change procedure

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: M-DIR-02, S00-ACC-02, COM-06

The nine contracts are proposed at version 1.0.0 (`specifications/SHARED_CONTRACTS.md`,
`specifications/contracts/FIELDS.md`, `gie/contracts/schemas/`). The schema lock
(`gie/contracts/schemas/LOCK.json`) has status `PROPOSED`: until the owner approves, 1.0.0 may be
corrected in place. On approval the status is set to `APPROVED`; from then on every change needs a
semantic-version bump according to section 8 of the contract specification, a decision-log entry
and coordinator integration; released schema files are immutable and CI enforces this.

## D-008 - Canonical JSON and deterministic identifiers

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-22, S00-ACC-01, G08-07, G02-02

The canonical serialisation is RFC 8785 (JCS) with exact integers. Identifiers are
`<prefix>_<128-bit hex>` derived by SHA-256 from documented identity parts
(`gie/contracts/identity.py`); random or clock-derived identifiers are forbidden. Records verify
their own identifier where all parts are fields. Market events de-duplicate by `market_event_id`;
orders are idempotent by `client_order_id`.

## D-009 - Total order by availability time

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-25, S10-ACC-02, S10-ACC-03

Records are processed in ascending `(availability time, stream rank, stream, seq)`. Availability
is receive time for market data and broker events and decision time for GIE's own records; source
event time is never used for ordering. Ticks keep their arrival position; late quotes are flagged,
not reordered.

## D-010 - Freshness fails closed at the boundary

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-23, G03-12, G06-04

A record is usable iff `now < valid_until`; data is fresh iff `0 <= age < max_age`; data from the
future is an error, not fresh. All limits are research parameters.

## D-011 - Paper-only routing lock; "paper" means in-process simulation

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G08-03, G08-10, M-AUTH-13, M-AUTH-14, OWN-14, COM-11, M-DIR-07

Four independent layers (`gie/core/routing_lock.py`): no live member in the vocabulary used by the
contracts; a literal `False` constant that is read from nowhere; a guard that admits only
registered simulated adapter types; a static scan in CI (`tools/check_live_lock.py`).
Interpretation that needs the owner's confirmation (open item U-06): "paper" is simulation inside
the GIE process against observed prices. Sending orders to a broker **demo** account is treated as
real routing and is locked as well. Unlocking is impossible by configuration; it would need a code
change to the lock, a new MAJOR version of `OrderEvent` and a recorded owner decision.

## D-012 - Master-specification safety rules are structural contract invariants

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G04-09, G04-10, G04-11, G04-12, G04-13, G05-04, G05-08, G07-09, G07-10

Rules that the master specification states without exception are built into the contracts so that
violating records cannot exist (list in section 9 of the contract specification). Two of them
involve an interpretation (open item U-12): "no averaging down" is enforced as "the volume of a
position never increases" - adding exposure always needs a new plan, a new risk approval and
becomes a new position; and "no widening protective stops" is enforced as "the stop never moves
away from the entry and is never removed".

## D-013 - Trading day is the Europe/Prague calendar date

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G05-10, S08-ASG-08

`trading_day` is the calendar date in `Europe/Prague`; days run from Prague midnight to Prague
midnight (23/25 h on DST changes). Whether the real account resets at exactly this instant is an
account rule to be verified by session 08 (open item U-04).

## D-014 - Provisional vocabularies pending the audit

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S02-ASG-06, S02-ASG-08, S01-ASG-06

Because the six engines could not be inspected, engine-specific vocabularies are open strings
(evidence kinds, level roles, setup types, reason codes) or small provisional enums
(`EvidenceStage`, `MarketSession`, `TickFlag`, `ModelKind`); see section 11 of the contract
specification. Session 02's feature dictionary will fix the vocabulary; refinements follow the
version rules.

## D-015 - Requirement traceability and evidence are generated, never typed

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: OWN-08, M-AUTH-16, M-AUTH-17, M-GATE-01, M-GATE-03

Tests carry `requirement` / `acceptance` markers; `pytest --gie-evidence` writes a JSON file with
every test outcome; `tools/gie_registers.py` derives the acceptance register status from that file,
validates the requirements matrix (no requirement can disappear, no status `verified` without an
existing evidence file, no `blocked` without a blocker id) and renders the human-readable
registers. `tools/ci.py` runs every check and writes one machine-readable result file. The review
PDF is generated from those files.

## D-016 - Branching

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: M-AUTH-09, M-AUTH-10, M-AUTH-11, M-DIR-06

All work is pushed only to `claude/gold-intelligence-engine-l9uw10`; `main` is never touched and
no pull request is opened unless the owner asks. From wave 2 on, each session works in its own
local branch and worktree (`gie/sNN-<name>`); the coordinator merges a session branch into the
development branch only after the owner has approved that session's review package.

## D-017 - Baselines are frozen by hash; quoted results are unverified claims

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G02-08, G02-09, G09-01, G09-02, G09-07, S00-ACC-05

The existing baselines (GCE-B, Round 1, Round 2, Cavemen trade sets) will be frozen as SHA-256
manifests (`tools/baseline_freeze.py`, `specifications/baselines/`) the first time the production
tree is accessible, and verified from then on. The figures quoted in G09 are recorded as claims to
be reconciled by sessions 02 and 10; nothing in this repository treats them as verified.

## D-018 - Research parameters live in one labelled file

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: M-DIR-08, G05-05, G01-11

All numerical settings named by the specification are in
`specifications/research_parameters.toml` with unit and requirement id, labelled
`PROPOSED_RESEARCH_PARAMETERS`, loaded through `gie.core.params`. The loader refuses any other
label in this project phase.

---

# Open items for the owner

## U-01 - Access to the existing system `D:\ftmo_eurusd`

- Status: OPEN
- Requirements: S00-ASG-01, S00-DEL-04

The production tree is on the owner's machine and cannot be reached from this cloud session
(blocker B-001). Which of the proposed ways should be used: (a) the owner runs the read-only audit
tool `tools/audit/gie_baseline_audit.py` and returns its output folder; (b) the owner connects the
machine through Claude Remote Control; (c) the owner provides a copy of the code without
credentials (for example a private GitHub repository added to this session)?

## U-02 - Where will GIE finally live?

- Status: OPEN
- Requirements: COM-03, S11-ACC-05

The briefs mention `dashboard/gie/` and "existing tabs unaffected", which suggests that GIE is to be
integrated into the existing repository. Is the GitHub repository `Hello` the permanent home, or
should the `gie/` package later be merged into the existing code base?

## U-03 - Meaning of "proposed recovery at -1.75%"

- Status: OPEN
- Requirements: G05-03, S08-ASG-11, S08-ACC-02

Read as hysteresis: once DEFENSIVE at -2 %, the account returns to NORMAL only when the daily
result has recovered to -1.75 % or better. Is that the intended meaning?

## U-04 - Actual account rules

- Status: OPEN
- Requirements: G05-10, G05-11, S08-ASG-08, S08-ASG-09, S08-ASG-10, G06-05

Needed by session 08: FTMO product and phase, account currency, the reference equity the daily
limits are measured against, the exact daily reset instant, news-trading restrictions, overnight /
weekend / rollover rules, and which other systems trade the same account.

## U-05 - Definitions behind the quoted baseline figures

- Status: OPEN
- Requirements: G09-02, G09-07, G09-08

Is "+0.462R" the mean per trade or the total? What is the denominator of "18% day coverage" and
of the daily-breach statistics? How was "4.9% drawdown at 0.5% risk" computed?

## U-06 - Confirm that "paper" excludes broker demo accounts

- Status: OPEN
- Requirements: M-AUTH-13, G08-10

See D-011.

## U-07 - Definition of an "eligible day"

- Status: OPEN
- Requirements: G01-07, G10-08, S10-ACC-06

Needed for the trade-frequency goal and for every per-day statistic.

## U-08 - Runtime environment

- Status: OPEN
- Requirements: S00-ASG-06, G03-07

Operating system, Python version and hardware of the machine that will run GIE, and whether it is
the same machine that runs the existing bots (resource isolation, G07-08).

## U-09 - Authorised market data

- Status: OPEN
- Requirements: S01-ASG-10, G06-06, G09-06, G09-10

Which tick history and which external feeds (XAGUSD, DXY, GC, yields, S&P 500, VIX, USDJPY, news)
are licensed and available? Round 2 did not store raw ticks.

## U-10 - Instrument specification of XAUUSD at the broker

- Status: OPEN
- Requirements: S07-ASG-15, S08-ASG-02

Contract size per lot, price digits, volume step, minimum stop distance, margin and swap rules.
They are broker data, not contract constants (see CR-003).

## U-11 - Further shared contracts in wave 2

- Status: OPEN
- Requirements: COM-06, S02-DEP-01, S08-DEP-01

`Candle`, `InstrumentSpec`, `AccountSnapshot` and the broker calendar are expected to be proposed
by sessions 01, 07 and 08 as shared contracts (CR-003). Confirm that they are added through the
coordinator with the same framework rather than defined locally.

## U-12 - Confirm the two interpretations in D-012

- Status: OPEN
- Requirements: G04-10, G04-11

A position's volume never increases; a stop never moves away from the entry and is never removed.

## U-13 - How supervisor review reaches the coordinator

- Status: OPEN
- Requirements: OWN-04, M-AUTH-03

The supervisor (ChatGPT) cannot be contacted from this session. It is assumed that the owner
forwards the review package and returns the supervisor's recommendations together with the
approval decision.

## U-14 - Are the -2 % and +5 % levels adjustable research parameters as well?

- Status: OPEN
- Requirements: G05-01, G05-02, G05-05, G05-06, M-DIR-08

The master specification attaches "configurable and subject to validation" only to the recovery
(-1.75 %) and emergency-halt (-3 %) levels. All four levels are kept in
`specifications/research_parameters.toml` because of M-DIR-08 ("proposed numerical settings are
research parameters"), but the owner should confirm whether -2 % (defensive) and +5 % (profit
protection) are fixed rules of the account or may be studied and changed.

## U-15 - Scope of "ablations" in brief 10

- Status: OPEN
- Requirements: S10-ASG-04, S10-ASG-05, S10-ASG-06, S10-ASG-07, S10-ASG-08, S10-ASG-09

"Test hybrid, multiple positions, sizing, exits, news and intermarket ablations." Does "ablations"
apply to all six items (an ablation study of each component) or only to news and intermarket? The
broader reading is recorded so that nothing is lost.
