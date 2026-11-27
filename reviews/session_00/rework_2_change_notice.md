# Session 00 - rework 2: what changed in the shared foundation

Written by the coordinator for reviewers and for the maintainers of the independent test suites.
It lists every change of behaviour or interface made after review round 2 (40 findings,
`reviews/session_00/evidence/review_round2_findings.json`), the verification of the round-1
dispositions and the defect reports of the independent test authors. The reasons are in
`reviews/session_00/review_round2_dispositions.yaml` and in `docs/DECISION_LOG.md` (D-020, D-021,
D-024 .. D-028). The exact text of every field and invariant is generated into
`specifications/contracts/FIELDS.md`; the module docstrings state the rules in prose.

Nothing here weakens a rule the owner stated without exception (no live routing, no widening of a
protective stop, no averaging down, one executed alternative per group, fail closed).

## 1. Time: one step, one instant (D-024)

* In replay **and** paper a component sees the instant of the input that is being processed
  ("step time"). Only the run loop moves time.
* `gie.core.clock.PaperClock(system=None)`: `now_ns()` returns the step instant; `wall_ns()` reads
  the operating-system clock (for latency measurement and the run loop only);
  `begin_step(time_ns)` moves the step instant - it refuses an instant earlier than the current
  one (`ClockRegressionError`) and one later than `wall_ns()` (`FutureDataError`).
* `PollingScheduler(clock)` requires exactly a `PaperClock`. `call_at` refuses a due time in the
  past, exactly like `ReplayClock`; `call_after` is relative to the step instant;
  `run_due(limit_ns=None)` begins a step at each due time, in `(due time, registration order)`.
* `ClockView(clock)` and `SchedulerView(scheduler)` are what components receive: read step time,
  schedule and cancel timers - nothing that moves time or fires timers.
* Measured latency never enters a record.

## 2. Run identity (D-027)

* `RunManifest` gained `risk_regime` (`RiskRegime.CHALLENGE_RULES` / `UNCONSTRAINED_RESEARCH`),
  `code_tree_dirty` and `models` (a tuple of `RunModel(model_version_id, status, validated,
  valid_from_ns, valid_until_ns)`, at most 4096). `model_version_ids` is gone.
  `created_time_ns` is optional: null in a replay, required in a paper run.
* `run_id` is derived from the content: `identity.run_id(mode, manifest_sha256)` where the hash
  covers every other field (`gie.contracts.run.derive_run_id(**fields)`). Build a manifest with
  `RunManifest.create(**fields)`. A manifest whose `run_id` does not match its content is refused
  (RUN-1).
* RUN-2 replay has a data period and no creation time, paper has a creation time; RUN-5
  `UNCONSTRAINED_RESEARCH` only with role `RESEARCH`, and a `PRIMARY` run pins only `ACTIVE` models.
* `RunManifest.model(model_version_id, time_ns=None)` returns the pinned entry that covers the
  instant. `RunContext` wraps clock and scheduler in the read-only views, validates its arguments
  (`ConfigError`) and offers `seed_for(purpose)` (`gie.core.ids.derive_seed`).

## 3. Journal (D-020 amended)

* `MemoryJournal(clock=None)`; `FileJournal(path, *, clock=None, fsync=True, opener=None)`.
  Checks, in this order: record type and run -> stream position (duplicate, conflict, gap) ->
  provenance against the manifest (data origin not better than the run's, tick source declared,
  tick inside the replay period, scoring model pinned with the pinned status) -> time (with a
  clock: the ordering time equals the step instant; tick receive times never decrease across
  streams; a derived record needs `as_of_time_ns <=` the input watermark and is refused before any
  input; the watermark is advanced by `Tick` and `ClockReading`) -> the run ledger.
* A refused record changes nothing. `publish` writes (and with `fsync=True` synchronises) before
  memory changes; after a write error the journal is failed and refuses every later publish.
  `commit()` synchronises once per step for journals opened with `fsync=False`.
* A file journal holds an operating-system lock on `<path>.lock`; a second writer is refused.
  `FileJournal.open_for_restart(path, ...)` removes a torn last line (`repair_torn_tail`,
  `repaired_bytes`). Reopening verifies canonical form and fingerprint of every line.
* `JournalReader(path)` is the read-only, tailing reader (`poll()`, `read()`, `streams()`,
  `run_id`); it tolerates a last line that is still being written.
* `journal.ledger` is the one lifecycle ledger of the run; `journal.input_watermark_ns`.

