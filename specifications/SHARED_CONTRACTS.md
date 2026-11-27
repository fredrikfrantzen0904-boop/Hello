# GIE shared contracts - normative specification

Contract set version **1.0.0** - status **PROPOSED** (awaiting owner approval at the Session 00
review gate; see `docs/DECISION_LOG.md` D-007).

This document defines the rules that hold for *every* shared contract: units, timestamps,
identifiers, freshness, error semantics, event ordering, serialisation and versioning.
The fields of each contract are listed in the generated reference
`specifications/contracts/FIELDS.md`; the machine-readable form is the JSON Schemas in
`gie/contracts/schemas/`; the executable form is the Python package `gie.contracts`.
If these ever disagree, that is a defect: the code and the generated files are checked
against each other in CI, and this prose is the intended meaning.

Requirement sources: `S00-ASG-11..25`, `G01-G10` (see `specifications/requirements_matrix.yaml`).

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

---

## 1. Scope and data flow

Nine contracts are shared by all subsystems:

| contract | produced by | consumed by | what it is |
|---|---|---|---|
| `Tick` | 01 market data | 02, 03, 06, 07, 10, 11 | one bid/ask quote of one instrument from one feed |
| `Evidence` | 02 strategy, 03 intermarket | 04, 05, 09, 11 | one causal, directional observation |
| `Candidate` | 04 prediction | 05, 09, 11 | one scored opportunity in one direction |
| `TradePlan` | 05 decision | 08, 07, 09, 11 | one executable way to trade a candidate |
| `RiskDecision` | 08 risk | 05, 07, 09, 11 | approval / reduction / rejection of a plan |
| `OrderEvent` | 07 execution | 06, 08, 09, 10, 11 | one step in the life of one simulated order |
| `Position` | 07 execution | 06, 08, 09, 10, 11 | snapshot of one position on the account |
| `ModelVersion` | 09 learning (registry) | 04, 05, 08, 11 | one immutable model artifact and its promotion status |
| `Clock` | 00 core | all | the time source and its serialisable reading |

```
Tick --> Evidence --> Candidate --> TradePlan --> RiskDecision --> OrderEvent --> Position
 01      02 / 03        04            05             08               07            07
                                                                       ^
                                              06 reflex (per tick) ----+   protective orders only
ModelVersion (09) is referenced by Candidate and TradePlan.   Clock (00) is injected everywhere.
```

A subsystem MUST NOT redefine, copy or locally extend a shared contract (COM-06).  A subsystem
that needs a further shared data type (for example `Candle`, `InstrumentSpec`, `AccountSnapshot`)
proposes it to the coordinator as a change request; once approved it is added through the same
framework, registry, schema lock and tests.

Records are **immutable facts**.  Nothing is ever updated in place: a change is a new record
(`revision + 1`, or the next `order_seq`) that refers to the one it replaces.

## 2. Units

| quantity | representation | field naming | notes |
|---|---|---|---|
| instant | `int` nanoseconds since 1970-01-01T00:00:00 UTC | `*_time_ns`, `*_until_ns` | range `0 .. 2^63-1` |
| duration | `int` nanoseconds, `>= 0` | `*_ns` documented as duration | |
| price | exact decimal, `> 0` | `*_price`, `bid`, `ask` | USD per troy ounce for XAUUSD; quote currency per unit otherwise |
| volume | exact decimal | `*_lots`, `*_volume` | broker lots; lot size is broker data (01/07), not a contract constant |
| money | exact decimal | `*_amount` | account currency, given by `account_currency` (ISO 4217) |
| fraction | exact decimal or float | `*_frac` | `0.05` means 5 %; percent numbers are never used |
| R multiple | float | `*_r` | result divided by the amount initially risked; always **net of costs** |
| score / probability | float in `[0, 1]` | `*_score`, `*_frac` | |
| trading day | string `YYYY-MM-DD` | `trading_day` | calendar date in `Europe/Prague` (section 3.4) |

Rules:

* Prices, volumes, money and every fraction that takes part in a **risk limit comparison**
  (`risk_frac`, `daily_pnl_frac`, `profit_floor_frac`, `close_frac`) are exact decimals
  (`decimal.Decimal` in Python).  Binary floats MUST NOT be used for them, so that boundary rules such
  as "at exactly the floor no new risk" (G05-08) are decided exactly.
