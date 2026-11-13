# Account 3 / E - Platform capabilities, publishing engine and analytics import

Owner: Account 3 agent E (`work/account-3-e-publishing`). Package: `nexus/intelligence/publishing/`.
All API facts below were read on the cited first-party page on **2026-11-12**. Media specs (aspect, resolution, fps,
duration, size, text limits, thumbnails, captions, safe zones) are **not duplicated**: they live in
`nexus/registry/platforms.yaml` (fields carry `kind` documented/observed/experimental/unverified + source + date), and
`capabilities.matrix()` merges them with the API facts of `nexus/intelligence/publishing/capabilities.yaml`.

**No real upload has been made by this work.** The YouTube adapter is implemented against the documented protocol and
tested with recorded responses; it has not been run against a live channel (no owner credentials in this environment).

## 1. Capability matrix

### Media / text limits (from platforms.yaml; `doc` = documented, `obs` = third-party observed, `ed` = editorial, `-` = unverified)

| Destination | Aspect | Max res | Max duration | Max file | Title | Description / caption | Hashtags | Thumbnail | Safe zone |
|---|---|---|---|---|---|---|---|---|---|
| YouTube | 16:9 (any accepted) doc | 7680x4320 doc | 12 h doc (15 min default for unverified accounts) | 256 GB doc | 100 chars doc | 5000 **bytes** doc | 60 max (all ignored above) doc; tags list 500 chars doc | 1280+ wide 16:9 JPG/PNG <=2 MB, verified account doc | - |
| YouTube Shorts | <=1:1 (vertical/square) doc | 1080x1920 doc | 180 s doc | 256 GB doc | 100 doc | 5000 bytes doc | 60 doc | 9:16, Studio desktop, verified doc | top 15% / bottom 35% obs |
| TikTok | 9:16 | see yaml | 600 s via API doc | 4 GB doc | none (caption is the text) | 2200 UTF-16 doc | - | frame chosen in app - | top 15% / bottom 30% obs |
| Instagram Reels | 9:16 | see yaml | 900 s via API doc | 300 MB doc | none | 2200 doc | 30 doc (20 @mentions) | 9:16 JPG <=8 MB doc | top 14% / bottom 35% obs |
| Instagram Feed | 4:5 | see yaml | 900 s doc | 300 MB doc | none | 2200 doc | 30 doc | JPG <=8 MB, 1:1 grid crop doc | centre 1:1 ed |
| Facebook (Page video) | 16:9 | see yaml | 240 min obs | 10 GB obs | 255 ed | 5000 ed | - | - | - |
| Snapchat Spotlight | 9:16 | see yaml | 60 s obs | - | none | 160 ed | 3 ed | - | ed |

Captions: YouTube accepts SRT/VTT/SBV/TTML/... (doc); other destinations get burned-in captions + an SRT sidecar.

### API reality (capabilities.yaml)

