# Disk management

## The incident (development container, 2026-11-13)
During the Account 2 Demo 1 render the shared cloud development container ran out of writable space. Measured before any
cleanup (`docs/results/a2_storage/disk_before.json`, produced by `scripts/maintenance/dev_container_inventory.py`):
**3.09 GB free**, with
| Location | Size | Owner / purpose | Decision |
|---|---|---|---|
| model weights `.nexus/models` | 6.96 GB | NEXUS install (Whisper, Piper, LLM, MADLAD, SigLIP) | kept |
| agent scratch (render benchmark homes, 12 copies of a 312 MB home + renders) | 6.78 GB | Account 2 render benchmark; results in `a2_render_bench.json` | deleted |
| pip download cache | 3.79 GB | pip; re-downloadable | purged |
| Agent D workdir (incl. a 928 MB test venv) | 2.91 GB | generation/enhancement benchmark; results recorded | deleted |
| Agent A / Agent C workdirs | 1.47 GB | renderer and audio benchmarks; results recorded | deleted |
| old smoke/test homes, superseded Demo 3 run | 1.5 GB | earlier sessions; results recorded | deleted |
| pytest `tmp_path` of the last 3 runs | 0.31 GB | rendered test videos | deleted; retention now "failed tests only, last run" |
| demo homes (Demo 1/2/4 finals, audio review package) | 1.46 GB | evidence for PR #6 | kept (finals); caches left for the disk manager |

After cleanup: **19.74 GB free** (`disk_after.json`). Nothing of the application, no test code, no fixture, no licence
manifest and no small machine-readable evidence was deleted.

**What caused the accumulation:** every benchmark/demo/test run created a complete NEXUS home (cache + projects) and
nothing ever bounded or removed them; render caches had no quota; pytest kept three runs of rendered videos.
**This was the development container, not the owner's machine** - the measurements say nothing about the owner's disk.

## What prevents it now
1. **Bounded caches.** Render and media caches have a quota (`cache_quota_gb`, default 20 GB); a cache hit refreshes the
   entry's time, and the least recently used entries are evicted first (`cache_lru` rule).
2. **Preflight.** Every render estimates the bytes it needs (final + segments + pieces + margin; checked against a real
   render in `test_render_estimate_covers_a_real_render`) and is refused before it starts if it would leave less than
   `min_free_gb`. LRU eviction runs first. Downloads and asset installs preflight too.
3. **Finished outputs leave.** A verified external archive copy releases the local final video and its intermediate
   segments/pieces (`intermediate_after_archive`, shared intermediates are kept).
4. **Stale files are recognised by rule:** partial downloads (`incomplete_download`), abandoned temp/render-work files
   (`abandoned_temporary`), old draft previews (`stale_preview`), logs of finished jobs (`old_job_log`), rotated logs.
5. **Logs rotate** (server/worker at 20 MB × 3 generations; the disk-manager journal at 2 MB × 2).
6. **Tests clean up after themselves:** `tmp_path_retention_policy = "failed"`, `tmp_path_retention_count = 1`
   (`pyproject.toml`). The test suite itself is untouched.

## Safety rules (enforced in code, tested)
Cleanup is **plan -> execute**: `plan()` lists every file, its size, the rule and the reason; `execute(plan_id)` deletes
exactly that list and refuses if anything changed since (new file, file used, file now protected). Each file is
re-validated immediately before deletion. Only regular files are deleted; directories are removed only when empty.

Never deleted (`protected()` / `check_deletable()`):
- anything outside NEXUS_HOME and the working directory (user documents), symbolic links;
- `secrets/`, the vault, `nexus.db`, `settings.json`, `storage.json`, `storage_rules.json`, `disk.json`, `backups/`;
- project source media (`projects/*/media`) and exports; every file a timeline references (clips, layers, audio,
  music, LUTs, overlays);
- final renders (only `nexus/archive.py` may release one, after a verified external copy);
- files of a project with an active job; cache entries used after the oldest running render started;
- models (only abandoned `*.part` downloads, and not while a model download runs);
- asset library files (the library manages its own quota, references and favourites);
- the local file of any archive record that is not `released` (the only copy).

## Using it
- UI: **Storage & Backup** page -> "Disk on this computer": free space, active renders, every category, local vs archived
  final videos, limits, and "Show what can be cleaned" -> "Delete exactly these N files".
- CLI: `python -m nexus disk status | plan [--rule R] | clean --plan <id> | settings`.
- API: `GET /api/disk`, `POST /api/disk/settings`, `POST /api/disk/cleanup` (dry run without `plan_id`).

## Settings (`NEXUS_HOME/disk.json`)
`warn_free_gb` 10 · `min_free_gb` (defaults to settings `min_free_disk_gb`, 2) · `cache_quota_gb` 20 ·
`asset_cache_quota_gb` 10 · `preview_max_age_days` 7 · `tmp_max_age_hours` 12 · `job_log_max_age_days` 30 ·
`log_max_mb` 20 · `log_keep` 3 · `render_margin` 1.25. Working directory: environment variable `NEXUS_WORK_DIR`.

Tests: `tests/fast/test_a2_diskman.py` (usage, thresholds, preflight + eviction, rules, protection, changed plans,
symlinks/outside paths, active renders, intermediates after archive, log rotation, render estimate, work directory).
