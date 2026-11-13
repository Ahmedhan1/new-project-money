# Account 1 → Accounts 2 and 3: integration handoff

**Branch:** `work/account-1-cloud-platform` (PR #4, draft, based on PR #3's branch `claude/hopeful-allen-luubcy`;
nothing is merged into `main`). **Start from the head of that branch** (the hash is in `NEXUS_STATE.json` →
`git.head`, and in `docs/ACCOUNT1_ACCEPTANCE_RESULTS.md`).

Account 2 = editing / rendering / video generation. Account 3 = research, translation, localization, publishing,
analytics, AI-provider selection. Account 1 owns the cloud platform these run on.

## 1. Contracts (stable, versioned)
Python: `nexus/contracts.py`. JSON Schema: `docs/contracts/*.v1.schema.json` and `GET /api/v1/contracts`.
Field-by-field reference with examples: `docs/ACCOUNT1_CONTRACTS.md`.

| Contract | You use it to |
|---|---|
| `ProductionJobManifest/v1` | submit remote work (`task`, `params`, `inputs` = artifact ids, `requires` {gpu, vram_gb, providers}, `priority` 0-9, `max_retries`, `idempotency_key`, `output_destination`) |
| `WorkerExecutionResult/v1` | read the outcome: status, accelerator, metrics, verified `artifacts`, `log_tail` (redacted), checkpoint |
| `ArtifactManifest/v1` | every file (sha256, size, mime, kind, locations, provenance, retention) |
| `JobCheckpoint/v1` | resumable steps (`ctx.checkpoint(step, state)` in a worker task) |
| `RemoteWorkerCapability/v1`, `ProviderHealth/v1`, `QuotaStatus/v1` | what compute/storage can do now; `measured=true` only from a completed probe |
| `StorageProvider/v1` (+ behaviour in `nexus/cloud/storage/base.py`) | storage adapters |
| `MediaSource/v1`, `MediaIngestionJob/v1` | bring media in (rights must be declared) |
| `AIProviderCapability/v1` | **Account 3** declares its AI providers; Account 1 only routes/meters |
| `ProductionEvent/v1`, `NotificationEvent/v1` | `nexus.cloud.events.emit(type, message, job_id, **data)` |

Rules: extra fields are rejected; a breaking change is a new `/v2` model next to v1, never an edit of v1;
`tests/fast/test_contracts.py` fails if a schema changes without re-export.

## 2. Submitting work (Account 2: render / generate; Account 3: translate / publish jobs)
1. **Worker side:** add `nexus/cloud/worker_kit/tasks/<name>.py` with `@task("<name>")`. Inputs arrive materialised
   and checksum-verified; call `ctx.checkpoint(step, state)` after each expensive step and `ctx.artifact(path, kind)`
   for every output. Instructions: the docstring of `nexus/cloud/worker_kit/tasks/__init__.py`.
2. **Dispatcher side:** `nexus/cloud/compute/tasks.py`: `register("<name>", TaskSpec(gpu="required"|"preferred"|"no",
   min_vram_gb=..., internet=..., resumable=...))`.
3. **Submit:** `from nexus.cloud.compute.dispatcher import submit, cancel, status, quota_status` or
   `POST /api/v1/compute/jobs` with a `ProductionJobManifest` body. Always set `idempotency_key`.
4. **Follow:** `GET /api/v1/compute/jobs/{id}` (status, result, events, checkpoints, artifacts) or the event stream
   `GET /api/v1/events?job_id=`.
5. **Inputs larger than 512 KiB** must be reachable by URL (`params.input_url`, https) or an artifact with a location the
   worker can read; Kaggle rejects pushes of about 3 MB (measured: HTTP 400).

Measured worker: Kaggle, 2× Tesla T4 (15 GB each), 31 GB RAM, 4 CPU, torch 2.11/CUDA 13, ffmpeg 6.1 with NVENC,
internet on. A Wan-class video model fits (Account 2): see `scripts/gpu_video_bench.py` (Wan 2.1 1.3B, ~8 GB VRAM).

