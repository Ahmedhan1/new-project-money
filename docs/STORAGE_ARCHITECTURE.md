# Storage architecture (Account 2: assets, working storage, daily archive)

Three layers, each with one owner and one rule set. Account 2 does **not** run its own storage router: everything that
leaves the machine goes through Account 1's storage layer (`nexus/cloud/storage`, PR #4).

```
            ┌────────────────────────────── this computer ──────────────────────────────┐
 render ──► │ A. LOCAL WORKING STORAGE   NEXUS_WORK_DIR or NEXUS_HOME/cache  (bounded)   │
            │    segments · pieces · drafts · proxies · temp audio · uploads            │
            │    + active projects NEXUS_HOME/projects (sources, timelines, finals)     │
            │    + bounded asset cache NEXUS_HOME/assets (quota, LRU, on demand)        │
            └──────────────┬───────────────────────────────────────────┬────────────────┘
                           │ install(id): generate / pinned download   │ archive_output(): upload + verify
                           ▼                                           ▼
            B. EXTERNAL ASSET SOURCES                       C. EXTERNAL PRODUCTION ARCHIVE
            pinned URLs + SHA-256 (fonts, CC0 packs);       Account 1 storage connections (Drive drive_a/b/c,
            nothing downloaded at install time              LocalProvider = another disk); folder NEXUS/Masters,
                                                            NEXUS/Thumbnails, NEXUS/ProjectFiles; retention "archive"
```

## A. Local working storage (Account 2, `nexus/diskman.py`)
| What | Where | Bound |
|---|---|---|
| Render segments / pieces / draft pieces / audio / overlay PNGs | `<work>/segments`, `pieces`, `pieces_draft`, `audio`, `overlays` | `cache_quota_gb` (20) LRU; intermediates of an archived final are released |
| Proxies, thumbnails, visual features, TTS, music, SFX, B-roll, generation | `<work>/proxies` … | same quota |
| Uploads, transfers, render checkpoints | `<work>/uploads`, `transfers`, `projects/*/work` | `tmp_max_age_hours` (12) |
| Draft previews | `projects/*/renders/*_draft_*`, `pieces_draft` | `preview_max_age_days` (7) |
| Library assets | `NEXUS_HOME/assets/<category>/` | `asset_cache_quota_gb` (10), LRU of re-creatable assets |
| Logs | `NEXUS_HOME/logs` | server/worker logs rotated by the launcher (Account 1, 20 MB × 5); job logs of finished jobs 30 days |

`<work>` is `NEXUS_WORK_DIR` when set (a fast local disk), otherwise `NEXUS_HOME/cache`. Every render first estimates
the space it needs and is refused before it starts when it would leave less than `min_free_gb` (`disk-preflight`
stage); unprotected LRU cache entries are evicted first. Settings: `NEXUS_HOME/disk.json`, the Storage page, or
`python -m nexus disk settings`.

## B. External asset storage (Account 2, `nexus/assetlib`)
The catalog (`nexus/registry/asset_library.yaml`) describes 182 assets; nothing is downloaded at installation time.
`install(id)` either generates the file locally (LUTs, overlays, sounds, templates: own work) or downloads one pinned file
with HTTP range resume and SHA-256 verification (OFL fonts from google/fonts at a fixed commit, one CC0 SFX pack).
Re-creatable assets above the quota are evicted (never favourites, user imports, or files a timeline uses) and come back
on demand, verified again. Hosts are declared in `nexus/registry/components.yaml`, so the Cost Guard allows them.

Large reusable libraries could also live in Account 1 storage as artifacts (kind `other`); this is not needed today
(the whole library, everything installed, is 116 MB) and is listed as a future option in the Account 1 handoff.

## C. External production archive (Account 2 logic on Account 1 storage, `nexus/archive.py`)
Final videos plus thumbnail, captions, QA/quality reports and an archive manifest go to the connection named in
`NEXUS_HOME/archive/settings.json` (`connection_id`), or to Account 1's placement order (`storage_rules.json`). Account 1
provides: resumable crash-safe upload, dedupe by SHA-256, `ArtifactManifest/v1` registry, verified fetch. Account 2 adds
the daily-output state machine, a strict verification before any local deletion, crash recovery, the archive browser
and retrieval. Details: `docs/DAILY_OUTPUT_ARCHIVE.md`.

### Which provider
The owner already authorised Google Drive through Account 1 (drive_a/b/c, real-verified by Account 1 on 2026-11-12, 15 GB
each, hard quota, cannot bill). That is the preferred destination: no new account, no billing. An external disk/NAS via
Account 1's LocalProvider also works and counts as external only if it is on a different device than `NEXUS_HOME`.
Backblaze B2 (10 GB, no card) is eligible in Account 1's registry if more space is needed; it needs an owner-created
account. Free space is never assumed unlimited: Account 1 refuses placement when a provider reports no limit, keeps
a reserve, and fails over only along the owner's order.

## Ownership summary
| Concern | Owner | Module |
|---|---|---|
| Storage providers, OAuth, resumable upload, artifact registry, placement rules, remote retention | Account 1 | `nexus/cloud/storage/*` |
| Backups of NEXUS_HOME | release candidate | `nexus/storage`, `nexus/backup.py` |
| Local disk measurement, preflight, cleanup, cache quotas | Account 2 | `nexus/diskman.py` |
| Daily output archive state, verification, release, retrieval | Account 2 | `nexus/archive.py` |
| Asset catalog, install, licences, previews, apply | Account 2 | `nexus/assetlib/*` |
