# GIE shared contracts - normative specification

Contract set version **1.0.0** - status **PROPOSED** (awaiting owner approval at the Session 00
review gate; see `docs/DECISION_LOG.md` D-007).  Revision 2: reworked after the independent
in-session review (`reviews/session_00/review_round1_dispositions.yaml`).

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

Ten contracts are shared by all subsystems.  Nine are named by the brief for Session 00; the
tenth, `RunManifest`, makes the run identity that all of them refer to verifiable.

| contract | produced by | consumed by | what it is |
|---|---|---|---|
| `RunManifest` | composition root (00) | all | identity and reproducibility record of one run |
| `Tick` | 01 market data | 02, 03, 04, 05, 06, 07, 08, 10, 11 | one bid/ask quote of one instrument from one feed |
| `Evidence` | 02 strategy, 03 intermarket | 04, 05, 09, 11 | one causal, directional observation |
| `Candidate` | 04 prediction (only) | 05, 09, 11 | one scorer's view of one opportunity |
| `TradePlan` | 05 decision | 08, 07, 09, 11 | one executable way to trade an opportunity |
| `RiskDecision` | 08 risk | 05, 07, 09, 11 | approval / reduction / rejection of a plan |
| `OrderEvent` | 07 execution | 05, 06, 08, 09, 10, 11 | one step in the life of one simulated order |
| `Position` | 07 execution | 05, 06, 08, 09, 10, 11 | snapshot of one position on the account |
| `ModelVersion` | 09 learning (registry) | 04, 05, 08, 11 | one immutable model artifact and its promotion status |
| `Clock` | 00 core | all | the time source, the scheduler and the serialisable reading |

```
Tick --> Evidence --> Candidate --> TradePlan --> RiskDecision --> OrderEvent --> Position
 01      02 / 03        04            05             08               07            07
                                                                       ^
                                              06 reflex (per tick) ----+   protective orders only
ModelVersion (09) is referenced by Candidate and TradePlan.   Clock (00) is injected everywhere.
```

A subsystem MUST NOT redefine, copy, subclass or locally extend a shared contract (COM-06); the
framework refuses a contract class defined outside `gie.contracts`.

**Shared types that are still missing.**  The independent review found that further shared
types are needed before later sessions can finish: `Candle`, the broker calendar / session
classifier, `InstrumentSpec`, `AccountSnapshot`, single-valued observations with release
vintages, news and calendar events, a market-condition / trading-restriction record, the
decision-stream record of session 05, the outcome / label record, and the per-flag tick usability
policy.  They are **not** defined here: their content depends on the audit of the existing system
and on broker facts, and inventing them would be inventing data.  They are listed with their
proposing session and the gate before which each is required in `docs/CHANGE_REQUESTS.md`
(CR-003, CR-005) and in `specifications/dependency_graph.yaml` (`contract_extensions`).  Each is
added by the coordinator through the same framework, registry, schema lock and tests.

Records are **immutable facts**.  Nothing is ever updated in place: a change is a new record
(`revision + 1`, or the next `order_seq`) that refers to the one it replaces.

## 2. Units

| quantity | representation | field naming | notes |
|---|---|---|---|
| instant | `int` nanoseconds since 1970-01-01T00:00:00 UTC | `*_time_ns`, `*_until_ns` | range `0 .. 2^63-1` |
| duration | `int` nanoseconds, `>= 0` | `*_ns` documented as duration | |
| price | exact decimal, `> 0` | `*_price`, `bid`, `ask` | USD per troy ounce for XAUUSD; quote currency per unit otherwise |
| level of a non-price series | exact decimal, any sign | `PriceLevel.price`, `PriceCondition.price` | e.g. a real yield; must be `> 0` for XAUUSD |
| volume | exact decimal | `*_lots`, `*_volume` | broker lots; lot size is broker data, not a contract constant |
| money | exact decimal | `*_amount` | account currency, given by `account_currency` (ISO 4217) |
| fraction | exact decimal or float | `*_frac` | `0.05` means 5 %; percent numbers are never used |
| R multiple | float | `*_r` | result divided by the amount risked; always **net of costs** |
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
* A value that is unknown or not applicable is `null`.  `0` always means the number zero.  An
  *unscored* candidate has `expected_net_r = null`; *missing* evidence has `null` scores, never `0`
  (S02-ACC-03); a feature whose value is absent or `null` is unknown, never 0.
