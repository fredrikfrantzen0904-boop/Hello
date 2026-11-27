# GIE implementation status

> GENERATED - do not edit. Rendered by `python -m tools.gie_registers render` from
> `specifications/requirements_matrix.yaml`, `specifications/acceptance_tests.yaml`, `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md`.
> Change the source and render again; `render --check` (run in CI) rejects a stale copy.

## Requirements by session

A requirement is counted for a session when the session is listed in its `sessions` or has recorded
a contribution; the status is the overall status of the requirement. Requirements shared by several
sessions appear in several rows; the last row counts every requirement once.

| session | requirements | not_started | in_progress | partial | implemented | verified | blocked | not_applicable |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| S00 | 171 | 171 | 0 | 0 | 0 | 0 | 0 | 0 |
| S01 | 126 | 126 | 0 | 0 | 0 | 0 | 0 | 0 |
| S02 | 107 | 107 | 0 | 0 | 0 | 0 | 0 | 0 |
| S03 | 121 | 121 | 0 | 0 | 0 | 0 | 0 | 0 |
| S04 | 115 | 115 | 0 | 0 | 0 | 0 | 0 | 0 |
| S05 | 126 | 126 | 0 | 0 | 0 | 0 | 0 | 0 |
| S06 | 122 | 122 | 0 | 0 | 0 | 0 | 0 | 0 |
| S07 | 135 | 135 | 0 | 0 | 0 | 0 | 0 | 0 |
| S08 | 142 | 142 | 0 | 0 | 0 | 0 | 0 | 0 |
| S09 | 117 | 117 | 0 | 0 | 0 | 0 | 0 | 0 |
| S10 | 141 | 141 | 0 | 0 | 0 | 0 | 0 | 0 |
| S11 | 104 | 104 | 0 | 0 | 0 | 0 | 0 | 0 |
| S12 | 103 | 103 | 0 | 0 | 0 | 0 | 0 | 0 |
| **all** | 584 | 584 | 0 | 0 | 0 | 0 | 0 | 0 |

## Acceptance tests by session

| session | acceptance tests | planned | passing | failing | blocked | partial |
|---|---:|---:|---:|---:|---:|---:|
| S00 | 6 | 6 | 0 | 0 | 0 | 0 |
| S01 | 10 | 10 | 0 | 0 | 0 | 0 |
| S02 | 5 | 5 | 0 | 0 | 0 | 0 |
| S03 | 5 | 5 | 0 | 0 | 0 | 0 |
| S04 | 6 | 6 | 0 | 0 | 0 | 0 |
| S05 | 7 | 7 | 0 | 0 | 0 | 0 |
| S06 | 8 | 8 | 0 | 0 | 0 | 0 |
| S07 | 8 | 8 | 0 | 0 | 0 | 0 |
| S08 | 8 | 8 | 0 | 0 | 0 | 0 |
| S09 | 6 | 6 | 0 | 0 | 0 | 0 |
| S10 | 9 | 9 | 0 | 0 | 0 | 0 |
| S11 | 5 | 5 | 0 | 0 | 0 | 0 |
| S12 | 8 | 8 | 0 | 0 | 0 | 0 |
| **all** | 91 | 91 | 0 | 0 | 0 | 0 |

## Requirements that are blocked, partial, in progress or implemented

Everything that is started but not verified, with what each session delivered and what blocks it.

### blocked (0)

None.

### partial (0)

None.

### in_progress (0)

None.

### implemented (0)

None.

## Open blockers, change requests and open items

- blockers: 1 open of 1
- change requests: 7 open of 8
- open items: 18 open of 18

