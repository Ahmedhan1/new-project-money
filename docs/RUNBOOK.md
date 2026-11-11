# Runbook

## Windows (the normal way)
| File | Does |
|---|---|
| `NEXUS_SETUP.bat` | One-time install (re-runnable): Python env, packages, FFmpeg check, models, doctor. `-SkipModels` to defer the 7 GB download |
| `NEXUS_START.bat` | Checks → recovers jobs → starts worker + API → waits for health → opens `http://127.0.0.1:8765` |
| `NEXUS_STOP.bat` | Stops both services. Running jobs resume from their checkpoint at next start |
| `NEXUS_STATUS.bat` | Services, jobs, last backup + drill, storage, Git state, next development tasks |
| `NEXUS_DOCTOR.bat` | Every component's version/health, hardware, zero-spend manifest, startup checks, storage, project memory |
| `NEXUS_BACKUP.bat` | Verified backup to all destinations + restore drill |
| `NEXUS_RESTORE.bat` | Restore latest snapshot into a new folder |
| `NEXUS_UPDATE.bat` | Stop → safety backup → `git pull --ff-only` → packages → doctor. Never touches project data |
| `NEXUS_CHECKPOINT.bat` | Save development state, commit safe files, push (never forces) |
| `NEXUS_RESUME.bat` | Print what a new session needs first |

**Status of these files:** the Python commands behind them are tested (Linux + Windows CI). The `.bat`/`.ps1` wrappers have
not yet been run on a real Windows machine — first run is task WIN-1; report anything that fails.

## Linux / macOS / any shell
```bash
scripts/setup.sh                       # or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m nexus start        # same behaviour as NEXUS_START
.venv/bin/python -m nexus status | stop | doctor | backup --drill | restore DIR | checkpoint | resume | tasks | profile | jobs list
```

## Settings
- Port: `NEXUS_PORT` env var, or `"port"` in `NEXUS_HOME/settings.json` (default 8765). Host is `127.0.0.1` (local only).
- Data location: `NEXUS_HOME` (default `.nexus` next to the code).
- Storage destinations: `NEXUS_HOME/storage.json` (`docs/STORAGE.md`).

## Troubleshooting
| Symptom | Do |
|---|---|
| "NEXUS is not installed on this computer yet" | Run `NEXUS_SETUP.bat` |
| "port … is already used" | Close the other program or `set NEXUS_PORT=8770` |
| Start fails after health wait | The last API log lines are printed; full logs in `.nexus/logs/api.log`, `worker.log` |
| Job stuck / failed | UI → Jobs → **Resume**, or `python -m nexus jobs list` / `jobs show ID` / `jobs retry ID`; per-job log in `.nexus/logs/jobs/` |
| Job shows `interrupted` | It was an in-process job (analysis/remix/series/animation) when the app stopped — run the action again (task JOB-2 moves these onto the durable ledger) |
| Backup `BLOCK` | Reason is printed: provider not eligible, not authorised, or passphrase missing |
| Checkpoint blocked | It lists the offending files (secret, media, > 2 MB). Remove or `.gitignore` them; nothing was committed |
