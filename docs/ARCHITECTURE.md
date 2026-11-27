# GIE architecture (Session 00 foundation)

Status: **PROPOSED** - submitted for the owner's approval at the Session 00 review gate
(deliverable S00-DEL-01). Scope: what Session 00 built and the frame the other twelve sessions
build into. It describes only what exists in this repository or is specified for later sessions;
no behaviour of sessions 01-12 is implemented.

## 1. Purpose and limits

GIE is a paper / replay research system that observes several markets and trades **only XAUUSD**
(G01, G06). The targets in the specification are research goals, not established performance
(G01-11). Real order routing is locked for the whole project phase (G08-10). The existing system
was not audited (blocker B-001); everything that depends on it is marked provisional.

## 2. Layers and timing (G03)

```
                     +--------------------- Clock and Scheduler (injected everywhere) -------------------+
 market data (01) -> | Tick -> REFLEX (06) every tick: protect open positions; never waits for anything  |
                     |      -> TACTICAL (05, 07, 08) every tick or second: plans, risk gate, orders       |
                     |      -> STRATEGIC (02, 03, 04) on completed candles / background: evidence, models |
                     +-------------------------------------------------------------------------------------+
 learning (09), backtesting (10), dashboard (11), QA (12) read the same records; they are never in the tick path.
```

* **Reflex** is deterministic, uses only ticks, positions and order events, and never calls a
  model, the network or the dashboard.
* **Tactical** consumes prepared candidates and plans; it never trains and never calls an LLM.
* **Strategic** work publishes records asynchronously; a slow strategic task can make evidence
  stale, which the freshness rules then reject - it can never delay a protective decision.

The proposed latency targets (tick < 1 ms, protection < 5 ms, tactical < 20 ms) are research
parameters to measure against with `gie.core.latency` (p50/p95/p99/max); nothing guarantees them.
Measured cost of the foundation itself is in the review package and says nothing about the
owner's hardware.

## 3. Data flow and contracts

`Tick -> Evidence -> Candidate -> TradePlan -> RiskDecision -> OrderEvent -> Position`, plus
`ModelVersion`, `Clock` and `RunManifest` (the tenth contract, decision D-019). Rules, units and
invariants: `specifications/SHARED_CONTRACTS.md`; every field: `specifications/contracts/FIELDS.md`;
JSON Schemas: `gie/contracts/schemas/`. Principles:

1. **Immutable facts.** Records are never updated; a change is a new record that names its
   predecessor.
2. **Valid by construction.** A record that violates a field rule or an invariant cannot exist,
   whether it is built in code or decoded from JSON.
3. **Deterministic.** Canonical JSON (RFC 8785), exact decimals, content-derived identifiers,
   injected time and timers.
4. **Causal.** Every derived record carries the market-data watermark it was computed from; data
   from the future is an error; a derived record never outlives its inputs.
5. **Fail closed.** Any error, stale or missing input forbids creating risk; reducing risk stays
   allowed.
6. **Explicitly labelled.** Every record names its run, mode and the origin of its data; synthetic
   fixture data can never be relabelled as market data downstream.

Three kinds of rules and where each is enforced:

| kind of rule | example | enforced by |
|---|---|---|
| inside one record | the stop is on the losing side of the entry | the record's invariants (construction and decoding) |
| across a sequence of records | a stop is only tightened; nothing follows a terminal order state; one executed plan per alternative group | `gie.contracts.ledger.LifecycleLedger` |
| between a plan, its approval and an order | the order is the approved plan, within the granted volume, unexpired | `gie.contracts.authorization` |

The ledger and the authorisation checks are applied in two places that no component can bypass:
the journal (everything published) and the order gateway (everything sent to an adapter).

## 4. Packages

