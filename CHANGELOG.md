# Changelog

## 0.2.0 — 2026-11-11 — Recoverability, storage, job ledger, launchers
### Added
- **Project memory & recovery engine** (`nexus/state.py`): `NEXUS_STATE.json`, auto-updated block in `NEXUS_HANDOFF.md`,
  task registry (`nexus/registry/tasks.yaml`), `nexus checkpoint|resume|status|tasks`. Checkpoints refuse secrets, media,
  weights, databases and files > 2 MB, and never force-push.
- **Durable job ledger** (`nexus/jobs.py`, `nexus/workflows.py`): jobs are database rows with stage checkpoints, heartbeats,
  retries and crash recovery; separate worker process (`nexus worker`). **Every** API job runs on it (create, render,
  localize, analyze, remix, package, series, film, animate, backup); the in-memory queue is gone.
- **Federated storage** (`nexus/storage/`): `StorageBackend` interface, local and rclone backends, provider registry with
  zero-spend eligibility and free-allowance caps (`nexus/registry/storage.yaml`).
- **Verified backups** (`nexus/backup.py`): content-addressed, incremental, optionally AES-256-GCM encrypted snapshots;
  post-backup size + SHA-256 verification; restore into a new directory with path relocation and reference check;
  restore drill; pruning. `nexus backup|verify|restore|storage`.
- **One-command start/stop** (`nexus/launcher.py`): preflight checks, job recovery, worker + API as background processes,
  health check, browser. Windows launchers `NEXUS_*.bat` + `scripts/windows/setup.ps1`.
- UI: Storage & Backup page; job table shows stages, resume count and a Resume button. API: `/api/health`, `/api/storage`,
  `/api/backup`, `/api/jobs/{id}/retry|cancel`.
- CI (`.github/workflows/ci.yml`): lint + 28 fast tests on Linux and Windows, tree scan for secrets/media. No models.
- Stage timing metrics (`nexus profile`).
### Fixed
- Fresh clone: `nexus models fetch` crashed because the data folder did not exist yet.
- Create-from-idea produced one-sentence scenes (18 s video for 4 scenes): scene-depth gate, reviewer findings are fed back
  to the writer, prompt wording A/B-tested against the local model, empty LLM answers are retried instead of rendered.
- Create-from-idea found no stock images for descriptive queries (every scene fell back to a gradient card): query ladder.
  `thumb.wikimedia.org` declared as a Wikimedia Commons host.
- Jobs were lost (and shown as "running" forever) when the app restarted.
- Subtitle cues straddled sentence boundaries; they now break at sentence/clause ends.
- Font paths were Linux-only (Windows font candidates added). SQLite opened in WAL mode for API + worker processes.

## 0.1.0 — 2026-11-11 — Foundation (previous session)
- Cost guard + component manifest, FFmpeg wrapper, ASR/TTS/MT, timeline renderer with segment cache, analysis, editing
  proposals, QA with auto-correction, localization, remix, create-from-idea, Manim/Blender/motion graphics, series and film
  planning, compute federation, content factory, publishing packages, REST API, single-page UI, research catalog (113 entries).
