# GIE decision log

Persistent record of architectural and procedural decisions (requirements S00-ASG-27, OWN-17).

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
declared next to the fields. Every record that exists in memory is valid. A contract class can only
be defined inside `gie.contracts` (the framework refuses any other module), so a subsystem cannot
create a local variant (COM-06).
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

Ten contracts are proposed at version 1.0.0: the nine named by the brief and `RunManifest`
(D-019). Rules: `specifications/SHARED_CONTRACTS.md`; fields: `specifications/contracts/FIELDS.md`;
schemas: `gie/contracts/schemas/`. A schema also publishes the behaviour that is not expressible as
structure (stream, ordering time, rank, identity rule, transition tables) as `x-gie-behaviour`, so
that a change of behaviour is a detectable contract change, and every contract has golden messages
under `tests/gie/golden/` that later versions must keep reading. The schema lock
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

*Amended 2026-11-27 after review round 1 (findings PD-01..PD-03, DT-04..DT-08).* A trading
opportunity has a stable `opportunity_id` (setup type, side, horizon and the market events it rests
on - not the evidence revisions); `candidate_id` adds the scoring model and the revision, so an
active model, a shadow model and an unscored candidate of the same opportunity are distinct records;
`alternative_group_id` is derived from the opportunity and links the alternative plans of which at
most one may be executed. `client_order_id` is derived from the authorising risk decision (entries)
or from the position (protective and reducing orders).

*Amended 2026-11-27 after review round 2 (verification items DT-05, DT-06, DT-16).* Every identity
function validates every part with the vocabulary of the corresponding record field and fails with
a contract error only. The scope of each identifier is stated in `gie/contracts/identity.py`:
identifiers that contain the run name one thing among all runs; identifiers of market events and
models are derived from what they describe and are deliberately equal across runs; identifiers of
evidence, opportunities, candidates, alternative groups and plans contain no run and are therefore
named by `(run_id, id)` in any store that holds more than one run. A float field reads an integer
literal exactly when it is the canonical rendering of a double (golden message
`tests/gie/golden/TradePlan-1.0.0/float_edge_values.json`).

## D-009 - Total order by availability time

- Status: SUPERSEDED
- Superseded by: D-020
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

*Amended 2026-11-27 after review round 1 (findings SF-21, PD-14).* A tolerance for clock skew is
bounded to one second and is refused entirely in replay (there is no skew between a virtual clock
and replayed data). Expiry propagates: a derived record never outlives any of its inputs
(`gie.contracts.causality.check_derivation`), and an order never outlives the plan and the approval
that authorise it. No freshness limit is invented in Session 00: each consuming session requests the
limits it needs as research parameters through the coordinator (D-018).

## D-011 - Paper-only routing lock; "paper" means in-process simulation

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G08-03, G08-10, M-AUTH-13, M-AUTH-19, OWN-14, COM-11, M-DIR-07

Four independent layers (`gie/core/routing_lock.py`): no live member in the vocabulary used by the
contracts; a literal `False` constant that is read from nowhere; a guard that admits only
registered simulated adapter types; a static scan in CI (`tools/check_live_lock.py`).
Interpretation that needs the owner's confirmation (open item U-06): "paper" is simulation inside
the GIE process against observed prices. Sending orders to a broker **demo** account is treated as
real routing and is locked as well. Unlocking is impossible by configuration; it would need a code
change to the lock, a new MAJOR version of `OrderEvent` and a recorded owner decision.

*Amended 2026-11-27 after review round 1 (safety lens, findings SF-01..SF-12).* The guard layer was
hardened: the genuine enum members are captured at import and compared by identity; an adapter
class can be registered only from the execution or backtesting package (or tests), must declare the
simulated venue, must define `__slots__` throughout (so nothing can be replaced on an instance) and
all six adapter methods as plain functions, which are remembered at registration - a class modified
afterwards is refused; `RoutingGuard` is final and immutable. The guard is no longer something the
execution engine "must remember to call": the only object through which an order can reach an
adapter is the order gateway `gie.contracts.guarded.GuardedExecutionAdapter` (D-021), and the
static scan forbids adapter calls anywhere else. The hashes of the lock module and of the gateway
are pinned in `specifications/lock_pins.json`; changing either file fails CI until the pin is
updated together with a decision-log entry.
Honest limit, unchanged: Python cannot stop a person who rewrites the lock module or lies inside an
adapter method; that is what the scan, the pins, the tests and review are for.

