# Decisions

Ids from D-11 up were assigned on separate account branches, so Account 1 and Account 3 both have entries D-11 to D-15.
Each entry names its account; quote a decision as "D-13 (Account 1)" or "D-13 (Account 3)". Ids are not renumbered, so
existing references stay valid.

**D-1 Keep the `nexus/` package layout; do not restructure into apps/backend/core/workers.** The suggested tree is a
target shape for a larger codebase; today it would only move ~6 k lines of working, tested code and break imports for
no functional gain. New subsystems follow the same separation inside the package (`nexus/storage/`, `jobs.py` vs
`workflows.py`). Revisit when a second app (desktop shell) exists.

**D-2 Job durability via SQLite rows + stage checkpoints, not a queue server.** Redis/Celery/Temporal add services to
install on a creator's Windows PC. SQLite (WAL) is already the store; one worker process is the right concurrency for a
4-core machine; a cross-process lock keeps "one heavy job at a time".

**D-3 Own snapshot engine + rclone transport instead of restic/Kopia.** See `STORAGE.md` → Backup tool decision.

**D-4 Cloudflare R2 is blocked** although it is the best technical fit: it needs billing details and bills overage.
Backblaze B2 (card-free) is the recommended S3-compatible remote; a second local disk is the recommended first copy.

**D-5 Hugging Face Hub is not a backup destination.** 100 GB of free private storage exists, but the Hub is for
models/datasets; user videos do not belong there.

**D-6 Windows packaging: launchers now, portable Python later, no Electron.** `.bat` + `.venv` gives one-click start
today. Next step (WIN-2) is a portable embedded-Python folder; a Tauri shell is optional polish. Electron is rejected
(heavy for the target hardware). No packaging before WIN-1 is verified on real Windows.

**D-7 Branching: `main` = verified; work on short-lived branches via pull requests.** No `develop` branch until more
than one stream of work exists. Prior work lived only on `claude/optimistic-franklin-376ndo`; this session's branch is
built on top of it and its PR brings both into `main`.

**D-8 Lint scope = real defects only** (`pyproject.toml`): undefined names, unused code, bug-prone patterns. Style
reformatting of the existing code is deliberately out of scope.

**D-9 Grammar-constrained JSON for local-LLM structured output.** The 4B model intermittently answers `{}` in free JSON
mode; a JSON schema makes required keys and array lengths impossible to skip. Prompt wording alone was not reliable.

**D-10 No LICENSE file yet.** Choosing a licence (or keeping the repository private/all-rights-reserved) is the owner's
decision; noted in the handoff as a pending user decision.

**D-11 (Account 3) AI quality bars are soft by default.** Each AI task has a minimum quality (router `TASKS[...]["min_q"]`).
A 6-item benchmark measured local Qwen3-4B at 3.3/10 on reasoning (thinking off), below the 4 needed for deep research,
reference interpretation, quality review and analytics interpretation. Failing those tasks outright would stop the
product on any machine without a GPU or an authorised hosted tier. Decision: when nothing eligible meets the bar, the
router runs the best eligible model and returns `below_bar` (required vs measured score); the UI shows "draft quality -
review before use". Callers that must not accept a draft pass `strict=True`. Nothing is ever routed to a paid endpoint or
past a privacy class to reach the bar.

**D-12 (Account 3) Provider exclusions.** Cerebras (payment method required for API access per its docs, 2026-11-12) and
Hugging Face Inference Providers (no free credits on free accounts) are excluded by the router's zero-spend gate even if
authorised. Second local provider: llama.cpp's OpenAI-compatible server with Qwen3-0.6B (Apache-2.0) - proves
multi-provider routing and failover with no hosted API. Details: `docs/ACCOUNT3_MODEL_COMPARISON.md`.

**D-13 (Account 3) Shared contracts layout.** `nexus.contracts` is a package owned jointly: Account 1 in `cloud.py`
(pydantic, `contract: "Name/v1"`), Account 2 in `media.py` (dict schemas, `schema: "nexus.Name/1"`), Account 3 in
`nexus.intelligence.contracts`; the package `__init__` re-exports both original APIs and offers `registry()` +
`validate_any()`. Chosen over renaming because neither owner's imports change and the collision becomes testable.

**D-14 (Account 3) Visual context gate is metadata-first.** Images are accepted only when their own Commons metadata
places them (country, then named place), dates them compatibly, is not a map/emblem out of context, and — when the
scene asks for a structure — names that structure. Found necessary by inspecting four real renders (Demo 4, Demo 10
runs 1-3). Image classification (SigLIP) is optional evidence, never downloaded during a render.

