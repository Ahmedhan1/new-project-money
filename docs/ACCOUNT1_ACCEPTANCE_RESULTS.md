# Account 1 acceptance results (2026-11-12, refreshed 2026-11-13 production-closure round)

**KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER.**

Classes: **RV** real-verified · **MO** mocks/fake-server only · **P** partial · **BU** blocked by user · **BP** blocked by
provider · **NI** not implemented. Finalization mission (Parts 1-12), demos A-K.

| Demo | Class | What was run | Evidence |
|---|---|---|---|
| A. Stable HTTPS URL for the Studio | **BU** | App runs and the browser E2E passes on the dev host; a permanent host needs the owner (recommended: owner machine + Tailscale Funnel, zero cost). Nothing deployed publicly. | `ACCOUNT1_PROVIDER_COMPARISON.md` §2.1, `tests/e2e/test_studio_browser.py` |
| B. Real Google Drive A/B/C | **RV** (revoke: **MO**) | Device flow on three real accounts, identity, encrypted refresh tokens, forced refresh, folders, resumable upload interrupted + resumed, sha256, download, move, delete; B cannot see A's file. Real revocation not run (would force the owner to reconnect). | `storage_drive_live.json`, `storage_drive_live_isolation.json` |
| C. Kaggle GPU, one account | **RV** | Private kernels, 2× T4, probe + NVENC transcode, checksums, idempotent re-submit. | `kaggle_probe.json`, `kaggle_transcode.json`, `lead_demos.json` |
| D. Interrupt + resume from checkpoint | **RV** | Worker stopped after 2/4 chunks, resumed at chunk 3. | `kaggle_resume.json` |
| E. Drive → remote GPU → Drive, no PC download | **RV** | Drive A source (49.9 MB) → private Kaggle dataset → T4 NVENC (16× realtime) → Drive B `NEXUS/OriginalMedia/`; 144,587,842 bytes, sha256 matched after re-download. Job `rjob_be153ebe2736`. | `lead_demos.json`, Studio library |
| F. Control-plane crash recovery | **RV** | API SIGKILLed during a 6-chunk job; after restart it re-attached to the same Kaggle run (no duplicate GPU run). | `lead_demos.json` (`demo_D_control_plane_crash`) |
| G. Scheduler daily target, pause/resume/stop | **RV** | Real probe jobs; ETA from measured median; stop condition reached. | `lead_demos.json` (`demo_F_scheduler`) |
| H. Quota exhaustion → safe pause | **P** (simulated) | Simulated exhaustion pauses safely with reason; GPU jobs wait queued (safe production mode, attempts 0) with no account switching. A real Kaggle quota message has not been seen and is not pursued (Kaggle closed; never forced). | `test_scheduler.py`, `test_cloud_compute.py` |
| I. Secure link ingestion | **RV** | Authorised https source, redirect re-validated, ffprobe, sha256, provenance; SSRF 34/34 refused; platform pages refused. | `ingest_live.json`, `ingest_ssrf_live.json` |
| J. Browser-only operation + observability | **RV** (dev host) | Studio: jobs, storage, alerts (storage threshold, quota labelled as estimate), GPU quota bar; alerts clear when resolved. | `tests/e2e/test_studio_browser.py`, `GET /api/v1/alerts` |
| L. `batch_llm` on the Kaggle GPU (Account 3 request) | **RV** | Qwen2.5-0.5B-Instruct, Tesla T4 x2, 8/8 prompts, 2 checksum-verified parts joined, no endpoint. Job `rjob_877fa89eb0ac`. | `batch_llm_live.json`, `test_cloud_batch_llm.py` |
| K. CI green on the newest commit | **RV** | `73c0023`: Linux 427 passed / 5 skipped, Windows 427 passed / 5 skipped, windows-e2e success. | [run 43407933476](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933476), [e2e 43407933450](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933450) |
| M. Production profile (fail-closed startup, readiness, single instance, graceful shutdown, limits) | **RV** (CI) / **BU** (host) | Real subprocess: refuses with incomplete config, serves with full config (docs off, ready 200); second API process refused; lifespan audit + unclean-restart detection. On an owner host: not yet (host not approved). | `test_production.py`, `ACCOUNT1_PRODUCTION_DEPLOYMENT.md` |
| N. Vault backup / disaster recovery | **RV** (local) / **BU** (Drive) | Backups carry vault ciphertext, never the key; restore validates decryptability (no/right/wrong key); offline key export/verify; never overwrites. Real Drive restore: owner-authorised step, not run. | `test_production.py`, `test_storage_backup.py`, `ACCOUNT1_DISASTER_RECOVERY.md` |
| O. Three-account merged tree in production mode | **RV** (Linux, local) / **BP** (Windows: Actions down) | A1 `f9d8f11` + A2 `d979df1` + A3 `62414bf` + `apply.sh`: 862 passed / 0 failed / 12 skipped; A3 `batch_llm` manifests run on the real Dispatcher; live `serve` with `NEXUS_ENV=production`: unconfigured exit 1, configured 12/12 READY, ready 200, owner routes 401/200, second instance refused, graceful SIGTERM audited, off-home backup + restore drill verified, 0 secret occurrences in logs/backups, wrong vault key → NOT READY. | `ACCOUNT1_FINAL_RELEASE_AUDIT.md` §7.1, §7.6 |

