# Account 1 integration contracts (v1)

Source of truth: `nexus/contracts.py` (lead). JSON Schemas: `docs/contracts/<Model>.v1.schema.json`, also served live at
`GET /api/v1/contracts`. This document explains every contract, gives a valid example of each (the examples below are
parsed and validated by `tests/fast/test_contracts.py`, so they cannot drift from the code) and states the
**behaviour contracts** that sit around the data contracts: storage providers, the remote dispatcher, idempotency and
events.

## Rules that apply to every contract

| Rule | Consequence for clients |
|---|---|
| Every top-level model carries `contract = "<Name>/v1"` (`StorageProviderInfo` carries `StorageProvider/v1`; `ArtifactLocation` is an embedded value object versioned with `ArtifactManifest`). | Send the literal or omit it (the default fills it). A `/v2` value is rejected by a v1 model. |
| `extra="forbid"`: unknown fields are an error. | A typo fails loudly (HTTP 422 from the API) instead of being dropped. |
| No secrets in contracts. Providers are named by `connection_id` (e.g. `drive_a`, `kaggle_main`), never by token. | Never put a token in `params`, `metadata`, `provenance`, `message` or `log_tail`. The event bus redacts stored secrets and token-shaped strings, but that is a safety net, not a channel. |
| Times are UNIX seconds (float, UTC); sizes are bytes; checksums are lowercase hex SHA-256. | `"sha256": "ABC..."` is rejected. |
| IDs are `<prefix>_<12 hex>`: `art_`, `ing_`, `rjob_`, `evt_`, `ntf_`. Generated when omitted. | Clients may supply their own id when they need it before submission. |
| Versioning: additive optional fields only within v1; anything else is a new `/v2` model served side by side. | `test_committed_schemas_match` fails CI when a model changes without the committed schema changing. |

Quick reference

| Contract | Produced by | Consumed by |
|---|---|---|
| ArtifactManifest (+ ArtifactLocation) | storage adapters, workers, ingest | everyone (the only way files cross module/account boundaries) |
| QuotaStatus, ProviderHealth | storage + compute providers | Studio, scheduler, dispatcher |
| StorageProviderInfo | each storage adapter (static `INFO`) | Studio, transfer engine |
| MediaSource, MediaIngestionJob | Studio / Account 3 research (sources) | ingest (sub-agent C) |
| RemoteWorkerCapability | compute providers (probe job) | dispatcher placement |
| ProductionJobManifest | Accounts 1/2/3, scheduler | dispatcher (sub-agent B) |
| JobCheckpoint, WorkerExecutionResult | remote workers | dispatcher, Studio |
| AIProviderCapability | Account 3 | Account 1 routing/metering |
| ProductionEvent, NotificationEvent | event bus (`nexus/cloud/events.py`) | Studio, scheduler, other accounts |

---

## 1. Artifacts and storage

### ArtifactLocation
One copy of an artifact on one storage connection. `ref` is what the provider needs to find the file again (Drive file
id, object key, or a path relative to the local storage root); `path` is for humans. `verified_at` is set when a
checksum of that copy was confirmed after transfer.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `provider` | str | **required** |  |
| `connection_id` | str | **required** |  |
| `ref` | str | **required** | provider-native id or key (Drive file id, object key, local relative path) |
| `path` | str | `''` | human-readable path, e.g. NEXUS/Masters/prj_x/master.mp4 |
| `verified_at` | float \| None | `None` |  |

<!-- example: ArtifactLocation -->
```json
{"provider": "gdrive", "connection_id": "drive_b", "ref": "1AbCdEfGhIjKlMnOpQrStUv", "path": "NEXUS/Masters/prj_2b1a5a2f07/master.mp4", "verified_at": 1794500000.0}
```