* **What 1 R is.**  On a `Candidate`, 1 R is the price distance `risk_unit_price` (the reference
  stop distance the model assumed).  On a `TradePlan` and a `PlanEstimate`, 1 R is the plan's own
  risk distance (effective entry to stop).  Plan-level estimates come from the plan scorer
  (`PlanScorer`), never from rescaling candidate numbers.  On a `Position`, 1 R is
  `initial_risk_amount`.
* **Uncertainty** has two separate meanings and two fields: `net_r_stdev` is the dispersion of a
  single outcome; `expected_net_r_stderr` (with `support_count`) is the uncertainty of the estimate
  itself.  A scored record states at least one of them.  Which of them a gate uses is a research
  decision (open item U-16).
* **Direction of evidence.**  `bullish_score`, `bearish_score`, levels and invalidation of an
  `Evidence` record always describe the instrument named in its `symbol`.  They are never
  pre-translated into a gold direction; how an observation on another instrument relates to gold is
  learned downstream (G06-08, G06-09: never assume fixed correlations).
* The only tradable symbol is `XAUUSD` (G01-01, G06-07).  `Candidate`, `TradePlan` and `OrderEvent`
  can only carry `XAUUSD`; so can a `Position` owned by GIE.  `Tick` and `Evidence` may describe other
  observed instruments (G06-06).  Broker symbol names are mapped to canonical symbols by market data.

## 3. Time

### 3.1 The times of a record (S01-ASG-11)

| name | meaning | where |
|---|---|---|
| **event time** | when something happened at its source (source timestamp converted to UTC) | `Tick.event_time_ns`, `OrderEvent.event_time_ns`, `Evidence.anchor_time_ns` |
| **receive time** | when the GIE process observed it on its own clock - the *availability* instant | `Tick.receive_time_ns`, `OrderEvent.receive_time_ns` |
| **decision time** | clock reading at which a GIE component created a record | `decision_time_ns` |
| **market-data watermark** | receive time of the newest market data the record depends on, directly or through its inputs | `as_of_time_ns` |
| **observable time** | receive time of the input from which a revision's state could first be determined | `Evidence.observable_time_ns` |

Invariants: `as_of_time_ns <= decision_time_ns` (**CAUSAL-1**) and, on evidence,
`observable_time_ns <= as_of_time_ns`.

* A producer MUST be *prefix invariant*: the record it emits depends only on inputs received at
  or before its watermark.  The acceptance test (S02-ACC-01) compares, for the same run
  configuration, the canonical bytes of the records produced from the full input with those
  produced from the input truncated after input *k* (identified by its ordering key): every record
  with a watermark at or before input *k* MUST be identical.
