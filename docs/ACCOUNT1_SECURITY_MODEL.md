# Account 1 - security model and threat model

Scope: the Account 1 cloud layer (`nexus/cloud/`, `nexus/vault.py`, `/api/v1`), its remote workers and its storage
connections. Method: assets, actors, trust boundaries, then a STRIDE table in which every threat names the control
and the test that proves it. "Open" means no automated proof yet - those items are listed in section 7.

## 1. Assets

| Asset | Where | Impact if lost / leaked |
|---|---|---|
| Owner API token (`NEXUS_API_TOKEN`) | host secret store / env | full control of NEXUS |
| Vault key (`NEXUS_VAULT_KEY` or `NEXUS_HOME/secrets/vault.key`) | host | decrypts every stored credential; losing it = every connection must be re-authorised |
| Google OAuth refresh tokens (`drive_a`, `drive_b`, `drive_c`), OAuth client secret | vault | read/write of the owner's Drive files in scope |
| Kaggle API token (single account) | vault | GPU quota, kernels, datasets of the owner's Kaggle account |
| Per-worker tokens (`nxw_...`) | worker only (server keeps the SHA-256) | claim/report jobs of that worker |
| Owner media and project data | Drive, `NEXUS_HOME/projects`, worker scratch | privacy, rights, unpublished work |
| Job integrity (inputs, outputs, manifests) | SQLite, artifacts | wrong/malicious output published |
| Zero-spend guarantee | `costguard`, provider policies | money spent |

## 2. Actors

* **Owner** - the only legitimate human user.
* **Remote attacker on the internet** - reaches a forwarded/hosted port.
* **Malicious website** visited by the owner - can make the owner's browser send requests (CSRF, DNS rebinding).
* **Malicious media / URL** - a file or link the owner imports (crafted MP4, URL pointing at internal services).
* **Compromised or misbehaving worker** - a Colab notebook or GPU box with a worker token; a Kaggle kernel.
* **Other coding agents / CI** - can commit to the repository; may leak a secret into Git, a notebook or a log.
* **Provider (Google, Kaggle)** - trusted for its own service, not for anything else.

## 3. Trust boundaries

```
 [Browser / other site] --B1--> [Control plane (FastAPI, vault, SQLite)] --B3--> [Kaggle kernel / pull worker]
                                      |   ^                                  \--B4--> [Google Drive A | B | C]
 [Internet URL / uploaded media] --B2-+   +-- [Repository / CI / notebooks] (B5: what may leave the host)
```
B1 HTTP API (owner auth, CSRF, headers) - B2 untrusted input (SSRF, size, parsers) - B3 remote compute (worker
auth, private kernels, no long-lived tokens on workers) - B4 storage accounts (per-connection isolation, OAuth) -
B5 publication of anything (Git, notebooks, logs, errors, browser).

## 4. Controls implemented by sub-agent A (`nexus/cloud/security.py`, `audit.py`, `routes_security.py`)

* **Owner authentication** (`require_owner`): bearer token or signed session cookie, constant-time comparisons.
  Without `NEXUS_API_TOKEN` only *direct* loopback requests are accepted: a request with proxy headers
  (`X-Forwarded-*`, `Forwarded`, `X-Real-IP`, ...) or a non-loopback `Host` header is remote. This closes two holes of
  "loopback = trusted": port forwarding / tunnels (Codespaces connects from 127.0.0.1) and DNS rebinding.
* **Session cookie** `nexus_session`: `HttpOnly`, `SameSite=Strict`, `Secure` on HTTPS, 12 h, value
  `s1.<issued>.<HMAC-SHA256(token, issued)>` - the token itself never reaches the browser; rotating the token ends all
  sessions.
