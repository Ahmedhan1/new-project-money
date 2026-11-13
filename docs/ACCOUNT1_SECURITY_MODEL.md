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

## 7. Open risks (honest)

1. **Legacy routes unauthenticated** (T28): `/api/*` and `/` rely on binding to `127.0.0.1`. Through a Codespaces
   forward or any proxy they are reachable by whoever can reach the port. Fix (lead, `api.py`): a middleware that calls
   `security.check(request)` for every path outside `/api/v1/health/live` - exact code in the sub-agent A report.
2. **`POST /api/v1/session` (routes_core)** compares the token itself without the rate limiter and stores the raw token
   in the cookie. Fix: delegate to `security.open_session(request, response, r.token)`.
3. Security headers are only active after the lead calls `security.install(app)` in `api.py`.
4. `vault.TOKEN_RE` does not know the `nxw_` worker-token prefix, so a leaked worker token is not redacted from free
   text. Add `nxw_[A-Za-z0-9_-]{40,}`.
5. No fuzzing of ffprobe/ffmpeg inputs; media parsing relies on timeouts and size caps. Keep FFmpeg updated.
6. Rate limiting is per process and in memory (restart resets it) and keyed on the socket address: behind a proxy all
   clients share one address, so an attacker can lock the owner out for 5 minutes (fails closed; acceptable).
7. Codespaces: whether its port forwarder always adds `X-Forwarded-*` headers is assumed, not measured. The rule is
   therefore: **with a forwarded port, always set `NEXUS_API_TOKEN`**.
8. The vault key sits next to the vault in `NEXUS_HOME` unless `NEXUS_VAULT_KEY` is injected; a host compromise
   exposes both. Acceptable for a single-owner app; hosted deployments must inject the key.
9. `ProductionJobManifest.params` is free-form; nothing stops a client from putting a token into it. The dispatcher
   should reject token-shaped values (`vault.TOKEN_RE`) on submit.

## 8. Procedures

### Kaggle credential: CLOSED — ACCEPTED AS-IS BY PROJECT OWNER (2026-11-13)
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
* Hosted: generate a key once (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
  keep it in the host's secret store as `NEXUS_VAULT_KEY` **and** in the owner's password manager.
* Local/Codespaces: copy `NEXUS_HOME/secrets/vault.key` into the password manager. Without it, a restored backup's
  secrets are unreadable (fail closed) and every connection must be re-authorised - nothing else is lost.
* Never commit it, never put it into the same backup archive as the vault unless that archive is encrypted with a
  different passphrase (`NEXUS_BACKUP_PASSPHRASE`).

### Codespaces port visibility
Keep the forwarded port **Private** (default). Never switch it to Public. Set `NEXUS_API_TOKEN` as a Codespaces secret.

### Incident
Revoke the affected credential at the provider first, then rotate the owner token, then read
`GET /api/v1/security/audit` and `/api/v1/events` for the time window.