* Arithmetic on these exact decimals MUST use the context `gie.core.canonical.EXACT` (64 significant
  digits; a result that would need rounding raises instead of being rounded).  Python's default
  decimal context rounds silently at 28 digits and MUST NOT be used for them.
* Statistical quantities (scores, probabilities, R estimates, model metrics) are IEEE-754 doubles.
  NaN and infinities are invalid everywhere.
* A value that is unknown or not applicable is `null`.  `0` always means the number zero.  In
  particular an *unscored* candidate has `expected_net_r = null`, and *missing* evidence has `null`
  scores, never `0` (S02-ACC-03).
* The only tradable symbol is `XAUUSD` (G01-01, G06-07).  `Candidate`, `TradePlan` and `OrderEvent`
  can only carry `XAUUSD`; so can a `Position` owned by GIE.  `Tick` and `Evidence` may describe other
  observed instruments (G06-06).  Broker symbol names (suffixes etc.) are mapped to canonical symbols
  by market data (01).

## 3. Time

### 3.1 The three times (S01-ASG-11)

| name | meaning | where |
|---|---|---|
| **event time** | when something happened at its source (broker timestamp converted to UTC) | `Tick.event_time_ns`, `OrderEvent.event_time_ns`, `Evidence.anchor_time_ns` |
| **receive time** | when the GIE process observed it on its own clock - the *availability* instant | `Tick.receive_time_ns`, `OrderEvent.receive_time_ns` |
| **decision time** | clock reading at which a GIE component created a record | `decision_time_ns` |

Every record that GIE derives from market data additionally carries the causality watermark
`as_of_time_ns`: the receive time of the newest input it depends on.  Invariant **CAUSAL-1**:
`as_of_time_ns <= decision_time_ns`.  A producer MUST be *prefix invariant*: the record it emits
at a given watermark depends only on inputs received at or before that watermark, so truncating
the input after the watermark cannot change the record (S02-ACC-01, S10-ACC-02).

End-to-end delay of a decision is `decision_time_ns - as_of_time_ns` on the run clock; the compute
time actually spent is measured separately with `gie.core.latency` (section 3.5).

### 3.2 Clock (the `Clock` contract)

All code obtains time from an injected `gie.core.clock.Clock`:

```python
class Clock(Protocol):
    mode: ClockMode            # REPLAY or SYSTEM
    def now_ns(self) -> int    # instant; never decreases
    def monotonic_ns(self) -> int   # for elapsed time only; never decreases
```

Components MUST NOT read the operating-system clock directly (`time.time`, `datetime.now`, ...).
`ReplayClock` is virtual time: it moves only when the replay driver advances it, never backwards
(`ClockRegressionError`), and fires timers in `(due time, registration order)`; while a timer runs,
`now_ns()` equals that timer's due time.  `SystemClock` is the UTC system clock for paper mode,
clamped so that it never decreases, and it counts observed backward steps.
`ClockReading` (contract name `Clock`) is the serialisable reading used for heartbeats and
data-freshness displays.  A REPLAY run uses a REPLAY clock and a PAPER run the SYSTEM clock (CLK-2).

### 3.3 Freshness (S00-ASG-23, G03-12, G06-04)

Freshness is evaluated against the injected clock and **fails closed at the boundary**:

* *Expiry.*  `Evidence`, `Candidate`, `TradePlan` and `RiskDecision` carry `valid_until_ns`.  A
  record is usable iff `now_ns < valid_until_ns`; at the expiry instant it is already expired.
* *Age.*  Data with a reference instant is fresh iff `0 <= now_ns - reference_ns < max_age_ns`.
  The limits (`max_age_ns` per feed and per decision stage) are research parameters.
* *Future data.*  A reference instant later than `now_ns` is never "very fresh".  It is clock skew
  or look-ahead leakage and raises `FutureDataError`.  A tolerance for cross-host skew exists for
  paper mode and MUST be `0` in replay.

A consumer MUST check freshness immediately before it acts.  A stale or expired input MUST NOT
lead to an action that creates or increases risk; the consumer records the rejection with a reason
code.  Helpers: `gie.core.freshness`.

### 3.4 Trading day (G05-10)

`trading_day` is the calendar date of the instant in `Europe/Prague` (CET/CEST), because the
account's daily limits reset at Prague midnight.  A trading day is the half-open interval from
Prague midnight to the next Prague midnight and is 23 or 25 hours long on DST changes
(`gie.core.timeutil.trading_day`, `trading_day_bounds_ns`).  Whether the actual account resets at
exactly this instant is an account rule to be verified by session 08 (open item U-04).  Sessions,
holidays, rollover and weekend closures are the broker calendar owned by session 01 and are not
part of the trading-day definition.