* **CSRF**: cookie-authenticated POST/PUT/PATCH/DELETE need `X-NEXUS-CSRF: 1` (custom header = CORS preflight, never
  granted) **and** a same-origin `Origin`/`Referer` (own host, the proxy's `X-Forwarded-Host`, or `NEXUS_PUBLIC_ORIGIN`).
  Loopback mode refuses state-changing requests with a foreign `Origin`.
* **Brute force**: 10 failures per client per 5 min -> 429 (also for the correct token while locked); the failure map is
  bounded (10,000 clients). Lockout is logged once per window.
* **Worker tokens**: `issue_worker_token` returns `nxw_` + 256 random bits once; the vault stores only its SHA-256
  (encrypted); `verify_worker_token` compares in constant time; `revoke_worker_token`; `require_worker` dependency
  (rate limited, audited). Sub-agent B's `compute/pull.py` uses the same scheme (hash only, constant time).
* **Security headers** (`security.install(app)`): CSP (`frame-ancestors 'none'`, `object-src 'none'`,
  `connect-src 'self'`, `base-uri 'none'`, `form-action 'self'`) for HTML, `default-src 'none'` for API responses,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, COOP/CORP same-origin,
  `Permissions-Policy`, `Cache-Control: no-store` on `/api/`, HSTS on HTTPS.
* **Audit log** (`security_audit`): auth failures, lockouts, sessions, worker tokens, connection connect/disconnect,
  OAuth, scans. Keys that look secret are replaced by `[removed]`, strings go through `vault.redact`, 90-day / 20,000-row
  retention.
* **Endpoints that never return secrets**: `/api/v1/security/secrets` (names + whitelisted metadata + `readable`
  flag), `/api/v1/security/scan` (file + token kind, no characters of the value), `/api/v1/health` (vault counts only).

## 5. STRIDE threat table

| # | STRIDE | Threat | Control | Proof (test) |
|---|---|---|---|---|
| T1 | S | Remote request without credentials | `require_owner`: remote needs the token | `test_cloud_security.py::test_remote_denied_without_token`, `test_bearer_ok_and_wrong` |
| T2 | S | Forwarded port (Codespaces, tunnel) arrives from 127.0.0.1 and is trusted as local | proxy headers => remote | `test_proxied_loopback_is_remote` |
| T3 | S | DNS rebinding: evil.example resolves to 127.0.0.1, browser calls the local API | `Host` must be a loopback name | `test_dns_rebinding_host_refused` |
| T4 | T/E | CSRF from a site the owner visits (cookie session or loopback) | `X-NEXUS-CSRF` + same-origin; SameSite=Strict | `test_cookie_session_and_csrf`, `test_loopback_cross_origin_post_refused`, `test_proxy_origin_via_forwarded_host` |
| T5 | S | Token brute force | per-client rate limit, 429, audit | `test_rate_limit_after_failures` |
| T6 | I | Token theft from the browser (XSS, JS storage) | HttpOnly signed cookie, token never in JS; CSP | `test_cookie_session_and_csrf`, `test_security_headers` |
| T7 | T | Clickjacking of the Studio | `frame-ancestors 'none'`, `X-Frame-Options: DENY` | `test_security_headers` |
| T8 | E | Unauthorised remote dispatch (anyone submits GPU jobs / reads results) | `/api/v1/compute/*` behind `require_owner`; pull endpoints behind per-worker tokens; worker sees only its own jobs | `test_worker_token_hashing` (A); B: `tests/fast/test_cloud_compute.py` |
| T9 | S/E | Stolen or leaked worker token | hash-only storage, constant time, per-worker revoke/rotate, audit | `test_worker_token_hashing` |
| T10 | I | Token leakage into logs, events, errors | `events.emit` redacts stored secrets + token patterns; audit strips secret keys; error texts redacted by providers | `test_recovery` (`redaction_in_events`), `test_audit_strips_secrets`; C/B redaction tests |
| T11 | I | Token leakage into Git / notebooks | `vault.scan_repo` on tracked files (CI), `nexus checkpoint` refuses secrets, worker bundles carry no long-lived tokens | `test_no_secrets.py::test_tracked_files_contain_no_tokens`, `test_scanner_positive_control` (finds a Kaggle token in a committed notebook and in history) |
| T12 | I | Credential files committed by mistake | `.gitignore` + tracked-file check | `test_no_credential_files_tracked`, `test_gitignore_core`; `test_gitignore_credentials` is **xfail until the lead adds `*.secret`, `kaggle.json`, `client_secret*.json`** |
| T13 | I | API returns secret values | secrets endpoint whitelists metadata, scan returns kinds only | `test_secrets_endpoint_never_returns_values`, `test_audit_and_scan_endpoints` |
| T14 | I | SSRF: imported URL targets 127.0.0.1, metadata service (169.254.169.254), private ranges, redirects to them, non-HTTP schemes | ingest resolves DNS and refuses private/loopback/link-local/reserved addresses at every redirect hop, http(s) only, pinned resolved IP | C: `tests/fast/test_cloud_storage.py` / ingest tests (open until C's tests land on the account branch) |
| T15 | T | Path traversal in storage paths, artifact names, upload names, worker results (`../`, absolute, drive letters, zip-slip) | `safe_rel` in storage, artifact names are base names, worker id regex, vault name regex | C: storage tests; A: `test_worker_token_hashing` (`../etc` refused) |
| T16 | D/E | Malicious media exploits ffprobe/ffmpeg or exhausts CPU/RAM | MIME allow-list, `max_bytes`, ffprobe with a timeout, size/duration caps, run as the app user (no root), workers on remote sandboxes (Kaggle) for heavy decode | C: ingest tests (open: no fuzzing; see section 7) |
| T17 | D | Unbounded downloads fill the disk | `MediaIngestionJob.max_bytes` (default 4 GiB) enforced while streaming, Content-Length not trusted, disk-free in health | C: ingest tests; A: `test_health_endpoint_shape` (disk) |
| T18 | I | Cross-account data access (Drive A credential used for Drive B) | one provider instance per `connection_id`, reads only its own vault entry; transfers bound to their connection (`resume` refuses a foreign transfer) | C: `test_cloud_storage.py` (connection isolation) |
| T19 | I | Accidental public publication (Kaggle kernel public, Drive file shared "anyone with link") | Kaggle kernels pushed with `is_private: true`; Drive uploads create no permissions; scope `drive.file` | B: kaggle metadata test; C: no `permissions.create` call (open: assert in a test) |
| T20 | E | Kaggle multi-account abuse (ToS) | exactly one Kaggle connection (`kaggle_primary`), quota tracked, no quota evasion | B: compute tests |
| T21 | S/T | OAuth: CSRF on callback, code interception, open redirect | random single-use `state` bound to the connection (10 min), PKCE S256, exact redirect-URI allow-list, device flow for headless | C: OAuth tests |
| T22 | I | Refresh token at rest | Fernet (AES-128-CBC + HMAC-SHA256) in the vault, dir 0700 / files 0600 | `test_recovery` (restart, wrong key) |
| T23 | I/R | Revocation: disconnect leaves a valid token at Google | disconnect revokes at `oauth2.googleapis.com/revoke`, deletes the vault entry, audit `connection.disconnect` | C: OAuth tests; A: `audit.EVENTS` |
| T24 | D | Vault key corrupted/lost | fail closed: `get` returns None, health shows `vault.ok=false` / `undecryptable`; restore from key backup | `test_recovery` (`corrupted_key_fails_closed`, `wrong_env_key_fails_closed`, `missing_key_fails_closed_and_backup_restores`) |
| T25 | D | Schema upgrade breaks existing installs | append-only migrations | `test_recovery` (`migration_from_v1`, `migration_from_v0`) |
| T26 | R | Owner cannot tell who did what | audit log + event history | `test_rate_limit_after_failures`, `test_audit_and_scan_endpoints` |
| T27 | I | Codespaces port made public | forwarded ports must stay **private** (GitHub login) **and** `NEXUS_API_TOKEN` set; health shows `auth.mode` | T1/T2 tests; owner procedure (section 8) |
| T28 | E | Legacy `/api/*` routes have no auth when exposed through a proxy | **open** - lead to guard them (see section 7) | none yet |
| T29 | T | Contract drift between accounts (silent data loss) | `extra="forbid"`, versioned literals, committed schemas | `test_contracts.py` |
| T30 | $ | Spending money | `costguard`, `billing_required` refused, free-tier quotas tracked | existing `costguard` tests |

## 6. OAuth requirements (Drive, sub-agent C) - checklist

`state` random, single-use, expiring, bound to the connection id - PKCE S256 - redirect URI exact match against a
configured list (no wildcard, no open redirect) - minimum scope (`drive.file`) - refresh token only in the vault -
access tokens in memory only - revoke on disconnect - device flow for headless hosts - identity (e-mail) shown to the
owner so the right account is connected to A/B/C - errors from Google redacted before logging.

## 7. Risk register (re-audited 2026-11-13 at the production-closure round)

### 7.1 Closed since the first audit (each with a regression test)
| # | Risk | Status | Evidence |
|---|---|---|---|
| 1 | Legacy `/api/*` and `/` relied on binding to 127.0.0.1 | closed: `_owner_guard` middleware in `api.py`; `/docs`, `/redoc`, `/openapi.json` now owner-only too, and not served in production | `test_api_docs_are_owner_only_in_development`, `test_every_v1_route_requires_auth` |
| 2 | `POST /api/v1/session` compared the token without the limiter and put the raw token in the cookie | closed: delegates to `security.open_session` (rate limited, HMAC-signed expiring value) | `test_cookie_session_and_csrf`, `test_session_cookie_attributes` |
| 3 | Security headers depended on `install(app)` | closed: installed in `api.py` | `test_security_headers` |
| 4 | `nxw_` worker tokens not redacted | closed: `nxw_[A-Za-z0-9_-]{40,}` is in `vault.TOKEN_RE` (`nexus/vault.py`) | code; redaction path covered by `test_audit_strips_secrets` |
| 6 | Rate limiting keyed on the socket address: behind a proxy one attacker locks out the owner | closed: the real client is taken from `X-Forwarded-For` **only** when the peer is in `NEXUS_TRUSTED_PROXIES`; spoofed headers from other peers are ignored. Per-process by design: single instance is enforced (`instance.py`) | `test_rate_limit_identity_behind_trusted_proxy`, `test_single_instance_lock_refuses_a_second_api_process` |
| 8 | Vault key next to the vault | closed for production: the `vault_key_offdisk` gate refuses to start with `NEXUS_HOME/secrets/vault.key`; the key is injected and kept offline (`vault-key export/verify`); backups never contain it | `test_production_gates_*`, `test_backup_carries_vault_ciphertext_never_the_key`, `test_offline_vault_key_export_and_verify` |
| 9 | A client could put a token into `ProductionJobManifest.params` (which travel to Kaggle kernels) | closed: `submit()` refuses token-shaped strings and any stored secret value, without echoing it | `test_job_manifest_with_a_credential_is_refused` |
| 10 | Duplicate `nexus_session` cookies: the decision depended on the server's cookie-parser order (Account 2's report against httpx 0.28) | closed: every presented value is checked; access only with a valid signature; a forged or planted sibling never grants access and cannot lock the owner out. `/studio/auth` uses the same rule | `test_duplicate_session_cookies_are_decided_by_signature_not_parser_order` |
| 11 | No request-size limit on JSON or the legacy upload; upload type unchecked | closed: 8 MiB JSON / 20 GiB multipart, also for chunked bodies (413); legacy upload allow-list (415), base-name only, private temp folder | `test_body_and_upload_limits` |
| 12 | A misconfigured hosted deployment silently ran in loopback/dev mode | closed: `NEXUS_ENV=production` refuses to start unless every gate passes; no token in production = 503 for every request | `test_production_startup_refuses_then_serves_without_docs` |

### 7.2 Still open (accepted, documented)
5. No fuzzing of ffprobe/ffmpeg inputs; media parsing relies on timeouts, size caps and the type allow-list. Keep FFmpeg
   updated on the host.
7. Codespaces: whether its port forwarder always adds `X-Forwarded-*` headers is assumed, not measured. Rule: **with a
   forwarded port, always set `NEXUS_API_TOKEN`** (production makes this mandatory).
13. Rate-limit counters are in memory: a restart resets them (fails open for one window of 10 attempts per client, a
    token of >= 32 random characters is not brute-forceable in that budget).
14. Kaggle: KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER (see §8). The residual exposure of the once-shared token is a fact,
    not an open engineering item.
15. The CSP keeps `'unsafe-inline'` for the self-contained single-file UI; mitigated by `connect-src 'self'`,
    `frame-ancestors 'none'`, no third-party script, and escaping of user text in the UI.

### 7.3 Account 2's cookie/CSRF report, explained
Account 2's audit (`ACCOUNT2_FINAL_RELEASE_AUDIT.md` §10, `ACCOUNT2_HANDOFF_TO_ACCOUNT1.md` §4) recorded
`test_cloud_security::test_cookie_session_and_csrf` failing with httpx 0.28 on an earlier Account 1 commit.
Reproduced on 2026-11-13 (httpx 0.28.1, Starlette 1.7.0): httpx keeps the server-set cookie (domain `testserver.local`)
next to a manually set one (no domain) and sends **both** (`nexus_session=GOOD; nexus_session=FORGED`). Which value
the server read depended on the cookie parser (this Starlette keeps the last one); with a parser keeping the first,
the old test saw the genuine cookie and got 200 where it expected 401. So: **an environment-dependent ambiguity in
the test, not an authentication bypass** - in every case the server only accepted a value with a valid HMAC, and the
client really held a valid session. It was fixed in the test in `2ac7f96` (jar cleared, one cookie per assertion;
Account 3's `RESULT.md` r2 confirms it passes), and is now also fixed in the server (§7.1 #10) with a deterministic
regression test that sends both orders explicitly. Current HEAD: the test passes on Linux and Windows CI.

## 8. Procedures

### KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER (2026-11-13)
Kaggle API tokens were once shared in a chat. The owner decided to keep the current Kaggle configuration unchanged and
closed the topic, so no rotation is requested. The residual exposure stays a fact: anyone who saw that chat could use
the token within the account's own Kaggle quota until the owner chooses to expire it. NEXUS's controls are unchanged:
the token lives only in the encrypted vault, is redacted from logs and events, never travels in a kernel, and the
repository and history scans are clean. If the owner ever reopens the topic, the procedure is: Kaggle -> Settings ->
API -> Expire API token, create one new token, and store it through the vault only.
Generic rotation: Google - remove NEXUS under Google Account -> Security -> Third-party access (revokes refresh
tokens), reconnect drive A/B/C; OAuth client secret - reset in Google Cloud console, re-enter; owner token - set a new
`NEXUS_API_TOKEN` and restart (all sessions end); worker tokens - `issue_worker_token(id)` again (old one stops working)
or revoke.

### Vault key backup
See `ACCOUNT1_DISASTER_RECOVERY.md` §2: `python -m nexus vault-key export --to <removable drive>` (never printed,
never next to the data or a backup destination), `vault-key verify --from-file`, fingerprint on the label; production
injects the key as `NEXUS_VAULT_KEY` and refuses to start with a key file in `NEXUS_HOME`. Backups carry the vault as
ciphertext and never the key.

### Codespaces port visibility
Keep the forwarded port **Private** (default). Never switch it to Public. Set `NEXUS_API_TOKEN` as a Codespaces secret.

### Incident
Revoke the affected credential at the provider first, then rotate the owner token, then read
`GET /api/v1/security/audit` and `/api/v1/events` for the time window.