| package | owner | content | status |
|---|---|---|---|
| `gie.core` | 00 | errors, canonical JSON, identifiers, time and trading day, clocks and schedulers, freshness, ordering, latency, research parameters, routing lock | implemented |
| `gie.contracts` | 00 | field framework, ten contracts, identity functions, codec, registry, schemas and lock, compatibility checker, ordering keys, derivation check, lifecycle ledger, authorisation checks, journal, order gateway, service interfaces | implemented |
| `gie.market_data` | 01 | ticks, storage, replay source, candles, calendar | not started |
| `gie.strategy_intelligence` | 02 | six engine adapters as evidence producers | not started |
| `gie.intermarket` | 03 | external feeds, lead/lag, volatility and news | not started |
| `gie.prediction` | 04 | net-R and payoff models, similarity, destinations | not started |
| `gie.decision` | 05 | plan construction, ranking, unified decision stream | not started |
| `gie.reflex` | 06 | per-tick protection | not started |
| `gie.execution` | 07 | paper/replay order state machine, simulated broker | not started |
| `gie.risk` | 08 | exposure, sizing, daily states, profit floor | not started |
| `gie.learning` | 09 | logs, discovery, model registry, promotion | not started |
| `gie.backtesting` | 10 | replay harness, validation statistics | not started |
| `dashboard/gie` | 11 | read-only dashboard | not started |
| `tests/gie/qa`, `reviews/qa` | 12 | independent verification | not started |
| `gie.app` | 00 | composition root that wires approved subsystems | created at first integration |

No package directory is created before its session starts: an empty package would be a placeholder.

**Import rule (D-006).** A subsystem imports only `gie.core`, `gie.contracts` and itself.
Subsystems call each other only through the `Protocol` classes in `gie/contracts/interfaces.py`
(`TickSink`, `TickSource`, `RecordSink`, `RecordSource`, `PlanScorer`, `RiskGate`,
`ExecutionAdapter`, `OrderGateway`, `ModelRegistry`). Checked by `tools/check_architecture.py`,
which also forbids wall-clock reads outside `gie/core/clock.py` and unseeded randomness, `uuid4`
and salted `hash()` anywhere in `gie/`.

## 5. Session dependency graph

Source of truth: `specifications/dependency_graph.yaml` (verified in CI).

```
wave 1:  00 Coordinator
wave 2:  01 Market Data (00)        02 Strategy (00; data contracts of 01)
         07 Execution (00, 01; risk contract of 08)        08 Risk (00; execution/data contracts)
wave 3:  03 Intermarket (00, 01)    04 Prediction (00, 01, 02; 03 optional)
         05 Decision (00, 02, 04; risk contract of 08)     06 Reflex (00, 01, 07, 08)
wave 4:  09 Learning (00, 04, 05, 10)   10 Backtesting (00; harness early, modules integrated later)
         11 Dashboard (00; read-only contracts of all)     12 QA (00; scaffolding early, ongoing)
```

Two gates (defined in the dependency graph, procedure in `docs/WORKTREES.md`): a session *starts*
when the owner has approved the contracts, opened its wave, and the shared types it needs exist;
it is *integrated* when its dependencies and its own review package are approved. Nine shared
types that later sessions need are deliberately not defined yet (`contract_extensions` in the
graph, change requests CR-003 and CR-005): each is proposed by the session that owns the knowledge
and added to `gie.contracts` by the coordinator.

## 6. Routing lock (G08-03, G08-10)

"Real routing" is any order message leaving the process towards a broker, terminal or exchange,
demo accounts included (D-011). Layers:

1. **Vocabulary** - `RunMode` = {REPLAY, PAPER}, `ExecutionVenue` = {SIMULATED}. `OrderEvent.venue`
   and every record's `mode` use them, so a live order or live run cannot be constructed or
   decoded; the schema check fails if either vocabulary ever changes.
2. **Constant** - `LIVE_ROUTING_PERMITTED = False`, a literal read from nowhere: no environment
   variable, file, flag or setting can change it.
3. **Guard and gateway** - the only path to an execution adapter is the order gateway
   `gie.contracts.guarded.GuardedExecutionAdapter`. Before every call it asks `RoutingGuard`, which
   admits only adapter classes that were registered from the execution or backtesting package (or
   tests), declare the simulated venue, have no per-instance attributes, and still have exactly the
   method functions they were registered with. The gateway then calls those registered functions,
   not whatever the object currently exposes.
4. **Static scan** - `tools/check_live_lock.py` in CI: broker order-entry calls and broker SDK
   imports, process and network escapes in GIE code, adapter calls outside the gateway, any
   weakening of the lock module, and the pinned hashes of the lock module and the gateway
   (`specifications/lock_pins.json`).

