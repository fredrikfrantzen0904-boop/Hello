# GIE shared contracts - normative specification

Contract set version **1.0.0** - status **PROPOSED** (awaiting owner approval at the Session 00
review gate; see `docs/DECISION_LOG.md` D-007).  Revision 3: reworked after two rounds of
independent in-session review (`reviews/session_00/review_round1_dispositions.yaml`,
`reviews/session_00/review_round2_dispositions.yaml`; what changed in the last rework:
`reviews/session_00/rework_2_change_notice.md`).

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
| `TradePlan` | 05 decision | 06, 07, 08, 09, 11 | one executable way to trade an opportunity |
| `RiskDecision` | 08 risk | 05, 07, 09, 11 | approval / reduction / rejection of a plan |
| `OrderEvent` | 07 execution (the only writer) | 05, 06, 08, 09, 10, 11 | one step in the life of one simulated order |
| `Position` | 07 execution | 05, 06, 08, 09, 10, 11 | snapshot of one position on the account |
| `ModelVersion` | 09 learning (registry) | 04, 05, 08, 11 | one immutable model artifact and its promotion status |
| `Clock` | 00 core | all | the time source, the scheduler and the serialisable reading |

```
Tick --> Evidence --> Candidate --> TradePlan --> RiskDecision --> OrderEvent --> Position
 01      02 / 03        04            05             08               07            07
                                                                       ^
                 06 reflex (per tick) -- ProtectionPort (tighten stop, exit) --+
ModelVersion (09) is referenced by Candidate and TradePlan.   Clock (00) is injected everywhere.
```

Order events have **one writer**, session 07 (execution engine and simulated broker).  The reflex
engine never writes an order event: it states an intent through `interfaces.ProtectionPort` and
reads the result from the journal (D-025).

A subsystem MUST NOT redefine, copy, subclass or locally extend a shared contract (COM-06); the
framework refuses a contract class defined outside `gie.contracts`.

**Shared types that are still missing.**  The independent reviews found that further shared
types are needed before later sessions can finish: `Candle`, the broker calendar / session
classifier, the per-flag tick usability policy, `InstrumentSpec` with the shared valuation
function, `AccountSnapshot` with the daily risk state, single-valued observations with release
vintages, news and calendar events, a market-condition / trading-restriction record, the
decision-stream record of session 05, the outcome / label record, the step driver, health events,
the run-configuration and research bookkeeping records, feed status, latency summaries, run and
learning results, the display projection, the reservation life cycle and the persistence
extensions of the journal.  They are **not** defined here: their content depends on the audit of
the existing system, on broker facts or on the design of the session that owns the knowledge, and
inventing them would be inventing data.  They are listed as CX-01 .. CX-19 with their proposing
session, their consumers and what each proposal must settle in `docs/CHANGE_REQUESTS.md` (CR-003,
CR-005) and in `specifications/dependency_graph.yaml` (`contract_extensions`).  Each is added by
the coordinator through the same framework, registry, schema lock and tests.

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

* Prices, volumes, money and the fractions of a risk decision (`risk_frac`, `daily_pnl_frac`,
  `profit_floor_frac`) and of a plan (`close_frac`) are exact decimals (`decimal.Decimal` in
  Python).  Binary floats MUST NOT be used for them.
* **Amounts decide, fractions report** (D-028).  A limit of the account is decided on amounts in
  the account currency: `daily_pnl_amount`, `profit_floor_amount`, `risk_amount`, open and pending
  risk.  That is what makes "at exactly the floor no new risk" (G05-08) exact.  The fractions on a
  risk decision are derived from the amounts and `reference_equity_amount` by
  `gie.core.canonical.fraction_of` - 12 fractional digits, the daily result rounded down, a floor
  or a risk rounded up, so a fraction never looks better than the amounts - and the record verifies
  the derivation (RSK-9).  A consumer MUST NOT decide a boundary on a fraction.
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
  itself.  A scored candidate states at least one of them or at least two quantiles of the outcome
  distribution (CND-5); a plan and a plan estimate state at least one of the two fields (PLN-12,
  EST-1).  Which of them a gate uses is a research decision (open item U-16).