### 3.5 Latency measurement (G03-10, G03-11)

Durations are measured with `time.perf_counter_ns` through `gie.core.latency.LatencyRecorder`,
which reports count, min, mean, p50, p95, p99 and max (nearest-rank percentiles on observed
samples).  The targets of G03 (tick < 1 ms, protection < 5 ms, tactical < 20 ms) are *proposed
research parameters* (`specifications/research_parameters.toml`); nothing in the contracts
promises a latency.

## 4. Identifiers (S00-ASG-22)

* Form: `<prefix>_<32 lower-case hex>`; the hex part is the first 128 bits of the SHA-256 of the
  canonical JSON (section 7) of `[prefix, [identity parts...]]`.  Prefixes are listed in
  `FIELDS.md`.  Identifiers are opaque: consumers compare them, never parse them.
* **Deterministic.**  Identity parts are strings, integers, exact decimals and lists of those.
  Floats, wall-clock readings taken at creation, random numbers (`uuid4`) and unpersisted process
  counters MUST NOT be used.  Replaying the same inputs therefore reproduces the same identifiers.
* **Recomputable.**  `gie.contracts.identity` holds the one identity function per identifier.
  Where all identity parts are fields of the record, the record verifies its own identifier on
  construction (`EVD-1`, `CND-1`, `PLN-1`, `PLN-2`, `RSK-1`, `RSK-3`, `OEV-1`, `MDL-1`).
* **Market events de-duplicate by identity** (G02-02, S02-ACC-02).
  `market_event_id(kind, symbol, timeframe, anchor_time_ns, level_prices)` is shared by every
  producer that observes the same event.  Two `Evidence` records with equal `market_event_id` are
  the same market event seen by different engines and MUST be counted once downstream.
* **Orders are idempotent by identity** (G08-07).
  `client_order_id(run, origin, intent, attempt)` is recomputed identically after a crash, so a
  re-sent request is recognised as a duplicate.  `order_event_id(client_order_id, order_seq)`
  identifies each lifecycle event.
* A stream position is `(stream name, seq)`; a tick is identified by `(source, symbol, seq)`.

## 5. Error semantics (S00-ASG-24)

All errors derive from `gie.core.errors.GieError` and carry a stable machine `code`, a message and
a `details` dictionary of JSON scalars.  The codes are listed in `FIELDS.md`.

1. **Fail closed.**  When a `GieError` occurs while a component is deciding whether to create or
   increase risk, it MUST NOT create that risk.  Actions that only reduce risk (cancel a pending
   entry, close a position, tighten a stop) remain permitted.
2. **Never repair input.**  Invalid input is rejected with `ContractValidationError` naming the
   field path and the violated rule.  It is never clamped, defaulted, rounded or coerced.  Every
   record that exists in memory is valid: validation runs on construction and on decoding.
3. **Unknown is not zero.**  Unavailable data is represented as `null` / `MISSING` and propagates as
   "unknown".  Consumers MUST NOT substitute a neutral value.
4. **Unknown vocabulary is refused.**  An unknown enum value, contract name or MAJOR version is an
   error, not a warning (section 8).
5. **Errors are data.**  `GieError.to_dict()` is the journal form.  Errors are never swallowed; a
   component either handles one explicitly according to rule 1 or lets it propagate.
6. **Lifecycle violations.**  `validate_order_transition`, `validate_position_transition` and
   `validate_model_transition` raise `IllegalTransitionError`; the offending record MUST NOT be
   journaled as valid history.

Rejections that are part of normal operation (a plan refused by risk, a candidate that fails the
expectancy gate) are **not** errors: they are ordinary records with `REJECTED` status and reason
codes, so that learning can study them (G07-04).

## 6. Event ordering (S00-ASG-25)

### 6.1 Streams and sequence numbers

Every record belongs to exactly one stream and carries the producer's sequence number `seq`,
starting at 0 and increasing by exactly 1 per record of that stream:

| contract | stream name | ordering time |
|---|---|---|
| `Tick` | `tick/<source>/<symbol>` | `receive_time_ns` |
| `OrderEvent` | `order_event/<producer>` | `receive_time_ns` |
| `Clock` | `clock/<producer>` | `wall_time_ns` |
| `ModelVersion` | `model_version/<producer>` | `status_time_ns` |
| all others | `<contract>/<producer>` | `decision_time_ns` |

