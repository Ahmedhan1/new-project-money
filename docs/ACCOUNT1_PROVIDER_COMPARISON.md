# Account 1 - storage, hosting and compute providers (zero-spend comparison)

Research date: **2026-11-12**. Every figure below cites the official page it came from and was read on that date.
Anything not confirmed on an official page is marked **unverified** - treat it as a hint, not a fact. Providers change
free tiers often (Oracle, Fly.io and Hugging Face all changed theirs in 2025-2026), so re-check before relying on one.
Rule for NEXUS (AGENTS.md): zero spend, no service that needs a payment method, never bypass a quota, never create
extra accounts to multiply a free allowance. Nothing in this document was activated or signed up for.

## 1. Storage

| Provider | Free allowance (official) | Card / billing needed? | Fit for NEXUS |
|---|---|---|---|
| **Google Drive** (per Google account) | 15 GB shared across Gmail, Drive and Photos [G1]. Over quota: Drive uploads stop; content may be deleted after 2 years over quota [G1]. | No | **Primary remote store.** Up to three owner accounts (drive_a/b/c), each a real person-owned account. |
| Drive API | Free. 1,000,000 quota units/min/project, 325,000/min/user; 750 GB upload/day/user; 5 TB max file; 403 `userRateLimitExceeded` / 429 -> truncated exponential backoff [G2]. Google says charges for exceeding request limits are **planned later in 2026**, with no charge below 400,000,000 quota units/project/day and 90 days' notice [G2]. | No | NEXUS uses a tiny fraction of that; costguard must be re-checked when Google publishes the billing terms. |
| Cloudflare R2 | 10 GB-month Standard storage, 1 M Class A, 10 M Class B ops/month; egress free [C1]. | Payment method to enable R2: **unverified** (third-party guides say a card is requested before the first bucket; the official pricing page does not say). | Not eligible until the card question is confirmed "no". |
| Backblaze B2 | First 10 GB free; egress free up to 3x average monthly storage; Class A/B/C free, first 2,500 Class D/day free [B1]. | **Unverified** (pricing page does not say). | Possible secondary; needs an owner check of the sign-up flow. |
| Supabase Storage | Free plan: 1 GB file storage, 50 MB max upload, 5 GB egress, 500 MB DB, 2 active projects, **paused after 1 week of inactivity** [S1]. | **Unverified** (sources disagree). | Too small for media; 50 MB upload cap rules out masters. |
| Firebase Cloud Storage | Spark (no-cost) plan lists Cloud Storage as "not applicable"; the 5 GB no-cost quota is on **Blaze** (pay-as-you-go) [F1]. | Blaze needs a billing account. | Not eligible. |
| Google Cloud Storage | 5 GB-month (US regions) [GC1]. | "A Google Cloud billing account is required to access the Free Tier" [GC1]. | Not eligible. |
| Local disk (NEXUS_HOME) | The volume's real free space (measured, `shutil.disk_usage`). | No | Default destination; always available. |

NEXUS behaviour (implemented in `nexus/cloud/storage/artifacts.py`): placement follows the OWNER's ordered list in
`storage_rules.json`, uses the provider-reported quota (`about.storageQuota`, source `provider_api`), keeps a reserve,
skips a provider that reports no limit (never assumes unlimited), and fails rather than spreading data over extra
accounts to get around a limit.

## 2. Hosting / control plane (FastAPI + SQLite + FFmpeg)