* **Direction of evidence.**  `bullish_score`, `bearish_score`, levels and invalidation of an
  `Evidence` record always describe the instrument named in its `symbol`.  They are never
  pre-translated into a gold direction; how an observation on another instrument relates to gold is
  learned downstream (G06-08, G06-09: never assume fixed correlations).
* The only tradable symbol is `XAUUSD` (G01-01, G06-07).  `Candidate`, `TradePlan` and `OrderEvent`
  can only carry `XAUUSD`; so can a `Position` owned by GIE.  `Tick` and `Evidence` may describe other
  observed instruments (G06-06, G06-10 .. G06-17).  Broker symbol names are mapped to canonical symbols by market data.

## 3. Time

### 3.1 The times of a record (S01-ASG-11)

| name | meaning | where |
|---|---|---|
| **event time** | when something happened at its source (source timestamp converted to UTC) | `Tick.event_time_ns`, `OrderEvent.event_time_ns`, `Evidence.anchor_time_ns` |
| **receive time** | when the GIE process observed it on its own clock - the *availability* instant | `Tick.receive_time_ns`, `OrderEvent.receive_time_ns` |
| **decision time** | step time (section 3.2) at which a GIE component created a record | `decision_time_ns` |
| **market-data watermark** | receive time of the newest market data the record depends on, directly or through its inputs | `as_of_time_ns` |
| **observable time** | receive time of the input from which a revision's state could first be determined | `Evidence.observable_time_ns` |

Invariants: `as_of_time_ns <= decision_time_ns` (**CAUSAL-1**) and, on evidence,
`observable_time_ns <= as_of_time_ns`.

* A producer MUST be *prefix invariant*: the record it emits depends only on inputs received at
  or before its watermark.  The acceptance test (S02-ACC-01) compares, for the same run
  configuration, the canonical bytes of the records produced from the full input with those
  produced from the input truncated after input *k* (identified by its ordering key): every record
  with a watermark at or before input *k* MUST be identical.
* **Delays that can be read from records** (all are differences of declared instants):
  *feed delay* of a tick is `receive_time_ns - event_time_ns` (it includes the offset of the source
  clock; the corrected value is the business of session 01); *data age at the decision* is
  `decision_time_ns - as_of_time_ns`; *notification delay* of evidence is
  `decision_time_ns - observable_time_ns` (S02-ACC-05, "no five-minute notification delay").
  Because a component sees step time, the last two are zero whenever a record is produced in the
  step of the input it depends on - in paper exactly as in replay.  The test form of the
  notification-delay rule is therefore: a revision is emitted in the same step as the input whose
  receive time equals its `observable_time_ns`.
* **Delays that cannot be read from records**: how long processing really took.  *Processing
  delay* per stage and the *end-to-end delay* from the receipt of the triggering input to the
  order request are measured with the wall clock (section 3.5) and reported as latency evidence;
  the latency summary record that carries them is extension CX-14.  The age of the *oldest* input
  of a decision (for example old evidence combined with a fresh tick) is the difference between
  the decision time and that input's own `as_of_time_ns`; a decision record that lists it per
  input is part of CX-08.
* `check_derivation(child, *inputs)` (`gie.contracts.causality`) checks the rules that relate a
  derived record to its inputs: same run, not created before the inputs were available, watermark
  covers the inputs, does not outlive them, origin not better than theirs.

### 3.2 Clock, step time and scheduler (the `Clock` contract)

All code obtains time from an injected `gie.core.clock.Clock` and schedules future work through
an injected `gie.core.clock.Scheduler`:

```python
class Clock(Protocol):
    mode: ClockMode                 # REPLAY or SYSTEM
    def now_ns(self) -> int         # step time; never decreases
    def monotonic_ns(self) -> int   # for elapsed time only; never decreases
    regressions_observed: int       # backward steps of the time source seen so far (0 in replay)

class Scheduler(Protocol):
    def call_at(self, due_ns, callback) -> int      # absolute instant, not in the past
    def call_after(self, delay_ns, callback) -> int # relative to step time
    def cancel(self, handle) -> None
```

Components MUST NOT read the operating-system clock, sleep or start timer threads
(`time.time`, `datetime.now`, `threading.Timer`, ...); CI enforces this.