`gie.core.ordering.SequenceTracker` classifies each arrival: next in sequence - accepted; same
position and identical content - an idempotent **duplicate** that is dropped; same position and
different content - `SequenceConflictError`; a skipped position - `SequenceGapError` (or accepted
and reported where gaps are tolerated); an older position that can no longer be verified -
`OutOfOrderError`.  A refused arrival never changes state.

Within a stream the ordering time MUST be non-decreasing.

### 6.2 Total order of a run

All records of a run are processed in ascending `EventKey = (time_ns, rank, stream, seq)`:

1. `time_ns` is the ordering time above, i.e. the **availability** instant, never the source event
   time.  This is the rule that prevents look-ahead in replay (S10-ACC-02): information is processed
   when it could have been known, not when it happened.
2. `rank` breaks ties between kinds of information that became available at the same instant:
   clock, tick, order event, position, risk decision, evidence, candidate, trade plan, model version
   (`StreamRank`).  Execution facts therefore precede intelligence derived at the same instant.
3. `stream` and `seq` make the order total and deterministic.

`gie.contracts.ordering.ordering_key(record)` computes the key; `gie.core.ordering.merge_ordered`
merges sorted streams deterministically and refuses unsorted or ambiguous input.  Replay of the
same inputs MUST reproduce the same sequence of records byte for byte (S01-ACC-08, S07-ACC-08,
S10-ACC-01).

### 6.3 Out-of-order market data

`Tick.seq` is assigned at ingest in arrival order, so it is always increasing.  A quote whose
source timestamp is older than its predecessor's keeps its arrival position and is flagged
`OUT_OF_ORDER`; nothing is reordered after the fact, because consumers have already seen the
earlier arrivals.

## 7. Serialisation

* The wire form of a record is one JSON object: the envelope `contract` (name) and
  `schema_version` (`MAJOR.MINOR.PATCH`) plus all fields.  Optional fields are always written, as
  explicit `null`.
* Decimals are JSON **strings** in plain notation: optional `-`, digits, optional fraction; no
  exponent, no `+`, no leading zeros, at most 18 integer and 12 fractional digits.  Writers emit the
  canonical form without trailing fractional zeros (`"2650.5"`); readers also accept trailing zeros.
* Instants, durations and sequence numbers are JSON **integers**.  Readers MUST parse JSON integers
  exactly as 64-bit integers; nanosecond instants exceed 2^53, so JavaScript consumers need a
  BigInt-aware parser.
* Floats are JSON numbers; enum members are their string value; lists are arrays; nested value
  objects are objects without an envelope.
* **Canonical JSON** is RFC 8785 (JCS) - keys sorted by UTF-16 code units, no whitespace,
  ECMAScript number formatting for non-integers - with the single deviation that integers are
  written as exact digits whatever their size.  Canonical bytes are what is journaled, hashed
  (`fingerprint`) and compared in replay-determinism tests.  Lists whose order carries no meaning
  have a defined canonical order (`x-gie-canonical-order` in the schema); producers may supply any
  order.
* Parsing is strict: UTF-8 only, no duplicate keys, no NaN/Infinity, no integers beyond 64 bits.

## 8. Versioning and compatibility (S00-ACC-02)

Each contract has its own semantic version; the contract set has `CONTRACT_SET_VERSION`.

| change | bump | examples |
|---|---|---|
| documentation only | PATCH | field description text |
| additive | MINOR | new optional field; new enum value |
| anything else | MAJOR | removed/renamed field, new required field, changed type, bound, pattern, unit, optionality or invariant |

Reader rules (`gie.contracts.codec.from_wire`):

* different MAJOR - refused with `ContractVersionError`;
* same MAJOR, message MINOR not newer than the reader - strict: an unknown field is an error, a
  missing optional field is `null`;
* same MAJOR, message MINOR newer than the reader - unknown fields are ignored (a newer minor may
  only have added optional fields); unknown enum values are still refused;
* PATCH never affects decoding.

Because unknown enum values fail closed, a producer MUST NOT emit a new enum value until every
consumer of that stream runs a version that knows it; rolling such a change out is a coordinator
integration step.

