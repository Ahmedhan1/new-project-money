# AGENTS.md — rules for every coding agent working on NEXUS VIDEO AI

## Start of every session
1. Read `NEXUS_HANDOFF.md`, then `NEXUS_STATE.json`.
2. `git status -sb && git log --oneline -5` — never assume the tree is clean.
3. `python -m nexus resume` — prints next tasks and production jobs that need attention.
4. Continue the first ready task in `nexus/registry/tasks.yaml`. **Do not restart, re-scaffold or re-plan the project.**

## Hard rules
- **Zero spend.** No paid API, subscription, paid hosting/GPU, or service that needs a payment method. Every tool, model,
  service and storage provider must be declared in `nexus/registry/components.yaml` / `storage.yaml` with its cost facts;
  `costguard` and `storage.eligibility` enforce this in code. Never bypass a quota or a provider restriction.
- **No destructive Git.** No force-push, no history rewrite, no deleting branches or other agents' work.
- **Never commit** secrets, `.env`, media, model weights, databases, caches or files > 2 MB. `nexus checkpoint` refuses them; do not work around it.
- **Never upload user media or project data** to any remote without the user's explicit authorisation (`"authorized": true` in `storage.json`), and never to public repositories.
- **Honest status.** `implemented` = code exists. `tested` = an automated test passes. `verified` = works in the integrated
  product, with evidence written in the task. A module existing is not a finished capability. Never call a video
  flawless; QA reports say what was checked.
- **Do not repeat expensive work**: no re-downloading models that exist, no re-running heavy tests whose results are current.

## How to work
- Loop: inspect → pick task → implement → test → measure → update task status + docs → `python -m nexus checkpoint` → next task.
- Fast tests (`python -m pytest tests/fast -q`, seconds, no models) must pass before every checkpoint. `python -m ruff check .` too.
- Heavy tests A–I (`tests/test_*.py`) need models and minutes of CPU: run only the ones your change touches; commit their `docs/results/*` evidence.
- New long-running work goes through the durable job ledger (`nexus/jobs.py`, handlers in `nexus/workflows.py`) as named stages — never as an in-memory closure.
- New storage/compute providers are adapters behind `nexus/storage` / `nexus/compute.py`; nothing else may import a provider SDK.
- If blocked by an account, quota or missing authorisation: mark the task `blocked` with `blocked_by`, and continue with the next ready task.
- Record non-obvious choices in `docs/DECISIONS.md`, defects in `docs/KNOWN_ISSUES.md`, milestones in `docs/PROGRESS.md`.

## End of every session (or when context is nearly full)
1. Update statuses/`next` in `nexus/registry/tasks.yaml`.
2. Update the hand-written part of `NEXUS_HANDOFF.md` if what works / what is broken changed (keep it < 1,500 words).
3. `python -m nexus checkpoint -m "<summary>"` — regenerates `NEXUS_STATE.json` + the handoff's auto block, scans, commits, pushes.
4. Confirm the push result that the command prints. If it could not push, say so in the final message.
