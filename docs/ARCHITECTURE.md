# Architecture (as built)

```
 Browser UI (nexus/ui/index.html, single page)            NEXUS_*.bat / python -m nexus <cmd>
        │  REST                                                   │
 ┌──────▼──────────── API process (nexus/api.py, FastAPI, 127.0.0.1) ──────────────┐
 │ submits jobs ─────────────┐        (no in-memory queue: every job is a ledger row) │
 └───────────────────────────┼──────────────────────────────────────────────────────┘
                             ▼
        SQLite (WAL)  NEXUS_HOME/nexus.db : projects · jobs (ledger) · assets+licences · cost ledger · metrics · docs
                             ▲
 ┌───────────────────────────┼──── Worker process (python -m nexus worker) ─────────┐
 │ jobs.claim → workflows.<handler>(job) → job.stage(name, fn) → checkpoint per stage │
 │ heartbeat every 5 s · recover() re-queues jobs whose worker died · heavy_lock      │
 └────────────────────────────────────────────────────────────────────────────────────┘
        │ uses
        ▼
 create · edit · remix · localize · series · animation/* · publish · factory        (production logic)
 media/* (FFmpeg, analysis) · speech/* (ASR, TTS, MT) · llm · timeline/* (ops, render, subs, text) · qa
 costguard  ← every tool/model/network call is authorised against registry/components.yaml (zero spend)
 compute    ← worker/provider adapters (local real; Kaggle/Colab adapters; remote GPU simulated)
 storage/   ← StorageBackend adapters (local, rclone) + provider eligibility (registry/storage.yaml)
 backup     ← snapshots, verify, restore, relocate, drill
 state      ← project memory: NEXUS_STATE.json, handoff auto block, tasks.yaml, checkpoint (scan → commit → push)
 launcher   ← preflight, start/stop services, health
```

**Data** (`NEXUS_HOME`, default `.nexus/`, never in Git): `nexus.db`, `projects/<id>/{media,timelines,audio,renders,reports,work}`,
`models/`, `assets/`, `cache/`, `logs/{api,worker}.log`, `logs/jobs/<id>.log`, `run/*.json` (pid files), `backups/` (default destination),
`settings.json`, `storage.json`, `last_backup.json`.

**Invariants**
- Originals are read-only; every timeline save is a new version.
- A job is a database row. A finished stage is never re-executed; its JSON output is the checkpoint.
- Nothing reaches the network or a tool without `costguard.authorize` / `check_host`.
- A backup is "OK" only after verification; a restore never writes into existing data.
