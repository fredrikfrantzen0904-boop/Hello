# GIE architecture (Session 00 foundation)

Status: **PROPOSED** - submitted for the owner's approval at the Session 00 review gate.
Scope of this document: what Session 00 built and the frame the other twelve sessions build into.
It describes only what exists or is specified; behaviour of sessions 01-12 is not implemented.

## 1. Purpose and limits

GIE is a paper / replay research system that observes several markets and trades **only XAUUSD**
(G01, G06). The targets in the specification are research goals, not established performance
(G01-11). Real order routing is locked for the whole project phase (G08-10).

## 2. Layers and timing (G03)

```
                     +--------------------------- Clock (injected everywhere) ---------------------------+
 market data (01) -> | Tick -> REFLEX (06) every tick: protect open positions; never waits for anything  |
                     |      -> TACTICAL (05, 07, 08) every tick or second: plans, risk gate, orders       |
                     |      -> STRATEGIC (02, 03, 04) on completed candles / background: evidence, models |
                     +--------------------------------------------------------------------------------------+
 learning (09), backtesting (10), dashboard (11), QA (12) read the same records; they are never in the tick path.
```

* **Reflex** is deterministic, uses only ticks, positions and order events, and never calls a
  model, the network or the dashboard.
* **Tactical** consumes prepared candidates and plans; it never trains and never calls an LLM.
* **Strategic** work publishes records asynchronously; a slow strategic task can make evidence
  stale, which the freshness rules then reject - it can never delay a protective decision.

The proposed latency targets (tick < 1 ms, protection < 5 ms, tactical < 20 ms) are research
parameters to measure against with `gie.core.latency` (p50/p95/p99/max); nothing guarantees them.

## 3. Data flow and contracts

`Tick -> Evidence -> Candidate -> TradePlan -> RiskDecision -> OrderEvent -> Position`, plus
`ModelVersion` and `Clock`. Rules, fields and invariants: `specifications/SHARED_CONTRACTS.md` and
`specifications/contracts/FIELDS.md`. Principles:

1. **Immutable facts.** Records are never updated; changes are new records.
2. **Valid by construction.** A record that violates a field rule or an invariant cannot exist.
3. **Deterministic.** Canonical JSON, content-derived identifiers, injected time, total ordering.
4. **Causal.** Records are ordered by when information became available; each derived record
   carries a causality watermark; data from the future is an error.
5. **Fail closed.** Any error or stale input forbids creating risk; reducing risk stays allowed.
6. **Explicitly labelled.** Every record names its run, mode and the origin of its data.

## 4. Packages

| package | owner | content | status |
|---|---|---|---|
| `gie.core` | 00 | errors, canonical JSON, ids, time and trading day, clocks, freshness, ordering, latency, research parameters, routing lock | implemented |
| `gie.contracts` | 00 | field framework, nine contracts, identity functions, codec, registry, schemas and lock, compatibility checker, ordering keys, service interfaces | implemented |
| `gie.market_data` | 01 | ticks, storage, replay, candles, calendar | not started |
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
(`TickSink`, `RecordSink`, `RiskGate`, `ExecutionAdapter`, `ModelRegistry`). Checked by
`tools/check_architecture.py`, which also forbids wall-clock reads outside `gie/core/clock.py` and
unseeded randomness / `uuid4` anywhere in `gie/`.

## 5. Session dependency graph

Source of truth: `specifications/dependency_graph.yaml` (verified in CI).

```
wave 1:  00 Coordinator
wave 2:  01 Market Data (00)        02 Strategy (00; data contracts of 01)
         07 Execution (00, 01; risk contract of 08)        08 Risk (00; execution/data contracts)
wave 3:  03 Intermarket (00, 01)    04 Prediction (00, 01, 02; 03 optional)
         05 Decision (00, 02, 04; risk contract of 08)     06 Reflex (00, 01, 07, 08)
wave 4:  09 Learning (00, 04, 05, 10)   10 Backtesting (00; scaffolding early)
         11 Dashboard (00; read-only contracts of all)     12 QA (00; scaffolding early, ongoing)
```

"Contract of NN" dependencies are satisfied by the shared contracts and interfaces of Session 00
plus the additional shared types that the session proposes first (CR-003).

## 6. Routing lock (G08-03, G08-10)

"Real routing" is any order message leaving the process towards a broker, terminal or exchange,
demo accounts included (D-011). Layers:

1. **Vocabulary** - `RunMode` = {REPLAY, PAPER}, `ExecutionVenue` = {SIMULATED}. `OrderEvent.venue`
   and every record's `mode` use them, so a live order or live run cannot be constructed or decoded.
2. **Constant** - `LIVE_ROUTING_PERMITTED = False`, a literal read from nowhere: no environment
   variable, file, flag or setting can change it.
3. **Guard** - `RoutingGuard.authorize(adapter)` admits only adapter types registered as simulated
   and re-checks the declarations on the instance. The execution engine (07) must call it before
   every adapter operation.
4. **Static scan** - `tools/check_live_lock.py` in CI: broker order-entry calls, broker SDK imports
   outside read-only market data, any tampering with the constant or vocabularies, network clients
   in decision / protection packages.

Honest limit: Python cannot stop a person who edits the lock module itself; such an edit is caught
by the scan, by `tests/gie/core/test_routing_lock.py` and by review. There is no way to unlock by
configuration.

## 7. Determinism and replay

Replay (10) drives the same components as paper mode through the same interfaces with a
`ReplayClock`. Byte-identical reruns follow from: injected time; no wall-clock reads; no unseeded
randomness; content-derived identifiers; canonical JSON; a total order of all records; per-stream
sequence numbers with duplicate / gap / conflict detection.

## 8. Persistence and recovery (design frame for 07/08, not implemented in 00)

The journal is an append-only sequence of canonical record bytes per stream. `(stream, seq)` makes
appends idempotent; `client_order_id` makes orders idempotent across restarts; the newest
`OrderEvent` of an order and the newest `Position` revision describe the full current state, so
restart reconciliation is "replay the journal, then compare with the simulated broker and emit
`RECONCILED` events". `RecordSink` is the interface; the storage engine is chosen by sessions 01/07.

## 9. Quality gates

`python -m tools.ci` (also run by `.github/workflows/gie-ci.yml` on Linux and Windows) executes:
lint and format (ruff), strict typing (mypy), schema/lock/evolution check, generated-docs check,
live-lock scan, architecture check, protected-path check, register validation, the test suite with
machine-readable evidence, and baseline verification. Tests are marked with requirement ids;
registers and the review PDF are generated from the evidence (D-015).

## 10. Known limits of the foundation

* The existing system was not audited (B-001); language choice and engine vocabularies are provisional.
* Contract validation and canonical encoding are pure Python; measured costs are in the review
  package. They are far below the proposed 1 ms tick target on the development container, but that
  is not evidence about the owner's hardware or about full tick processing.
* The JSON Schemas express structure; decimal bounds and cross-field invariants are carried as
  `x-gie-*` annotations and are enforced by the Python implementation only.
* No subsystem behaviour (data, strategy, prediction, decision, reflex, execution, risk, learning,
  backtesting, dashboard, QA) exists yet.
