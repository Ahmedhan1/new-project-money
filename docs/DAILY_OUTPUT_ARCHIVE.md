# Daily output archive

Final daily videos are kept **permanently in external storage**. The local copy is deleted only after the external copy
was verified byte-for-byte, so the only copy of a deliverable is never deleted. Module: `nexus/archive.py`; storage:
Account 1 (`nexus/cloud/storage`).

## The daily flow
1. Generate the video (director / factory / editor).
2. Render with QA (`render` job; the `disk-preflight` stage refuses a render that cannot finish).
3. Final QA + quality score.
4. Final export (`export` job, optional).
5. **Archive** (`archive` job; started automatically after a final render when a destination is configured
   and `auto_archive` is on, or by the "Archive this render externally" button / `python -m nexus archive add`):
   the final video, its captions, a thumbnail, the QA and quality reports, and an archive manifest are uploaded through
   Account 1's `artifacts.store_file` (resumable, crash-safe, deduplicated by SHA-256, retention `archive`).
6. **Verify** each upload with the provider's own metadata: Drive's `sha256Checksum` (`provider_sha256`); else the
   provider's md5 against the local file (`provider_md5`); else download the copy and hash it (`download_sha256`).
7. **Persist** the archive manifest (`NEXUS_HOME/archive/<project>/<video>.archive.json`, also uploaded) and the ledger
   record (`docs` table, kind `a2_archive`).
8. **Update the project** (`project.data["archive"][<video>]`: state, SHA-256, size, remote artifact id/path).
9. **Release the local copy** only when: every file of the deliverable is verified; the destination is external (another
   provider, or a LocalProvider on a different device); the remote copy re-verifies *now*; the local file's SHA-256 still
   equals the archived SHA-256; and the file is a render/export of that project. Intermediate segments/pieces of that
   render are released too (unless another project uses them). Thumbnail, captions, reports and manifest stay local.

## Failure behaviour
| Situation | What happens |
|---|---|
| Destination unreachable / upload error | record `failed` with the reason and `next_retry` (exponential backoff 1 min .. 6 h, 8 attempts); local file kept |
| Connection cut mid-upload | Account 1 keeps the provider session and the acknowledged byte; `recover()` resumes the same transfer (no second upload) |
| Crash after the upload, before verification | `recover()` asks the destination whether these bytes already exist (by SHA-256) and verifies them: no duplicate entry |
| Remote copy corrupted (verification fails) | `failed`, local kept; the retry re-uploads (Account 1's location check rejects the bad copy) |
| Remote copy damaged later, before release | re-verification at release fails: local kept, record `failed` |
| Local file edited after archiving | release refused ("differs from the archived bytes") |
| Archive on the same disk (Account 1 LocalProvider inside NEXUS_HOME, or its configured root `NEXUS_LOCAL_STORAGE` on the same device as the final) | verified, but release refused: no space gained, still one disk; no setting overrides this |
| No storage layer (Account 2 branch without Account 1) | `ArchiveUnavailable`; outputs stay local, nothing deleted |

A worker re-queues interrupted or due uploads automatically (`jobs.schedule_archive_recovery`, an `archive-recover` job).

## Browse and retrieve
Storage page -> "Daily video archive": thumbnail, name, state, size, verification method, where it lives, whether a local
copy exists, and **Download back** (verified by SHA-256, preflighted for disk space, written via `.part`).
CLI: `python -m nexus archive list | status | retrieve --record <id> | recover`. API: `GET /api/archive`,
`POST /api/projects/{pid}/archive`, `POST /api/archive/retrieve`, `POST /api/archive/retry`.

## Retention
**No automatic deletion of archived videos exists.** `archive.settings()["retention"]["enabled"]` is `false` and
`save_settings` refuses to enable it. A future retention policy needs a separate, owner-approved change. Account 1's own
cleanup deletes only `temporary` artifacts; archive uploads are stored with retention `archive`.

## Settings (`NEXUS_HOME/archive/settings.json`)
`connection_id` (e.g. `drive_a`; default: Account 1's placement order) · `auto_archive` true · `release_local_after_verify`
true · `max_attempts` 8. There is **no same-disk override**: `save_settings` rejects `allow_same_disk`, and a hand-edited
value is ignored.

## Tests
- `tests/fast/test_a2_archive.py` (mock destination, Account 2 alone): verified upload then release then retrieve; failed
  upload keeps the only copy and retries; interrupted upload resumes without a second upload; crash after upload;
  duplicates; corrupted copy; same disk / changed local file; md5-only and no-checksum providers; retention cannot be
  enabled; path guard.
- `tests/fast/test_a2_archive_account1.py` (real Account 1 storage code, skipped without Account 1): Drive adapter over
  Account 1's protocol-faithful fake Drive: archive + Drive sha256 verification + release + retrieve; cut connection
  resumes the same Drive session; remote damage blocks release; LocalProvider on the same disk is not external.

A real Google Drive upload from this branch has **not** been run: it needs the owner's OAuth tokens (Account 1's vault).
Account 1 real-verified Drive upload/resume/checksums on 2026-11-12.