| Destination | Upload endpoint | Scopes / access | Review / audit | Unaudited-app visibility | Quota | Analytics |
|---|---|---|---|---|---|---|
| YouTube + Shorts | resumable `POST googleapis.com/upload/youtube/v3/videos` [1][3] | `youtube.upload` [1]; OAuth installed-app, loopback + PKCE, OOB no longer supported [6] | none for the owner's own Cloud project; quota extension form for more [2] | "not restricted to private viewing mode" [1] - NEXUS still uploads private | **100 videos.insert/day, 1 unit each, own bucket**; 10,000 units/day other; thumbnails.set 50; videos.list 1 [2] | YouTube Analytics API `GET youtubeanalytics.googleapis.com/v2/reports` [5]: views, estimatedMinutesWatched, averageViewDuration, averageViewPercentage, audienceWatchRatio (elapsedVideoTimeRatio, 1 video/request), subscribersGained ... [4]. **Impressions / impressions CTR: not on the metrics page** -> Studio CSV export |
| TikTok | `POST open.tiktokapis.com/v2/post/publish/video/init/` [7] | `video.publish` + Content Posting API product [7] | audit required [7] | **private only until audited** [7] | no cap stated [7] | Display API video object: view/like/comment/share counts only [9]; retention via TikTok Studio export |
| Instagram Reels / Feed | `POST /{ig-user-id}/media` (REELS) -> rupload -> status_code poll -> `media_publish` [10] | `instagram_business_content_publish` (IG Login) or `instagram_content_publish`+`pages_read_engagement` (FB Login); professional account; no PPA pending [10] | Advanced/Standard access (review process not on page) [10] | **no draft/private mode**: publish is public | 100 posts / 24 h (carousel section says 50) [10] | media insights incl. views, reach, ig_reels_avg_watch_time, reels_skip_rate [11] (business/creator account) |
| Facebook | `POST /{page-id}/videos`; reels `POST /{page-id}/video_reels` [12][13] | Page token: pages_show_list, pages_read_engagement, pages_manage_posts [12] | Meta app review | Pages only; API reels are public [13] | 30 API reels / Page / 24 h [13] | `GET /{video-id}/video_insights`: total_video_views, total_video_avg_time_watched, total_video_retention_graph; Page videos only, 2 years [14] |
| Snapchat Spotlight | **none public** (Public Profile API is "allowlist only", scope `snapchat-profile-api`, docs show reads) [16] | - | allowlist via Snap contact [16] | - | - | Marketing API = ads only [15]; organic: in-app Insights -> manual |

Sources: [1] https://developers.google.com/youtube/v3/docs/videos/insert · [2] https://developers.google.com/youtube/v3/determine_quota_cost ·
[3] https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol · [4] https://developers.google.com/youtube/analytics/metrics ·
[5] https://developers.google.com/youtube/analytics/reference/reports/query · [6] https://developers.google.com/identity/protocols/oauth2/native-app ·
[7] https://developers.tiktok.com/doc/content-posting-api-get-started · [8] https://developers.tiktok.com/doc/content-posting-api-reference-direct-post ·
[9] https://developers.tiktok.com/doc/tiktok-api-v2-video-object · [10] https://developers.facebook.com/docs/instagram-platform/content-publishing ·
[11] https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights · [12] https://developers.facebook.com/docs/video-api/guides/publishing ·
[13] https://developers.facebook.com/docs/video-api/guides/reels-publishing · [14] https://developers.facebook.com/docs/graph-api/reference/video/video_insights/ ·
[15] https://developers.snap.com/api/marketing-api/Ads-API/introduction · [16] https://developers.snap.com/api/marketing-api/Public-Profile-API/GetStarted ·
[17] https://help.snapchat.com/hc/en-us/articles/7012293789972-How-do-I-submit-a-Snap-to-Spotlight-from-the-web. All read 2026-11-12.

Note on the brief's "videos.insert = 1600 units of 10,000/day": the current pages [1][2] say otherwise (1 unit per call
in a separate Video Uploads bucket, 100 calls/day). The quota ledger in this package uses the current figures and
records them as configuration (`QUOTA` in `adapters/youtube.py`), so a change is a one-line edit.

[6] does not mention the 7-day refresh-token expiry for OAuth consent screens in "Testing" status; that behaviour is
**unverified** here, but owners should publish their consent screen (or expect to reconnect) if tokens stop working.

## 2. Publishing engine (`engine.py`)

- `connect_account(platform, label, external_id, secret_ref, scopes)` -> `pub_accounts` row. `secret_ref` is a vault
  name (`pub.<platform>.<label>.<kind>`); the engine refuses anything that looks like a token.
- `create(project_id, artifact, platform, metadata, ...)` -> `PublishingManifest` (contract v1). Visibility defaults to
  the platform's safest mode (YouTube private; others draft). Asking for `public`/`unlisted` without approval keeps the
  manifest `private` and status `awaiting_approval` (the request is recorded).