| Platform | Free allowance (official) | Card? | Persistence / sleep | Verdict for the control plane |
|---|---|---|---|---|
| Cloudflare Workers | 100,000 requests/day, **10 ms CPU/request**, 128 MB memory, 50 subrequests [C2]. | No | No local disk. | Not suitable for FastAPI+FFmpeg; possible tiny relay/webhook only. |
| Cloudflare Pages | 500 builds/month, 20,000 files, 25 MiB/file; Functions count against the Workers quota [C3]. | No | Static. | Static Studio UI only. |
| GitHub Pages | 1 GB site, 100 GB/month soft bandwidth, 10 builds/h; not for commercial SaaS / sensitive transactions [GH1]. | No | Static. | Static docs only; not for the authenticated Studio. |
| Render (free web service) | 750 instance-hours/month/workspace; **spins down after 15 min idle** (~1 min wake); **ephemeral filesystem, no persistent disk**; free Postgres expires after 30 days; "do not use for production" [R1]. | Unverified | SQLite would be lost on every spin-down. | Demo only; not a durable control plane. |
| Fly.io | "New organizations don't have a free tier"; trial $1 credit for 7 days; card required after trial [FL1]. | **Yes** | - | Not eligible. |
| Railway | One-time $5 trial (30 days), then $1 free credit/month; trial volumes deleted 30 days after credits expire [RW1]. | Unverified | - | $1/month is not enough for an always-on API; not recommended. |
| Koyeb | Official pricing page shows only a "Free 5h" Postgres (0.25 vCPU, 1 GB) [K1]; free web instance (512 MB, scale-to-zero) and card policy: **unverified**, sources conflict (one says a card hold is required since Feb 2026). | Unverified | - | Not recommended until verified. |
| Hugging Face Spaces | CPU Basic 2 vCPU / 16 GB / 50 GB **non-persistent** disk; sleeps when unused. **Gradio and Docker Spaces now require a paid plan** (PRO); free accounts get static Spaces and up to 2 ZeroGPU Gradio Spaces [HF1]. | Paid plan for Docker | Ephemeral. | Not eligible for a FastAPI Docker control plane any more. |
| Oracle Cloud Always Free | Docs historically: Ampere A1 = 4 OCPU / 24 GB; 2026 third-party reports say it was halved to 2 OCPU / 12 GB (1,500 OCPU-h and 9,000 GB-h per month) - figures **unverified** (oracle.com returned 403 to our fetch). Idle instances (95th-pct CPU < 20 % over 7 days) may be reclaimed [O1, docs page found but not fetched]. | Sign-up card: **unverified** | Persistent block volume. | Technically the best fit (real VM, disk, FFmpeg) **if** the owner confirms no charge is possible; needs the owner. |
| Google Cloud Run / e2-micro | 2 M requests/month; e2-micro 1 VM + 30 GB disk [GC1]. | Billing account required [GC1]. | - | Not eligible. |
| Owner's own PC / GitHub Codespaces | (current setup) | No | Persistent | **Current recommendation**: keep the control plane on the owner's machine/Codespace; use Drive for durable artifacts. |

## 3. Compute (GPU)

| Provider | Facts | Card? | Notes |
|---|---|---|---|
| Kaggle Notebooks | 12 h CPU/GPU sessions, 9 h TPU; ~30 GPU-h/week "floating" quota shown in account settings - **unverified** (kaggle.com/docs pages render client-side; our fetch got no numbers). Kaggle terms: one account per person - **unverified** here. | No | Already integrated by sub-agent B (`www.kaggle.com` declared). Read the live quota from the account, never assume. |
| Google Colab (free) | "At most 12 hours", idle timeouts, GPUs not guaranteed, limits fluctuate; disallowed: file hosting/media serving, web services, SSH/remote desktop on free runtimes, **multiple accounts to get around limits** [CO1]. | No | Interactive hand-off only; must not be used as a backend or media server. |
| Hugging Face ZeroGPU | Free personal accounts: up to 2 ZeroGPU Gradio Spaces [HF1]. | No | Possible for small demos; quotas not researched (unverified). |

## 4. Owner setup: Google OAuth client (free, no billing)

1. Open https://console.cloud.google.com/ and create a project. **Do not enable billing** (the Drive API does not need it [G2]).
2. APIs & Services -> Library -> enable **Google Drive API**.
3. Google Auth Platform (OAuth consent screen): user type **External**, app name "NEXUS", your support e-mail. Scopes:
   `https://www.googleapis.com/auth/drive.file`, `openid`, `email`. `drive.file` is **non-sensitive** (only files
   NEXUS creates or you open with it) so no verification/security assessment is needed; the full `drive` scope is
   restricted and would need one [G3]. Add your own Google accounts as **test users**.
   *Important*: while the app is in "Testing", refresh tokens for external users **expire after 7 days** [G4] - you
   would have to reconnect weekly. Switch the publishing status to "In production" (no review is needed for
   non-sensitive scopes; Google may show an "unverified app" screen that you accept for your own accounts). Other
   reasons a token stops working: you revoke it, 6 months unused, more than 100 refresh tokens per account per client [G4].