## Tests
- Fast suite: CI at `73c0023` **427 passed, 5 skipped** (Linux and Windows); dev container **428 passed, 4 skipped**. Same 432 tests; the skips depend on fonts and rclone (audit §2).
- Three-account integration at A1 `f9d8f11` + A2 `d979df1` + A3 `62414bf` (with Account 3's `batch_llm` fix) + `apply.sh`: **862 passed, 0 failed, 12 skipped** (Linux, local; audit §7.1). Superseded: 766 passed at A3 `fd71471`.
- Production acceptance matrix (auth, CSRF, session expiry, vault, backup/restore, interrupted transfers, duplicates, worker loss, restart, scheduler, storage failure, rate limiting, uploads, clean install, production startup, graceful shutdown): audit §8.
- Three-account integration and the final verdict: `docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md`.
- Real browser E2E of the Studio: passed. Security recovery checks: 7/7. Secret scan (tracked files): clean.
- Live Kaggle evidence: probe, transcode, worker-loss resume, control-plane-crash resume, scheduler run.

## Required tests from the mission, and where they are
| # | Test | Where / status |
|---|---|---|
| 1 | Clean-clone setup | `scripts/release_check.py` (fresh clone + fast suite) |
| 2 | Codespaces launch | not run (no Codespace in this session); same uvicorn command as Demo A |
| 3 | Browser UI startup | `tests/e2e/test_studio_browser.py` |
| 4 | OAuth state validation | `tests/fast/test_cloud_storage.py` (single-use, expiry, bound to connection) |
| 5-7 | Drive connection, multi-account isolation, refresh tokens | fake-server tests; real: owner (Demo B) |
| 8-10 | Resumable upload, interrupted recovery, checksum | fake-server tests + SIGKILL evidence `storage_resume.json` |
| 11-14 | Kaggle auth, submission, status, output retrieval | live evidence (Demo C) + `tests/live/test_kaggle_live.py` |
| 15 | GPU job manifest | `tests/fast/test_cloud_compute.py`, contracts tests |
| 16 | Worker crash recovery | Demo D (both kinds) |
| 17-18 | Quota / storage exhaustion | `test_scheduler.py`, `test_cloud_compute.py`, live Demo F |
| 19-20 | Job priority, safe cancellation | `test_cloud_compute.py`, `test_scheduler.py` |
| 21-23 | URL ingestion, SSRF, unauthorised source rejection | `test_ingest.py` (71) + live evidence |
| 24 | No-secret-in-Git scan | `tests/fast/test_no_secrets.py`, `POST /api/v1/security/scan` |
| 25 | API integration | studio/compute/storage/security API tests; route-auth inventory test |
| 26 | Browser end-to-end | `tests/e2e/test_studio_browser.py` |

## Not complete (stated plainly)
Not exercised for real:
- a permanent host (owner decision; production profile ready, `production-check` not yet run on a host);
- real Drive token revocation;
- a Drive backup/restore run (local backup/restore with vault validation is tested);
- forced failover between real Drives;
- a real owner-GPU/Colab pull worker;
- testing on the owner's machine.

OAuth consent is still in Testing mode: refresh tokens expire after 7 days until the owner publishes it. A real Kaggle
quota message has not been seen; Kaggle is closed and the quota is never forced. Details and the verdict:
`docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md`.
