# NEXUS VIDEO AI

Local-first, zero-spend AI video production: edit, remix, create from an idea, animate, dub and subtitle, plan series,
package for platforms. Everything runs on your computer with free, open-source tools; a cost guard blocks anything that
could bill.

## Start (Windows)
1. Double-click **`NEXUS_SETUP.bat`** (once; needs internet, ~7 GB of models).
2. Double-click **`NEXUS_START.bat`** → the app opens at `http://127.0.0.1:8765`.

Other launchers: `NEXUS_STOP`, `NEXUS_STATUS`, `NEXUS_DOCTOR`, `NEXUS_BACKUP`, `NEXUS_RESTORE`, `NEXUS_UPDATE`,
`NEXUS_CHECKPOINT`, `NEXUS_RESUME` — see `docs/RUNBOOK.md`. Linux: `bash scripts/setup.sh`, then `python -m nexus start`.

## Status
Early but real: the honest capability map is `docs/GAP_ANALYSIS.md`, test evidence is `docs/TEST_REPORT.md`, known
defects are `docs/KNOWN_ISSUES.md`.

## For developers and coding agents
Read **`NEXUS_HANDOFF.md`** first, then `AGENTS.md`. Work queue: `python -m nexus tasks`.
`python -m ruff check . && python -m pytest tests/fast -q` must pass before every change is checkpointed.

| Doc | Content |
|---|---|
| `PROJECT_BRIEF.md` | What NEXUS is and its non-negotiables |
| `docs/ARCHITECTURE.md` | Processes, data layout, invariants |
| `docs/STORAGE.md` · `docs/BACKUP_RECOVERY.md` | Where data lives, providers and the zero-spend rule, backup/restore |
| `docs/RUNBOOK.md` | Every launcher/command, settings, troubleshooting |
| `docs/DECISIONS.md` · `docs/PROGRESS.md` · `CHANGELOG.md` | Why, when, what changed |
| `docs/RESEARCH.md` | Catalog of 113 evaluated open-source components |