| id | type | status | title | requirements |
|---|---|---|---|---|
| B-001 | blocker | OPEN | The existing system `D:\ftmo_eurusd` is not reachable from this session | S00-ASG-01, S00-ASG-02, S00-ASG-03, S00-ASG-04, S00-ASG-05, S00-ASG-06, S00-ASG-07, S00-ACC-05, S00-ACC-06, S00-DEL-04, G02-08, G02-09 |
| CR-002 | change request | OPEN | Repository layout differs from the proposed paths | COM-03, S00-DEP-02, S12-DEP-02 |
| CR-003 | change request | OPEN | Additional shared contracts are needed by later sessions | COM-06, S01-ASG-04, S01-ASG-05, S01-ASG-12, S07-ASG-15, S08-ASG-01, S08-ASG-02, S08-ACC-08, S02-DEP-01, S08-DEP-01, S09-DEP-01, S11-DEP-01 |
| CR-004 | change request | OPEN | Interpretations built into the contracts need confirmation | G04-10, G04-11, G05-03, M-AUTH-13 |
| CR-005 | change request | OPEN | One shared policy for what may be done with a flagged tick | S01-ASG-06, S01-ASG-08, G03-12, G08-06, S06-ACC-03 |
| CR-006 | change request | OPEN | Additions to the shared contracts beyond the nine named by the brief | S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-ASG-25, COM-06 |
| CR-007 | change request | OPEN | Evidence fields of a risk decision proposed by session 08 | S08-ASG-01, S08-ASG-05, G04-02, G04-03, G04-08 |
| CR-008 | change request | OPEN | Attribution of an estimate is recorded but not yet checked | S05-ACC-07, S11-ASG-05, S02-DEL-01, S04-ASG-08 |
| U-01 | open item | OPEN | Access to the existing system `D:\ftmo_eurusd` | S00-ASG-01, S00-DEL-04 |
| U-02 | open item | OPEN | Where will GIE finally live? | COM-03, S11-ACC-05 |
| U-03 | open item | OPEN | Meaning of "proposed recovery at -1.75%" | G05-03, S08-ASG-11, S08-ACC-02 |
| U-04 | open item | OPEN | Actual account rules | G05-10, G05-11, S08-ASG-08, S08-ASG-09, S08-ASG-10, G06-05 |
| U-05 | open item | OPEN | Definitions behind the quoted baseline figures | G09-02, G09-11, G09-12, G09-13, G09-07, G09-08 |
| U-06 | open item | OPEN | Confirm that "paper" excludes broker demo accounts | M-AUTH-13, G08-10 |
| U-07 | open item | OPEN | Definition of an "eligible day" | G01-07, G10-08, S10-ACC-06 |
| U-08 | open item | OPEN | Runtime environment | S00-ASG-06, G03-07 |
| U-09 | open item | OPEN | Authorised market data | S01-ASG-10, G06-06, G06-10, G06-11, G06-12, G06-13, G06-14, G06-15, G06-16, G06-17, G09-06, G09-10 |
| U-10 | open item | OPEN | Instrument specification of XAUUSD at the broker | S07-ASG-15, S08-ASG-02 |
| U-11 | open item | OPEN | Further shared contracts proposed by later sessions | COM-06, S02-DEP-01, S08-DEP-01, S11-DEP-01 |
| U-12 | open item | OPEN | Confirm the two interpretations in D-012 | G04-10, G04-11 |
| U-13 | open item | OPEN | How supervisor review reaches the coordinator | OWN-04, M-AUTH-03 |
| U-14 | open item | OPEN | Are the -2 % and +5 % levels adjustable research parameters as well? | G05-01, G05-02, G05-05, G05-06, M-DIR-08 |
| U-15 | open item | OPEN | Scope of "ablations" in brief 10 | S10-ASG-04, S10-ASG-05, S10-ASG-06, S10-ASG-07, S10-ASG-08, S10-ASG-09 |
| U-16 | open item | OPEN | Which "uncertainty" gates a decision | G02-04, S04-ASG-08, S05-ASG-13, S05-ACC-05, S11-ASG-04, G10-18 |
| U-17 | open item | OPEN | Which model statuses may lead to paper orders | G07-08, G07-09, G07-10, G07-12, G04-08 |
| U-18 | open item | OPEN | Read-only access for the paper data connection | M-AUTH-13, M-AUTH-15, G08-10, S01-ASG-01 |

## Decisions

- PROPOSED: 26
- APPROVED: 1
- REJECTED: 0
- SUPERSEDED: 1