*Amended 2026-11-27 after review round 2 (findings L2B-16, L2E-15; verification items SF-01,
SF-08).* A class body can assign `__module__` freely, so the allow-lists no longer trust it alone:
registration of a simulated adapter also requires that the module whose code calls the
registration and the module that owns each adapter method (`function.__globals__`) are allowed,
and the record framework requires that the code which creates a record class belongs to
`gie.contracts` or the tests. The code, defaults and closures of the registered adapter methods are
remembered and compared on every use. The self-check also detects an alias attribute added to the
run-mode or venue enumeration. The static scan, its tests and the pins
(`specifications/lock_pins.json`) exist now; the scan also forbids file-system writes below `gie/`
outside the journal module, because a file dropped into a terminal's exchange folder would be a
routing channel that needs no network call.

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

*Amended 2026-11-27 after review round 1 (findings SF-13..SF-20, DT-02).* A single record can only
carry single-record rules. Rules about a *sequence* of records (a stop is only ever tightened, a
replacement adds no risk, nothing follows a terminal state, at most one alternative of a group is
executed) are enforced by `gie.contracts.ledger.LifecycleLedger`, and rules about the *relation*
between a plan, its approval and the order (same plan content as approved, volume within the grant,
nothing expired) by `gie.contracts.authorization`; both are applied by the order gateway and by the
journal (D-021), so a violating sequence is refused where it would be published or sent.
Positions of other systems on the same account (`PositionOwner.EXTERNAL`) are observations, not
GIE decisions: a widened or removed stop or a grown volume of an external position is recorded as
observed, never refused, and GIE never sends an order for such a position.

*Amended 2026-11-27 after review round 2 (findings L2E-01, L2E-08; verification items SF-10,
SF-13).* A position grows only through fills of its own entry order (partial fills); any other
increase of volume stays unrepresentable (no averaging down, no martingale). An entry may fill at or
beyond its stop - the fill is what happened and must be recordable - while the stop itself is never
wider than the initial stop. A resting entry must lie beyond the market it was priced against (plan
rule PLN-14, order rule OEV-17), so a "limit" order that is in fact marketable, with a geometry the
risk gate did not evaluate, cannot be built; a fill is strictly earlier than the expiry of its
order.

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

*Amended 2026-11-27 after review round 1 (findings DI-15..DI-18).* Scores, levels and invalidation
of an evidence record always describe the instrument named in its `symbol`; how an observation on
another instrument relates to gold is learned and reported by session 04 (G06-09), never
pre-translated by the producer. `INACTIVE` (the engine looked and found nothing: scores 0/0) and
`MISSING` (the engine could not evaluate: scores null, reason mandatory) are different states and
neither is ever stored as 0.5 or merged with another engine's record.

*Amended 2026-11-27 after review round 2 (verification items DI-06, DI-15, DI-21).* The stage of an
observation never goes back along its revisions; a setup that falls back is INVALIDATED and a new
observation starts. The slot rule, the meaning of a pending limit and the price series of a candle
close are stated in `specifications/SHARED_CONTRACTS.md` section 11 as provisional rules for
session 02 to confirm against the engines after the audit (B-001).

## D-015 - Requirement traceability and evidence are generated, never typed

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: OWN-08, OWN-16, OWN-17, OWN-18, M-AUTH-16, M-AUTH-17, M-AUTH-26, M-GATE-01, M-GATE-03

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

*Amended 2026-11-27 after review round 1 (findings DI-22, RM-01).* Every parameter declares its
kind (duration, fraction, count) and is validated strictly; the hash of the parameter set is
recorded in the `RunManifest` of every run, so results can always be tied to the exact settings.
A session that needs a further parameter (for example a freshness limit) requests it through the
coordinator with the requirement it serves; sessions never hard-code numbers and Session 00 invented
none.

## D-019 - `RunManifest` is added as a tenth shared contract

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S10-DEL-03, S10-DEL-04, S10-ASG-10, S10-ACC-01, M-AUTH-16, M-GATE-05, S11-ASG-15