* **End-to-end delay** of a decision is `decision_time_ns - as_of_time_ns`.  **Notification delay**
  of evidence is `decision_time_ns - observable_time_ns` (S02-ACC-05, "no five-minute notification
  delay").  In replay, virtual time does not advance while code runs, so both are zero unless the
  run models latency; the replay form of the notification-delay test is that a revision is emitted
  in the same processing step as the input whose receive time equals its `observable_time_ns`.
* `check_derivation(child, *inputs)` (`gie.contracts.causality`) checks the rules that relate a
  derived record to its inputs: same run, not created before the inputs were available, watermark
  covers the inputs, does not outlive them, origin not better than theirs.

### 3.2 Clock and scheduler (the `Clock` contract)

All code obtains time from an injected `gie.core.clock.Clock` and schedules future work through
an injected `gie.core.clock.Scheduler`:

```python
class Clock(Protocol):
    mode: ClockMode                 # REPLAY or SYSTEM
    def now_ns(self) -> int         # instant; never decreases
    def monotonic_ns(self) -> int   # for elapsed time only; never decreases
    regressions_observed: int       # backward steps of the time source seen so far (0 in replay)

class Scheduler(Protocol):
    def call_at(self, due_ns, callback) -> int      # absolute instant
    def call_after(self, delay_ns, callback) -> int # relative; same behaviour in replay and paper
    def cancel(self, handle) -> None
```

Components MUST NOT read the operating-system clock, sleep or start timer threads
(`time.time`, `datetime.now`, `threading.Timer`, ...); CI enforces this.

* `ReplayClock` is virtual time and the scheduler of a replay: it moves only when the replay
  driver advances it, never backwards, and fires timers in `(due time, registration order)`; while a
  timer runs, `now_ns()` equals that timer's due time.
* `SystemClock` is the UTC system clock for paper mode.  It never decreases **and never stands
  still**: if the operating system steps the clock backwards, time continues at the pace of the
  monotonic source, so freshness and expiry keep failing closed; each step is counted in
  `regressions_observed`.  Whether risk may be created after a clock step is a rule of session 08
  (fail closed by default).
* `PollingScheduler` is the scheduler of a paper run: the loop calls
  `run_due(limit_ns=<receive time of the next input>)` before delivering that input.
* **Timer rule (both modes):** a timer never fires early; a timer due at instant *t* fires before
  any input that became available at *t* is delivered.
* **Background work in replay.**  Results of asynchronous strategic work and modelled decision
  latency are delivered as records at a *virtual* instant scheduled through the scheduler (with a
  modelled, seeded delay that is part of the run configuration) - never "when a thread finishes".
  Measured latency (section 3.5) is evidence only; it never enters a record or influences a decision
  in replay.
* `ClockReading` (contract name `Clock`) is the serialisable reading.  A REPLAY run uses a REPLAY
  clock and a PAPER run the SYSTEM clock (CLK-2).

### 3.3 Freshness (S00-ASG-23, G03-12, G06-04)

Freshness is evaluated against the injected clock and **fails closed at the boundary**:

* *Expiry.*  `Evidence`, `Candidate`, `TradePlan`, `RiskDecision` and resting entry orders carry
  `valid_until_ns`.  A record is usable iff `now_ns < valid_until_ns`; at the expiry instant it is
  already expired.
* *Age.*  Data with a reference instant is fresh iff `0 <= now_ns - reference_ns < max_age_ns`.
* *Future data.*  A reference instant later than `now_ns` is never "very fresh": it is clock skew
  or look-ahead leakage and raises `FutureDataError`.  A tolerance for cross-host skew exists for
  paper mode only; the clock-based helpers (`require_fresh_now`, ...) refuse any tolerance in replay
  and bound it to one second.

**Reference instants.**  A source event time is never the freshness reference.

| what is checked | reference instant | meaning |
|---|---|---|
| market data of a stream | `receive_time_ns` of the newest accepted tick (`feed_silence_ns`) | *feed silence*: how long nothing arrived |
| transport delay of one tick | `receive_time_ns - event_time_ns - estimated source clock offset`, evaluated by 01 at ingest | *source delay*; expressed by the flag `DELAYED` |
| evidence, candidate, plan, approval | `valid_until_ns` | expiry only |
| a derived record's inputs | `as_of_time_ns` | age of the market data behind a decision |

**Expiry propagates.**  A derived record MUST NOT outlive its inputs:
`entry order <= risk approval <= plan <= candidate <= every evidence it uses`
(`check_derivation`, `validate_entry_authorization`).  A consumer MUST check freshness immediately
before it acts.  A stale or expired input MUST NOT lead to an action that creates or increases
risk; the consumer records the rejection with a reason code.  The limits themselves (`max_age_ns`
per feed and stage) are research parameters; none is invented in Session 00.

### 3.4 Trading day (G05-10)

`trading_day` is the calendar date of the instant in `Europe/Prague` (CET/CEST), because the
account's daily limits reset at Prague midnight.  A trading day is the half-open interval from
Prague midnight to the next Prague midnight and is 23 or 25 hours long on DST changes
(`gie.core.timeutil.trading_day`, `trading_day_bounds_ns`).  Whether the actual account resets at
exactly this instant is an account rule to be verified by session 08 (open item U-04).  Sessions,
holidays, rollover and weekend closures are the broker calendar owned by session 01.

### 3.5 Latency measurement (G03-10, G03-11)

Durations are measured with `time.perf_counter_ns` through `gie.core.latency.LatencyRecorder`,
which reports count, min, mean, p50, p95, p99 and max (exact nearest-rank percentiles on observed
samples).  The targets of G03 are *proposed research parameters*; nothing in the contracts promises
a latency.

## 4. Identifiers (S00-ASG-22)

* Form: `<prefix>_<32 lower-case hex>`; the hex part is the first 128 bits of the SHA-256 of the
  canonical JSON (section 7) of `[prefix, [identity parts...]]`.  Identifiers are opaque: consumers
  compare them, never parse them.
* **Deterministic.**  Identity parts are strings, integers, exact decimals and lists of those.
  Floats, wall-clock readings taken at creation, random numbers and unpersisted counters MUST NOT be
  used.  Replaying the same inputs reproduces the same identifiers.
* **Recomputable and verified.**  `gie.contracts.identity` holds the one identity function per
  identifier (the table of identity parts is in that module and is published in every schema under
  `x-gie-behaviour`).  Every identifier whose parts are fields of the record is verified by the
  record on construction: `market_event_id`, `evidence_id`, `opportunity_id`, `candidate_id`,
  `plan_id`, `alternative_group_id`, `decision_id`, `reservation_id`, `client_order_id`,
  `order_event_id`, GIE `position_id`, `model_version_id`, `run_id`, and every `supersedes_*` link.

**Market events and exact de-duplication (G02-02, S02-ACC-02).**

* `market_event_id = f(kind, symbol, timeframe, anchor_time_ns, prices of the identity levels)`.
  A level is an identity level when its `identity` flag is true.  Two engines that report the same
  kind of event on the same symbol and timeframe, anchored at the same instant with the same
  identity levels, obtain the same id.  Which levels are identity levels and what the anchor is, per
  kind, is fixed by the feature dictionary of session 02 and MUST be the same for every engine
  (default for candle-defined events: the open time of the defining candle).  The identity never
  changes between revisions (`validate_evidence_transition`).
* INACTIVE and MISSING records describe no market event.  They carry the engine-scoped
  `evaluation_slot_id` instead, so a known "no" of one engine and an "unknown" of another can never
  be merged.
* `group_by_market_event(records)` is the one de-duplication function: one group per
  `market_event_id`, a deterministic representative, and the rule that an event **counts once** -
  scores of several engines for the same event are never summed.

**Opportunities, candidates and alternatives.**

* `opportunity_id = f(setup_type, side, horizon, market_event_ids)` is stable: it does not change
  when evidence is revised, when another engine confirms the same event, or when another model scores
  it.
* `candidate_id = f(opportunity_id, scoring model or none, revision)`: an active model, a shadow
  model and a baseline score the same opportunity side by side without colliding.
* `alternative_group_id = f(opportunity_id)`: every plan for an opportunity, whatever its
  candidate revision or scorer, is in one group, and at most one plan of a group is ever executed
  (enforced by the lifecycle ledger on entry orders).

**Orders.**  The idempotency key of an entry is derived from its *risk decision*
(`client_order_id(run, risk_decision_id, "ENTRY", 0)`), so one approval can produce exactly one
entry order and a re-sent request is recognised as a duplicate (G08-07).  Protective and exit
orders derive theirs from the position and an `attempt` number.

**Scope of a stream position.**  A stream position is `(stream name, seq)`.  For run-scoped
records the stream key is `(run_id, stream name)` and `seq` starts at 0 and continues across
restarts of the same run.  `Tick.seq` is assigned once, when the tick is ingested into the tick
store, never restarts, and is preserved by replay; a backfill, re-import or re-flagging creates a new
versioned `source`.  `external_position_id` is derived from a locally configured account *alias*,
never from the broker login.

## 5. Error semantics (S00-ASG-24)

All errors derive from `gie.core.errors.GieError` and carry a stable machine `code`, a message and
a `details` dictionary of JSON scalars that can always be journaled.  Codes: `FIELDS.md`.

1. **Fail closed.**  When a `GieError` occurs while a component is deciding whether to create or
   increase risk, it MUST NOT create that risk.  Actions that only reduce risk remain permitted.
2. **Never repair input.**  Invalid input is rejected with `ContractValidationError` naming the
   field path and the violated rule.  It is never clamped, defaulted, rounded, stripped or coerced.
   Every record that exists in memory is valid: validation runs on construction and on decoding, and
   only exact instances of the registered contract classes are accepted anywhere.
3. **Unknown is not zero.**  Unavailable data is `null` / `MISSING` and propagates as "unknown".
4. **Unknown vocabulary is refused.**  An unknown enum value, contract name or MAJOR version is an
   error, not a warning.
5. **Errors are data.**  `GieError.to_dict()` is the journal form.  Errors are never swallowed.
6. **Sequence violations.**  The validators of section 9.2 raise `IllegalTransitionError`; the
   offending record never becomes part of a run's history (section 6.2).

Rejections that are part of normal operation (a plan refused by risk, a candidate that fails the
expectancy gate) are **not** errors: they are ordinary records with a status and reason codes, so
that learning can study them (G07-04).

## 6. Event ordering (S00-ASG-25)

### 6.1 Streams and sequence numbers

Every record belongs to exactly one stream and carries the producer's sequence number `seq`,
increasing by exactly 1 per record of that stream:

| contract | stream name | ordering time | rank |
|---|---|---|---|
| `RunManifest` | `run_manifest/<producer>` | `created_time_ns` | 0 |
| `Clock` | `clock/<producer>` | `wall_time_ns` | 5 |
| `Tick` | `tick/<source>/<symbol>` | `receive_time_ns` | 10 |
| `OrderEvent` (broker report) | `order_event/<producer>` | `receive_time_ns` | 20 |
| `Position` | `position/<producer>` | `decision_time_ns` | 30 |
| `Evidence` | `evidence/<producer>` | `decision_time_ns` | 40 |
| `Candidate` | `candidate/<producer>` | `decision_time_ns` | 50 |
| `TradePlan` | `trade_plan/<producer>` | `decision_time_ns` | 60 |
| `RiskDecision` | `risk_decision/<producer>` | `decision_time_ns` | 70 |
| `OrderEvent` (`*_REQUESTED`) | `order_event/<producer>` | `receive_time_ns` | 80 |
| `ModelVersion` | `model_version/<producer>` | `status_time_ns` | 90 |

Within a stream the ordering time MUST be non-decreasing.  A new shared contract obtains its
stream prefix, ordering-time field and rank in `gie.contracts.ordering.STREAMS` when the coordinator
adds it.  `gie.core.ordering.SequenceTracker` classifies each arrival: next in sequence - accepted;
same position and identical content - an idempotent **duplicate** that is dropped; same position and
different content - `SequenceConflictError`; a skipped position - `SequenceGapError`; an older
position that can no longer be verified, or a decreasing ordering time - `OutOfOrderError`.  A
refused arrival never changes state; `restore()` re-establishes a stream after a restart.
Tick streams tolerate gaps (a feed can lose data; the gap is reported and no new risk may be created
on it); order-event streams and all streams of GIE's own records are gap-free.

### 6.2 Input order and journal order

Two orders exist and answer different questions.

**Input order.**  A run is driven by *exogenous inputs*: timers, ticks (later also other
observations) and, when a recorded session is replayed, broker reports.  Inputs are delivered in
ascending `EventKey = (availability time, rank, stream, seq)`.  The availability time is the
receive time - never the source event time; this is the rule that prevents look-ahead in replay
(S10-ACC-02).  `merge_ordered` merges sorted input streams deterministically and refuses unsorted
or ambiguous input.

**Journal order - the total order of a run.**  Each input is processed to completion before the
next one is delivered (*run-to-completion step*).  Every record produced in the step is published
to the run's journal, which numbers records in publication order (`journal_seq`).  That number is
the total order of the run.  It is causal by construction - a record is always published after the
records it was derived from, also when a whole decision chain shares one instant, which is the
normal case in replay - and it is reproducible because processing is deterministic.  Replaying the
same inputs MUST reproduce the journal byte for byte (S01-ACC-08, S07-ACC-08, S10-ACC-01).
Consumers that read history (09, 10, 11, 12) read the journal in this order (`RecordSource`).

The ranks of GIE's own records follow the data flow, so that `EventKey` remains a causal fallback
for ordering records that come from different journals.

A journal (`gie.contracts.journal`) belongs to exactly one run; its first record is the
`RunManifest`.  Before a record is appended it MUST pass: exact registered contract type and the
journal's run and mode; its stream sequence; the sequence rules of its lifecycle chain (section
9.2); and the look-ahead guard - the market-data watermark of a derived record may not lie beyond
the newest tick that was journaled.  A refused record leaves the journal unchanged.
`MemoryJournal` and the durable `FileJournal` (one canonical JSON line per record, replayed and
verified when reopened; a torn last line after a crash is detected and removed only by an explicit
repair) are the reference implementations.

