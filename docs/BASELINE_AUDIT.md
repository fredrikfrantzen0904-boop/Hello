# Baseline audit - status report

Deliverable S00-DEL-04. Requirements S00-ASG-01 .. S00-ASG-07, S00-ACC-05, S00-ACC-06.

## Verdict

**NOT PERFORMED - BLOCKED (B-001).** The audit of the existing system `D:\ftmo_eurusd` could not be
carried out in Session 00 because that directory is on the owner's machine and is not reachable
from the cloud session in which this work ran. This document records exactly what was and was not
examined. Nothing below describes the existing trading system, because nothing about it was seen.

## 1. What was examined

Only the Git repository attached to this session.

| item | fact | how obtained |
|---|---|---|
| repository | `fredrikfrantzen0904-boop/Hello` | session configuration |
| branches before this work | `main` and `claude/gold-intelligence-engine-l9uw10`, both at commit `35e32cff311eef08294540fd88d9a01a5a2f7c41` ("Create Start", 2026-11-27 10:17:33 +0100) | `git log`, `git rev-parse` |
| files before this work | one file: `Start`, 203 bytes, git blob `ff972e3e86e8628ee696ede5ca1a3c8f92fe1197`, SHA-256 `ba4580ba3d0e2b046fb11612e871ef985974fdb535da4a5ab329dc4c4e05e450` | `git ls-tree -r`, `sha256sum` |
| content of `Start` | seven lines of generic git bootstrap commands (create README, init, add remote, push); no trading code | read |
| trading code, engines, data, tests, dashboards in this repository | none | `git ls-tree -r origin/main` |

`Start` is registered as a protected path (`specifications/dependency_graph.yaml`); CI verifies on
every run that it is byte-identical to the original (`tools/check_protected_paths.py`). `main` has
not been modified.

## 2. What the specification asks for and its state

| requirement | asked | state |
|---|---|---|
| S00-ASG-01 | audit `D:\ftmo_eurusd` without modifying production | blocked; read-only audit tool delivered (`tools/audit/`) |
| S00-ASG-02 | inventory languages | unknown |
| S00-ASG-03 | inventory processes | unknown |
| S00-ASG-04 | inventory data | unknown |
| S00-ASG-05 | inventory tests | unknown |
| S00-ASG-06 | inventory hardware | unknown (the owner's machine); the development container is described in the review evidence only so that measured timings can be interpreted |
| S00-ASG-07 | inventory broker interfaces | unknown |
| S00-ACC-05 | baseline regression | harness delivered and self-tested on a labelled synthetic fixture; real check reports BLOCKED |
| S00-ACC-06 | no production changes | holds trivially for the production tree (it was never accessed); verified for this repository; the audit tool is tested to leave an audited tree byte-identical |

## 3. What is known about the existing system - from the specification only

These statements are quoted from the master specification. They are **claims to be reconciled**,
not findings of this audit (see `specifications/baselines/claims.yaml`, decision D-017, open item U-05).

* Six engines exist: Cavemen, Silver Bullet, Liquidity Hunter, ORB + VWAP, VWAP Trend Rider,
  Sweep & Reversal (G02).
* Existing systems that must not be modified: Gold Bot, AI trader, breakout bot, GCE baselines
  (authority clause). Baselines named: GCE-B, "exact Round 1 baselines" (G02).
* Round 2: 23,481 setups; GCE-B 134 trades, +0.462R, 18 % day coverage, 4.9 % drawdown at 0.5 % risk;
  no corrected statistical significance; a learned score with 96 % coverage but a negative recent
  period; raw ticks were not stored in Round 2 (G09).
* Open reconciliations named by the specification: 429 vs 473 Cavemen trades, daily breach
  denominators, live delay (G09).
* A dashboard with existing tabs exists (brief 11); notifications currently have a five-minute delay
  that must not be carried over (brief 02, "no five-minute notification delay").

## 4. How the audit will be completed

1. The owner chooses an access route (open item U-01). Recommended: run the delivered tool.

   ```powershell
   python tools\audit\gie_baseline_audit.py audit --root D:\ftmo_eurusd --out C:\gie_audit\run1 --processes
   ```

   It only reads; it writes nothing under `D:\ftmo_eurusd`, uses no network and emits no
   credentials. It copies no file contents except three narrow kinds the inventory needs: package
   names and version specifiers from dependency files, the column names of CSV / TSV files, and
   Python version strings. Files with secret-like names, files in excluded directories and files
   above the size cap are listed but not hashed (details and limits: `tools/audit/README.md`).
2. The owner reviews the output folder and uploads it to this session.
3. The coordinator writes the inventory (languages, processes, data, tests, hardware, broker
   interfaces) from `inventory.json` / `processes.json`, identifies the baseline artifacts, and the
   owner confirms which files constitute GCE-B, Round 1 and Round 2.
4. The baselines are frozen (`python -m tools.baseline_freeze freeze ...`) and the manifests are
   committed; from then on `S00-ACC-05` runs for real wherever the artifacts are present.
5. Decisions D-003 (language) and D-014 (provisional vocabularies) are revisited; an addendum to the
   Session 00 review package is submitted.

## 5. Proof obligations for "no production changes"

* The audit tool is tested to leave an audited tree byte-identical with unchanged modification
  times, and to refuse an output directory inside the audited tree
  (`tests/gie/tools/test_baseline_audit.py`).
* On the owner's machine: run `manifest` before and after any GIE activity and `compare` them; an
  identical manifest is the proof. The first manifest also becomes the reference for all later sessions.