### ArtifactManifest
The unit of exchange for files. A file is identified by `sha256` (content) and `artifact_id` (record); the same content
may have several `locations` (e.g. the worker scratch copy and the Drive B master). `provenance` records licence, source
URL, generator and model so publishing can prove rights. `retention`: `keep` (never auto-deleted), `temporary` (may be
deleted after `expires_at`), `archive` (cold storage candidate). Registered in the `artifacts` table, deduplicated by
`sha256`.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | ArtifactManifest/v1 | `'ArtifactManifest/v1'` |  |
| `artifact_id` | str | generated |  |
| `kind` | source \| proxy \| analysis \| master \| short \| localized \| thumbnail \| audio \| subtitle \| project_file \| archive \| log \| checkpoint \| other | **required** |  |
| `name` | str | **required** |  |
| `mime` | str | **required** |  |
| `size` | int | **required** | (Ge(ge=0)) |
| `sha256` | str | **required** | (pattern='^[0-9a-f]{64}$') |
| `project_id` | str \| None | `None` |  |
| `produced_by_job` | str \| None | `None` |  |
| `locations` | list[ArtifactLocation] | `[]` |  |
| `provenance` | dict[str, Any] | `{}` | licence, source URL, generator, model... |
| `retention` | keep \| temporary \| archive | `'keep'` |  |
| `expires_at` | float \| None | `None` |  |
| `created_at` | float | now |  |

<!-- example: ArtifactManifest -->
```json
{"contract": "ArtifactManifest/v1", "artifact_id": "art_0123456789ab", "kind": "master", "name": "master.mp4", "mime": "video/mp4", "size": 734003200, "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "project_id": "prj_2b1a5a2f07", "produced_by_job": "rjob_a1b2c3d4e5f6", "locations": [{"provider": "gdrive", "connection_id": "drive_b", "ref": "1AbCdEfGhIjKlMnOpQrStUv", "path": "NEXUS/Masters/prj_2b1a5a2f07/master.mp4", "verified_at": 1794500000.0}], "provenance": {"source": "drive_a:1ZyXwVu", "licence": "owned", "generator": "nexus render_timeline"}, "retention": "keep", "expires_at": null, "created_at": 1794499000.0}
```

### QuotaStatus
A measured or documented quota. `limit = null` means the provider does not expose it - it is never guessed. `source`
says where the numbers came from; only `provider_api` and `measured` may drive automatic decisions, `documented` is a
planning hint. `exhausted = true` makes the dispatcher/transfer engine skip the connection until `period` rolls over.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | QuotaStatus/v1 | `'QuotaStatus/v1'` |  |
| `provider` | str | **required** |  |
| `connection_id` | str | **required** |  |
| `resource` | storage_bytes \| gpu_seconds_week \| runtime_seconds \| api_requests \| egress_bytes \| other | **required** |  |
| `limit` | float \| None | `None` | None = not exposed by the provider (never guessed) |
| `used` | float \| None | `None` |  |
| `remaining` | float \| None | `None` |  |
| `period` | str \| None | `None` | e.g. 'weekly', 'per-session'; None = not periodic |
| `source` | provider_api \| measured \| documented \| unknown | **required** |  |
| `measured_at` | float | now |  |
| `exhausted` | bool | `False` |  |

<!-- example: QuotaStatus -->
```json
{"contract": "QuotaStatus/v1", "provider": "kaggle", "connection_id": "kaggle_main", "resource": "gpu_seconds_week", "limit": 108000, "used": 7260, "remaining": 100740, "period": "weekly", "source": "measured", "measured_at": 1794500000.0, "exhausted": false}
```

### ProviderHealth
Result of a health check of one connection. `identity` (account e-mail/username) is shown to the owner only and never
logged in events. `unauthorized` means re-authorisation is needed (expired/revoked refresh token); `not_configured`
means no credential exists.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | ProviderHealth/v1 | `'ProviderHealth/v1'` |  |
| `provider` | str | **required** |  |
| `connection_id` | str | **required** |  |
| `status` | ok \| degraded \| unavailable \| unauthorized \| quota_exhausted \| not_configured | **required** |  |
| `detail` | str | `''` |  |
| `identity` | str \| None | `None` | authorised account e-mail/username, shown to the owner only |
| `scopes` | list[str] | `[]` |  |
| `last_success` | float \| None | `None` |  |
| `last_error` | str \| None | `None` |  |
| `checked_at` | float | now |  |
| `quotas` | list[QuotaStatus] | `[]` |  |