### 6.3 Market data arriving out of order

`Tick.seq` is assigned at ingest in arrival order.  A quote whose source timestamp is lower than
the highest source timestamp seen so far on its stream keeps its arrival position and is flagged
`OUT_OF_ORDER`; nothing is reordered after the fact.  A quote whose source fields all equal those of
a tick already ingested within the look-back window is flagged `DUPLICATE`.  For imported history
without a recorded receive time, `receive_time_ns = max(receive time of the previous tick of the
stream, event_time_ns + modelled feed delay)` and the flag `RECEIVE_TIME_MODELLED` is mandatory;
results derived from such streams are never evidence about latency (G09-10).  What a consumer may
do with a flagged tick - per flag and per purpose - is a single shared policy still to be defined
by session 01 before any consumer uses ticks (CR-005).

## 7. Serialisation

* The wire form of a record is one JSON object: the envelope `contract` (name) and
  `schema_version` (`MAJOR.MINOR.PATCH`) plus all fields.  Optional fields are always written, as
  explicit `null`; a key that is missing from a message whose version already has the field is an
  error.
* Decimals are JSON **strings** in plain notation: optional `-`, digits, optional fraction; no
  exponent, no `+`, no leading zeros, at most 18 integer and 12 fractional digits.  Writers emit the
  canonical form without trailing fractional zeros; readers also accept trailing zeros.