Mechanical enforcement: the JSON Schemas are generated from the code and committed together with
`gie/contracts/schemas/LOCK.json` (SHA-256 per released schema).  CI
(`python -m gie.contracts.schema_gen --check`) fails if a released schema was edited, if the code
no longer matches the published schema of its declared version, if a contract disappeared, or if
the difference to an earlier version of the same MAJOR is larger than the declared bump
(`gie.contracts.compat`).  Golden messages of every released version are kept under
`tests/gie/golden/` and must decode forever within their MAJOR.

While the lock status is `PROPOSED` (before owner approval) version 1.0.0 may still be corrected in
place; once the status is `APPROVED` every change requires a version bump and a decision-log entry.

## 9. Safety rules that are built into the contracts

These are structural: a record that violates them cannot be constructed or decoded.

| rule | where | source |
|---|---|---|
| No live mode or real venue exists in the vocabulary | `RunMode`, `ExecutionVenue`, `OrderEvent.venue` | G08-03, G08-10, M-AUTH-13/14 |
| Only XAUUSD can be planned, ordered or held by GIE | `Candidate`, `TradePlan`, `OrderEvent`, `Position` (POS-3) | G01-01, G06-07 |
| Every plan has a hard stop on the losing side and targets on the winning side | PLN-5, PLN-6 | G04-12 |
| Every entry order carries its plan, its risk approval and an attached protective stop | OEV-5 | G04-12, G08-08 |
| A rejection grants nothing; an approval is bound to one plan, reserves risk and expires | RSK-2, RSK-3, RSK-8 | G04, S08-ACC-06 |
| Emergency halt never approves | RSK-5 | G05-04 |
| At or below the daily profit floor nothing is approved | RSK-7 | G05-08 |
| A stop is never wider than the initial stop and never removed | POS-4, `validate_position_transition` | G04-11 |
| A protective stop order may only be tightened when replaced | `validate_order_transition` (REPLACED rules) | G04-11 |
| No replacement can add risk: an order's volume never grows, and an entry order's prices and attached stop are fixed to what risk approved | `validate_order_transition` (REPLACED rules) | G04-09, G04-10, S07-ASG-04 |
| An execution adapter is only reachable through the routing guard | `GuardedExecutionAdapter` | G08-03 |
| A position is never increased | POS-2, `validate_position_transition` | G04-09, G04-10 |
| The executable net P&L peak never decreases | POS-5, `validate_position_transition` | G04-13 |
| A record cannot depend on data received after it was created | CAUSAL-1 | G10, S10-ACC-02 |
| Missing evidence has null scores; inactive evidence has zero scores | EVD-2, EVD-5 | S02-ACC-03 |
| A model can be ACTIVE only after SHADOW and only with a recorded owner approval; rollback re-activates the retired parent, again only with a recorded approval | MDL-2, MDL-6, `MODEL_STATUS_TRANSITIONS` | G07-09, G07-10, G07-11 |
| Every record names its run, mode and the origin of its data (fixtures are labelled) | `RunScoped` header, `Tick.origin` | COM-07, M-AUTH-07 |

The paper-only routing lock itself is specified in `gie/core/routing_lock.py` and
`docs/ARCHITECTURE.md` section "Routing lock".

## 10. Service interfaces

`gie.contracts.interfaces` defines the `Protocol` classes through which subsystems call each
other: `TickSink`, `RecordSink`, `RiskGate`, `ExecutionAdapter` and `ModelRegistry`.  They are part
of the shared contract surface and change under the same rules.  An execution engine never holds
a raw `ExecutionAdapter`; it holds a `gie.contracts.guarded.GuardedExecutionAdapter`, which
authorises the adapter with the routing guard before every call and checks every request and
every returned event.  All calls are synchronous and
MUST NOT wait for the network, a model in training or the dashboard (G03-05, G03-06, S06-ASG-07).

## 11. Provisional elements

The following are deliberately provisional because the existing system could not be audited in
Session 00 (blocker B-001).  They are valid, tested contract elements, but their vocabulary is
expected to be refined by a MINOR/MAJOR change once the six engines have been inspected:

* `EvidenceStage` values and the reserved evidence kinds / level roles;
* `MarketSession` values (session boundaries are broker-calendar data of session 01);
* `TickFlag` values (thresholds and detection are owned by session 01);
* `ModelKind` values;
* reason-code vocabularies (open strings matching `^[A-Z][A-Z0-9_]{1,47}$`; each producing
  subsystem publishes its dictionary).
