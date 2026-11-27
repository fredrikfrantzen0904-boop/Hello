# GIE baseline audit - instructions for the owner

`tools/audit/gie_baseline_audit.py` makes an inventory of the existing system in `D:\ftmo_eurusd` so that the
Gold Intelligence Engine project can describe it without ever touching it. You run it on your own
machine, look at the result, and send the result folder back. (Requirements S00-ASG-01..07,
S00-ACC-06 "no production changes", S00-DEL-04, M-AUTH-12, M-AUTH-15; blocker B-001.)

## What it does and does not do

| It does | It does NOT |
|---|---|
| read the files under `--root` (read-only, mode `rb`) | create, change, rename, delete or "touch" anything under `--root` |
| write 3-4 text files into a **new** folder `--out` | write anywhere else, or overwrite an existing file |
| list files with size, date and SHA-256 fingerprint | copy file contents |
| report *where* keywords occur (file, line number, rule name) | report the text of the line or the matched value |
| read package names and versions from `requirements.txt` etc. | report URLs, tokens, passwords (such lines are dropped and counted) |
| report column names of `.csv` / `.tsv` files | report data rows |
| list files that look like secrets (`.env`, `*.key`, `*password*` ...) | open or fingerprint them (unless you add `--hash-sensitive`) |
| report the computer name only as a fingerprint | use the network, start `git`, run or import anything from the folder |

While it runs, a guard inside Python blocks any attempt of the tool itself to write below `--root`,
to use the network, or to start a program other than the two Windows queries of `--processes`.

## How to run it (Windows PowerShell)

You need Python 3.8 or newer and nothing else: the tool is the single file `tools/audit/gie_baseline_audit.py`
(standard library only, no installation). Use a Python that is installed **outside**
`D:\ftmo_eurusd` (not the virtual environment of the trading system) and keep the tool **outside**
that folder too - it refuses to start otherwise. Best time: when the trading system is idle
(weekend / market closed), because fingerprinting reads every file once.

```powershell
cd C:\path\to\this\repository
python tools\audit\gie_baseline_audit.py audit --root D:\ftmo_eurusd --out C:\gie_audit\run1 --processes --count-rows
```

* `--root` the folder to look at. `--out` a folder that does not exist yet (or is empty) and is not
  inside `--root`.
* `--processes` (optional) also lists running programs and scheduled tasks that may belong to the
  trading system: program name, process id, memory; for a task its name, status, last/next run and
  the program it starts. Arguments of tasks are replaced by `<redacted>`; command lines of running
  programs are never read.
* `--count-rows` (optional) counts the rows of `.csv` / `.tsv` files.
* Progress is printed while it runs. Exit code 0 = finished (files that could not be read are listed
  in the result, they do not stop the run); 2 = the arguments were refused and nothing was done;
  3 = the built-in guard had to block something (never expected - please send `audit_log.txt`).

Other options: `--hash-sensitive`, `--max-hash-bytes N` (skip fingerprinting of files larger than N
bytes), `--exclude-dir NAME`, `--no-default-excludes`, `--include-venvs`, `--no-csv-headers`,
`--max-line-count-bytes N`, `--max-scan-bytes N`, `--max-list-entries N`, `--progress-every N`.
`python tools\audit\gie_baseline_audit.py audit --help` shows all of them.

## How to verify that nothing changed

Take a fingerprint list ("manifest") before and after, and compare the two:

```powershell
python tools\audit\gie_baseline_audit.py manifest --root D:\ftmo_eurusd --out C:\gie_audit\before.jsonl
python tools\audit\gie_baseline_audit.py audit    --root D:\ftmo_eurusd --out C:\gie_audit\run1 --processes --count-rows
python tools\audit\gie_baseline_audit.py manifest --root D:\ftmo_eurusd --out C:\gie_audit\after.jsonl
python tools\audit\gie_baseline_audit.py compare  C:\gie_audit\before.jsonl C:\gie_audit\after.jsonl
echo $LASTEXITCODE
```