The brief names nine contracts. The independent review showed that none of them can answer "which
run produced this record, from which data, configuration, parameters, seed and code": every
record carries a `run_id`, but nothing defined what a run is. `RunManifest` is the first record of
every run (stream rank 0, `seq` 0): mode (REPLAY / PAPER), role (PRIMARY / SHADOW / RESEARCH), data
origin, hashes of configuration and research parameters, seed, code commit and, for replay, the
data period. Its `run_id` is derived from its content. This is an addition to the specification and
is therefore also filed as change request CR-006; the owner may reject it, in which case the fields
move into a session-10 specific record and `run_id` becomes an opaque label again.

## D-020 - The order of a run is its journal; inputs are ordered by availability

- Status: PROPOSED
- Date: 2026-11-27
- Supersedes: D-009
- Requirements: S00-ASG-25, S10-ASG-01, S10-ACC-01, S10-ACC-02, S10-ACC-03, S07-ACC-08, G08-04

Two different orders exist and D-009 conflated them.
*Input order*: exogenous inputs (ticks, broker reports, timers) are delivered in ascending
`(availability time, stream rank, stream, seq)`; availability is receive time, never source time.
*Journal order*: the run loop takes one input, lets every consumer run to completion and publishes
every record of that step to the journal before the next input is taken. The append position
(`journal_seq`) is the total order of the run; records created at the same instant keep their
causal order (tick, evidence, candidate, plan, risk decision, order request).
The reference journal lives in `gie.contracts.journal` (`MemoryJournal`, `FileJournal`): it is
append-only, idempotent per `(stream, seq)`, refuses conflicting content, gaps on run streams,
decreasing times, look-ahead (a derived record whose market-data watermark `as_of_time_ns` lies
beyond the newest journaled tick) and every lifecycle violation, and a file journal detects a torn
or altered line when it is opened. Replay and paper use the same loop and the same journal; a
deterministic replay is therefore "same inputs produce the same journal bytes".
Sessions 01/07 may choose a different storage engine behind the same `RecordSink` / `RecordSource`
interfaces; the rules above are the contract.

*Amended 2026-11-27 after review round 2 (findings L2B-01, L2B-03, L2B-07, L2B-13, L2E-14, L2E-20;
verification items DT-01, DT-12).* The journal can be given the clock of the run; a record is then
accepted only at the step instant it belongs to (D-024). The look-ahead guard judges *declared*
watermarks - it cannot detect a producer that read later data and declared an earlier watermark;
the prefix-invariance test of session 10 remains mandatory. Checks run in a fixed order (type and
run, stream position, provenance against the manifest, time, run ledger) and a refused record
changes nothing. The journal owns the one lifecycle ledger of the run, which also enforces
referential integrity: a record refers only to records that are already part of the run. A file
journal is durable before visible, fails closed after a write error, holds an operating-system lock
against a second writer, offers one synchronisation per step (`commit`), repairs a torn last line
only through an explicit restart call, and verifies canonical form and fingerprint of every line it
reads back; `JournalReader` is the read-only, tailing reader for the dashboard and for analysis.
Known limits, measured in `reviews/session_00/evidence/benchmark.json`: journal and ledger keep the
run in memory; checkpoints, eviction and the storage of raw ticks are extension CX-19.

## D-021 - One order gateway enforces authorisation, lifecycle and the routing lock

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G08-03, G08-07, G08-10, G04-09, G04-10, G04-11, G04-12, G04-13, S07-ACC-06, S08-ACC-06, M-AUTH-19, OWN-14

`gie.contracts.guarded.GuardedExecutionAdapter` is the only path from any GIE component to an
execution adapter. For every call it (1) checks the routing lock and the adapter (D-011), (2) for
an entry requires the selected plan and its risk approval and verifies them against the request
(`validate_entry_authorization`: same run, same plan content as approved by fingerprint, same
alternative group, side, order type, prices and attached stop as planned, volume within the grant,
nothing expired - failing closed exactly at the boundary), (3) for any other order requires the
position and verifies that the order can only reduce or protect it (`validate_reducing_order`),
(4) admits the request and every report returned by the adapter to the lifecycle ledger, which
refuses illegal transitions and a second executed alternative of the same group, and (5) treats an
identical repeated request as a duplicate without calling the adapter again.
Why in the shared contracts and not in session 07: the same rules must hold for the simulated
broker, the reflex engine, the backtester and any later adapter, and they are the rules the owner
stated without exception. Session 07 implements the simulator *behind* the gateway; session 08
implements the risk gate that issues the approvals the gateway demands.