## 4. Lifecycle ledger

* `LifecycleLedger(*, referential=True)`. With `referential=True` (the journal's ledger) a record
  must refer to records that are already part of the run: candidate -> evidence, plan -> candidate
  (and a target that names a scenario -> that destination of the candidate, same price), risk
  decision -> plan, entry order -> plan and approval, reduce-only order -> a position whose entry
  order has filled, position -> its entry order and its protective order.
* Aggregates: one executed alternative per group; at most one working protective stop per
  position; a position never exceeds the cumulative fill of its entry order; the daily risk state
  per run across all risk producers (`daily_risk(run_id)`); the model registry rules.
* Lookups: `order, position, model, candidate, plan, newest_plan, decision, daily_risk,
  group_entry_order, open_orders, open_positions, position_orders, protective_order,
  entry_order_of, reducing_volume`.

## 5. Order gateway (D-021 amended, D-025)

* `GuardedExecutionAdapter(adapter, guard, *, clock, journal)` and
  `GuardedExecutionAdapter.create(adapter_type, guard, *, clock, journal, **kw)`. The gateway has
  no ledger of its own: it reads plan, approval and position from `journal.ledger`.
* `submit(request)`, `cancel(request)`, `replace(request)` take the request only. Order:
  routing lock -> authorisation -> `journal.publish(request)` (write-ahead) -> adapter. A request
  that is already journaled is forwarded again (the adapter is idempotent per `order_event_id`).
* `poll()` returns `PollResult(events, duplicates, errors)` (`clean` when there is no error):
  every report is journaled one by one; a refused report is returned in `errors` and does not
  hide the others.
* Single writer: order events are written by session 07 only. The reflex engine uses
  `interfaces.ProtectionPort` (`tighten_stop`, `exit`).
* `validate_entry_authorization(plan, decision, request, *, now_ns, role)`,
  `validate_reducing_order(position, request, *, protective=None, other_reducing_lots=0)`,
  `validate_protective_cancel(position, request)`. Wrong argument types are a
  `ContractValidationError`. `EXECUTABLE_MODEL_STATUSES` (D-026) maps the run role to the model
  statuses a plan may rest on: `PRIMARY` -> `ACTIVE`; `SHADOW` -> `ACTIVE`, `SHADOW`; `RESEARCH`
  -> `ACTIVE`, `SHADOW`, `RESEARCH` (a rule set is a registered model version of kind `RULESET`).
  `REJECTED`, `ROLLED_BACK` and `RETIRED` models never lead to an entry in any run.

## 6. OrderEvent

* New fields: `originator` (`OrderOriginator.EXECUTION` / `BROKER`, immutable), `max_slippage`
  (entry requests; equals the plan's), `fill_tick_source`, `fill_tick_seq` (the quote a fill was
  matched against).
* An order created by the simulated broker (attached stop or take profit, broker-side close)
  enters through `poll()` as `SUBMIT_REQUESTED` with originator `BROKER` and a reason code; an
  `ENTRY` is always `EXECUTION`; the gateway's `submit` accepts `EXECUTION` only.
* Transitions added: `PENDING_CANCEL` stays `PENDING_CANCEL` on a late `ACCEPTED`, `REPLACED` or
  `REPLACE_REJECTED`; `(PENDING_CANCEL, REJECTED) -> REJECTED`. A protective stop never expires.
* `validate_order_transition`: only `receive_time_ns` must not decrease; a report is strictly
  later than the request it answers; `seq` is compared within one producer only; the volume of a
  reduce-only order may change in a replacement, the volume of an entry never grows.
* Invariants changed or new: OEV-9, OEV-11, OEV-13 .. OEV-18 (see FIELDS.md).

## 7. Position

* New fields `protective_client_order_id` and `unprotected_reason` (POS-9: an open position names
  its protective order or says why it has none).
* POS-4 relaxed: the stop is never wider than the initial stop; an entry may fill at or beyond the
  stop (the fill is what happened). `peak_time_ns` is the receive time of the quote that made the
  peak.
* Transition: `initial_volume_lots` may grow while the entry order fills, the open volume grows
  by no more than that, and never otherwise; a new peak lies between the two snapshots.

## 8. RiskDecision: amounts decide, fractions report (D-028)

* New fields `daily_pnl_amount` and `profit_floor_amount`. Limits are decided on amounts in the
  account currency; fractions are derived with `gie.core.canonical.fraction_of(amount, base, *,
  round_up)` (12 digits; a result is rounded down, a floor or a risk is rounded up, so a fraction
  never looks better than the amounts) and must equal the derivation (RSK-9).
* RSK-6 .. RSK-8 (floor and surplus on amounts, including open and pending risk), RSK-13 (an
  approval states the aggregates and `margin_required <= margin_free`).
* `advance_daily_risk(summary, decision) -> DailyRiskSummary` is the sequence rule of a trading
  day: the floor never decreases or disappears, a halt is sticky, the reference equity is frozen.
  `validate_daily_state_sequence(previous, decision)` is the pairwise wrapper.

## 9. ModelVersion and registry

* New field `evaluation_data_origin`; `validated` needs a non-fixture training **and** evaluation
  origin; mandatory for `ACTIVE`.
* `RegistryHistory` (`check`, `admit`, `model`, `active`): one `ACTIVE` version per
  `(kind, model_name)`; a rollback names the version of the same family that was active
  immediately before; components are registered before the composite.
  `validate_registry_history(entries)` uses it.

## 10. Identity

* Every identity function validates every part (vocabulary patterns of the record fields, integers
  within range, finite decimals) and fails with a `GieError` only.
* `identity.run_id(mode, manifest_sha256)` (was: configuration hash and label).
* `external_position_id`: the account alias may not consist of digits or contain five or more
  digits in a row.

## 11. TradePlan, Evidence, Tick

* PLN-13 entry trigger and invalidations are prices > 0. PLN-14 a resting entry of a usable plan
  lies beyond the market it was planned against (LIMIT: BUY `entry_price <= reference_price`,
  SELL `>=`; STOP: BUY `>=`, SELL `<=`). PLN-15 targets are named once. `Target.scenario`
  (optional) links a target to the candidate's destination.
* `validate_plan_transition`: for a non-MARKET plan the risk distance between entry and stop may
  not grow between revisions.
* `validate_evidence_transition`: the stage never goes back.
* `feed_silence_ns` validates `now_ns` and the type of the last tick.

## 12. Contract framework

* A float field accepts an integer literal exactly when it is the canonical rendering of a double.
* Every schema object states `minProperties` = number of declared properties (every key is
  written, optional ones as `null`).
* An invariant that cannot be evaluated is a `ContractValidationError` naming the invariant.
* Encoding something that is not a registered contract record is an `UnknownContractError`.
* A record class must be created by code of `gie.contracts` or `tests` (a spoofed `__module__` is
  refused).
* Compatibility: a new optional field is MINOR (also with `x-gie-since`); any difference of a
  `mode` / `venue` vocabulary, and a new property of that name without exactly the routing
  vocabulary, is MAJOR; version errors are `ContractVersionError`.
* The schema lock hashes file bytes; schema and golden files are written with LF on every
  platform; `behaviour()` returns copies.
* `check_derivation` applies the applicable rules to an `OrderEvent` child.
* Golden messages were regenerated (the lock is still PROPOSED); `tests/gie/golden/traces/
  run_story.jsonl` is a golden run trace that must be accepted by the journal in order.

## 13. Core

* `gie.core.canonical.fraction_of`, `gie.core.ids.derive_seed(run_seed, purpose)`.
* `SequenceTracker.check(...)` (no state change) and `observe` = check + accept;
  `restore(stream, last_seq, recent=None, last_time_ns=None)`.
* `routing_lock`: `RoutingGuard` is final by metaclass; the adapter registry is keyed by identity;
  registration requires an allowed defining module, an allowed registering module and methods
  whose globals belong to an allowed module; code, defaults and closures of the registered methods
  are remembered and compared; the self-check also detects alias attributes on the enums.
  `registered_adapter_types()` returns a frozenset.
* `gie.core.errors.describe` / `safe_text`: an error message is always journalable.
* `ResearchParameters` is immutable; `trading_day_bounds_ns` refuses days outside the supported
  range.

## 14. Samples

`tests/gie/samples.py`: `RUN_ID` is derived from the sample manifest; `run_manifest()` uses
`RunManifest.create`; `run_story()` returns one coherent run of 28 records (manifest, model, ticks,
evidence, candidate, plan, approval, entry order with partial fills, broker-created stop, position
snapshots, stop tightening, close) that a `MemoryJournal` accepts in order. All sample data is
`DataOrigin.SYNTHETIC_FIXTURE`.