**D-15 (Account 3) Benchmarked remote models are batch-only.** Kaggle T4 runs measured 10 open-weight models; valid ones
are recorded as `availability: batch_only` profiles. Interactive routing stays local. Critical tasks below their
quality bar return `requires_human_review`; dialect output is always reviewed (best measured 0.52).

**D-16 (Account 3) Zero-spend gate fails closed.** The router previously refused only `payment_method_required is True`.
That let the string `unverified` (Groq, Mistral) through once the owner authorised a provider and added a key. Now:
- A hosted provider is eligible only when its catalogue entry says the boolean `false` and has a dated `verified`.
- The model must be listed in the catalogue, or carry the free suffix.
- Authorisation must be the boolean `true`, a key must be present, and an enforceable quota must exist: documented
  rpm/rpd/tpm/tpd, or the owner's `daily_requests`.
- The facts are re-read from the catalogue, so a caller-supplied entry cannot loosen them (quotas take the stricter value).
- An unknown `hosting` value is treated as hosted.
- `costguard.check_host(..., free_tier_provider=, model=)` no longer trusts the name. It re-runs the full gate through
  `router.hosted_call_allowed` and refuses if that gate fails or cannot be evaluated.
- Regression tests: `tests/fast/test_zero_spend_fail_closed.py` (no network; sockets trapped).

**D-17 (Account 3) batch_llm jobs carry one model and the prompts.** `bench_v2.account1_manifest` used to send
`params.models` (a list), `items` as a count and `runtime: "ollama"`. Account 1's `batch_llm` rejects that shape on the
worker. It now emits one ProductionJobManifest/v1 per model:
- `params.model` is a Hugging Face repo;
- `params.items` is the prompt list;
- the engine is transformers, vllm or echo;
- the idempotency key is computed over the params, and the job is Kaggle only.

`batch_jobs.validate_manifest` refuses the old shape with a message naming the field. Models whose fp16 weights exceed
24 GiB (2x T4) are listed as not eligible, never silently dropped. Ollama stays inside A3's own benchmark kernels and is
never a batch_llm engine.

**D-18 (Account 3) A finished render is not a publishable result.** Publishing needs Account 2's QualityReport for that
exact file: `PASSED_AUTOMATED_CHECKS`, no critical finding, and a matching checksum. Public or unlisted also needs human
QA approval (`human_approval` starting with APPROVED), native review for the same language variety where required, and
the owner's disclosure decision where the platform asks for a judgement (YouTube realism). Unknown provenance is
disclosed as AI. Content is labelled non-AI only on an owner attestation with no AI records. A platform that cannot
carry a required label and has no private mode (Facebook /videos) refuses AI content.

**D-19 (Account 3) Analytics rows carry their origin.** Every record is real_platform_data, user_uploaded_data,
public_benchmark_data or simulated_test_data. Simulated rows never conclude an experiment, and fixtures and demos say
simulated. Ambiguous dates (01/02/2026) and publish times without a stated time zone are not guessed.

**D-20 (Account 3) PR #5 follows its base by merging, never by rebasing (2026-11-13).** The base branch gained
Account 1's PR #4. Rebasing 98 commits would rewrite a published branch and need a force-push, so the base was merged
into the A3 branch instead. Every conflict kept both sides; generated files were regenerated. Each later base change
is handled the same way.

**D-11 `batch_llm` is a bounded batch job on Kaggle, not a model server (Account 1, 2026-11-13).** Kaggle does not allow
persistent serving, and the owner's PC must not receive multi-GB weights implicitly. So the task loads one model inside
the kernel, answers a finite JSONL, writes checksum-verified parts with a checkpoint after each part, and exits. Routing
is `providers=("kaggle",)`. The weight size is checked from Hub metadata before download. `transformers` is the default
engine because it is preinstalled on Kaggle; vLLM is used only if already present. Ollama was rejected because it runs a
local server process.

**D-12 Shared contracts: Account 1 adopts the agreed layout only through the integration merge (2026-11-13).**
`nexus/contracts.py` stays as-is on Account 1's branch. `integration/contracts_v1/apply.sh` moves it to
`nexus/contracts/cloud.py` during the A1 → A2 → A3 merge. Committing the package layout on Account 1's branch alone would
collide with Account 2's `nexus/contracts/__init__.py` in the same way.