*Amended 2026-11-27 after review round 2 (findings L2B-04, L2E-02, L2E-04, L2E-05, L2E-16).* The
gateway no longer keeps a ledger of its own and no longer accepts plan, approval or position from
its caller: it reads them from the ledger of the run's journal (newest revision only), so the
gateway and the journal cannot disagree. The order of every call is: routing lock, authorisation,
journal (write-ahead), adapter. A request that is already journaled is forwarded again - the adapter
is idempotent per `order_event_id` - so a crash between journal and adapter loses nothing. `poll()`
journals every report on its own and returns `PollResult(events, duplicates, errors)`; a refused
report never hides the reports after it. Reduce-only rules are aggregates of the ledger: at most one
working protective stop per position, working exit and take-profit volume within the open volume.

## D-022 - Timers come from an injected scheduler; there are no timer threads

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-19, S00-ACC-03, G03-12, G08-06, S06-ASG-06, S10-ASG-01

Everything that must happen when no market data arrives (feed-silence detection, closing a candle
in a quiet market, expiry, once-per-second tactical work) is scheduled through
`gie.core.clock.Scheduler`. In replay the `ReplayClock` fires timers exactly at their due instant
while it advances; in paper `PollingScheduler` fires them on the event-loop thread at the first poll
at or after the due instant. Both fire in `(due time, registration order)` and never early, so the
same component code runs unchanged in both modes and replay stays deterministic. `SystemClock`
never goes backwards and never stands still when the operating-system clock is stepped back; it
counts such steps so that health monitoring can fail closed on an unstable clock.

## D-023 - Tests and reviews are written by someone other than the implementer

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: OWN-04, OWN-09, M-AUTH-06, M-AUTH-08, S12-ASG-08, COM-10

Within the single coordinating session (D-001) independence is obtained by separation of roles:
the coordinator writes the foundation code; separate sub-agents that did not write it derive the
tests from the specification and report defects instead of fixing them; a panel of reviewer
sub-agents, one per group of consuming sessions plus a safety and a determinism reviewer, examines
the contracts against the owner's specification. Every finding and its disposition is kept verbatim
in `reviews/session_00/` (136 findings in round 1). A failing test is never weakened to pass: either
the documented behaviour changed (the test is rewritten to the new documentation, equally strict)
or the code is wrong (it is fixed or the failure is reported). This does not replace the
supervisor's independent review or session 12; it reduces what they have to find.

*Amended 2026-11-27 (round 2).* A second panel reviewed the reworked foundation from the viewpoint of
the consuming sessions (40 findings, `reviews/session_00/evidence/review_round2_findings.json`), and
separate verifiers checked every disposition of round 1 against the code instead of trusting the
coordinator's claim (`reviews/session_00/evidence/review_round1_verification.json`); what they found
unfinished was fixed in the second rework (`reviews/session_00/rework_2_change_notice.md`). After
that rework the test authors' suites were maintained by engineers who again did not write the code,
under the same rule: a test follows documented behaviour, never the code. Limits of this
independence, stated plainly: all of these roles are sub-agents instructed by the coordinator;
during the second rework a usage limit stopped several of them mid-task, their unfinished work was
completed by the coordinator or by sub-agents of another model, and the extension of
`tools/make_golden.py` (golden run trace) was written by the coordinator itself. There was no third
review round. The supervisor's review and session 12 remain the independent instances.

## D-024 - Step time: a component sees the instant of the input it is processing

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S00-ASG-19, S00-ASG-21, S00-ACC-03, S10-ASG-01, S10-ACC-01, S10-ACC-02, S06-ASG-01, G03-10, G03-11