**Step time (D-024).**  A run is a sequence of steps; one step processes one input - a tick, a
broker report or a timer that is due.  In replay **and** in paper `now_ns()` is the instant at
which that input became available, and it does not move while the step runs.  Only the run loop
moves time.  Components are handed read-only views (`ClockView`, `SchedulerView`): they can read
step time and schedule or cancel timers, and nothing else.

* `ReplayClock` is the virtual time and the scheduler of a replay: the loop moves it with
  `advance_to`, never backwards; it fires timers in `(due time, registration order)`, and while a
  timer runs `now_ns()` equals that timer's due time.
* `PaperClock` is the step clock of a paper run: `begin_step(time_ns)` sets the step instant - an
  earlier instant is refused (`ClockRegressionError`) and so is one later than the system clock
  (`FutureDataError`); `wall_ns()` reads the system clock and is for the loop and for latency
  measurement only.  `PollingScheduler` is its scheduler: the loop calls
  `run_due(limit_ns=<receive time of the next input>)` before it begins the step of that input, and
  every due timer is a step of its own at its due time.
* `SystemClock` is the UTC system clock behind `PaperClock`.  It never decreases **and never
  stands still**: if the operating system steps the clock backwards, time continues at the pace of
  the monotonic source, so freshness and expiry keep failing closed; every backward step is
  counted in `regressions_observed`.  Whether risk may be created after a clock step is a rule of
  session 08 (fail closed by default).
* **Timer rule (both modes):** a timer never fires early; a timer due at instant *t* fires before
  any input that became available at *t* is delivered; `call_at` refuses an instant in the past.
* **Background work.**  Results of asynchronous strategic work are handed to the loop, which
  publishes them as records in a later step, stamped with that step's instant and with the
  market-data watermark the computation really used.  In replay the same result is delivered
  through the scheduler at a *virtual* instant (with a modelled, seeded delay that is part of the
  run configuration) - never "when a thread finishes".
* Measured latency (section 3.5) is evidence only; it never enters a record or influences a
  decision.
* `ClockReading` (contract name `Clock`) is the serialisable reading.  A REPLAY run uses a REPLAY
  clock and a PAPER run the SYSTEM clock (CLK-2).  The journal counts a `ClockReading` as an input
  marker: it advances the input watermark like a tick, so the look-ahead guard also works in a run
  that does not journal raw ticks.

### 3.3 Freshness (S00-ASG-23, G03-12, G06-04)

Freshness is evaluated against the injected clock and **fails closed at the boundary**:

* *Expiry.*  `Evidence`, `Candidate`, `TradePlan`, `RiskDecision` and resting entry orders carry
  `valid_until_ns`.  A record is usable iff `now_ns < valid_until_ns`; at the expiry instant it is
  already expired.
* *Age.*  Data with a reference instant is fresh iff `0 <= now_ns - reference_ns < max_age_ns`.
* *Future data.*  A reference instant later than `now_ns` is never "very fresh": it is clock skew
  or look-ahead leakage and raises `FutureDataError`.  A tolerance for cross-host skew exists for a
  system clock only: the clock-based helpers (`is_fresh_now`, `require_fresh_now`,
  `is_expired_now`, `require_not_expired_now`) accept a tolerance only when the clock's mode is
  exactly `ClockMode.SYSTEM` and bound it to one second (a constant of `gie.core.freshness`, not a
  research parameter).  Components MUST use these helpers; the plain functions that take a
  caller-chosen `now` are for `gie.core` and tests (CI rule).

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
a latency.  A measured duration is never written into a contract record (section 3.2); what the
foundation itself costs per record and per step on the build machine is in
`reviews/session_00/evidence/benchmark.json`.

## 4. Identifiers (S00-ASG-22)

* Form: `<prefix>_<32 lower-case hex>`; the hex part is the first 128 bits of the SHA-256 of the
  canonical JSON (section 7) of `[prefix, [identity parts...]]`.  Identifiers are opaque: consumers
  compare them, never parse them.
* **Deterministic.**  Identity parts are strings, integers, exact decimals and lists of those.
  Floats, wall-clock readings taken at creation, random numbers and unpersisted counters MUST NOT be
  used.  Replaying the same inputs reproduces the same identifiers.  In the hashed canonical JSON
  a decimal part is written as the object `{"d": "<canonical decimal string>"}`, so the decimal
  `1` and the text `"1"` can never produce the same identifier.
