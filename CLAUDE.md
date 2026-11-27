# Gold Intelligence Engine (GIE) - working rules

Paper / replay research system for **XAUUSD only**. Real order routing is **locked for the whole
project phase**. Read this file, `specifications/`, `docs/DECISION_LOG.md` and the shared contracts
before editing anything.

## Authority

- **Owner** (project owner): final authority; approves material changes, merges and model promotion.
- **ChatGPT**: architectural supervisor and independent reviewer (recommendations).
- **Claude (coordinator)**: implements; integrates reviewed work; never reduces agreed scope.

## Non-negotiable rules

1. **No live trading.** Never enable, weaken or bypass `gie/core/routing_lock.py`. No order may
   leave the process towards any broker, terminal or exchange - demo accounts included.
2. **Do not touch existing systems.** The pre-existing file `Start` and anything belonging to the
   existing Gold Bot, AI trader, breakout bot or GCE baselines must stay byte-identical. The
   production tree `D:\ftmo_eurusd` is read-only, always.
3. **Never silently simplify, invent data, or mark placeholders complete.** If something cannot be
   done, record a blocker / change request in `docs/CHANGE_REQUESTS.md` and report it. Unknown is
   reported as unknown.
4. **Test fixtures are labelled.** Synthetic data carries `DataOrigin.SYNTHETIC_FIXTURE` and lives
   under `tests/`; it is never presented as market data or performance evidence.
5. **Shared contracts are not redefined unilaterally.** `gie/contracts/` and `gie/core/` change
   only through the coordinator with a decision-log entry and the version rules of
   `specifications/SHARED_CONTRACTS.md` section 8.
6. **No credentials** in code, logs, fixtures, evidence or commits.
7. **Stop at the review gate.** Each session ends with a review package; nothing proceeds or
   merges without owner approval. Current state: **Session 00 only**; sessions 01-12 not started.
8. Numbers in the specification (latency targets, -2 % / -1.75 % / -3 % / +5 %, 5 % ROI) are
   **research parameters**, not approved limits and not established performance. They live in
   `specifications/research_parameters.toml`; never hard-code them.

## Where things are

| path | content |
|---|---|
| `specifications/source/`, `specifications/extracted/` | the owner's PDFs and their text (never edit) |
| `specifications/requirements_matrix.yaml` | every requirement with status (source of truth) |
| `specifications/acceptance_tests.yaml` | acceptance test register |
| `specifications/SHARED_CONTRACTS.md` | normative contract rules (units, time, ids, ordering, errors, versions) |
| `specifications/contracts/FIELDS.md` | generated field reference (do not edit) |
| `specifications/dependency_graph.yaml` | session dependencies, path mapping, import rules, protected paths |
| `specifications/research_parameters.toml` | proposed research parameters |
| `docs/DECISION_LOG.md` | decisions (D-nnn), open items (U-nn) |
| `docs/CHANGE_REQUESTS.md` | blockers (B-nnn) and change requests (CR-nnn) |
| `docs/IMPLEMENTATION_STATUS.md` | generated status summary |
| `docs/ARCHITECTURE.md`, `docs/WORKTREES.md`, `docs/BASELINE_AUDIT.md` | architecture, branch/worktree procedure, audit status |
| `gie/core/` | clock, ids, canonical JSON, freshness, ordering, latency, routing lock, errors (stdlib only) |
| `gie/contracts/` | the nine shared contracts, codec, schemas, compatibility checker, interfaces |
| `tools/` | audit, CI checks, register and review tooling |
| `tests/gie/<area>/` | tests; `tests/gie/samples.py` holds labelled synthetic sample records |
| `reviews/session_NN/` | review package (PDF + machine-readable evidence) per session |

## Code rules

- Python >= 3.11. `gie.core` and `gie.contracts` use the standard library only.
- Subsystem packages import only `gie.core` and `gie.contracts` (never each other); wiring happens
  in the coordinator's composition root. See `specifications/dependency_graph.yaml`.
- Time comes from an injected `gie.core.clock.Clock`. Never call `time.time()`, `datetime.now()`
  or similar in `gie/` code (except inside `gie/core/clock.py` and latency measurement).
- Prices, volumes, money and risk fractions are `decimal.Decimal`; never floats.
- Identifiers come from `gie.contracts.identity`; never `uuid4`, never random, never wall-clock.
- Errors derive from `gie.core.errors.GieError`; fail closed; never repair invalid input.
- No network calls, LLM calls or model training in any tick/decision path.

## Commands

```bash
python -m pip install -e ".[dev]"          # once
python -m tools.ci                         # full integration CI (same steps as .github/workflows)
python -m pytest                           # tests
python -m pytest --gie-evidence=out.json   # tests + machine-readable evidence
python -m ruff check . && python -m ruff format --check .
python -m mypy
python -m gie.contracts.schema_gen --check # schemas match code, lock intact, evolution rules hold
python -m tools.render_contract_docs --check
```

## Test rules

- Mark every test with the requirement ids it verifies:
  `@pytest.mark.requirement("S00-ASG-22")`, and acceptance tests additionally with
  `@pytest.mark.acceptance("S00-ACC-01")`.
- A test that cannot run because an external dependency is missing must `pytest.skip` with an
  explicit `BLOCKED: <id>` reason; it must never pass vacuously.
- Never weaken, skip or delete a test to obtain a green run. Report the failure instead.

## Git

- Development branch: `claude/gold-intelligence-engine-l9uw10`. Never push elsewhere, never push
  to `main`, never open a pull request unless the owner asks, never merge without owner approval.
- One logical change per commit; the commit message names the requirement ids.
