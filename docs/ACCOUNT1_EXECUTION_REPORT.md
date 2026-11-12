# Account 1 execution report (2026-11-12)

## Recovery audit (start)
HEAD `c93847d` on `claude/hopeful-allen-luubcy` (PR #3, draft, CI green); `main` at `ec5af11`, untouched. Two older
session branches exist. Working tree clean. The new branch `work/account-1-cloud-platform` was created from `c93847d`
(the verified release candidate, not the older `main`). PR #4 targets PR #3's branch.

## Order of work
1. Lead: verified the Kaggle token with the official CLI (2.2.5) and ran a real GPU probe kernel **before** building on it
   (2× Tesla T4, output checksum matched). Stored the token in a new encrypted vault; scanned the whole Git history.
2. Lead: shared foundation so four parallel streams could not diverge: 14 contracts v1, vault, migration 2, event bus,
   owner auth, `/api/v1` router mounts.
3. Four sub-agents in separate git worktrees with exclusive file ownership: A security/architecture, B compute,
   C storage/ingest, D Studio/scheduler. Three of them stopped at the account's 5-hour usage limit and were resumed
   after the reset; all four delivered, and their work is committed.
4. Lead review and integration: merged each branch, re-ran every test, applied each sub-agent's requests for files they
   did not own, and checked behaviour against the running app (not only against reports).

## Defects found during integration (fixed, each with a test)
- CI: a backup test corrupted "the largest blob", which became the database once the cloud tables existed.
- Studio would have been unusable in token mode: no CSRF header on writes; `/auth` compared the cookie with the raw
  token after sessions became signed values.
- A fake token literal in a test tripped the secret scanner (now built at runtime).
- `GET /api/v1/compute/quota` returned fields outside `QuotaStatus/v1` (contract forbids extras): found by validating
  the live response against the published contract.
- 59 legacy `/api/*` routes had no authentication (relied on binding to 127.0.0.1): now behind the owner guard.

## Policy decisions
- One Kaggle account only. A second token supplied in chat was **not used** (Kaggle terms: one account per person;
  using it to continue after quota exhaustion would be quota evasion). Fallback is a different provider type (local CPU,
  owner GPU PC, owner's Colab) or waiting.
- Minimum Drive scope `drive.file`; an OAuth connection alone does not authorise uploads (`authorized_remote` rule).
- Platform page URLs (YouTube, TikTok, Instagram, Facebook, X, Vimeo) are refused at ingestion; the owner uploads or uses
  the platform's creator export instead.
- Zero spend: no billable resource created; Google Drive declared as a zero-cost component with its hosts.

## Measured
GPU: Tesla T4 ×2, 15 GB each, 23.9 TFLOPS fp16 / 4.1 fp32 matmul; NVENC 5.6× realtime at 720p; Kaggle round trip
~95 s for a tiny job (queue + boot dominate). GPU seconds used this session: about 290 of an assumed 108,000/week.

## Owner actions outstanding
Rotate both Kaggle tokens pasted in chat; Google OAuth client + Drive A/B/C consent; choose a host; back up the vault key.