* **Number tokens do not carry their type; the field type decides.**  Instants, durations, counts
  and sequence numbers are JSON integers and MUST be parsed exactly as 64-bit integers (nanosecond
  instants exceed 2^53: JavaScript consumers need a BigInt-aware parser).  Float fields may appear as
  integer literals (`1.0` is written `1`; `1e19` is written `10000000000000000000`).  An integer
  literal beyond 64 bits is accepted only if it is exactly the canonical rendering of a double.
* Enum members are their string value; lists are arrays; nested value objects are objects without
  an envelope.  Free text is printable text (no control characters).
* **Canonical JSON** is RFC 8785 (JCS) - keys sorted by UTF-16 code units, no whitespace,
  ECMAScript number formatting for non-integers - with the single deviation that integers within 64
  bits are written as exact digits.  Canonical bytes are what is journaled and compared in
  replay-determinism tests.  Lists whose order carries no meaning have a defined canonical order
  (`x-gie-canonical-order` in the schema); producers may supply any order.
* The **fingerprint** of a record (duplicate detection, plan binding) is the SHA-256 of contract
  name, MAJOR version and fields; MINOR and PATCH are excluded so that compatible versions agree.
* Parsing is strict: UTF-8 only, no duplicate keys, no NaN/Infinity, no lone surrogates, no number
  that overflows or underflows a double, nesting depth at most 64.