* **Validated.**  Every identity function validates every part with the vocabulary of the
  corresponding record field (codes, symbols, enumeration values, integers within 64 bits, finite
  decimals) and fails with a `GieError`; it never returns an identifier for malformed input.
* **Scope.**  An identifier that contains the run (`rsk`, `rsv`, `ord`, `brk`, `oev`, `pos`, and
  `run` itself) names one thing among all runs.  The identifiers of market events, evaluation
  slots and models are derived from what they describe and are deliberately equal across runs.
  The identifiers of evidence, opportunities, candidates, alternative groups and plans contain no
  run: two runs over the same data produce the same identifiers, possibly with different content.
  Such a record is named by `(run_id, id)`; a store that holds more than one run MUST key it that
  way.  The table of identity parts and scopes is the docstring of `gie.contracts.identity`.
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
  (default for candle-defined events: the open time of the defining candle).  That table does not
  exist yet: it is the first deliverable of session 02, together with the location and canonical
  format of the feature dictionary (CR-008), and no engine may emit evidence before the coordinator
  has integrated it.  The identity never changes between revisions
  (`validate_evidence_transition`).
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
never from the broker login: an alias that consists of digits or contains five or more digits in a
row is refused, because identifiers are written to journals and evidence files.

## 5. Error semantics (S00-ASG-24)

All errors derive from `gie.core.errors.GieError` and carry a stable machine `code`, a message and
a `details` dictionary of JSON scalars that can always be journaled.  Codes: `specifications/contracts/FIELDS.md`.

1. **Fail closed.**  When a `GieError` occurs while a component is deciding whether to create or
   increase risk, it MUST NOT create that risk.  Actions that only reduce risk remain permitted.
2. **Never repair input.**  Invalid input is rejected with `ContractValidationError` naming the
   field path and the violated rule.  It is never clamped, defaulted, rounded, stripped or coerced.
   Every record that exists in memory is valid: validation runs on construction and on decoding, and
   only exact instances of the registered contract classes are accepted anywhere.
3. **Unknown is not zero.**  Unavailable data is `null` / `MISSING` and propagates as "unknown".
4. **Unknown vocabulary is refused.**  An unknown enum value, contract name or MAJOR version is an
   error, not a warning.
5. **Errors can always be recorded.**  `GieError.to_dict()` is a dictionary of JSON scalars -
   message and detail values are made printable and bounded, whatever raised them.  Errors are
   never swallowed.  There is no error *record* among the contracts yet: the health / error event
   is extension CX-11; until it exists a component logs `to_dict()` and fails closed.  Encoding
   something that is not a contract record is itself an error (`UnknownContractError`).
6. **Sequence violations.**  The validators of section 9.2 raise `IllegalTransitionError`; the
   offending record never becomes part of a run's history (section 6.2).

Rejections that are part of normal operation are **not** errors: a plan refused by risk is a
`RiskDecision` with outcome REJECTED, a plan or scored candidate that fails a gate is a revision
with status REJECTED and reason codes, so that learning can study them (G07-04).  A rejection at
the level of a whole ranking pass, and of a candidate that was never scored, has no record yet -
that is the decision record of session 05 (CX-08).

## 6. Event ordering (S00-ASG-25)

### 6.1 Streams and sequence numbers

Every record belongs to exactly one stream and carries the producer's sequence number `seq`,
increasing by exactly 1 per record of that stream:

| contract | stream name | ordering time | rank |
|---|---|---|---|
| `RunManifest` | `run_manifest/<producer>` | `data_start_ns` (replay) / `created_time_ns` (paper) | 0 |
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

An order-event stream holds requests and reports.  They have different ranks because a broker
report is an *input* of a run while a request is one of GIE's own records; `EventKey` is therefore
not monotonic in `seq` within an order-event stream, and the order of the stream itself is `seq`
(the order of the run is the journal, section 6.2).

Within a stream the ordering time MUST be non-decreasing.  A new shared contract obtains its
stream prefix, ordering-time field and rank in `gie.contracts.ordering.STREAMS` when the coordinator
adds it.  `gie.core.ordering.SequenceTracker` classifies each arrival: next in sequence - accepted;
same position and identical content - an idempotent **duplicate** that is dropped; same position and
different content - `SequenceConflictError`; a skipped position - `SequenceGapError`; an older
position that can no longer be verified, or a decreasing ordering time - `OutOfOrderError`.  A
refused arrival never changes state (`check()` classifies without accepting); `restore()`
re-establishes a stream after a restart.  Within one order, `seq` and the request / report order
are compared within one producer only, and a report is strictly later than the request it answers.
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

