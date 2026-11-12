# Decisions

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
