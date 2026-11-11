# Storage — NEXUS Federated Storage System

## What lives where
| Data | Home | Why |
|---|---|---|
| Code, docs, tests, schemas, registries, project memory | **GitHub** | versioned, reviewable, small |
| Projects (timelines, reports, narration, originals, renders), database, settings | **`NEXUS_HOME`** on the local disk (default `./.nexus`) | large, private |
| Model weights | `NEXUS_HOME/models`, re-downloadable from the repos in `nexus/registry/models.yaml` | never backed up; only the manifest is |
| Backups | one or more **destinations** (below) | verified, versioned, optionally encrypted |
| Caches, render work files, logs | local only | reproducible — never backed up |

Git never holds media, weights, databases or backups (`.gitignore` + `nexus checkpoint` enforce it). Git LFS and GitHub
Releases are deliberately not used for backups (small shared allowance; Releases are for installers).

## The adapter
`nexus/storage/__init__.py` — everything else in NEXUS uses only `StorageBackend`:
`put · get · size/exists · sha256 · list · delete · usage · available`.

| Backend | Covers | Notes |
|---|---|---|
| `LocalBackend` | another disk, external drive, NAS share | atomic writes; default destination |
| `RcloneBackend` | Backblaze B2, any S3-compatible store, Google Drive, OneDrive, SFTP, WebDAV… (70+ rclone backends) | multipart upload, resume and retries come from rclone; credentials stay in rclone's own config, never in NEXUS or Git |

Adding a provider = a profile in `nexus/registry/storage.yaml` (cost facts) — no application code changes.

## Zero-spend rules (enforced in `storage.eligibility` / `open_destination` / `backup_to`)
1. A provider that **needs a payment method** or **can bill automatically** is refused. Unknown providers are refused until a verified profile exists.
2. NEXUS keeps each destination under **90 % of its free allowance** (or your lower `limit_gb`). Data that does not fit is skipped in reverse priority order and **listed in the result** — never billed, never silent.
3. A remote destination is used only with `"authorized": true` (your explicit consent to upload private data) **and** an encryption passphrase.

## Provider research (checked 2026-11-11 — re-verify in the provider's console before enabling)
| Provider | Free allowance | Card needed | Can bill | Verdict |
|---|---|---|---|---|
| Local / external drive / NAS | your disk | no | no | **Default.** A second physical disk is the best first backup |
| Backblaze B2 | 10 GB storage; free egress up to 3× stored/month | no (per Backblaze; a third-party tracker disagrees — check at sign-up) | not without a card | **Eligible** — best free S3-compatible option |
| Google Drive | 15 GB (shared with Gmail/Photos), hard quota | no | no | **Eligible** |
| OneDrive | 5 GB, hard quota | no | no | **Eligible** (metadata-sized backups) |
| Hugging Face Hub (private) | 100 GB private storage on free accounts | no | no | **Not for backups** — intended for models/datasets; fine for NEXUS-produced weights/datasets only |
| Cloudflare R2 | 10 GB-month, 1 M class-A / 10 M class-B ops, free egress | **yes** (billing details to activate) | **yes**, overage is billed automatically | **Blocked** by the zero-spend rule despite being the best technical fit |
| Git LFS / GitHub Releases | small, shared with bandwidth | no | no | **Blocked** as backup targets |

Unverified points are marked `verify:` in `storage.yaml`. Sources: Cloudflare R2 pricing docs, Backblaze pricing page and
"Free Isn't Always Free" blog post, Hugging Face Hub storage-limits docs, GitHub plan docs.

## Backup tool decision
rclone (transport) + NEXUS's own snapshot engine (`nexus/backup.py`). restic and Kopia were considered: both are excellent
(encryption, dedup, snapshots) but add a second binary to install on Windows and their repositories are opaque to NEXUS —
it could not tier data by priority, enforce a free-allowance cap per object, or rewrite project paths on restore. The
built-in engine is ~400 lines, has no extra dependency beyond `cryptography`, and is covered by tests. See `DECISIONS.md` D-3.

## Configure destinations — `NEXUS_HOME/storage.json` (never committed)
```json
{"destinations": [
  {"name": "local",    "provider": "local", "root": "C:/NEXUS/.nexus/backups", "enabled": true},
  {"name": "usb",      "provider": "local", "root": "E:/nexus-backup", "enabled": true},
  {"name": "b2",       "provider": "backblaze-b2", "remote": "nexusb2:my-bucket/nexus", "authorized": true,
   "limit_gb": 8, "tiers": ["state", "metadata", "production"], "enabled": true}
]}
```
Without this file there is one destination: `NEXUS_HOME/backups` — same disk, so it protects against mistakes, **not**
against losing the disk. `nexus status` and the UI say so.

### Add a remote destination (user action — an agent must never create accounts)
1. Create the free account **without adding a payment method**. For B2: create a private bucket and an application key limited to it.
2. `rclone config` → new remote (e.g. `nexusb2`). Credentials are stored by rclone, outside the repository.
3. Add the destination to `storage.json` with `"authorized": true`.
4. Set `NEXUS_BACKUP_PASSPHRASE` (and store a copy somewhere safe — without it nobody can restore).
5. `python -m nexus storage` → destination shows `OK`; `python -m nexus backup --to b2 --drill` → must end with `restore drill … verified`.

## Backup tiers (priority order)
`state` (database snapshot, settings, model manifest) → `metadata` (timelines, QA reports, scripts) → `production`
(narration audio) → `assets` (licensed images + licence records) → `originals` (your uploaded media) → `exports`
(final renders; off by default — reproducible from the above).