`EventKey` orders *inputs*.  It is not a way to order derived records that come from different
journals; those are ordered by their journals.

A journal (`gie.contracts.journal`) belongs to exactly one run; its first record is the
`RunManifest`, and it owns the one lifecycle ledger of the run (section 9.2).  Before a record is
appended it MUST pass, in this order:

1. **type and run** - an exact registered contract type, of the journal's run and mode;
2. **stream position** - next in its stream (an identical repetition is an idempotent duplicate,
   different content at the same position is a conflict, a skipped position a gap);
3. **provenance against the manifest** - the data origin of a record is not better than the run's;
   a tick comes from a declared source and, in a replay, lies inside the declared data period; a
   candidate or plan is scored by a model that the manifest pins for that instant, with exactly
   the pinned status;
4. **time** - when the journal has the clock of the run, the ordering time of the record equals
   the step instant; tick receive times never decrease across all tick streams; a derived record
   is refused before any input was journaled and when its watermark `as_of_time_ns` lies beyond
   the *input watermark* (the newest journaled `Tick` or `ClockReading`);
5. **the run ledger** - the sequence rules of its lifecycle chain and the references to records
   that must already be part of the run (section 9.2).

A refused record leaves journal and ledger unchanged.  The look-ahead guard judges *declared*
watermarks: it cannot detect a producer that read later data and declared an earlier watermark.
The prefix-invariance test of section 3.1 therefore remains mandatory for every producer
(sessions 02, 03, 04 and 10).

`MemoryJournal` and the durable `FileJournal` are the reference implementations.  A file journal
is one canonical JSON line per record and is **durable before visible**: a record is written (and
synchronised, unless the journal is opened for one synchronisation per step with `commit()`)
before it becomes part of the in-memory state; after a write error the journal refuses every later
record until it is reopened.  It holds an operating-system lock, so a second writer is refused.
When reopened it verifies canonical form and fingerprint of every line; a torn last line after a
crash is detected and removed only by the explicit restart call.  A restarted process continues
with the manifest stored in the journal.  `JournalReader` is the read-only, tailing reader
(dashboard, analysis): it never writes and tolerates a last line that is still being written.
Known limits: journal and ledger keep the run in memory; checkpoints, eviction and the storage of
raw ticks are extension CX-19.

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
  instants exceed 2^53).  **The integer-token rule:** an integer field is written as an optional
  `-` and digits only; a token with a fraction or an exponent (`1.0`, `1e3`) is refused for an
  integer field even when its value is integral.  Float fields may appear as integer literals,
  because the canonical writer renders an integral double with the shortest digits that identify
  it (`1.0` is written `1`; `1e19` is written `10000000000000000000`; the double
  1234567890123456768 is written `1234567890123456800`).  A float field accepts an integer literal
  exactly when it is that canonical rendering of a double; any other integer would have to be
  rounded and is refused.
* **Canonical JSON MUST NOT be parsed with a double-precision JSON parser** (`JSON.parse` in a
  browser): 64-bit integers would be silently rounded.  A browser consumer uses the display
  projection (extension CX-17), in which such values are strings.
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
* Every schema object is closed and complete: `additionalProperties: false` and `minProperties`
  equal to the number of declared properties (every key is written); `required` lists the fields
  that may not be `null`.