* The JSON Schemas are **necessary but not sufficient**: decimal bounds, canonical order,
  cross-field invariants, lifecycle rules, the integer-token rule and the printable-text rule are
  carried as `x-gie-*` annotations and enforced by the reference implementation.
* Storage engines (for example the tick store of session 01) may use any encoding from which the
  canonical bytes are reproduced exactly; a round-trip test proves it.

## 8. Versioning and compatibility (S00-ACC-02)

Each contract has its own semantic version; the contract set has `CONTRACT_SET_VERSION`.

| change | bump | examples |
|---|---|---|
| documentation only | PATCH | field description text |
| additive | MINOR | new optional field; new enum value |
| anything else | MAJOR | removed/renamed field, new required field, changed type, bound, pattern, unit, optionality, invariant, or behaviour (transition table, identity rule, immutable fields, stream, rank); **any** change of the run-mode or venue vocabulary |

Reader rules (`gie.contracts.codec.from_wire`):

* different MAJOR - refused with `ContractVersionError`;
* same MAJOR, message MINOR not newer than the reader - strict: an unknown field is an error, and
  so is a missing field that the message's version already has;
* same MAJOR, message MINOR newer than the reader - unknown fields are ignored (a newer minor may
  only have added optional fields); unknown enum values are still refused;
* PATCH never affects decoding.

Because unknown enum values fail closed, a producer MUST NOT emit a new enum value until every
consumer of that stream runs a version that knows it; rolling such a change out is a coordinator
integration step.

