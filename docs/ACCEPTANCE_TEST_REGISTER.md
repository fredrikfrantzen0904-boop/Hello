# GIE acceptance test register

> GENERATED - do not edit. Rendered by `python -m tools.gie_registers render` from
> `specifications/acceptance_tests.yaml`.
> Change the source and render again; `render --check` (run in CI) rejects a stale copy.

One entry per acceptance requirement of the session briefs. 'status' is maintained by tools/gie_registers.py from recorded pytest evidence: planned (no tests yet), passing (all marked tests passed), failing (any marked test failed or errored), blocked (cannot be executed; see blockers), partial (tests pass but part of the scope is blocked or skipped). It is never set by hand to passing. For acceptance tests that are evaluations (a comparison or a measurement), 'passing' means the evaluation ran reproducibly and its result was recorded as evidence - never that the result was favourable.

## Totals by status

| status | acceptance tests |
|---|---:|
| planned | 91 |
| passing | 0 |
| failing | 0 |
| blocked | 0 |
| partial | 0 |
| **total** | 91 |

## S00

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S00-ACC-01 | Contract round-trip tests | planned | - | `tests/gie/contracts/test_roundtrip.py`<br>`tests/gie/contracts/test_roundtrip_property.py`<br>`tests/gie/contracts/test_golden.py` |  |
| AT-S00-ACC-02 | Contract compatibility tests | planned | - | `tests/gie/contracts/test_compatibility.py`<br>`tests/gie/contracts/test_schema_lock.py` |  |
| AT-S00-ACC-03 | Replay clock | planned | - | `tests/gie/core/test_clock.py` |  |
| AT-S00-ACC-04 | Paper-only routing lock | planned | - | `tests/gie/core/test_routing_lock.py`<br>`tests/gie/tools/test_check_live_lock.py`<br>`tests/gie/contracts/test_guarded_adapter.py` |  |
| AT-S00-ACC-05 | Baseline regression | planned | - | `tests/gie/baseline/test_baseline_regression.py` | B-001 |
| AT-S00-ACC-06 | No production changes | planned | - | `tests/gie/tools/test_protected_paths.py`<br>`tests/gie/tools/test_baseline_audit.py` | B-001 |

## S01

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S01-ACC-01 | Out-of-order tick tests | planned | - |  |  |
| AT-S01-ACC-02 | Duplicate tick tests | planned | - |  |  |
| AT-S01-ACC-03 | Missing tick tests | planned | - |  |  |
| AT-S01-ACC-04 | Stale tick tests | planned | - |  |  |
| AT-S01-ACC-05 | DST tests | planned | - |  |  |
| AT-S01-ACC-06 | Holiday tests | planned | - |  |  |
| AT-S01-ACC-07 | Rollover tests | planned | - |  |  |
| AT-S01-ACC-08 | Deterministic replay | planned | - |  |  |
| AT-S01-ACC-09 | Throughput distributions | planned | - |  |  |
| AT-S01-ACC-10 | Latency distributions | planned | - |  |  |

## S02

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S02-ACC-01 | Prefix invariance | planned | - |  |  |
| AT-S02-ACC-02 | Exact event deduplication | planned | - |  |  |
| AT-S02-ACC-03 | Inactive vs missing evidence | planned | - |  |  |
| AT-S02-ACC-04 | Baseline parity | planned | - |  |  |
| AT-S02-ACC-05 | No five-minute notification delay | planned | - |  |  |

## S03

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S03-ACC-01 | No future data leakage | planned | - |  |  |
| AT-S03-ACC-02 | No revised release leakage | planned | - |  |  |
| AT-S03-ACC-03 | Delayed-feed rejection | planned | - |  |  |
| AT-S03-ACC-04 | Missing-source degradation | planned | - |  |  |
| AT-S03-ACC-05 | Incremental out-of-period value versus gold-only<br>Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. | planned | - |  |  |

## S04

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S04-ACC-01 | Purged chronological splits | planned | - |  |  |
| AT-S04-ACC-02 | Calibration by setup and session | planned | - |  |  |
| AT-S04-ACC-03 | Leakage mutation tests | planned | - |  |  |
| AT-S04-ACC-04 | Cost stress | planned | - |  |  |
| AT-S04-ACC-05 | Model inference latency | planned | - |  |  |
| AT-S04-ACC-06 | Reproducibility | planned | - |  |  |

## S05

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S05-ACC-01 | No future candidate ranking | planned | - |  |  |
| AT-S05-ACC-02 | Expiry tests | planned | - |  |  |
| AT-S05-ACC-03 | Contradiction tests | planned | - |  |  |
| AT-S05-ACC-04 | Duplicate plan handling | planned | - |  |  |
| AT-S05-ACC-05 | Positive expectancy gate | planned | - |  |  |
| AT-S05-ACC-06 | Session coverage<br>Note: An evaluation: 'passing' means coverage was measured reproducibly and recorded. | planned | - |  |  |
| AT-S05-ACC-07 | Explainability | planned | - |  |  |