- Idempotency key = sha256(artifact sha256 or uri+bytes, platform, connection, canonical metadata). Same key -> the
  existing manifest is returned (`deduplicated: true`); no second upload ever starts.
- `approve(id, by)` sets `approval='approved_by_owner'` (the only value that unlocks public/unlisted, and the only way
  Instagram/Facebook adapters - which cannot upload privately - will publish).
- `enqueue(id, schedule=None)`, `process(now)` (due jobs only), status tracking (`draft, awaiting_approval, approved,
  queued, uploading, uploaded, failed, cancelled`), retries with exponential backoff (30 s * 2^n, cap 1 h, 5 attempts,
  honours Retry-After), owner-action failures (`blocked`) are never retried automatically, full `history` per manifest.
- Durable recovery: `@jobs.handler("publish-upload")` registered in `nexus/intelligence/publishing/jobs_handler.py`.
  YouTube resumable session URIs are saved in the manifest, so a recovered job resumes the same upload session.

### Release gate and disclosure (2026-11-13)
`enqueue()` and `run_one()` refuse any upload unless `release_gate.evaluate()` passes:
- Account 2's QualityReport for this exact output (`PASSED_AUTOMATED_CHECKS`, no critical finding, checksum OK);
- a decidable AI-content disclosure;
- the language state.

Public or unlisted also needs human QA approval, native review for the same variety and, on YouTube, the owner's
realism decision. Adapters send the disclosure field: YouTube `status.containsSyntheticMedia`, TikTok `post_info.is_aigc`,
Instagram `is_ai_generated`. Facebook /videos cannot carry one and refuses AI content. Details:
`docs/ACCOUNT3_PUBLISHING_RELEASE_GATE.md`.

## 3. Adapters