* The JSON Schemas are **necessary but not sufficient**.  Carried as `x-gie-*` annotations and
  enforced by the reference implementation: decimal bounds, canonical order of lists, cross-field
  invariants and the behavioural surface.  Enforced by the reference decoder **only** (a generic
  schema validator cannot express them): the integer-token rule, the float-literal rule, the
  printable-text rule, the calendar validity of `trading_day`, the strict-parsing rules below and
  every lifecycle rule.  A consumer that validates with the schema alone has not validated a
  message.
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
independent of the lock status.  The lock hashes file bytes, and schemas and golden files are
written with LF line ends on every platform.  Golden messages of every released version are kept
under `tests/gie/golden/` and must decode with every later reader of the same MAJOR; the golden
check does not decode a directory of another MAJOR (the current reader refuses it by design) and
lists it instead - reading it is the job of the migration tool below.  A golden *run trace*
(`tests/gie/golden/traces/run_story.jsonl`, one coherent synthetic run in journal order) must be
accepted by the journal in order, so the lifecycle rules are locked together with the messages.

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
| Every plan has a hard stop on the losing side and targets on the winning side of its effective entry; a resting entry lies beyond the market it was planned against (a "limit" that is marketable cannot be planned or ordered) | PLN-5, PLN-6, PLN-14, OEV-17 | G04-12 |
| An ARMED or SELECTED plan has positive expected net R | PLN-11 | S05-ASG-11, S05-ACC-05 |
| Every entry order carries its plan, its risk approval, its alternative group, a reference price and an attached protective stop on the losing side of the price asked for (a fill price is a fact and is not judged) | OEV-5, OEV-11 | G04-12, G08-08 |
| A resting entry order expires and is requested, and fills, strictly before its expiry; a protective stop never expires | OEV-13 | G03-12, S07-ACC-06 |
| Only the execution engine creates an entry; an order created by the simulated broker says why | OEV-15 | G04-09, G08-08 |
| Orders other than entries belong to a position and carry no plan or approval; a protective stop is a STOP order, a take profit a LIMIT order | OEV-6, OEV-8 | G04-09, G04-10 |
| One approval yields one entry order (the order id is derived from the decision) | OEV-7 | S08-ACC-06, G08-07 |
| A rejection grants nothing; an approval grants a positive size and risk, is bound to one plan revision and its content, reserves risk and expires | RSK-1..3, RSK-10 | G04, S08-ACC-06 |
| Emergency halt never approves | RSK-5 | G05-04 |
| At or below the daily profit floor nothing is approved; above it an approval, together with open and pending risk, stays within the surplus - decided on exact amounts | RSK-7, RSK-8 | G05-07, G05-08 |
| The fractions of a risk decision are derived from its amounts and never look better than them | RSK-9 | G04 |
| An approval states the aggregates it rests on (equity, open and pending risk, margin, gap stress) and never exceeds free margin; unknown is never approved | RSK-13 | G04-02, G04-04, G04-05, G04-07 |
| The stop of a GIE position is never wider than its initial stop; an open position names its working protective order or states why it has none | POS-4, POS-9 | G04-11, G04-12 |
| A GIE position holds at most what its entry order opened | POS-2 | G04-09, G04-10 |
| A record cannot depend on market data received after it was created; retrieved historical cases are prior-only | CAUSAL-1, EVD-11, CND-11 | G10, S04-ASG-06, S10-ACC-02 |
| Missing evidence has null scores; inactive evidence has zero scores | EVD-3, EVD-5 | S02-ACC-03 |
| Every identifier that can be recomputed from the record is verified | section 4 | S00-ASG-22 |
| A model is ACTIVE only with a recorded owner approval and its evidence (report, criteria, shadow runs); a model fitted on fixture data is never *validated* | MDL-2, MDL-5, MDL-7, `validated` | G07-09, G07-10 |
| Every record names its run, mode and the origin of its data; a run is identified by the content of its manifest (role, risk regime, data, configuration, parameters, seed, code, pinned models) | `RunScoped` header, `Tick.origin`, RUN-1, RUN-5 | COM-07, M-AUTH-07 |

### 9.2 Sequence and reference rules - enforced when a record is published

These rules relate a record to other records.  A single record cannot violate them; a sequence
can.  They are implemented by the `validate_*` functions and enforced by the `LifecycleLedger` of
the run, which the journal owns and through which **every record is admitted** (the order gateway
has no ledger of its own).  A record that breaks one is refused before it becomes part of a run's
history.