<!-- example: ProviderHealth -->
```json
{"contract": "ProviderHealth/v1", "provider": "gdrive", "connection_id": "drive_a", "status": "ok", "detail": "listed NEXUS/ in 0.4 s", "identity": "owner@example.com", "scopes": ["https://www.googleapis.com/auth/drive.file"], "last_success": 1794500000.0, "last_error": null, "checked_at": 1794500000.0, "quotas": [{"provider": "gdrive", "connection_id": "drive_a", "resource": "storage_bytes", "limit": 16106127360, "used": 2147483648, "remaining": 13958643712, "source": "provider_api"}]}
```

### StorageProviderInfo (`StorageProvider/v1`)
Static description of an adapter (its class attribute `INFO`). `billing_required = true` makes `storage.eligibility` /
`costguard` refuse the provider (zero-spend rule). `max_connections` is how many accounts of this provider NEXUS may
hold (Drive: 3 - `drive_a`, `drive_b`, `drive_c`).

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | StorageProvider/v1 | `'StorageProvider/v1'` |  |
| `provider` | str | **required** |  |
| `display` | str | **required** |  |
| `capabilities` | list[upload \| resumable_upload \| download \| list \| delete \| move \| checksum \| versions \| quota \| folders'] | **required** |  |
| `max_connections` | int | `1` |  |
| `billing_required` | bool | `False` |  |
| `notes` | str | `''` |  |

<!-- example: StorageProviderInfo -->
```json
{"contract": "StorageProvider/v1", "provider": "gdrive", "display": "Google Drive", "capabilities": ["upload", "resumable_upload", "download", "list", "delete", "move", "checksum", "versions", "quota", "folders"], "max_connections": 3, "billing_required": false, "notes": "drive.file scope: NEXUS only sees files it created or the owner picked"}
```

## 2. Media intake

### MediaSource
Where media comes from and who asserted the right to use it. `rights = unknown` is allowed for analysis but blocks
publishing. `declared_by` is recorded with the import.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | MediaSource/v1 | `'MediaSource/v1'` |  |
| `kind` | upload \| http_url \| drive_file \| provider_api \| artifact | **required** |  |
| `ref` | str | **required** | URL, Drive file id, provider item id, or artifact id |
| `connection_id` | str \| None | `None` |  |
| `rights` | owned \| licensed \| public_domain \| authorised \| unknown | `'unknown'` |  |
| `licence` | str \| None | `None` |  |
| `attribution` | str \| None | `None` |  |
| `declared_by` | str | `'owner'` | who asserted the rights (recorded with the import) |

<!-- example: MediaSource -->
```json
{"contract": "MediaSource/v1", "kind": "http_url", "ref": "https://images-assets.nasa.gov/video/example/example~orig.mp4", "connection_id": null, "rights": "public_domain", "licence": "NASA media guidelines", "attribution": "NASA", "declared_by": "owner"}
```

### MediaIngestionJob
One import. Status flow: `queued -> validating -> downloading -> verifying -> stored`, or `rejected` (policy: SSRF
target, MIME not in `allowed_mime`, larger than `max_bytes`, ffprobe refused the file) or `failed` (transient error after
retries). `reason` is always set for `rejected`/`failed`. On `stored`, `artifact` holds the manifest of the original.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | MediaIngestionJob/v1 | `'MediaIngestionJob/v1'` |  |
| `ingestion_id` | str | generated |  |
| `source` | MediaSource | **required** |  |
| `project_id` | str \| None | `None` |  |
| `destination` | str \| None | `None` | connection_id of the storage that keeps the original |
| `max_bytes` | int | `4294967296` | (Ge(ge=1)) |
| `allowed_mime` | list[str] | `['video/', 'audio/', 'image/']` |  |
| `status` | queued \| validating \| downloading \| verifying \| stored \| rejected \| failed | `'queued'` |  |
| `reason` | str \| None | `None` |  |
| `artifact` | ArtifactManifest \| None | `None` |  |

<!-- example: MediaIngestionJob -->
```json
{"contract": "MediaIngestionJob/v1", "ingestion_id": "ing_0123456789ab", "source": {"kind": "drive_file", "ref": "1ZyXwVuTsRqPoNmLkJiHg", "connection_id": "drive_a", "rights": "owned"}, "project_id": "prj_2b1a5a2f07", "destination": "drive_b", "max_bytes": 4294967296, "allowed_mime": ["video/", "audio/"], "status": "rejected", "reason": "mime application/x-msdownload not allowed"}
```

## 3. Compute

### RemoteWorkerCapability
What a worker can do. Declared values are planning hints; `measured = true` only when a probe job on that worker
reported them (e.g. Kaggle: `Tesla T4 x2`, 14.6 GB VRAM each, 31 GB RAM - measured 2026-11-12). Placement may only use
measured values for hard requirements such as `vram_gb`.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | RemoteWorkerCapability/v1 | `'RemoteWorkerCapability/v1'` |  |
| `provider` | local \| kaggle \| colab \| user_gpu \| other | **required** |  |
| `connection_id` | str | **required** |  |
| `accelerator` | str \| None | `None` | measured, e.g. 'Tesla T4 x2'; None = CPU only |
| `vram_gb` | float \| None | `None` |  |
| `ram_gb` | float \| None | `None` |  |
| `cpus` | int \| None | `None` |  |
| `max_runtime_s` | float \| None | `None` |  |
| `frameworks` | list[str] | `[]` |  |
| `internet` | bool | `False` |  |
| `measured` | bool | `False` | True only when the values come from a completed probe job |
| `measured_at` | float \| None | `None` |  |

<!-- example: RemoteWorkerCapability -->
```json
{"contract": "RemoteWorkerCapability/v1", "provider": "kaggle", "connection_id": "kaggle_main", "accelerator": "Tesla T4 x2", "vram_gb": 14.6, "ram_gb": 31.0, "cpus": 4, "max_runtime_s": 43200, "frameworks": ["torch", "ffmpeg"], "internet": true, "measured": true, "measured_at": 1794480000.0}
```

### ProductionJobManifest
A unit of remote work. `task` must be a task registered in the compute task registry (lower_snake_case, 2-49 chars;
validated). `inputs` are artifact ids (never URLs with credentials); the worker receives short-lived access, not the
owner's tokens. `requires` is matched against measured capabilities (`{"gpu": true, "vram_gb": 12}`). `priority`
0 = most urgent. `idempotency_key`: see below.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | ProductionJobManifest/v1 | `'ProductionJobManifest/v1'` |  |
| `job_id` | str | generated |  |
| `task` | str | **required** | registered task name, e.g. 'probe', 'ffmpeg_transcode', 'render_timeline', 'translate' |
| `owner_account` | account1 \| account2 \| account3 | `'account1'` |  |
| `project_id` | str \| None | `None` |  |
| `params` | dict[str, Any] | `{}` |  |
| `inputs` | list[str] | `[]` | artifact ids the worker needs |
| `output_destination` | str \| None | `None` | storage connection_id for results |
| `requires` | dict[str, Any] | `{}` | e.g. {'gpu': true, 'vram_gb': 12} |
| `priority` | int | `5` | 0 = most urgent (Ge(ge=0), Le(le=9)) |
| `max_runtime_s` | float | `3600` | (Gt(gt=0)) |
| `max_retries` | int | `2` | (Ge(ge=0), Le(le=10)) |
| `preferred_providers` | list[str] | `[]` |  |
| `idempotency_key` | str \| None | `None` | same key = same job; resubmission returns the existing one |
| `created_at` | float | now |  |

<!-- example: ProductionJobManifest -->
```json
{"contract": "ProductionJobManifest/v1", "job_id": "rjob_a1b2c3d4e5f6", "task": "ffmpeg_transcode", "owner_account": "account2", "project_id": "prj_2b1a5a2f07", "params": {"codec": "h264", "crf": 20, "height": 1080}, "inputs": ["art_0123456789ab"], "output_destination": "drive_b", "requires": {"gpu": false}, "priority": 3, "max_runtime_s": 5400, "max_retries": 2, "preferred_providers": ["kaggle", "local"], "idempotency_key": "prj_2b1a5a2f07:master:v3", "created_at": 1794499000.0}
```

### JobCheckpoint
Progress a worker can resume from. `seq` increases per job; `(job_id, seq)` is unique (`remote_checkpoints` table).
`state` must be small JSON (no media); large intermediate files are artifacts listed in `artifacts`.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | JobCheckpoint/v1 | `'JobCheckpoint/v1'` |  |
| `job_id` | str | **required** |  |
| `step` | str | **required** |  |
| `seq` | int | **required** | (Ge(ge=0)) |
| `state` | dict[str, Any] | `{}` |  |
| `artifacts` | list[str] | `[]` | artifact ids produced so far |
| `created_at` | float | now |  |

<!-- example: JobCheckpoint -->
```json
{"contract": "JobCheckpoint/v1", "job_id": "rjob_a1b2c3d4e5f6", "step": "encode_segments", "seq": 4, "state": {"segments_done": 41, "segments_total": 44}, "artifacts": ["art_aaaaaaaaaaaa"], "created_at": 1794499500.0}
```

### WorkerExecutionResult
Final report of one attempt. `lost` = the worker vanished (no heartbeat / kernel gone); `quota_exhausted` = provider
refused for quota (the dispatcher pauses that connection and fails over, it does not burn retries). `log_tail` is
redacted before storage. `checkpoint` is the last checkpoint, so a retry can resume.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | WorkerExecutionResult/v1 | `'WorkerExecutionResult/v1'` |  |
| `job_id` | str | **required** |  |
| `provider` | str | **required** |  |
| `connection_id` | str | **required** |  |
| `status` | succeeded \| failed \| cancelled \| lost \| quota_exhausted | **required** |  |
| `started_at` | float \| None | `None` |  |
| `finished_at` | float \| None | `None` |  |
| `duration_s` | float \| None | `None` |  |
| `accelerator` | str \| None | `None` |  |
| `metrics` | dict[str, Any] | `{}` | gpu util, peak vram, cpu, sizes, download time... |
| `artifacts` | list[ArtifactManifest] | `[]` |  |
| `log_tail` | str | `''` |  |
| `error` | str \| None | `None` |  |
| `checkpoint` | JobCheckpoint \| None | `None` |  |

<!-- example: WorkerExecutionResult -->
```json
{"contract": "WorkerExecutionResult/v1", "job_id": "rjob_a1b2c3d4e5f6", "provider": "kaggle", "connection_id": "kaggle_main", "status": "succeeded", "started_at": 1794499100.0, "finished_at": 1794499900.0, "duration_s": 800.0, "accelerator": "Tesla T4 x2", "metrics": {"gpu_util_avg": 0.61, "peak_vram_gb": 3.2, "download_s": 41.0}, "artifacts": [{"kind": "master", "name": "master.mp4", "mime": "video/mp4", "size": 734003200, "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"}], "log_tail": "encode done", "error": null, "checkpoint": null}
```

### AIProviderCapability
Declared by Account 3 for each AI provider it can call (local model or free API). Account 1 routes and meters by
`tasks` and `quotas`; it never hard-codes a provider. `commercial_use` must be `yes` before output is used in a
publishable product; `unverified` is treated as `no` by publishing.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | AIProviderCapability/v1 | `'AIProviderCapability/v1'` |  |
| `provider` | str | **required** |  |
| `connection_id` | str | **required** |  |
| `tasks` | list[str] | **required** |  |
| `models` | list[str] | `[]` |  |
| `local` | bool | `False` |  |
| `commercial_use` | yes \| no \| unverified | `'unverified'` |  |
| `quotas` | list[QuotaStatus] | `[]` |  |

<!-- example: AIProviderCapability -->
```json
{"contract": "AIProviderCapability/v1", "provider": "local_madlad", "connection_id": "local", "tasks": ["translate"], "models": ["madlad400-3b-mt-ct2"], "local": true, "commercial_use": "yes", "quotas": []}
```

## 4. Events

### ProductionEvent
Append-only history in `cloud_events`. `message` is redacted (stored secret values and token-shaped strings removed) and
capped at 2,000 characters before storage. `data` is structured and must not contain secrets. Types and who emits them
are listed in the behaviour contract below.

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | ProductionEvent/v1 | `'ProductionEvent/v1'` |  |
| `event_id` | str | generated |  |
| `job_id` | str \| None | `None` |  |
| `type` | job.submitted \| job.dispatched \| job.progress \| job.checkpoint \| job.succeeded \| job.failed \| job.cancelled \| job.retrying \| worker.lost \| quota.exhausted \| storage.uploaded \| storage.failed \| ingest.stored \| ingest.rejected \| scheduler.paused \| scheduler.resumed \| scheduler.stopped \| provider.health | **required** |  |
| `message` | str | `''` |  |
| `data` | dict[str, Any] | `{}` |  |
| `at` | float | now |  |

<!-- example: ProductionEvent -->
```json
{"contract": "ProductionEvent/v1", "event_id": "evt_0123456789ab", "job_id": "rjob_a1b2c3d4e5f6", "type": "job.checkpoint", "message": "41/44 segments", "data": {"seq": 4, "provider": "kaggle"}, "at": 1794499500.0}
```

### NotificationEvent
Owner-facing notification. Created automatically for `job.failed` (error), `storage.failed` (error), `worker.lost`,
`quota.exhausted`, `ingest.rejected`, `scheduler.stopped` (warning) and `job.succeeded` (success).

| Field | Type | Required / default | Meaning |
|---|---|---|---|
| `contract` | NotificationEvent/v1 | `'NotificationEvent/v1'` |  |
| `notification_id` | str | generated |  |
| `level` | info \| success \| warning \| error | **required** |  |
| `title` | str | **required** |  |
| `body` | str | `''` |  |
| `source_event` | str \| None | `None` |  |
| `read` | bool | `False` |  |
| `at` | float | now |  |

<!-- example: NotificationEvent -->
```json
{"contract": "NotificationEvent/v1", "notification_id": "ntf_0123456789ab", "level": "warning", "title": "quota exhausted (rjob_a1b2c3d4e5f6)", "body": "Kaggle weekly GPU allowance used; job moved to local", "source_event": "evt_0123456789ab", "read": false, "at": 1794499600.0}
```

---

## 5. Behaviour contracts

These are normative for the implementations owned by sub-agents B (compute) and C (storage); their code
(`nexus/cloud/compute/`, `nexus/cloud/storage/`) is the authority for exact signatures. Contract tests for each belong
next to the implementation.

### 5.1 StorageProvider (`nexus/cloud/storage/base.py`)

| Method | Semantics |
|---|---|
| `connect() -> ProviderHealth` | Validate the stored credential (vault) and record `identity`/`scopes` in `cloud_connections`. Never returns or logs a token. |
| `health() -> ProviderHealth` | Cheap check; `unauthorized` when the refresh token is revoked/expired, `quota_exhausted` when full. |
| `quota() -> QuotaStatus` | From the provider API when available (`source="provider_api"`), else `unknown` with `limit=null`. |
| `list(path)`, `stat(ref)`, `mkdirs(path)` | Namespace operations confined to the app root (`NEXUS/`). Paths are normalised; `..`, absolute paths and drive letters are refused (`safe_rel`). |
| `checksum(ref)` | `{"sha256", "md5", "source": "provider" or "computed"}`. Drive exposes md5 only, so sha256 is computed on download and stored in the artifact. |
| `delete(ref, only_app_created=True)` | By default deletes only files NEXUS created (Drive `appProperties`). Irreversible deletion of owner files requires the owner's explicit confirmation (CLAUDE.md). |
| `move(ref, dest_path)` | Within one connection only; cross-account copies go through download + upload. |
| `download(ref, dest, resume=True)` | Streams to a temp file in `dest`'s directory, resumes with Range, verifies size + checksum, then renames atomically. |
| `start_upload / query_upload / put_chunk / abort_upload` | Resumable primitives. The session URI is persisted in `transfers.session_uri` so an upload resumes after a crash; chunks are multiples of 256 KiB. |
| `upload(...)`, `resume(transfer_id)` | Shared engine: computes sha256 first, verifies after completion (`ChecksumMismatch` deletes the bad remote copy), records the location in the artifact manifest. `resume` refuses a transfer that belongs to another connection. |
| Errors | `NotConfigured`, `ReauthRequired`, `QuotaExceeded`, `ChecksumMismatch`, `TransientError` (retry with backoff), `UploadSessionExpired` (restart the session). Error texts are redacted. |
| Isolation | Each instance is bound to one `connection_id`; it reads only its own vault secret. `drive_a` code paths can never use `drive_b` credentials. |

### 5.2 Remote dispatcher (sub-agent B)

| Operation | Semantics |
|---|---|
| `submit(manifest)` | Validates the manifest and that `task` is registered; persists a `remote_jobs` row (`queued`) **before** anything leaves the machine; emits `job.submitted`. With an `idempotency_key` already present, returns the existing job (no second row, no second dispatch) - enforced by the unique index `remote_jobs_idem`. |
| placement | Chooses the first available provider in `preferred_providers` (else the default order) whose *measured* capability satisfies `requires`, that is not paused and whose quota is not exhausted. Emits `job.dispatched` with provider + connection. Remote providers are private (Kaggle kernels `is_private=true`). |
| progress | Heartbeats update `remote_jobs.heartbeat`; checkpoints go to `remote_checkpoints` and emit `job.checkpoint`. |
| `cancel(job_id)` | Sets `cancel_requested=1`; the provider is asked to stop; final state `cancelled`, event `job.cancelled`. Idempotent: cancelling a finished job is a no-op that returns its state. |
| retry | On `failed`/`lost`, `attempts < max_retries` -> `job.retrying`, resubmitted from the last checkpoint. `quota_exhausted` pauses that connection and fails over without consuming an attempt. After the last attempt -> `job.failed` (+ error notification). |
| results | The worker returns a `WorkerExecutionResult`; artifacts are verified (size + sha256) before the job is `succeeded`; outputs go to `output_destination`. |
| pull workers (Colab / user GPU) | Each worker has its own token, stored only as a SHA-256 hash, compared in constant time, revocable per worker (`nexus/cloud/compute/pull.py`, or `security.issue_worker_token` / `require_worker`). A worker can claim and report only jobs dispatched to it. |

### 5.3 Idempotency

* `ProductionJobManifest.idempotency_key`: same key = same job, forever (until the row is deleted). Use a natural key
  such as `"<project>:<deliverable>:<revision>"`.
* Artifacts are deduplicated by `sha256`; uploading the same content twice to the same connection returns the existing
  location.
* Transfers are resumable by `transfer_id`; re-running a finished transfer returns the finished file.
* Event ids are unique; consumers that poll `GET /api/v1/events?since=` must de-duplicate by `event_id`.

### 5.4 Events emitted

| Type | Emitter | Notification |
|---|---|---|
| `job.submitted`, `job.dispatched`, `job.progress`, `job.checkpoint`, `job.retrying` | dispatcher | - |
| `job.succeeded` / `job.failed` / `job.cancelled` | dispatcher | success / error / - |
| `worker.lost` | dispatcher (heartbeat timeout) | warning |
| `quota.exhausted` | compute or storage provider | warning |
| `storage.uploaded` / `storage.failed` | transfer engine | - / error |
| `ingest.stored` / `ingest.rejected` | ingest | - / warning |
| `scheduler.paused` / `scheduler.resumed` / `scheduler.stopped` | scheduler | - / - / warning |
| `provider.health` | health checks | - |

Security events (login failures, sessions, worker tokens, connection connect/disconnect, scans) are **not**
ProductionEvents: they go to the audit log (`nexus/cloud/audit.py`, `GET /api/v1/security/audit`).
