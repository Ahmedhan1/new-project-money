# Asset library + storage integration results

Branch `work/account-2-asset-library-storage` (from `d979df1`), PR #7 (draft, base = PR #6's branch; nothing merged).
Machine: shared 4-vCPU cloud development container, no GPU. Every number below was measured on 2026-11-13.

## 1. Disk incident: before / after
| | Free (writable) | Source |
|---|---|---|
| Before any cleanup | 3.09 GB | `docs/results/a2_storage/disk_before.json` |
| After removing identified, recorded leftovers (scratch benchmark homes, agent workdirs, pip cache, pytest outputs, superseded demo run) and the disk-manager pass below | 19.74 GB | `disk_after.json` (end of this work) |
| Disk manager on the demo/test homes (`nexus disk plan` -> `clean --plan`) | +317 MB (1752 files; 47 protected skipped) | this section |
Every location's owner and purpose was recorded before deletion. Kept: model weights, licensed benchmark media, the venv,
the repository, every final demo video (Demo 1/2/3/4) and the audio review package. Details: `docs/DISK_MANAGEMENT.md`.

## 2. External storage destination and verified operations
Destination code path: **Account 1** `nexus/cloud/storage` (PR #4, head `919f158`), used through its public functions;
Account 2 adds only the daily-output logic. Verified on a local merge of Account 1 `919f158` + this branch (+ Account 3's
shared-contracts layout), with Account 1's Google Drive adapter talking to Account 1's protocol-faithful fake Drive
server (real HTTP, real resumable sessions, provider checksums) and Account 1's LocalProvider:
| Operation | Result | Evidence |
|---|---|---|
| upload final + captions + thumbnail + QA/quality reports + archive manifest | verified, 5/5 files | `daily_archive_e2e.json` |
| verification | `provider_sha256` (Drive's sha256Checksum) | same |
| one copy on the destination, same SHA-256 | yes | same |
| local final deleted only after verification + re-verification | yes; events: queued -> uploading -> uploaded -> verified -> local copy deleted | same |
| intermediates of the archived render released | render cache 0.45 MB left after release | same |
| Account 1 registry entry | `ArtifactManifest/v1`, kind `master`, retention `archive` | same |
| project record | `project.data.archive[...]` with artifact id + manifest record | same |
| retrieval | downloaded back, identical SHA-256 | same |
| cut connection mid-upload | resumed the same Drive session, one file on Drive | `test_a2_archive_account1.py` |
| remote copy damaged before release | release refused, local kept | same |
| LocalProvider on the same disk | verified but not released ("not external") | same |
**Not done:** a live Google Drive upload from this branch (needs the owner's OAuth tokens; Account 1 real-verified Drive
upload/resume/checksums on three accounts on 2026-11-12). External storage is therefore integrated in code and tested
against Account 1's real storage code, **not yet verified against the owner's live Drive** (K-28).

## 3. Daily archive behaviour (Account 2 alone, mock destination)
`tests/fast/test_a2_archive.py`, 11 tests: verified upload -> release -> retrieve; failed upload keeps the only copy and
retries after backoff; interrupted upload resumes with no second upload; crash after upload recovered without a second
entry; same render archived twice = one entry; corrupted copy detected and re-uploaded; same-disk / edited-local refusals;
md5-only and no-checksum providers; retention cannot be enabled; release path guard.

## 4. Asset library
| | |
|---|---|
| Catalog | 182 assets: effects 21, transitions 76 (58 FFmpeg + 18 new), LUTs 14, frames 5, overlays 7, titles 6, templates 3, animations 14, sound effects 24 (+50 in the CC0 pack), music 5, fonts 7 |
| Licence check | 182/182 pass (`docs/ASSET_LICENSES.md`); licences: CC0-1.0, OFL-1.1, NEXUS-Proprietary (built-in renderer features) |
| Installed size, everything | 116 MB (67 installs) + 3.1 MB cached previews; nothing installed by default |
| Downloads | 7 OFL fonts (google/fonts @ 3e56677, licence text pinned too) + 1 CC0 pack (1.9 MB zip -> 50 .ogg), all SHA-256 verified |
| Previews | every built-in and generated asset rendered a preview through the real FFmpeg path, 0 failures (~141 s one-time) |
| Dependencies added | none |
Rendering proof (`tests/fast/test_a2_assetlib.py`, 32 tests): B&W LUT removes colour; 2.39 border blacks the top/bottom
rows; a cinematic hit raises audio energy at its start time; 5 extended transitions give a real in-between frame through
the piece pipeline; 11 finishing looks change pixels; 4 title motion presets animate; fallback on FFmpeg < 6; resumable
pinned download; safe pack extraction; import/export with tamper detection; uninstall refused while used; missing-asset
repair; LRU quota keeps favourites and used assets. Contact sheets: `docs/results/a2_assets/*.jpg`.

## 5. Browser (real Chromium against the running app)
`docs/results/a2_assets/ui_check.json`: 11/11 - catalog listed (182 cards), install a LUT and see its rendered preview,
favourite, transition preview on demand, checksum verification, apply an effect to the selected clip in the workspace,
apply a transition to the whole timeline, disk dashboard, cleanup dry run, archive browser, no JavaScript errors.
Found and fixed by this check: preview rendering could occupy all shared API threads (previews now have their own
2-thread limiter).

## 6. Tests and CI
- Account 2 branch, full fast suite (dev container, Linux): **400 passed, 0 failed, 7 skipped** (`docs/results/fast_tests.json`;
  skips include the 4 Account 1 storage tests, which run on the merged tree). An earlier run had 1 failure in
  `test_jobs::test_worker_killed_mid_job...` that did not reproduce in isolation, under load, or in the next full run.
- Local merge Account 1 `919f158` + this branch (+ shared-contracts layout): **603 passed, 0 failed, 10 skipped**,
  no code conflicts (`docs/results/a2_storage/merged_a1_a2_check.json`).
- Three accounts (PR #3 head a889cd8 incl. Account 1 f9d8f11 + this branch 1b2df1d + Account 3 62414bf + `apply.sh`):
  **923 passed, 0 failed, 12 skipped** (local merge, not pushed; resolutions in `docs/results/a2_storage/merged_a1_a2_check.json`).
- GitHub Actions: **not run** - since about 10:27 UTC every job in the repository (PRs #4, #5, #7) fails in 2 s without
  starting a step (account-level Actions limit/billing; owner action). Linux/Windows CI and the clean-machine Windows
  E2E (`scripts/acceptance/windows_e2e.py`, now also covering library/disk/archive endpoints) must be re-run when Actions
  works again.

## 7. Confirmations
- Final daily videos remain in the external archive: archived with retention `archive`; Account 2 has no code that
  deletes a remote copy; remote retention is disabled and cannot be enabled from settings.
- Local deletion happens only after a checksum-verified external copy (fresh re-verification at release time, local
  bytes compared with the archived SHA-256, destination must be external).
- No paid service, billing, account creation or OAuth authorisation was done.
- PR #6 is unchanged and unmerged.