## 3. API endpoints (all owner-authenticated unless noted)
Core: `GET /api/v1/contracts` (public), `POST /api/v1/session` (public; owner token → signed cookie), `GET /api/v1/health`,
`GET /api/v1/health/live` (public), `GET /api/v1/events`, `GET /api/v1/notifications`, `POST /api/v1/notifications/read`,
`GET /api/v1/security/{secrets,audit}`, `POST /api/v1/security/scan`.

Compute: `GET|POST /api/v1/compute/jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel|retry`, `GET /providers`,
`POST /providers/kaggle/verify|resume`, `GET /quota` (exactly `QuotaStatus/v1`), `GET /quota/details`,
`GET /workers`, `POST /workers/register`, `DELETE /workers/{id}`, `GET /workers/{id}/notebook`;
worker-token routes: `GET /workers/next`, `POST /workers/{id}/result`.

Storage: `GET /api/v1/storage/providers|connections|transfers|artifacts`, `GET|PUT /rules`,
`POST /connections/{cid}/device/start|device/poll|disconnect|folders`, `GET /connections/{cid}/health|quota|files|oauth/start`,
`GET /oauth/callback` (public, protected by single-use state), `POST /uploads` (multipart), `POST /transfers/{id}/resume`,
`POST /ingest`, `POST /ingest/upload`, `GET /ingest/{id}`, `GET /artifacts/{id}`, `POST /artifacts/{id}/copy`, `POST /cleanup`.

Studio: `GET /studio` (page), `GET /api/v1/studio/{summary,jobs,checkpoints,artifacts,resources,health,security,config,github}`,
`GET|PUT /scheduler`, `POST /scheduler/{start|pause|resume|stop-after-current|stop|tick}`,
`GET /artifacts/{id}/stream` (Range), `GET /auth` and `POST /logout` (public).

Writes authenticated by cookie need the header `X-NEXUS-CSRF: 1` and a same-origin request. Bearer clients
(`Authorization: Bearer $NEXUS_API_TOKEN`) are exempt from CSRF. The test `test_every_v1_route_requires_auth` fails if you
add an unauthenticated `/api/v1` route; a deliberate public route goes into `PUBLIC_V1` in that test with a reason.