Honest limit: Python cannot stop a person who rewrites the lock module or writes an adapter whose
`submit` method does something other than simulate. Those are caught by the scan, the pins, the
tests (`tests/gie/core/test_routing_lock.py`, `tests/gie/contracts/test_guarded_adapter.py`,
`tests/gie/tools/test_check_live_lock.py`) and by review - not by the runtime. There is no way to
unlock by configuration.

## 7. Order of events, determinism and replay

* **Inputs** (ticks, broker reports, timers) are taken in ascending availability time; ties are
  broken by stream rank, stream name and sequence number (`gie.core.ordering`, `gie.contracts.ordering`).
* **One step**: fire the timers due up to the next input; advance the clock to it (replay);
  deliver the input to the consumers in the order protective, tactical, strategic; publish every
  record of the step to the journal; only then take the next input.
* **The journal is the order of the run** (D-020). `gie.contracts.journal` provides the reference
  implementation (`MemoryJournal`, `FileJournal`): append-only, idempotent, refusing conflicting
  duplicates, gaps on run streams, decreasing times, look-ahead and lifecycle violations.
* Replay (10) drives the same components as paper through the same interfaces, with a
  `ReplayClock` that is both clock and scheduler; paper uses `SystemClock` and `PollingScheduler`
  on the event-loop thread (D-022). Timers fire in the same order in both.

Byte-identical reruns follow from: injected time and timers; no wall-clock reads; no unseeded
randomness; content-derived identifiers; canonical JSON; the run-to-completion step. What is *not*
covered by the foundation and must be shown by sessions 01/10 with real components: determinism of
third-party numeric libraries across machines, and of background work whose results are delivered
through the scheduler.

## 8. Persistence and recovery

`FileJournal` writes one canonical JSON line per record and verifies sequence numbers and
fingerprints when it is opened; a torn last line (crash during a write) is reported as
`JournalCorruptError` and can be removed with `repair_torn_tail`; any other damage is never
repaired automatically. Because `(stream, seq)` makes appends idempotent and `client_order_id`
makes orders idempotent, a restarted component can re-send what it is unsure about. The newest
`OrderEvent` of an order and the newest `Position` revision describe the current state, so restart
reconciliation is "re-read the journal, compare with the simulated broker, emit `RECONCILED`
events". The persistence behaviour of the execution engine itself (S07-ASG-07, S07-ASG-08) and the
choice of a storage engine for tick history (session 01) are **not** implemented in Session 00.

## 9. Quality gates

`python -m tools.ci` (also run by `.github/workflows/gie-ci.yml` on Linux and Windows, Python 3.11
and 3.13) executes: lint and format (ruff), strict typing (mypy), schema / lock / evolution check,
generated-docs check, golden-message check, live-lock scan, architecture check, protected-path
check, documentation-path check, secret scan, the test suite with machine-readable evidence and
coverage, register validation and rendering, and baseline verification. It writes one result file;
the review PDF is generated from that file and the registers (D-015).

Tests carry the ids of the requirements they verify. The test suites of the foundation and the two
review rounds were written by sub-agents that did not write the code under test (D-023).

## 10. Known limits of the foundation

* The existing system was not audited (B-001): the language choice (D-003) and the engine
  vocabularies (D-014) are provisional, and no baseline has been frozen.
* Contract validation and canonical encoding are pure Python. Measured costs are in the review
  package; they are not evidence about the owner's hardware or about full tick processing.
* The JSON Schemas express structure; decimal bounds, cross-field invariants and behaviour are
  carried as `x-gie-*` annotations and are enforced by the Python implementation only. A consumer in
  another language must implement them from `specifications/SHARED_CONTRACTS.md`.
* The ledger and the journal keep their state in memory for the life of a run; their cost per
  record and their memory use over a long paper run have been measured only on synthetic records.
* `SystemClock` has the resolution of the operating system (on Windows with Python older than 3.13
  the wall clock advances in steps of about 15.6 ms); the measured resolution of the paper machine is
  a deliverable of the session-01 feed benchmark (open item U-08).
* Nine shared types needed by later sessions do not exist yet (CR-003, CR-005).
* No subsystem behaviour (data, strategy, prediction, decision, reflex, execution, risk, learning,
  backtesting, dashboard, QA) exists yet.