| id | status | title | requirements |
|---|---|---|---|
| D-001 | APPROVED | One coordinating session with sub-agents instead of thirteen separate sessions | OWN-01, OWN-06, COM-01, M-AUTH-09, M-DIR-06 |
| D-002 | PROPOSED | Repository layout: everything importable lives in the `gie/` package | COM-03, S00-ASG-08, S00-DEP-02, S12-DEP-02 |
| D-003 | PROPOSED | Implementation language: Python 3.11+ | S00-ASG-02, G03-07, G03-08, G03-09 |
| D-004 | PROPOSED | Contracts are frozen standard-library dataclasses declared once | S00-ASG-11, S00-ASG-12, S00-ASG-13, S00-ASG-14, S00-ASG-15, S00-ASG-16, S00-ASG-17, S00-ASG-18, S00-ASG-19, S00-DEL-02 |
| D-005 | PROPOSED | Numeric representation | S00-ASG-20, S00-ASG-21, G05-08 |
| D-006 | PROPOSED | Subsystems never import each other | S00-ASG-10, COM-06, M-DIR-06, M-AUTH-10 |
| D-007 | PROPOSED | Contract set 1.0.0 and its change procedure | M-DIR-02, S00-ACC-02, COM-06 |
| D-008 | PROPOSED | Canonical JSON and deterministic identifiers | S00-ASG-22, S00-ACC-01, G08-07, G02-02 |
| D-009 | SUPERSEDED | Total order by availability time | S00-ASG-25, S10-ACC-02, S10-ACC-03 |
| D-010 | PROPOSED | Freshness fails closed at the boundary | S00-ASG-23, G03-12, G06-04 |
| D-011 | PROPOSED | Paper-only routing lock; "paper" means in-process simulation | G08-03, G08-10, M-AUTH-13, M-AUTH-19, OWN-14, COM-11, M-DIR-07 |
| D-012 | PROPOSED | Master-specification safety rules are structural contract invariants | G04-09, G04-10, G04-11, G04-12, G04-13, G05-04, G05-08, G07-09, G07-10 |
| D-013 | PROPOSED | Trading day is the Europe/Prague calendar date | G05-10, S08-ASG-08 |
| D-014 | PROPOSED | Provisional vocabularies pending the audit | S02-ASG-06, S02-ASG-08, S01-ASG-06 |
| D-015 | PROPOSED | Requirement traceability and evidence are generated, never typed | OWN-08, OWN-16, OWN-17, OWN-18, M-AUTH-16, M-AUTH-17, M-AUTH-26, M-GATE-01, M-GATE-03 |
| D-016 | PROPOSED | Branching | M-AUTH-09, M-AUTH-10, M-AUTH-11, M-DIR-06 |
| D-017 | PROPOSED | Baselines are frozen by hash; quoted results are unverified claims | G02-08, G02-09, G09-01, G09-02, G09-07, S00-ACC-05 |
| D-018 | PROPOSED | Research parameters live in one labelled file | M-DIR-08, G05-05, G01-11 |
| D-019 | PROPOSED | `RunManifest` is added as a tenth shared contract | S10-DEL-03, S10-DEL-04, S10-ASG-10, S10-ACC-01, M-AUTH-16, M-GATE-05, S11-ASG-15 |
| D-020 | PROPOSED | The order of a run is its journal; inputs are ordered by availability | S00-ASG-25, S10-ASG-01, S10-ACC-01, S10-ACC-02, S10-ACC-03, S07-ACC-08, G08-04 |
| D-021 | PROPOSED | One order gateway enforces authorisation, lifecycle and the routing lock | G08-03, G08-07, G08-10, G04-09, G04-10, G04-11, G04-12, G04-13, S07-ACC-06, S08-ACC-06, M-AUTH-19, OWN-14 |
| D-022 | PROPOSED | Timers come from an injected scheduler; there are no timer threads | S00-ASG-19, S00-ACC-03, G03-12, G08-06, S06-ASG-06, S10-ASG-01 |
| D-023 | PROPOSED | Tests and reviews are written by someone other than the implementer | OWN-04, OWN-09, M-AUTH-06, M-AUTH-08, S12-ASG-08, COM-10 |
| D-024 | PROPOSED | Step time: a component sees the instant of the input it is processing | S00-ASG-19, S00-ASG-21, S00-ACC-03, S10-ASG-01, S10-ACC-01, S10-ACC-02, S06-ASG-01, G03-10, G03-11 |
| D-025 | PROPOSED | Order events have one writer; the broker may create protective orders | S07-ASG-01, S07-ASG-06, S07-ASG-09, S07-ACC-02, S06-ASG-03, G08-07, G08-08, G04-12 |
| D-026 | PROPOSED | Which model may lead to a paper order depends on the role of the run | G07-08, G07-09, G07-10, G07-12, G04-08 |
| D-027 | PROPOSED | A run is identified by the content of its manifest | S10-ACC-01, S10-DEL-03, S10-DEL-04, S10-ASG-10, M-AUTH-16, M-GATE-05, S08-ASG-09 |
| D-028 | PROPOSED | Amounts decide, fractions report | S08-ASG-06, S08-ASG-07, S08-ACC-01, S08-ACC-02, S08-ACC-03, S08-ACC-04, S08-ACC-05, G05-02, G05-04, G05-06 |