## 4. Files
**Owned by Account 1** (change through a PR to this branch's owner, or coordinate): `nexus/contracts.py`, `nexus/vault.py`,
`nexus/cloud/**` (except the extension points below), `nexus/ui/studio.html`, `docs/contracts/`, `docs/ACCOUNT1_*`,
db migration 2 in `nexus/db.py`.

**Open for extension:** `nexus/cloud/worker_kit/tasks/*.py` (new worker tasks), `nexus/cloud/compute/tasks.py`
(`register` new TaskSpecs), new storage providers implementing `nexus/cloud/storage/base.py`, new `AIProviderCapability`
declarations (Account 3), scheduler plan task names (`plan.master`, `plan.short` in the scheduler config: Account 2
supplies `render_master` / `render_short`). New tables: add a NEW migration at the end of `MIGRATIONS` in `nexus/db.py`.

## 5. Environment variables
| Variable | Meaning |
|---|---|
| `NEXUS_HOME` | data directory (outside Git): db, vault, artifacts, storage |
| `NEXUS_API_TOKEN` | owner token; **required** on any host that is not loopback (otherwise only 127.0.0.1 is served) |
| `NEXUS_VAULT_KEY` | Fernet key for the vault (inject from the host's secret store; else a key file in NEXUS_HOME/secrets) |
| `NEXUS_PUBLIC_ORIGIN` | public origin for the CSRF same-origin check behind a proxy |
| `NEXUS_ENV` | `production` = production profile: refuses to start unless every gate passes (`python -m nexus production-check`); see `ACCOUNT1_PRODUCTION_DEPLOYMENT.md` |
| `NEXUS_TRUSTED_PROXIES` | the reverse proxy's address; only it may name the real client in `X-Forwarded-For` (rate limiting) |
| `NEXUS_MAX_BODY_BYTES` / `NEXUS_MAX_UPLOAD_BYTES` | request limits, default 8 MiB JSON / 20 GiB multipart (413 above) |
| `NEXUS_BACKUP_PASSPHRASE` | backup encryption for remote destinations (required in production when one exists) |
| `NEXUS_EMBEDDED_WORKER` | `0` = no in-process job worker / compute poller (scripts, tests) |
| `NEXUS_SCHEDULER_THREAD` | `0` = no scheduler background loop |
| `NEXUS_LIVE=1`, `NEXUS_LIVE_DRIVE=1` | enable live tests (`tests/live/`) |
Secrets (Kaggle token, Google OAuth client, refresh tokens, worker-token hashes) live only in the vault.

## 6. Test commands
```bash
python -m ruff check .
NEXUS_SCHEDULER_THREAD=0 python -m pytest -q tests/fast          # A1 HEAD: see ACCOUNT1_FINAL_RELEASE_AUDIT.md §2 (CI and dev-container counts)
python -m pytest -q tests/e2e/test_studio_browser.py            # real Chromium run of the Studio
NEXUS_LIVE=1 python -m pytest -q tests/live/test_kaggle_live.py  # needs the Kaggle token in the vault (uses GPU quota)
python scripts/release_check.py                                 # fresh clone + hygiene + lint + fast suite
```

## 7. Known limitations (honest)
- Google Drive: real-verified on three accounts (device flow, refresh, isolation, resumable upload, Demo E). Real token
  revocation and Drive backup/restore are tested on the fake server only. Uploads through the API are synchronous.
  OAuth consent is in Testing mode until the owner publishes it (refresh tokens expire after 7 days).
- Kaggle weekly GPU allowance is a documented default (30 h) the owner can override; usage is measured by NEXUS.
  Quota-error detection is pattern-based and has not yet seen a real Kaggle quota message.
- Cancel cannot stop a running Kaggle kernel (no official CLI command); it stops polling and never starts further steps.
- Pull workers (owner GPU / Colab) are tested through the API, not yet against a real remote machine.
- No permanent host chosen: the control plane runs locally/Codespaces; hosting needs the owner's decision (see
  `docs/ACCOUNT1_PROVIDER_COMPARISON.md`). The session cookie is `Secure`: non-localhost needs HTTPS.
- The rate limiter is in-memory, per process; production enforces a single API process per `NEXUS_HOME` (OS lock).
- Since `dafefe3`: a job manifest whose params/inputs contain a token-shaped string or a stored secret value is refused
  on submit (`ValueError`); JSON request bodies above 8 MiB get 413 (use multipart or an artifact for large inputs).

## 8. Next steps
- **Account 2:** implement `render_master`, `render_short`, `generate_video` as worker tasks; submit via the dispatcher;
  write outputs as artifacts; set the scheduler plan to these names. Start with Wan 2.1 1.3B on the T4.
- **Account 3:** declare AI providers with `AIProviderCapability/v1`; implement `translate`, `publish_*` tasks the same
  way; publishing must stay manual-approval until the owner authorises platform OAuth.
- **Owner:** publish the OAuth consent screen; approve a host; offline vault-key backup (`ACCOUNT1_DISASTER_RECOVERY.md`). KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER.
- Account 3 specifics (payload examples, error shapes, checkpoint rules, capability classes): `docs/ACCOUNT1_HANDOFF_TO_ACCOUNT3.md`.

## Shared-contracts integration status (refreshed 2026-11-13, production-closure round)
- Latest Account 2 head checked: `d979df1` (`work/account-2-cinematic-engine`). Earlier results at `a52a24a` are superseded.
- Integration at exact heads A1 `34a5ba1` + A2 `d979df1` + A3 `fd71471` + `apply.sh` (local, not pushed): INTEG_RESULT
- The previous figure (748 passed at A1 `e9ad5e9` + A2 `a52a24a`) is superseded and no longer quoted as current.
- Windows CI and the Windows clean-machine E2E on the merged tree are not run: they wait for Account 3's `batch_llm`
  manifest fix and the owner's merge approval (the merged tree is not pushed).
- No open Account 2 item for Account 1. The Account 2 report of a failing Account 1 cookie/CSRF test is explained and
  closed (`ACCOUNT1_SECURITY_MODEL.md` §7.3). Details: `docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md` §7.
