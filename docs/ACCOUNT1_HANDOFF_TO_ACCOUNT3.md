# Account 1 → Account 3: integration handoff (research, translation, localization, publishing, analytics, AI providers)

Read `docs/ACCOUNT1_HANDOFF_TO_ACCOUNT2.md` first: it holds the shared parts (contracts list, API routes, env vars,
test commands, file ownership). This file covers what is specific to Account 3, with example payloads, error shapes
and checkpoint rules. The same examples apply to Account 2 (swap the task names).

**Branch:** `work/account-1-cloud-platform` (PR #4, draft, based on PR #3; nothing merged into `main`). Start from its
head (`NEXUS_STATE.json` → `git.head`).

## 1. What Account 1 gives you
| You need | Use | Status |
|---|---|---|
| Run a task on a GPU / remote worker | `ProductionJobManifest/v1` → dispatcher | real-verified (Kaggle T4) |
| Read a source file stored in Drive without downloading to the owner's PC | `inputs: [artifact_id]`; the control plane fetches it into its cache, large inputs travel as a private Kaggle dataset | real-verified (Demo E, 49.9 MB) |
| Write results to a chosen Drive account | `output_destination: "drive_b"` or the storage rules by kind | real-verified (Demo E) |
| Declare an AI provider (translation, TTS, LLM...) | `AIProviderCapability/v1` | contract only: Account 3 implements the providers |
| Run an open-weight LLM over a batch of prompts | task `batch_llm` (§7) | real-verified (Kaggle T4, Qwen2.5-0.5B-Instruct) |
| Events / notifications | `nexus.cloud.events.emit(...)` / `notify(...)` | real |
| Secrets (API keys for your providers) | `nexus.vault.put/get` (encrypted, outside Git) | real |

## 2. Example payloads

### Submit (POST `/api/v1/compute/jobs`, or `dispatcher.submit(ProductionJobManifest(...))`)
```json
{
  "contract": "ProductionJobManifest/v1",
  "task": "translate_subtitles",
  "owner_account": "account3",
  "project_id": "proj_demo",
  "params": {"target_lang": "ar", "glossary": "default"},
  "inputs": ["art_3f2a9c0d1e77"],
  "output_destination": "drive_b",
  "requires": {"gpu": false, "providers": ["local", "kaggle"]},
  "priority": 4,
  "max_runtime_s": 1800,
  "max_retries": 2,
  "idempotency_key": "proj_demo:translate:art_3f2a9c0d1e77:ar"
}
```
Answer (`200`): `{"job_id": "rjob_...", "status": "queued", "created": true}`. The same `idempotency_key`
returns the existing job (`created: false`) and never starts a second run.

### Result (GET `/api/v1/compute/jobs/{id}` → `result`)
```json
{
  "contract": "WorkerExecutionResult/v1",
  "job_id": "rjob_be153ebe2736",
  "provider": "kaggle", "connection_id": "kaggle_primary",
  "status": "succeeded",
  "duration_s": 312.4, "accelerator": "Tesla T4",
  "metrics": {"encode_s": 72.6, "realtime_factor": 16.0},
  "artifacts": [{"contract": "ArtifactManifest/v1", "artifact_id": "art_...", "kind": "master", "name": "output.mp4",
                 "mime": "video/mp4", "size": 144587842, "sha256": "…", "produced_by_job": "rjob_be153ebe2736",
                 "locations": [{"connection_id": "drive_b", "ref": "…", "verified": true}]}],
  "log_tail": "(redacted, last lines)",
  "error": null,
  "checkpoint": null
}
```
When `output_destination` is set, `data.delivery` in the job record lists each artifact copied there, or the reason
it failed (a failed delivery never deletes the local copy).

### Declare an AI provider (Account 3)
```json
{
  "contract": "AIProviderCapability/v1",
  "provider": "local-whisper", "connection_id": "local",
  "tasks": ["transcribe", "translate_subtitles"],
  "models": ["whisper-small"],
  "local": true,
  "commercial_use": "unverified",
  "quotas": []
}
```
Rules: `commercial_use` stays `unverified` until you have checked the licence; a cloud provider's quota uses
`QuotaStatus/v1` with `source` `provider_api` only if the provider reports the number, otherwise `measured`/`documented`
(the UI labels these as estimates).

## 3. Error structures
| Where | Shape | Meaning |
|---|---|---|
| HTTP API | `{"detail": "<message>"}` with 401/403 (not owner, missing CSRF), 404 (unknown id), 409 (conflict, e.g. provider not ready), 422 (manifest invalid: extra field, bad priority), 502 (provider error, message redacted) | FastAPI standard |
| Job record | `status` ∈ `queued`, `running`, `succeeded`, `failed`, `cancelled`, `quota_exhausted`; `error` holds the latest reason (also while waiting, e.g. `waiting for a provider: kaggle: [quota_exhausted] ...`) | the job is never silently dropped |
| Worker result | `WorkerExecutionResult.status` ∈ `succeeded`, `failed`, `cancelled`, `lost`, `quota_exhausted`; `error` text; `log_tail` redacted | `lost` = no result from the worker: re-dispatched from the last checkpoint |
| Events | `ProductionEvent/v1` types `job.failed`, `worker.lost`, `storage.failed`, `quota.exhausted` ... with `data` | stream: `GET /api/v1/events?job_id=` |

Retries: a failed attempt is retried up to `max_retries`; `quota_exhausted` does not consume retries, it waits. Paid
compute is never chosen automatically; a job whose `requires.providers` lists only unavailable providers waits with a
reason (safe production mode).

## 4. Checkpoint rules (worker tasks)
1. Call `ctx.checkpoint(step, state)` after every expensive, deterministic step; `state` must be JSON and small
   (guideline: a few KiB; not enforced): store paths/indices, not data. Large intermediates go out as `ctx.artifact(path, kind="checkpoint")`.
2. On re-dispatch the bundle carries the last `JobCheckpoint/v1`; the task reads `ctx.resume_state` (its `state`) and
   `ctx.last_checkpoint` (`step`, `seq`, `artifacts`), and skips work already done (Demo D: chunks 1-2 skipped, 3-4 run).
3. `seq` only grows; never rewrite a past checkpoint. A task without checkpoints restarts from zero, so register it
   with `resumable=False`.
4. Steps must be idempotent: re-running the step after the last checkpoint must give the same output.
5. Outputs are trusted only after the control plane re-computes their SHA-256 (one mismatch fails the attempt and nothing
   from it is recorded).

## 5. Publishing and ingestion (Account 3 specific)
- Publishing to platforms stays **manual approval** until the owner authorises each platform's OAuth; Account 1 has
  no platform credentials and must not get them through chat.
- Media ingestion accepts direct https file URLs with declared rights, or uploads; platform page URLs (YouTube, TikTok,
  Instagram, Facebook, X, Vimeo) are refused by design. Use the platform's creator export for the owner's own videos.

## 6. Capability classification (Account 1, at handoff)
Legend: **RV** real-verified · **MO** mocks/fake-server only · **P** partial · **BU** blocked by user · **BP** blocked by
provider · **NI** not implemented.

| Capability | Class | Note |
|---|---|---|
| Kaggle GPU submit/status/output, checksums, idempotency | RV | 2× T4, NVENC |
| Worker-loss + control-plane-crash resume from checkpoint | RV | Demo D |
| Large input via private Kaggle dataset | RV | Demo E |
| Drive device flow, identity, encrypted refresh token, refresh | RV | 3 accounts |
| Drive A/B/C isolation | RV | B cannot see A's file |
| Drive resumable upload with interruption + resume, sha256, download, move, delete | RV | |
| Drive disconnect + token revocation | MO | real revoke forces the owner to reconnect; tested on the fake server |
| Drive backup/restore of NEXUS state | P | `nexus/backup.py` backup/restore + drill tested locally; not yet run against real Drive |
| Storage router (rules, failover, replication, reserve) | P | logic tested; real failover between Drives not forced |
| Storage + quota alerts, resolved alerts clear | RV | live on dev host |
| Scheduler + safe production mode | RV | real Kaggle jobs |
| Real Kaggle quota-exhaustion message | BP | detection pattern-based, not yet seen |
| Owner GPU PC / Colab pull worker | P | API tested; no real remote machine attached (BU) |
| Permanent HTTPS host | BU | recommendation: Tailscale Funnel on an owner machine |
| OAuth consent screen in production mode | BU | Testing mode: refresh tokens expire after 7 days |
| `batch_llm` worker task (Kaggle, bounded batch inference) | RV | Qwen2.5-0.5B-Instruct on T4 x2, 8/8 items (§7) |
| Wikidata Cost Guard declaration | RV (declaration) | host answers 429 to a generic User-Agent from the dev sandbox (§8) |
| AI providers (translation, TTS, LLM) | NI | Account 3 |
| Platform publishing | NI | Account 3, manual approval |

## 7. `batch_llm` (integration request A1-3)
A bounded, temporary job: one Hugging Face model, one finite list of prompts, one run. The model is loaded inside the
Kaggle kernel, every prompt is answered, the results come back as verified artifacts and the kernel exits. There is no
endpoint, port or background process. Routing: `gpu="required"`, `providers=("kaggle",)`. The owner's PC, `user_gpu`
and Colab are never chosen, and the worker refuses to download weights outside a Kaggle kernel.

```json
{
  "contract": "ProductionJobManifest/v1", "task": "batch_llm", "owner_account": "account3",
  "inputs": ["art_<prompts.jsonl>"],
  "params": {"model": "Qwen/Qwen2.5-0.5B-Instruct", "model_licence": "Apache-2.0", "engine": "transformers",
             "max_new_tokens": 256, "max_items": 500, "part_size": 32, "batch_size": 8, "max_model_gb": 8.0,
             "temperature": 0, "chat_template_kwargs": {"enable_thinking": false}, "output_format": "jsonl"},
  "max_runtime_s": 7200, "max_retries": 2, "idempotency_key": "a3:batch:<run-id>"
}
```
- Input JSONL line: `{"id", "task"?, "system"?, "prompt", "max_tokens"?}`. Small lists may go inline in `params.items`.
  Ids must be unique; more than `max_items` (hard cap 5000) is refused, not truncated.
- Output: the dispatcher joins the verified parts of all attempts into `batch_llm_results.jsonl` (or `.json`). Each row
  is `{"id", "task", "text", "usage": {"prompt_tokens", "completion_tokens"}, "finish_reason", "latency_s", "error"}`.
  `postprocess` reports `items` and `items_error`. A per-item error (prompt too long, a generate error after a one-by-one
  retry) never fails the job.
- Evidence: `batch_llm_summary.json` (model, engine, `simulated`, counts, tokens/s, limits, `persistent_endpoint: false`).
- Limits are checked before work starts. The weight size comes from Hub metadata before download (`max_model_gb`; an
  unknown size is refused). With T4 16 GB and fp16, models up to about 7B fit. `time_budget_s` (default 85 % of
  `max_runtime_s`) ends the attempt as `lost` after a finished part, and the next attempt resumes from the checkpoint,
  bounded by `max_retries`.
- `engine: "vllm"` is used only if the Kaggle image already has vLLM; the task never pip-installs an engine.
  `engine: "echo"` loads no model. It is the deterministic test engine, and its results are labelled `simulated: true`.
- **Change needed on Account 3's side:** `bench_v2.account1_manifest` currently sends `params.models` (a list),
  `params.items` (a count) and `runtime: "ollama"`. `batch_llm` takes one `model` per job and the prompts themselves
  (JSONL input or `params.items` as a list). Submit one job per model.
- Kaggle terms: one authorised account (`kaggle_primary`), its own weekly GPU quota, and no rotation. A quota error
  pauses the provider and the job waits (`quota_exhausted`); it never fails over to paid compute.

## 8. Research hosts (integration request A1-4)
- `www.wikidata.org` is declared by Account 1 (component `wikidata`, zero cost, CC0 data). Only that host is declared;
  `query.wikidata.org` (SPARQL) is not. From the dev sandbox (2026-11-13), the API answered **HTTP 429** to a generic
  User-Agent. Wikimedia requires a descriptive User-Agent with contact details, so `visual_context._wikidata_country`
  should send one and honour `Retry-After`. Reachability from the owner's PC is **not verified**.
- `api.gdeltproject.org` stays declared by **Account 3** (`gdelt-doc` on its branch). Account 1 does not duplicate it:
  `manifest()` keys by id, and `tests/fast/test_costguard_research_hosts.py` requires every id to be unique and at most
  one component to declare the GDELT host. From the dev sandbox the connection is reset, as Account 3 also reported.
  Reachability from the owner's PC is **not verified**.
