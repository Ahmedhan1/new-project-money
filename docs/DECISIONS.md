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