Mechanical enforcement: the JSON Schemas are generated from the code - including the behavioural
surface (`x-gie-behaviour`: stream, ordering time, rank, identity rules, immutable fields, transition
tables) - and committed with `gie/contracts/schemas/LOCK.json` (SHA-256 per released schema).  CI
(`python -m gie.contracts.schema_gen --check`) fails if a released schema was edited, if the code no
longer matches the published schema of its declared version, if a contract disappeared, if the
difference to an earlier version of the same MAJOR is larger than the declared bump, or if the
run-mode / venue vocabulary differs from `[REPLAY, PAPER]` / `[SIMULATED]` - the last check is
independent of the lock status.  Golden messages of every released version are kept under
`tests/gie/golden/` and must decode forever within their MAJOR.

A MAJOR bump makes older journals unreadable by the new code.  It therefore requires a migration
tool and a journal cut-over, decided and recorded by the coordinator (not needed for 1.x).

While the lock status is `PROPOSED` (before owner approval) version 1.0.0 may still be corrected in
place; once the status is `APPROVED` every change requires a version bump and a decision-log entry.

## 9. Safety rules

### 9.1 Record invariants - a violating record cannot be constructed or decoded

| rule | where | source |
|---|---|---|
| No live mode or real venue exists in the vocabulary | `RunMode`, `ExecutionVenue`, checked by identity | G08-03, G08-10, M-AUTH-13, M-AUTH-19 |
| Only XAUUSD can be planned, ordered or held by GIE | `Candidate`, `TradePlan`, `OrderEvent`, POS-3 | G01-01, G06-07 |
| Every plan has a hard stop on the losing side and destinations on the winning side of its effective entry | PLN-5, PLN-6 | G04-12 |
| An ARMED or SELECTED plan has positive expected net R | PLN-11 | S05-ASG-11, S05-ACC-05 |
| Every entry order carries its plan, its risk approval, its alternative group, a reference price and an attached protective stop on the losing side of order price, reference price and fill | OEV-5, OEV-11 | G04-12, G08-08 |
| A resting entry order expires; a protective stop never does | OEV-13 | G03-12, S07-ACC-06 |
| Orders other than entries belong to a position and carry no plan or approval; a protective stop is a STOP order, a take profit a LIMIT order | OEV-6, OEV-8 | G04-09, G04-10 |
| One approval yields one entry order (the order id is derived from the decision) | OEV-7 | S08-ACC-06, G08-07 |
| A rejection grants nothing; an approval grants a positive size and risk, is bound to one plan revision and its content, reserves risk and expires | RSK-1..3, RSK-10 | G04, S08-ACC-06 |
| Emergency halt never approves | RSK-5 | G05-04 |
| At or below the daily profit floor nothing is approved; above it an approval risks at most the surplus | RSK-7, RSK-8 | G05-07, G05-08 |
| The risk fraction never understates the risk amount | RSK-9 | G04 |
| A GIE position has a stop on the losing side of its entry that is never wider than the initial stop | POS-4 | G04-11, G04-12 |
| A record cannot depend on market data received after it was created; retrieved historical cases are prior-only | CAUSAL-1, EVD-11, CND-11 | G10, S04-ASG-06, S10-ACC-02 |
| Missing evidence has null scores; inactive evidence has zero scores | EVD-3, EVD-5 | S02-ACC-03 |
| Every identifier that can be recomputed from the record is verified | section 4 | S00-ASG-22 |
| A model is ACTIVE only with a recorded owner approval and its evidence (report, criteria, shadow runs); a model fitted on fixture data is never *validated* | MDL-2, MDL-5, MDL-7, `validated` | G07-09, G07-10 |
| Every record names its run, mode and the origin of its data; a run names its configuration | `RunScoped` header, `Tick.origin`, `RunManifest` | COM-07, M-AUTH-07 |

### 9.2 Sequence rules - enforced when a record is published

These rules relate a record to its predecessor.  A single record cannot violate them; a sequence
can.  They are implemented by the `validate_*` functions and enforced by the
`LifecycleLedger`, through which **every journal and the order gateway admit records**.  A record
that breaks one is refused before it becomes part of a run's history.

