# Gold Intelligence Engine (GIE)

Paper / replay **research** system for XAUUSD. **Real order routing is locked** for the whole
project phase; nothing in this repository can send an order to a broker.

**Current state: Session 00 (foundation) submitted for owner review. Sessions 01-12 are not started.**

| start here | |
|---|---|
| Review package of Session 00 | `reviews/session_00/` (PDF + machine-readable evidence) |
| Working rules for every contributor | `CLAUDE.md` |
| The owner's specification (PDFs and text) | `specifications/source/`, `specifications/extracted/` |
| Every requirement and its status | `specifications/requirements_matrix.yaml`, `specifications/REQUIREMENTS_MATRIX.md` |
| Shared contracts | `specifications/SHARED_CONTRACTS.md`, `specifications/contracts/FIELDS.md`, `gie/contracts/` |
| Decisions, open items, blockers | `docs/DECISION_LOG.md`, `docs/CHANGE_REQUESTS.md` |
| Architecture | `docs/ARCHITECTURE.md` |

```bash
python -m pip install -e ".[dev]"
python -m tools.ci            # every quality gate; writes machine-readable evidence
```

The pre-existing file `Start` is preserved unchanged.
