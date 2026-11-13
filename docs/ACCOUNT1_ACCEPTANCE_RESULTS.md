# Account 1 acceptance results (2026-11-12, updated 2026-11-13)

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
| H. Quota exhaustion → safe pause | **P** / **BP** | Simulated exhaustion pauses safely with reason; GPU jobs wait queued (safe production mode, attempts 0) with no account switching. A real Kaggle quota message has not been seen. | `test_scheduler.py`, `test_cloud_compute.py` |
| I. Secure link ingestion | **RV** | Authorised https source, redirect re-validated, ffprobe, sha256, provenance; SSRF 34/34 refused; platform pages refused. | `ingest_live.json`, `ingest_ssrf_live.json` |
| J. Browser-only operation + observability | **RV** (dev host) | Studio: jobs, storage, alerts (storage threshold, quota labelled as estimate), GPU quota bar; alerts clear when resolved. | `tests/e2e/test_studio_browser.py`, `GET /api/v1/alerts` |
| L. `batch_llm` on the Kaggle GPU (Account 3 request) | **RV** | Qwen2.5-0.5B-Instruct, Tesla T4 x2, 8/8 prompts, 2 checksum-verified parts joined, no endpoint. Job `rjob_877fa89eb0ac`. | `batch_llm_live.json`, `test_cloud_batch_llm.py` |
| K. CI green on the newest commit | see PR #4 checks | Linux + Windows + windows-e2e; tests never start background workers (`conftest.py`), production startup still does. | GitHub Actions on PR #4 |

## Tests
- Fast suite: **412 passed, 4 skipped** in a clean clone at `9ea4a73` (`scripts/release_check.py`), ruff clean.
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
A permanent public host (owner decision), real Drive token revocation and Drive backup/restore runs, a real owner-GPU/Colab
pull worker, and a real Kaggle quota-exhaustion message have not been exercised. OAuth consent is still in Testing mode
(refresh tokens expire after 7 days until the owner publishes it).