4. Clients -> Create client:
   * **"TVs and Limited Input devices"** -> device flow. Works from any browser/phone while NEXUS runs in a cloud
     container; the device flow officially allows `drive.file`, `drive.appdata`, `openid`, `email`, `profile` [G5].
   * **"Web application"** -> web flow; authorised redirect URI `https://<your-host>/api/v1/storage/oauth/callback`
     (`http://localhost:<port>/...` is accepted for local use) [G6].
5. Download the client JSON and on the NEXUS server run
   `python -m nexus.cloud.storage.setup_google path/to/client.json --delete-file`. It is stored encrypted in the vault
   (`google_oauth_client`), never in Git.
6. In the Studio (or `POST /api/v1/storage/connections/drive_a/device/start`) open the shown URL, enter the code with
   the Google account you want as drive_a; repeat for drive_b / drive_c with **different** accounts (the same account
   twice is refused). Then allow uploads in `PUT /api/v1/storage/rules` (`authorized_remote`).
7. Demo B (real evidence): `NEXUS_LIVE_DRIVE=1 NEXUS_LIVE_DRIVE_IDS=drive_a python -m pytest tests/live/test_drive_live.py`
   writes `docs/results/account1/storage_drive_live.json`.

**Required registry change before step 6** (`nexus/registry/components.yaml`, owned by the lead): a component
`google-drive` (monetary_cost_usd 0, payment_method_required false, account_required true, quotas "15 GB per account;
Drive API quota units, see developers.google.com/workspace/drive/api/guides/limits") with
`network_hosts: [accounts.google.com, oauth2.googleapis.com, www.googleapis.com]`. Until then every real Google call
is refused by costguard (the fake-Drive tests use loopback and are unaffected).

## 5. What is and is not evidence

* Real, produced here: live NASA ingestion (`docs/results/account1/ingest_live.json`), live SSRF refusals with the real
  resolver (`ingest_ssrf_live.json`), SIGKILL + resume of a 22.8 MB upload over real HTTP against the **local fake**
  Drive (`storage_resume.json`).
* **Not yet evidence: real Google Drive.** Consent needs the owner's OAuth client and browser (Demo B above).

## Sources (read 2026-11-12)

- [G1] https://support.google.com/googleone/answer/9312312
- [G2] https://developers.google.com/workspace/drive/api/guides/limits
- [G3] https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- [G4] https://developers.google.com/identity/protocols/oauth2 (refresh token expiration)
- [G5] https://developers.google.com/identity/protocols/oauth2/limited-input-device (allowed scopes)
- [G6] https://developers.google.com/identity/protocols/oauth2/web-server
- [G7] https://developers.google.com/workspace/drive/api/guides/manage-uploads (resumable protocol used by the adapter)
- [C1] https://developers.cloudflare.com/r2/pricing/
- [C2] https://developers.cloudflare.com/workers/platform/limits/
- [C3] https://developers.cloudflare.com/pages/platform/limits/
- [B1] https://www.backblaze.com/cloud-storage/pricing
- [S1] https://supabase.com/pricing
- [F1] https://firebase.google.com/pricing
- [GC1] https://docs.cloud.google.com/free/docs/free-cloud-features
- [GH1] https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits
- [R1] https://render.com/docs/free
- [FL1] https://docs.fly.io/about/pricing
- [RW1] https://docs.railway.com/reference/pricing/free-trial
- [K1] https://koyeb.com/pricing
- [HF1] https://huggingface.co/docs/hub/spaces-overview
- [O1] https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm (found via search; not fetched - unverified)
- [CO1] https://research.google.com/colaboratory/faq.html