| Adapter | Status | What it does |
|---|---|---|
| `youtube` | implemented, fixture-tested, **not live-tested** | OAuth installed-app helper (loopback + PKCE; owner's own client id/secret), resumable videos.insert with `privacyStatus=private` unless approved, thumbnails.set, status polling (videos.list processingDetails), quota ledger. No credentials -> `AdapterBlocked("credentials_missing")` with exact owner steps |
| `tiktok` | requires approved app | documented init -> PUT -> status flow behind a gate; refuses unless the owner states the app is approved (`app_approved`) and a token exists; unaudited => SELF_ONLY |
| `meta_instagram`, `meta_facebook` | requires approved app | documented container/publish flow behind the same gate; never publishes without owner approval because there is no private mode |
| `snapchat_manual` | manual export only | writes nothing remote; returns the manual-upload package path |

## 4. Packaging (`packaging.py`)
Per platform title / description / hashtags / CTA / thumbnail text / caption placement from the matrix limits. Text
comes from the model router (`title_generation`, privacy `internal`) when a provider is available, with a
deterministic fallback (`nexus.growth.hashtags` + transcript sentences) otherwise; every result is checked against
the limits (YouTube description in UTF-8 bytes, TikTok caption in UTF-16 units, `<`/`>` stripped for YouTube).

## 5. Analytics (`analytics.py`)
`AnalyticsRecord` (contract v1) from: YouTube Analytics API reports (adapter, owner OAuth) and CSV exports from YouTube
Studio, TikTok Studio, Instagram and Meta Business Suite (column maps per platform; unknown columns kept raw, missing
values left absent - nothing is filled in). Upsert key = (platform, video_id, date, source).

## 6. Status and evidence (honest)

| Piece | Status | Evidence |
|---|---|---|
| Capability matrix (7 destinations) | implemented, tested | `nexus/intelligence/publishing/capabilities.yaml`, `docs/results/account3/publishing_capability_matrix.json` |
| Engine: approval gate, idempotency, queue, schedule, backoff, history, durable job kind | implemented, tested (fast) | `tests/fast/test_intel_publishing.py` |
| YouTube adapter + OAuth helper | implemented, tested against recorded responses; **not verified live** (no owner credentials) | resumable + resume-after-503 tests; demo stops at `credentials_missing` |
| TikTok / Instagram / Facebook adapters | request flow written, **gated, never run live** | demo stops at `requires_approved_app` |
| Snapchat | manual package only | demo stops at `manual_only` |
| Packaging | tested; one real router call (local Qwen3-4B, privacy internal) in the demo, deterministic elsewhere | `publishing_demo.json` -> `youtube_shorts.text_source` |
| Analytics import | YouTube API parsing tested on documented response shapes; CSV maps **not verified against real exports** (`map_verified: false`); every record labelled `data_origin` (real_platform_data / user_uploaded_data / public_benchmark_data / simulated_test_data); dates normalised, publish times to UTC only with a stated zone, duplicates / invalid values reported, Total-row and API-vs-export reconciliation | `tests/fast/test_intel_analytics_quality.py`, `tests/fast/fixtures_publishing/` (synthetic, labelled) |
| Release gate (QualityReport, disclosure, language) | tested; Account 2's validator used on the merged tree | `tests/fast/test_intel_release_gate.py` |
| Owner private YouTube validation | ready for owner authorization; mock-tested only | `tests/fast/test_intel_owner_validation.py`, `docs/ACCOUNT3_YOUTUBE_OWNER_VALIDATION.md` |
| Provider readiness | no platform is real-provider verified | `capabilities.readiness()`, `GET /api/a3/platforms/readiness` |

Demo (`NEXUS_HOME=... python scripts/account3/publishing_demo.py`, ~4-5 min, mostly the one local LLM call under
`jobs.heavy_lock()`): no render existed under `.nexus/projects`, so 6 s ffmpeg test clips (16:9, 9:16, 4:5) were used;
7 private/draft manifests + packages (cover.jpg, caption.txt, metadata.json, MANUAL_UPLOAD.md under
`NEXUS_HOME/a3_data/publishing_demo/packages/`); every adapter stopped before any network call with an owner-action
message; a second run deduplicated the identical YouTube Shorts manifest. **No real upload happened.** That demo predates the
release gate; re-run today, it stops earlier with `release gate: no QualityReport attached` because the test clips
have no Account 2 QA.

Credentials without the vault: `NEXUS_PUB_YOUTUBE_<LABEL>_CLIENT_ID`, `..._CLIENT_SECRET`, `..._REFRESH_TOKEN`.

## Integration requests (for the lead)
1. **components.yaml**: declare a zero-cost component `youtube-data-api` with `network_hosts: [www.googleapis.com,
   youtubeanalytics.googleapis.com, oauth2.googleapis.com, accounts.google.com]` (and later `open.tiktokapis.com`,
   `graph.facebook.com`, `graph.instagram.com`, `rupload.facebook.com` once apps are approved). Until then the
   transport raises `AdapterBlocked("host_not_declared")` - cost guard enforced.
2. **workflows.py**: `import nexus.intelligence.publishing.jobs_handler  # noqa: F401` so workers know `publish-upload`.
   `jobs.run_job` holds `heavy_lock()` for every job; uploads are network-bound - consider exempting `publish-upload`.
3. **platforms.yaml corrections** (`capabilities.registry_diff()` lists them): YouTube analytics metrics verified +
   impressions/CTR not in API metrics page; YouTube thumbnails.set 50 units + scope; Facebook `video_insights` exists;
   Snapchat Public Profile API (allowlist) / Marketing API ads-only; Instagram resumable upload only with Facebook Login
   for Business, PPA blocks publishing, containers expire after 24 h.
4. **Account 1**: vault names cannot contain `/` (`NAME_RE`), so the router's `vault.get(f"ai/{pid}")` always misses;
   this package uses `pub.<platform>.<account>.<kind>`. An OAuth "connect account" UI/route belongs to Account 1 and can
   call `publishing.youtube_oauth.*`.
5. **api.py / UI**: routes for manifests (create / approve / enqueue / status), CSV analytics upload.