**D-13 The control plane is a single-process service; production enforces it (Account 1, 2026-11-13).** Rate-limit
counters, the remote-job poller, the alert loop and the scheduler thread live in one process, and SQLite is the only
state store. A shared rate-limit store (Redis etc.) would add a service for no benefit to a single owner. So
production takes an OS lock on `NEXUS_HOME/run/api.lock` (released by the kernel on death), `serve` runs uvicorn with
one worker, and behind the owner's reverse proxy the real client is taken from `X-Forwarded-For` only for peers in
`NEXUS_TRUSTED_PROXIES`. Horizontal scaling is documented as unsupported.

**D-14 Backups carry the vault as ciphertext and never the key (Account 1, 2026-11-13).** Without the vault entries a
restored host must re-authorise every Drive account; with entries and key together one backup copy would expose every
credential. So `*.secret` files go into the `state` tier (Fernet ciphertext, plus AES-256-GCM for remote
destinations) and `vault.key` never does; the key lives in the host's secret store (`NEXUS_VAULT_KEY`) and in an
offline copy made with `python -m nexus vault-key export` (never printed, refused inside NEXUS_HOME, the repository or a
backup destination). Production refuses to start while a key file sits in NEXUS_HOME.

**D-15 Production is an explicit, fail-closed profile (Account 1, 2026-11-13).** `NEXUS_ENV=production` turns
development conveniences (loopback without token, generated key file, API docs) off and refuses to start unless every
gate in `nexus/cloud/production.py` passes, so a misconfigured host can never silently serve in development mode.
**D-11 Visual understanding = SigLIP base (Apache-2.0) as int8 ONNX on onnxruntime; LAION CLIP only as a benchmark
metric.** The LAION ViT-B/32 card puts any deployed use out of scope, OpenAI CLIP ports are research-only, MobileCLIP is
Apple sample-code licensed. SigLIP: 213 MB opt-in download through the Cost Guard, 150-240 ms per frame on 2 CPU threads.

**D-12 Shared contracts layout (three accounts).** Account 2's schemas live in `nexus/contracts/media.py`; the package
`__init__` re-exports them on this branch and is replaced by the shared `__init__` from Account 3's
`integration/contracts_v1/apply.sh` at the three-account merge. Account 2 code reads its own version as
`contracts.media.VERSION` (the package-level `VERSION` means Account 1's `"v1"` after the merge).

**D-13 Account 2 work on Account 1 workers goes through Account 1's extension points only.** Worker task files under
`nexus/cloud/worker_kit/tasks/` (the folder Account 1 documents for Accounts 2/3) and `tasks.register(...)` at import;
no Account 1 file is edited. The worker cannot import the renderer, so Account 2 compiles plain-cut timelines into FFmpeg
steps; richer timelines render locally.

**D-14 B-roll from a library needs the film's named subject.** The broad gate (any of the programme's 12 top terms)
let an Earth storm through under Mars narration because "storm" matched. `subject` (API `broll_subject`, editor "B-roll
must show") is required metadata for library pictures; local shots of the user's own files are judged visually.


**D-15 Account 2 archives through Account 1's storage layer; no second router.** Daily finals go through
`artifacts.store_file` (resumable, dedupe, `ArtifactManifest/v1`, retention `archive`). Account 2 owns only the daily-output
state machine and a *stricter* verification: a local final is deleted only after a positive checksum match (provider
sha256, provider md5 against the local file, or download + hash), re-checked at release time, on an external destination.
Missing public operations are requested in `ACCOUNT2_HANDOFF_TO_ACCOUNT1.md`, not patched into Account 1's files.

**D-16 No automatic deletion of archived videos.** Remote retention is not implemented and cannot be enabled from
settings; a future policy needs an owner-approved change. Same-disk "archives" never release the local copy.

**D-17 Cleanup is rule-based, file-level, plan-then-execute.** No folder is ever deleted wholesale; every candidate is a
file inside an allowlisted NEXUS_HOME/work directory matched by a named rule, re-validated just before deletion, and a
changed plan is refused. Protection beats rules (sources, referenced files, finals, active jobs, secrets, models).

**D-18 Asset library: generate first, download only pinned licence-clean files.** LUTs, overlays, sounds, ambiences,
beds and templates are generated locally (own work, CC0); downloads are limited to pinned OFL fonts (google/fonts @
3e56677, licence text kept) and one pinned CC0 pack. Anything whose licence or access cannot be pinned is listed under
`optional_future`. No new dependency (PyAV, ImageMagick, G'MIC, VapourSynth, Blender evaluated, not added).

**D-19 Extended transitions are FFmpeg custom xfade expressions.** One code path (`transitions.filter_for`) for both
render pipelines; runtime detection of `a0(x,y)` support (FFmpeg >= 6) with a fallback to the nearest built-in transition,
so a render never fails on an older FFmpeg.