Review round 2 showed that "the same code runs in replay and paper" was not true for time: in
replay `now` was the instant of the input, in paper it was whenever the component happened to ask.
A record produced in paper could therefore carry a time that no replay of the same inputs would
reproduce, and a timer could fire "between" two inputs. Decision: in both modes `now_ns()` is the
*step instant* - the availability time of the input (tick, broker report or timer) that the run
loop is processing - and only the run loop moves it (`ReplayClock.advance_to`,
`PaperClock.begin_step`). Components receive read-only views (`ClockView`, `SchedulerView`); timers
are relative to the step instant and fire as steps of their own. How long processing really took is
measured separately (`LatencyRecorder`, wall clock) and never enters a record. Consequence for
sessions 06 and 07: a protective decision is stamped with the tick that caused it; the processing
delay is reported as a latency measurement, not as a later decision time. The shared step driver
that both modes use is extension CX-10 (coordinator) and must exist before any subsystem is
integrated.

## D-025 - Order events have one writer; the broker may create protective orders

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S07-ASG-01, S07-ASG-06, S07-ASG-09, S07-ACC-02, S06-ASG-03, G08-07, G08-08, G04-12

Two components writing events of the same order (execution engine and reflex engine) cannot keep
one gap-free order sequence. Decision: order events are written by session 07 only (engine and
simulated broker). The reflex engine states intents through `interfaces.ProtectionPort`
(`tighten_stop`, `exit`) and reads the result from the journal. Every order event carries its
`originator`: `EXECUTION` for an order the engine requested, `BROKER` for an order the simulated
broker created itself - the protective stop or take profit attached to an entry, or a broker-side
close. A broker-created order enters through `poll()`, must belong to a position whose entry order
has filled, and can only reduce or protect; an `ENTRY` is never broker-created, and the gateway's
`submit` accepts `EXECUTION` only. A position names its protective order or states why it has none
(`unprotected_reason`), and a protective stop never expires.

## D-026 - Which model may lead to a paper order depends on the role of the run

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: G07-08, G07-09, G07-10, G07-12, G04-08

Round 1 only made the model status visible on candidates and plans; any model could still drive an
approved entry (verification item PD-04). Decision, enforced by `validate_entry_authorization` and
by the journal: the manifest of a run pins its models with status and validity period
(`RunManifest.models`); a candidate or plan must be scored by a pinned model with exactly the pinned
status; an entry rests on a model whose status is allowed for the role of the run - `PRIMARY`:
`ACTIVE` only; `SHADOW`: `ACTIVE` or `SHADOW`; `RESEARCH`: additionally `RESEARCH`. A `REJECTED`,
`ROLLED_BACK` or `RETIRED` model never leads to an order. A promotion therefore takes effect in a
new run, never in the middle of one. This is the coordinator's proposal for open item U-17; until
the owner has promoted a first model, a primary paper run trades only what approved rule sets
propose.

## D-027 - A run is identified by the content of its manifest

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S10-ACC-01, S10-DEL-03, S10-DEL-04, S10-ASG-10, M-AUTH-16, M-GATE-05, S08-ASG-09

`run_id` was derived from mode, configuration hash and a label, so two runs that differed in role,
seed, code, data or models could share an identifier (findings L2B-05, L2B-06, L2B-11, L2B-12).
Decision: `run_id` is derived from the mode and the hash of every other field of the manifest
(role, risk regime, data origin, sources and period, configuration and parameter hashes, seed, code
commit and dirty flag, pinned models). Two replays with identical manifests are by definition the
same run and must produce the same journal; anything else is a different run. A replay manifest
contains no wall-clock time. `risk_regime` separates runs under the challenge rules from
unconstrained research (allowed only with role `RESEARCH`), so results of the two can never be
mixed unnoticed. Every pseudo-random element takes its seed from `RunContext.seed_for(purpose)`.
Not built here and recorded as extension CX-12 (session 10): the run-configuration document,
per-stream data digests, the run catalogue and the bookkeeping of experiments, trials and holdouts.

## D-028 - Amounts decide, fractions report

- Status: PROPOSED
- Date: 2026-11-27
- Requirements: S08-ASG-06, S08-ASG-07, S08-ACC-01, S08-ACC-02, S08-ACC-03, S08-ACC-04, S08-ACC-05, G05-02, G05-04, G05-06