`compare` prints which files were `added`, `removed` or `changed`. `"content_identical": true` and
exit code 0 mean that no listed file differs. Files whose date changed but whose content is the same
are listed under `mtime_only` (add `--strict-mtime` to treat that as a failure). Files without a
fingerprint (the secret-looking ones) are listed under `unverified`; they count as changed only if
their size or date changed. **An `unverified` file is not proven unchanged**, even when the exit
code is 0: for a complete proof take both manifests with `--hash-sensitive --no-default-excludes
--include-venvs` (the fingerprints of secret-looking files then appear in the manifest - only
fingerprints, never contents - so keep those two files private). The acceptance test "no production
changes" of the project accepts only manifests without unverified files.

If the trading system is running, it changes its own files (logs, data, caches). Those show up as
differences - they come from the trading system, not from the audit. A manifest takes as long as the
audit; if that is too long, compare `C:\gie_audit\run1\manifest.jsonl` with one later manifest.

## What to review before sending the folder back

All files in `--out` are plain text; open them in an editor and search for anything you do not want
to share.

| file | content |
|---|---|
| `inventory.json` | hardware summary, counts by language / folder / file type, tests, data files (column names), model files, where broker interfaces and credential-like lines occur (locations only), launch scripts, package names, Python environments, git branch and commit, excluded folders, everything that was skipped |
| `manifest.jsonl` | one line per file: path, size, date, SHA-256 |
| `processes.json` | only with `--processes`: programs and scheduled tasks as described above |
| `audit_log.txt` | what was done, with counts; the last line must be `READ-ONLY CHECK: no file under root was opened for writing` |

Please look at these points in particular:

* **Names are reported as they are.** File and folder names, git branch names, task names and the
  `--root` / `--out` paths you typed (they may contain your Windows user name) appear in the output.
* `credential_scan` in `inventory.json` lists where passwords, logins or tokens seem to be written
  in ordinary files (for example `config.py`, line 12). The values are not in the output - but the
  SHA-256 of such a file is in `manifest.jsonl`. For a file that contains little more than the
  secret, delete its line from `manifest.jsonl` before sending and tell us that you did.
* `data` -> `files` -> `columns`: the first line of a `.csv` is only reported when every field looks
  like a column name. Check that no entry shows data. `--no-csv-headers` switches this off.
* `dependency_manifests`: package names and versions only; `dropped` counts the lines left out.

## Known limitations

* **Developed and tested on Linux.** The Windows-specific parts (opening files without blocking
  other programs, paths longer than 260 characters, junction detection, memory size, CPU name,
  `tasklist` / `schtasks`) were checked by static analysis and with simulated input only, not on a
  real Windows machine. If one of them fails, the tool records the error and continues; please send
  `audit_log.txt` and the console output in that case.
* Windows may update the "last access" timestamp of files that are read; this depends on a system
  setting (`fsutil behavior query disablelastaccess`). Content, size and modification time are not
  affected.
* Reading every file takes time and disk activity (roughly 100-500 MB per second) and may trigger
  your antivirus scanner. Use `--max-hash-bytes` to skip very large files; they are then listed as
  not fingerprinted.
* A file that the trading system has locked exclusively cannot be read; it is listed under
  `unreadable`. A file that changes while it is read is marked `unstable`.
* Folders named `.git`, `__pycache__`, `node_modules`, `.venv`, `venv`, `env`, `.mypy_cache`,
  `.pytest_cache`, `.ruff_cache`, `.idea`, `.vscode` and detected Python environments are only
  counted (number of files, total size); `compare` does not see changes inside them. Use
  `--no-default-excludes --include-venvs` for a complete manifest.
* Links and junctions are listed (in the inventory), not followed, and are not part of a manifest,
  which holds regular files only; cloud "online-only" files are listed, not read. The baseline
  freeze of the project refuses a baseline whose patterns reach a link or a special file.
* Secret-looking files are recognised by their **name** only. The keyword and credential scans are
  heuristics over the first 8 MiB of source files and the first 1 MiB of configuration files: they
  give hints, not proof that something is present or absent.
* Process information is a snapshot of one moment. Scheduled tasks below `\Microsoft\` are only
  counted. Tasks of other users may be invisible without administrator rights (do not run the tool
  as administrator just for this).
* Git: branch, commit and worktree names are read from files; whether there are uncommitted changes
  is not determined (that would require running `git`).
* Hardware: operating system, CPU, core count, memory and disk size. Nothing about GPU or network.
* To read the Windows version, Python's own `platform` module may run the built-in Windows command
  `ver` once at start-up (depends on the Python version). Apart from that and the two queries of
  `--processes`, no program is started.