## S06

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S06-ACC-01 | Burst replay | planned | - |  |  |
| AT-S06-ACC-02 | Gap/slippage<br>Note: Two scenarios (gap; slippage): each needs its own marked test; the status is the worse of the two. | planned | - |  |  |
| AT-S06-ACC-03 | Stale tick | planned | - |  |  |
| AT-S06-ACC-04 | Slow-model isolation | planned | - |  |  |
| AT-S06-ACC-05 | No stop widening | planned | - |  |  |
| AT-S06-ACC-06 | p99/max latency<br>Note: Two measures (p99; max): both must be reported. | planned | - |  |  |
| AT-S06-ACC-07 | Profit giveback comparisons<br>Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. | planned | - |  |  |
| AT-S06-ACC-08 | Premature-exit comparisons<br>Note: An evaluation: 'passing' means the comparison ran reproducibly and was recorded, not that the result was favourable. | planned | - |  |  |

## S07

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S07-ACC-01 | Duplicate messages | planned | - |  |  |
| AT-S07-ACC-02 | Partial fill races | planned | - |  |  |
| AT-S07-ACC-03 | Rejected orders | planned | - |  |  |
| AT-S07-ACC-04 | Reconnect | planned | - |  |  |
| AT-S07-ACC-05 | Crash recovery | planned | - |  |  |
| AT-S07-ACC-06 | Stale plans | planned | - |  |  |
| AT-S07-ACC-07 | No real orders | planned | - |  |  |
| AT-S07-ACC-08 | Deterministic replay | planned | - |  |  |

## S08

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S08-ACC-01 | Boundary tests at -2%<br>Note: Tests read the threshold from specifications/research_parameters.toml. | planned | - |  |  |
| AT-S08-ACC-02 | Boundary tests at proposed -1.75%<br>Note: The meaning of the -1.75% level is open item U-03; tests read thresholds from specifications/research_parameters.toml. | planned | - |  |  |
| AT-S08-ACC-03 | Boundary tests at -3%<br>Note: The -3% level is proposed and configurable (G05-05); tests read it from specifications/research_parameters.toml. | planned | - |  |  |
| AT-S08-ACC-04 | Boundary tests at +5%<br>Note: Tests read the threshold from specifications/research_parameters.toml. | planned | - |  |  |
| AT-S08-ACC-05 | Exact-floor no new risk | planned | - |  |  |
| AT-S08-ACC-06 | Simultaneous reservations atomic | planned | - |  |  |
| AT-S08-ACC-07 | Gap stress | planned | - |  |  |
| AT-S08-ACC-08 | Other-account-position handling | planned | - |  |  |

## S09

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S09-ACC-01 | Active model immutable during training | planned | - |  |  |
| AT-S09-ACC-02 | Resource isolation | planned | - |  |  |
| AT-S09-ACC-03 | Leakage tests | planned | - |  |  |
| AT-S09-ACC-04 | Promotion rejection for weak evidence | planned | - |  |  |
| AT-S09-ACC-05 | Rollback | planned | - |  |  |
| AT-S09-ACC-06 | Reproducibility | planned | - |  |  |

## S10

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S10-ACC-01 | Deterministic replay | planned | - |  |  |
| AT-S10-ACC-02 | No lookahead | planned | - |  |  |
| AT-S10-ACC-03 | Tick ordering | planned | - |  |  |
| AT-S10-ACC-04 | Costs | planned | - |  |  |
| AT-S10-ACC-05 | Overlapping positions | planned | - |  |  |
| AT-S10-ACC-06 | Daily denominator checks | planned | - |  |  |
| AT-S10-ACC-07 | Purged walk-forward | planned | - |  |  |
| AT-S10-ACC-08 | Block bootstrap | planned | - |  |  |
| AT-S10-ACC-09 | Multiplicity | planned | - |  |  |

## S11

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S11-ACC-01 | Replay UI with real event payloads | planned | - |  |  |
| AT-S11-ACC-02 | Stale/disconnect state<br>Note: Two states (stale; disconnect): each needs its own marked test; the status is the worse of the two. | planned | - |  |  |
| AT-S11-ACC-03 | No hardcoded metrics | planned | - |  |  |
| AT-S11-ACC-04 | No blocking of reflex | planned | - |  |  |
| AT-S11-ACC-05 | Existing tabs unaffected | planned | - |  |  |

## S12

| id | acceptance test | status | tests passed/failed/skipped | test modules | blockers |
|---|---|---|---|---|---|
| AT-S12-ACC-01 | Full integration replay | planned | - |  |  |
| AT-S12-ACC-02 | Restart/disconnect<br>Note: Two scenarios (restart; disconnect): each needs its own marked test; the status is the worse of the two. | planned | - |  |  |
| AT-S12-ACC-03 | Stale feed | planned | - |  |  |
| AT-S12-ACC-04 | Burst load | planned | - |  |  |
| AT-S12-ACC-05 | Risk races | planned | - |  |  |
| AT-S12-ACC-06 | Overnight reset | planned | - |  |  |
| AT-S12-ACC-07 | Regression | planned | - |  |  |
| AT-S12-ACC-08 | Paper-only end-to-end tests | planned | - |  |  |