| rule | validator | source |
|---|---|---|
| An order follows its state machine; terminal states are final; a cancel request stays pending when a late confirmation of an earlier request arrives | `validate_order_transition`, `ORDER_TRANSITIONS` | S07-ASG-01, S07-ACC-02 |
| No replacement or reconciliation can add risk: the volume of an entry never grows, its prices and attached stop are fixed and its expiry only shortens; a protective stop only tightens and never gains an expiry | `validate_order_transition` | G04-09, G04-10, G04-11, S07-ASG-04 |
| At most one plan of an alternative group is ever executed | `LifecycleLedger` | S05-ACC-04 |
| A GIE position's stop never widens and is never removed; its volume grows only through fills of its own entry order and never beyond the cumulative fill; its executable net P&L peak never decreases | `validate_position_transition`, `LifecycleLedger` | G04-10, G04-11, G04-13, G04-15 |
| At most one working protective stop per position; working exit and take-profit volume stays within the open volume | `LifecycleLedger`, `validate_reducing_order` | G04-12, S07-ASG-05 |
| A plan's stop never moves away from the entry between revisions and, for a resting entry, the risk distance never grows; final statuses are final | `validate_plan_transition` | G04-11 |
| The identity of a market event never changes between evidence revisions; the stage never goes back | `validate_evidence_transition` | G02-02 |
| A model reaches ACTIVE only through SHADOW; one ACTIVE version per model family; a rollback names the version of the same family that was active immediately before and needs a new approval; components are registered before the composite | `validate_model_transition`, `RegistryHistory` | G07-09, G07-10, G07-11 |
| Within a trading day, across all risk producers of the run: an emergency halt stays in force, the profit floor never decreases or disappears, the reference equity is frozen | `advance_daily_risk` | G05-04, G05-06 |
| A record refers only to records that are already part of the run: candidate to evidence, plan to candidate (a target that names a scenario is that destination of the candidate), risk decision to plan, entry order to plan and approval, reduce-only order to a position whose entry order has filled, position to its entry order and protective order | `LifecycleLedger` (referential) | S10-ACC-02, G10 |

**Reduce-only orders.**  Every order that is not an entry belongs to exactly one open GIE
position and can only reduce or protect it: opposite side, volume within the open volume, a
protective stop that covers the whole open volume and is never looser than the position's stop.
While an entry order fills in parts the protective stop follows the open volume, so its volume may
change in a replacement; the volume of an entry never grows.  The trigger side is normative: a
protective stop of a long position triggers at or below its trigger price on the bid, of a short
position at or above it on the ask.

### 9.3 Authorisation - enforced by the order gateway

An execution engine reaches an adapter only through
`gie.contracts.guarded.GuardedExecutionAdapter` (interface `OrderGateway`).  The gateway is built
with the adapter, the routing guard, the clock and the journal of the run; it keeps no state of its
own.  `submit`, `cancel` and `replace` take the request only - plan, approval and position are
read from the ledger of the run's journal (newest revision), never from the caller.  For every
call, in this order:

1. **routing lock** - the adapter is authorised by the routing guard;
2. **authorisation** -
   * an **entry** needs its SELECTED plan and its unexpired risk approval
     (`validate_entry_authorization`): same run, same plan revision and plan content
     (`plan_fingerprint`), same alternative group, side, order type and price, the plan's stop and
     slippage bound, an attached take profit that is one of the plan's targets, volume within the
     grant, plan <= approval <= request <= now in time, nothing expired, the order not outliving
     plan or approval, and a model status that the role of the run allows (D-026: PRIMARY -
     ACTIVE; SHADOW - ACTIVE or SHADOW; RESEARCH - also RESEARCH; never REJECTED, ROLLED_BACK or
     RETIRED);
   * **every other order** needs the open GIE position it belongs to and may only reduce or
     protect it (`validate_reducing_order`, section 9.2); the protective stop of an open position
     cannot be cancelled (`validate_protective_cancel`);
   * a request is created by the execution engine (originator EXECUTION); GIE never places orders
     for positions of other systems;
3. **journal, then adapter (write-ahead)** - the request is published to the journal and only
   then forwarded.  A request that is already journaled is forwarded again: the adapter is
   idempotent per `order_event_id`, so a crash between journal and adapter loses nothing and no
   order of GIE can exist at the simulated broker that the journal does not know.

`poll()` journals every report of the adapter on its own and returns
`PollResult(events, duplicates, errors)`: a report the journal refuses is returned as an error and
never hides the reports after it.  A report is delivered at its receive instant; the simulated
broker schedules its own wake-up through the scheduler.  An order that the simulated broker
creates itself - the protective stop or take profit attached to an entry, a broker-side close -
enters through `poll()` as a request with originator BROKER and a reason code, and is admitted
only for a position whose entry order has filled (D-025).

