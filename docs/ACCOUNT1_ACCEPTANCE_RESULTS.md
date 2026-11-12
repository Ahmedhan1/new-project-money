# Account 1 acceptance results (2026-11-12)

Status per demo: **Done** (run for real, evidence linked) · **Done (local stand-in)** (real run, but a part that needs the
owner was replaced by a local equivalent) · **Blocked on owner** (built and tested, cannot finish without the owner).

| Demo | Status | Evidence |
|---|---|---|
| A. Browser startup from a clean environment | **Done** (dev host). A fresh NEXUS_HOME, the real app under uvicorn, Chromium: login, all 15 Studio sections, scheduler controls, mobile width; no page errors. The owner's PC installs nothing. A *permanent* host is not chosen yet (owner decision). | `tests/e2e/test_studio_browser.py`, `docs/results/account1/studio_*.png`, `studio_e2e.json` |
| B. Three Drive connections | **Blocked on owner.** OAuth web + device flows, 3 isolated connections, refresh rotation, revocation, quota, folders, resumable upload with crash recovery and checksum are implemented and tested over real HTTP against a protocol-faithful fake Google server (95 tests). Real Google needs the owner's OAuth client and consent in a browser. | `tests/fast/test_cloud_storage.py`, `docs/results/account1/storage_resume.json`, `tests/live/test_drive_live.py` (owner-gated) |
| C. Kaggle (one account), real GPU task, verified artifact | **Done.** Private kernels via the official CLI; 2× Tesla T4 (15 GB each), 31 GB RAM; probe 23.9 TFLOPS fp16; NVENC transcode of a 10 s clip to 1280×720 in 1.8 s; every artifact's SHA-256 re-computed and matched. Also submitted through the running app's HTTP API with the background poller; idempotent re-submission returns the same job. | `kaggle_probe.json`, `kaggle_transcode.json`, `lead_demos.json` (`demo_C_api_probe`) |
| D. Remote job: progress, logs, interrupt, resume from checkpoint | **Done.** (1) Worker interruption: chunked_transcode stopped after 2 of 4 chunks, re-dispatched with checkpoint 2, finished only chunks 3-4. (2) Control-plane crash: API process SIGKILLed 7 s after dispatching a 6-chunk NVENC job on a NASA clip, down 25 s; after restart the dispatcher re-attached to the same Kaggle run (attempt 1, no duplicate GPU run) and the job succeeded; output 1280×720, 242.4 s, checksum matched locally. | `kaggle_resume.json`, `lead_demos.json` (`demo_D_control_plane_crash`) |
| E. Cloud media flow (Drive → remote → Drive, no full-res download to the PC) | **Done (local stand-in) / Blocked on owner for Drive.** Source fetched by the Kaggle worker directly from an authorised https URL (NASA), processed on the GPU, result stored server-side (`NEXUS_HOME/compute/artifacts`); the browser only receives thumbnails/metadata unless the owner clicks play (Range stream). The Drive source/destination legs need Demo B. | `lead_demos.json`, Studio library screenshot |
| F. Scheduler: daily target, pause/resume, quota exhaustion | **Done.** Target 1 master + 1 short (demo plan = real Kaggle probe jobs): start (ETA from measured median), pause (no new dispatch for 100 s), simulated quota exhaustion (allowance setting below measured usage) → resume refused, paused with reason "quota", warning notification, no account switching; restore → resume → stop condition reached → stopped. | `lead_demos.json` (`demo_F_scheduler`), `tests/fast/test_scheduler.py` |
| G. URL ingestion | **Done.** NASA public-domain clip via https: redirect followed and re-validated, ffprobe-verified (h264+aac, 242.4 s), SHA-256, provenance, stored; duplicate detected on re-import. SSRF suite with the real resolver: 34/34 refused (private IPs via public DNS names, numeric tricks, metadata hosts, platform pages). | `ingest_live.json`, `ingest_ssrf_live.json`, `tests/fast/test_ingest.py` |

## Tests
- Fast suite (all four sub-agents integrated): **392 passed, 4 skipped** (`NEXUS_SCHEDULER_THREAD=0 pytest tests/fast`).
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
Real Google Drive (B, and the Drive legs of E), a permanent public host, a real owner-GPU/Colab pull worker, and a real
Kaggle quota-exhaustion message have not been exercised. Each needs the owner (or real quota exhaustion).