| rule | validator | source |
|---|---|---|
| An order follows its state machine; terminal states are final | `validate_order_transition`, `ORDER_TRANSITIONS` | S07-ASG-01 |
| No replacement or reconciliation can add risk: volume never grows; an entry's prices and attached stop are fixed and its expiry only shortens; a protective stop only tightens and never gains an expiry | `validate_order_transition` | G04-09, G04-10, G04-11, S07-ASG-04 |
| At most one plan of an alternative group is ever executed | `LifecycleLedger` | S05-ACC-04 |
| A GIE position's stop never widens and is never removed; its volume never grows; its executable net P&L peak never decreases | `validate_position_transition` | G04-10, G04-11, G04-13 |
| A plan's stop never moves away from the entry between revisions; final statuses are final | `validate_plan_transition` | G04-11 |
| The identity of a market event never changes between evidence revisions | `validate_evidence_transition` | G02-02 |
| A model reaches ACTIVE only through SHADOW; a re-activation names the rolled-back version and needs a new approval; one ACTIVE version per model family | `validate_model_transition`, `validate_registry_history` | G07-09, G07-10, G07-11 |
| Within a trading day an emergency halt stays in force and the profit floor never decreases | `validate_daily_state_sequence` | G05-04, G05-06 |

### 9.3 Authorisation - enforced by the order gateway

An execution engine reaches an adapter only through
`gie.contracts.guarded.GuardedExecutionAdapter` (interface `OrderGateway`), which before every call
authorises the adapter with the routing guard and then checks the request:

* an **entry** needs its SELECTED plan and its unexpired risk approval
  (`validate_entry_authorization`): same run, same plan revision and plan content
  (`plan_fingerprint`), same alternative group, side, order type and price, the plan's stop attached,
  volume not above the granted volume, nothing expired, and the order not outliving plan or approval;
* **every other order** needs the open GIE position it belongs to and may only reduce it
  (`validate_reducing_order`): opposite side, volume not above the open volume, a protective stop
  covering the whole volume and never looser than the position's stop; the protective stop of an
  open position cannot be cancelled;
* GIE never places orders for positions of other systems.

The four layers of the paper-only routing lock are specified in `gie/core/routing_lock.py` and
`docs/ARCHITECTURE.md` section 6.

### 9.4 Other systems and learning

`Position` records with owner `EXTERNAL` describe positions of other systems on the same account.
They report what is observed - GIE's own monotonic rules do not apply to them - and they count
towards account-wide risk (G05-12).  Learning and model fitting use GIE-owned positions only
(G07-06, no manual-trade learning).

## 10. Service interfaces and the run loop

`gie.contracts.interfaces` defines the `Protocol` classes through which subsystems call each
other: `TickSink`, `TickSource`, `RecordSink`, `RecordSource`, `PlanScorer`, `RiskGate`,
`ExecutionAdapter`, `OrderGateway` and `ModelRegistry`.  They are part of the shared contract
surface and change under the same rules.  `RunContext` (`gie.contracts.run`) hands every component
its run manifest, clock, scheduler, research parameters and output sink.

**One step of a run** (replay loop owned by session 10, ordered input by session 01, paper loop by
the composition root): fire the timers due at or before the next input's availability instant;
advance the clock to it (replay); deliver the input to the sinks in the order *protective* (reflex,
simulated broker) -> *tactical* (risk, decision, execution) -> *strategic* (evidence, prediction);
publish every record of the step to the journal before the next input.  A sink that raises is
marked failed and creates no risk until recovered; the other sinks still receive the input.

All calls are synchronous and MUST NOT wait for the network, a model in training or the dashboard
(G03-05, G03-06, S06-ASG-07).  In paper, strategic consumers are fed through a bounded queue and
publish their results later; a late result can make evidence stale, never delay protection.

## 11. Provisional elements

The following are deliberately provisional because the existing system could not be audited in
Session 00 (blocker B-001).  They are valid, tested contract elements, but their vocabulary is
expected to be refined by a MINOR/MAJOR change once the six engines have been inspected:

* `EvidenceStage` values, the reserved evidence kinds and level roles, and the per-kind identity
  levels and anchor convention (owned by the session-02 feature dictionary);
* `Timeframe` values;
* `MarketSession` values (session boundaries are broker-calendar data of session 01);
* `TickFlag` values and their thresholds (owned by session 01);
* `ModelKind` values;
* reason-code vocabularies (open strings matching `^[A-Z][A-Z0-9_]{1,47}$`; each producing
  subsystem publishes its dictionary).
