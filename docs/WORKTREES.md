# Branch and worktree procedure

Deliverable S00-DEL-05 ("worktree instructions"); requirements S00-ASG-08, M-AUTH-09, M-AUTH-10,
M-AUTH-11, M-DIR-06, D-001, D-016.

## Branches

| branch | purpose | who writes |
|---|---|---|
| `main` | untouched default branch of the repository | nobody in this project phase |
| `claude/gold-intelligence-engine-l9uw10` | development branch; the only branch that is pushed | coordinator only |
| `gie/sNN-<name>` (local) | work of one session, e.g. `gie/s01-market-data` | the sub-agent of that session |

Rules:

1. Two gates, as defined in `specifications/dependency_graph.yaml`:
   * **Start gate.** A session branch is created from the development branch once the owner has
     approved the Session 00 contracts and has opened the session's wave. Sessions of the same wave
     start together; a dependency that is not integrated yet is replaced by explicitly labelled test
     fixtures (COM-07) and the session may then not claim integration complete (COM-08).
     `contract_only` dependencies never delay a start.
   * **Integration gate.** A session branch is merged only when the review packages of all its
     `depends_on` sessions are approved and its own package is approved.
2. A session edits only the paths it owns (`owns` in the dependency graph). Shared files
   (`gie/core`, `gie/contracts`, `specifications/`, `docs/`, `CLAUDE.md`, `pyproject.toml`,
   `tools/`) are changed only by the coordinator; a session that needs a change files a change
   request in `docs/CHANGE_REQUESTS.md`.
3. A session ends by committing its review package under `reviews/session_NN/` and stopping.
4. The coordinator merges a session branch into the development branch only after the owner has
   approved that review package ("Do not merge without owner approval"). Merges are
   `--no-ff` so that each session stays one identifiable unit; the full CI must pass on the result.
5. No pull request is opened unless the owner asks for one. No force-push. `main` is never updated.

## Worktrees for parallel sessions

Parallel sessions never share a working directory. From the repository root:

```bash
# create (coordinator)
git worktree add -b gie/s01-market-data ../gie-s01 claude/gold-intelligence-engine-l9uw10
git worktree add -b gie/s02-strategy    ../gie-s02 claude/gold-intelligence-engine-l9uw10

# work (session sub-agent, inside its own worktree only)
cd ../gie-s01
python -m pip install -e ".[dev]"      # once per environment
python -m tools.ci                     # must pass before the review package is written

# integrate (coordinator, after owner approval of reviews/session_01/)
cd /path/to/main/worktree
git merge --no-ff gie/s01-market-data
python -m tools.ci
git push -u origin claude/gold-intelligence-engine-l9uw10

# clean up
git worktree remove ../gie-s01
```

In this single-session setup (D-001) the sub-agents are started with worktree isolation, which
performs the `git worktree add` step automatically; the rules above are identical.

## Keeping a session branch current

When the development branch advances (another session was integrated), the coordinator rebases or
merges it into the still-open session branches and re-runs CI there; sessions do not pull from each
other.

## The production tree

`D:\ftmo_eurusd` is **not** a worktree of this repository and is never written to by anything in
this project. The only tool that touches it is the read-only audit
(`tools/audit/gie_baseline_audit.py`), run by the owner.
