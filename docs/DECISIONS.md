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
