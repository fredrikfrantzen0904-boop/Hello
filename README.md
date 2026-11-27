# Gold Intelligence Engine (GIE)

Paper / replay **research** system for XAUUSD. **Real order routing is locked** for the whole
project phase: the repository contains no code that sends an order to a broker, terminal or
exchange, the vocabulary of the contracts has no live mode, and CI fails if either changes
(`gie/core/routing_lock.py`, `tools/check_live_lock.py`). A static scan and a runtime guard are not
a sandbox; what they cannot prevent is stated in `docs/ARCHITECTURE.md` section 6.

**Current state: Session 00 (foundation) submitted for owner review. Sessions 01-12 are not
started.** The existing system on the owner's machine has not been audited yet (blocker B-001 in
`docs/CHANGE_REQUESTS.md`); nothing about it is assumed here.

| start here | |
|---|---|
| Review package of Session 00 | `reviews/session_00/` (PDF + machine-readable evidence) |
| Working rules for every contributor | `CLAUDE.md` |
| The owner's specification (PDFs and text) | `specifications/source/`, `specifications/extracted/` |
| Every requirement and its status | `specifications/requirements_matrix.yaml`, `specifications/REQUIREMENTS_MATRIX.md` |
| Shared contracts | `specifications/SHARED_CONTRACTS.md`, `specifications/contracts/FIELDS.md`, `gie/contracts/` |
| Decisions, open items, blockers | `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md` |
| What each later session must propose first | `contract_extensions` in `specifications/dependency_graph.yaml` |
| Read-only audit tool for the owner's machine | `tools/audit/README.md` |
| Architecture | `docs/ARCHITECTURE.md` |

```bash
python -m pip install -e ".[dev]"
python -m tools.ci            # every quality gate; writes machine-readable evidence
```

The pre-existing file `Start` is preserved unchanged.