The daily limits are stated as fractions of a reference equity, but a fraction of an arbitrary
equity is rarely an exact decimal, and "exactly at the floor" cannot be decided on a rounded value
(finding L2E-18). Decision: a risk decision carries the amounts in account currency
(`daily_pnl_amount`, `profit_floor_amount`, risk, open and pending risk) and every boundary is
decided on amounts. Fractions remain on the record for reporting and are derived by one function,
`gie.core.canonical.fraction_of`, with a stated rounding direction (a result is rounded down, a
floor or a risk is rounded up), and the record verifies the derivation. The sequence rule of a
trading day - the floor never decreases or disappears, a halt is sticky, the reference equity is
frozen - is `advance_daily_risk`, enforced by the run ledger across all risk producers. The numeric
levels themselves stay research parameters (D-018).

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
- Requirements: G09-02, G09-11, G09-12, G09-13, G09-07, G09-08

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
the same machine that runs the existing bots (resource isolation, G07-08). Also needed: the
resolution of the system clock on that machine (about 15.6 ms on Windows before Python 3.13), which
bounds how finely receive times can be measured in a paper run; it is measured by the feed
benchmark of session 01.

## U-09 - Authorised market data

- Status: OPEN
- Requirements: S01-ASG-10, G06-06, G06-10, G06-11, G06-12, G06-13, G06-14, G06-15, G06-16, G06-17, G09-06, G09-10

Which tick history and which external feeds (XAGUSD, DXY, GC, yields, S&P 500, VIX, USDJPY, news)
are licensed and available? Round 2 did not store raw ticks.

## U-10 - Instrument specification of XAUUSD at the broker

- Status: OPEN
- Requirements: S07-ASG-15, S08-ASG-02

Contract size per lot, price digits, volume step, minimum stop distance, margin and swap rules.
They are broker data, not contract constants (see CR-003).

## U-11 - Further shared contracts proposed by later sessions

- Status: OPEN
- Requirements: COM-06, S02-DEP-01, S08-DEP-01, S11-DEP-01

Nine further shared types are needed before later sessions can start (candle, broker calendar,
tick usability policy, instrument specification, account snapshot, observations and news, market
conditions, the decision stream record, outcome labels): CR-003 and CR-005 name the proposing
session and the gate for each. Confirm that they are added through the coordinator with the same
framework rather than defined locally, and that a session writes no code against a type before it
is integrated (extension gate in `specifications/dependency_graph.yaml`).

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

## U-16 - Which "uncertainty" gates a decision

- Status: OPEN
- Requirements: G02-04, S04-ASG-08, S05-ASG-13, S05-ACC-05, S11-ASG-04, G10-18

The specification asks for "expected net R and uncertainty". Two different quantities fit that
word and the contracts carry both: the dispersion of a single trade's outcome (`net_r_stdev`) and
the uncertainty of the estimate itself (`expected_net_r_stderr` with `support_count`). Which of
them the decision engine must use to reject a plan, and which the dashboard shows as "uncertainty",
is the owner's (or supervisor's) call before session 05 starts. Until then a scored record must
state at least one and consumers must say which one they used.

## U-17 - Which model statuses may lead to paper orders

- Status: OPEN
- Requirements: G07-08, G07-09, G07-10, G07-12, G04-08

Candidates and plans carry the status of the model that scored them. Proposed rule for session 05
and 08: in a run with role PRIMARY only models that are ACTIVE (owner-approved) and approved
rule-set baselines may lead to an entry and to quality-dependent sizing; SHADOW and RESEARCH models
are scored and logged in SHADOW / RESEARCH runs only and never share a risk budget with the primary
run. Consequence to confirm: until the owner has promoted a first model, the primary paper run
trades only what the rule-set baselines propose.

## U-18 - Read-only access for the paper data connection

- Status: OPEN
- Requirements: M-AUTH-13, M-AUTH-15, G08-10, S01-ASG-01

Paper mode needs live prices from the broker terminal. The same terminal API that delivers prices
can also send orders. Proposal for session 01: connect only with read-only ("investor") credentials
if the broker offers them, keep all terminal access behind one read-only facade module that exposes
no order function (enforced by the static scan), and never store credentials in the repository.
Does the owner's account offer investor credentials, and may GIE use them? If not: is there an
account on which trading is disabled by the broker?
The same answer is needed for reading the account state (equity, margin and the positions of the
other systems, extension CX-05) and it must exclude any indirect way of driving the existing bots.
**Gate:** this item must be answered before session 01 connects to any terminal and before the
first PAPER run; until then paper runs use recorded or synthetic data only.
