# YouTube — owner authorization and private validation (runbook)

Status: **ready for owner authorization. Not verified on a real account.** All YouTube code has run only against
recorded or mock responses. A mock run proves nothing about a real upload. Nothing here is done by an agent: OAuth
tokens are never requested or passed through chat.

## 1. Google OAuth client (owner, once, free)
1. In https://console.cloud.google.com, create or pick **your own** project. This API needs no billing account.
2. Under APIs & Services, enable **YouTube Data API v3** and **YouTube Analytics API**.
3. On the OAuth consent screen, choose External and add the channel's Google account as a test user.
4. Under Credentials, create an OAuth client ID with application type **Desktop app**.
   - Redirect: loopback `http://127.0.0.1:<random port>` with PKCE S256. Desktop clients accept any loopback port,
     and the out-of-band flow no longer exists.
5. Scopes requested (`youtube_oauth.SCOPES`):
   - `youtube.upload` (videos.insert, thumbnails.set);
   - `youtube.readonly` (videos.list; also required by reports.query);
   - `yt-analytics.readonly` (Analytics reports).

## 2. Authorize and store tokens (owner's machine)
```bash
python -m nexus.intelligence.publishing.youtube_oauth --label main
```
The command asks for the client ID and secret locally, opens the browser, receives the code on the loopback port and
exchanges it with PKCE. It stores `client_id`, `client_secret` and `refresh_token` in Account 1's vault under
`pub.youtube.main.*`. Without the vault, they go in the matching environment variables (`creds.env_name`). Access tokens
are refreshed per run (`youtube_oauth.refresh`) and never stored. Nothing is written to Git, the DB or a report.

## 3. Dry run (no network request)
```bash
python -m nexus.intelligence.publishing.owner_validation youtube --file <render.mp4> --qa <project>/reports/<name>.qa.json --provenance prov.json
```
It checks that the credentials are present (by name only), runs the release gate (QualityReport `PASSED_AUTOMATED_CHECKS`
for this exact file, checksum, disclosure, language) and the quota. Verdict: `dry_run_ready` or `blocked`, with the
owner actions listed.

## 4. One private upload (explicit owner authorization)
```bash
python -m nexus.intelligence.publishing.owner_validation youtube --file <render.mp4> --qa <...qa.json> --provenance prov.json --thumbnail thumb.jpg --i-authorize-a-private-upload
```
- `privacyStatus=private`, with `containsSyntheticMedia` taken from the disclosure gate. The manifest is never approved.
- Resumable upload. An interrupted run resumes from the saved session on the next run, without a second insert.
- `videos.list` confirms that the video exists, plus its upload and processing status, and **that privacy is still
  private**. Any other value gives `UPLOADED_BUT_NOT_PRIVATE`; delete the video in Studio.
- The thumbnail goes through `thumbnails.set`. Custom thumbnails need a verified channel, so a refusal is recorded,
  not treated as a failure.
- Quota ledger: about 100 videos.insert calls a day in their own bucket, thumbnails.set costs 50 units, videos.list
  costs 1 (capabilities.yaml).
- Report: `docs/results/account3/youtube_private_validation.json`, with `mock: false` and `authorized_by_owner: true`.
  Only this file with verdict `verified_private_upload` moves YouTube to "real-provider verified".

## 5. Analytics import (after the upload has views)
- API: `analytics.youtube_daily(video_id, start, end, account)` and `youtube_retention(...)`. Records are labelled
  `real_platform_data`.
- Export: YouTube Studio → Advanced mode → Export → CSV, then `POST /api/a3/analytics/import` or
  `analytics.import_csv(path, "youtube_studio", period=..., tz="<channel time zone>")`. Records are labelled
  `user_uploaded_data`. The column map is **not verified** against a real export; unmapped columns are reported.
- Reconcile the two sources with `analytics.reconcile_api_vs_export`. The API can lag the export by a few days.

## 5b. Analytics validation checklist (one owner export)
The owner provides the file; the agent never logs in. Each step reports its result and fixes nothing silently:
1. **Column mapping.** Unmapped columns are listed in the import result.
2. **Dates and time zones.** Dates are normalised only when unambiguous. Publish times convert to UTC only with the
   channel's stated `tz`; an unavailable zone leaves them unconverted.
3. **Totals.** Rows are reconciled against the export's Total row (`reconcile_total`).
4. **Duplicates and invalid values.** Both are reported, not fixed.
5. **Data origin.** Every record is labelled `user_uploaded_data`; API rows are `real_platform_data`. Demo and public
   rows (`public_benchmark_data`, `simulated_test_data`) are never counted as the owner's results, and simulated rows
   never conclude an experiment.

Status: **implemented and tested on fixtures; not run on a real export** (ANA-1).

## 6. What stays owner-only
Public or unlisted publishing needs:
- `approve()`;
- human QA approval in the QualityReport;
- the realism decision (`disclosure_decision`);
- native review where the language requires it.

None of these is ever set by an agent.