The layers of the paper-only routing lock are specified in `gie/core/routing_lock.py` and
`docs/ARCHITECTURE.md`.

### 9.4 Other systems and learning

`Position` records with owner `EXTERNAL` describe positions of other systems on the same account.
They report what is observed - GIE's own monotonic rules do not apply to them - and they count
towards account-wide risk (G05-12).  Learning and model fitting use GIE-owned positions only
(G07-06, no manual-trade learning).

## 10. Service interfaces and the run loop

`gie.contracts.interfaces` defines the `Protocol` classes through which subsystems call each
other: `TickSink`, `TickSource`, `RecordSink` (with `commit`), `RecordSource`, `RecordListener`,
`PlanScorer`, `RiskGate`, `ExecutionAdapter`, `OrderGateway`, `ProtectionPort` and `ModelRegistry`.
They are part of the shared contract surface and change under the same rules.  `RunContext`
(`gie.contracts.run`) hands every component its run manifest, the read-only clock and scheduler
views, the research parameters, its output sink and `seed_for(purpose)` - the only source of
pseudo-randomness in a run.

**One step of a run** (the same step in replay and paper; the shared step driver is extension
CX-10 and is required before any subsystem is integrated):

1. the loop sets step time to the instant at which the input became available; timers that are
   due at or before that instant fire first, each as a step of its own;
2. the input is journaled and delivered to the tick sinks in the fixed order *protective* -
   simulated broker first, then position keeping, then the reflex engine - *tactical* (risk,
   decision, execution) - *strategic* (evidence, prediction);
3. every record a component publishes during the step is journaled at once and then delivered to
   the record listeners in the same tier order, before the publishing component continues;
   records published by listeners are handled the same way until nothing new is published (the
   loop bounds the number of rounds and fails the step if the bound is hit);
4. the step ends with `commit()` on the journal; only then is the next input taken.

A sink or listener that raises is marked failed and creates no risk until it has recovered; the
others still receive the input.

All calls are synchronous and MUST NOT wait for the network, a model in training or the dashboard
(G03-05, G03-06, S06-ASG-07).  Work that takes long runs outside the loop and its result enters as
a record of a later step (section 3.2); a late result can make evidence stale, never delay
protection.

## 11. Provisional elements

The following are deliberately provisional because the existing system could not be audited in
Session 00 (blocker B-001).  They are valid, tested contract elements, but their vocabulary is
expected to be refined by a MINOR/MAJOR change once the six engines have been inspected:

* `EvidenceStage` values, the reserved evidence kinds and level roles, and the per-kind identity
  levels and anchor convention (owned by the session-02 feature dictionary);
* the **slot rule**: after each evaluation an engine emits one current record per declared slot
  (engine, kind, symbol, timeframe, anchor) - ACTIVE or CONTRA when it found an event, INACTIVE
  when it evaluated and found none, MISSING when it could not evaluate - and a consumer treats a
  slot without an unexpired current record as MISSING, never as "no signal";
* the **pending limit** (brief 02: "Expose Cavemen pending limit at breakout close"): evidence of
  kind `pending_limit` describes an *intended* resting limit, not an order.  It is anchored at the
  close of the breakout candle, names the breakout in `related_market_event_id` and carries
  exactly one `PENDING_LIMIT` level and one directional score (EVD-12).  Proposed stage mapping,
  to be confirmed by session 02 against the engine: ARMED while the limit would be resting,
  TRIGGERED when price reached the level, COMPLETED when the engine's own logic considers it
  filled, state INVALIDATED when the engine would cancel it, EXPIRED at `valid_until_ns`;
* an invalidation with basis `CANDLE_CLOSE` refers to the close of the candle of the stated
  timeframe; which price series that candle is built from (bid, ask or mid) is fixed by the
  candle contract (CX-01) and is undefined until then;
* `Timeframe` values;
* `MarketSession` values (session boundaries are broker-calendar data of session 01);
* `TickFlag` values and their thresholds (owned by session 01);
* `ModelKind` values;
* reason-code vocabularies (open strings matching `^[A-Z][A-Z0-9_]{1,47}$`; each producing
  subsystem publishes its dictionary).
