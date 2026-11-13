# Account 1 - production deployment profile and runbook

Status: **engineering ready; not deployed.** Nothing here is exposed publicly, and no host has been bought or chosen.
The owner decides on the host, the network exposure and the release. No paid service is needed.

## 1. Two profiles
| | Development (default) | Production (`NEXUS_ENV=production`) |
|---|---|---|
| Access | Direct loopback clients without a token; remote clients need `NEXUS_API_TOKEN` | Every request needs the owner token (Bearer) or a signed session cookie. Loopback mode is off: no token returns 503. |
| Startup | Starts with any configuration | **Refuses to start** unless every required gate passes (§3) |
| Vault key | Generated into `NEXUS_HOME/secrets/vault.key` | Injected as `NEXUS_VAULT_KEY`. A key file inside `NEXUS_HOME` blocks startup. |
| API docs | `/docs`, `/redoc`, `/openapi.json` (owner-only) | Not served (404) |
| Processes | API + worker (`nexus start`) or embedded worker | Exactly one API process per `NEXUS_HOME` (OS lock); uvicorn `workers=1` |
| Logs | `warning` | `info` (access log off); size-rotated service logs |

Code: `nexus/cloud/production.py` (gates), `nexus/cloud/instance.py` (lock), `nexus/cloud/lifecycle.py` (start/stop),
`nexus/cloud/limits.py` (request limits), `nexus/__main__.py serve` (uvicorn settings).

## 2. Configuration (set in the host's secret store or service file, never in Git)
| Variable | Secret | Required in production | Purpose |
|---|---|---|---|
| `NEXUS_ENV=production` | no | yes | enables the profile |
| `NEXUS_API_TOKEN` | **yes** | yes (at least 32 characters) | owner token; rotating it ends every session |
| `NEXUS_VAULT_KEY` | **yes** | yes | Fernet key that decrypts the vault (§DR) |
| `NEXUS_PUBLIC_ORIGIN` | no | yes, `https://` only | the origin the owner opens; used by the CSRF Origin check |
| `NEXUS_TRUSTED_PROXIES` | no | yes | the reverse proxy's address as the API sees it (usually `127.0.0.1`); used to rate-limit per real client |
| `NEXUS_BACKUP_PASSPHRASE` | **yes** | yes, when a remote backup destination exists | AES-256-GCM backup encryption |
| `NEXUS_HOME` | no | recommended | data folder: database, vault ciphertext, projects |
| `NEXUS_MAX_BODY_BYTES` / `NEXUS_MAX_UPLOAD_BYTES` | no | optional | request limits (defaults 8 MiB JSON / 20 GiB multipart) |
| `NEXUS_PORT` | no | optional | default 8765 |

Generate the owner token **on the host** and put it straight into the secret store, for example:
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. Never paste it into chat, Git, a ticket or a notebook.

## 3. Startup gates (`python -m nexus production-check`)
`owner_token`, `vault_key`, `vault_key_offdisk`, `vault_readable`, `public_origin`, `bind_loopback`,
`trusted_proxies`, `database`, `disk`, `backup_offsite`, `backup_encryption`, `single_instance`.
Each gate reports `ok`, a detail with no secret value, and a fix. When any gate fails, `import nexus.api` raises
`ProductionConfigError`, so the service does not start. A misconfigured production service therefore never serves
in development mode.

Verified by `tests/fast/test_production.py`:
- `test_production_gates_fail_closed_and_name_fixes_without_values`
- `test_production_gates_pass_with_complete_configuration`
- `test_production_startup_refuses_then_serves_without_docs`, which uses a real subprocess.

## 4. Recommended zero-cost target (owner decision pending)
**One machine the owner already controls** (the existing Windows PC, or a Linux mini-PC/NAS), always on, with:
- NEXUS bound to `127.0.0.1:8765`;
- a local reverse proxy that terminates TLS. Two options:
  - **Tailscale Serve**: tailnet only, private. Preferred.
  - **Tailscale Funnel**: public internet. Only with the owner's explicit approval.
  
  Both are free on the Personal plan; see `ACCOUNT1_PROVIDER_COMPARISON.md` §2.1.

A paid VPS or PaaS is not needed and is not proposed. Kaggle stays a compute worker only (KAGGLE: CLOSED — ACCEPTED
AS-IS BY OWNER). It never hosts the control plane.

### 4.1 Linux (systemd), behind Tailscale Serve
1. Create a service user and data folder:
   - `sudo useradd -r -m -d /srv/nexus nexus`
   - clone the repository to `/srv/nexus/app`
   - `python3 -m venv /srv/nexus/venv && /srv/nexus/venv/bin/pip install -r /srv/nexus/app/requirements.txt`
2. Secrets file `/etc/nexus/nexus.env`, owned by root, mode `0640`, group `nexus`:
   ```
   NEXUS_ENV=production
   NEXUS_HOME=/srv/nexus/data
   NEXUS_PUBLIC_ORIGIN=https://<machine>.<tailnet>.ts.net
   NEXUS_TRUSTED_PROXIES=127.0.0.1
   NEXUS_API_TOKEN=<generated on this host>
   NEXUS_VAULT_KEY=<from the offline copy, see ACCOUNT1_DISASTER_RECOVERY.md>
   NEXUS_BACKUP_PASSPHRASE=<generated on this host, also kept offline>
   ```
3. `/etc/systemd/system/nexus.service`:
   ```
   [Unit]
   Description=NEXUS control plane (single instance)
   After=network-online.target
   [Service]
   User=nexus
   EnvironmentFile=/etc/nexus/nexus.env
   WorkingDirectory=/srv/nexus/app
   ExecStartPre=/srv/nexus/venv/bin/python -m nexus production-check --host 127.0.0.1
   ExecStart=/srv/nexus/venv/bin/python -m nexus serve --host 127.0.0.1 --port 8765
   Restart=on-failure
   RestartSec=5
   KillSignal=SIGTERM
   TimeoutStopSec=45
   NoNewPrivileges=true
   PrivateTmp=true
   ProtectSystem=full
   [Install]
   WantedBy=multi-user.target
   ```
   - Do not run a second unit or a second `serve` on the same `NEXUS_HOME`: the lock refuses it.
   - Logs go to journald, which rotates them (`journalctl -u nexus`).
4. Start the service:
   - `sudo systemctl daemon-reload && sudo systemctl enable --now nexus`
   - `curl -fsS http://127.0.0.1:8765/api/v1/health/ready` should return `{"ready":true}`.
5. Expose it privately with `tailscale serve --bg --https=443 http://127.0.0.1:8765`.
   - The device must be signed in to the owner's tailnet.
   - Public Funnel only after explicit owner approval.
6. Scheduled backup with a systemd timer or cron:
   `python -m nexus backup && python -m nexus verify` every `backup_every_hours` (default 24).
   A failed or overdue backup raises an alert (§7).

### 4.2 Windows (owner PC)
- `NEXUS_SETUP.bat`, then put the variables from §2 into **System Properties → Environment Variables (user)**.
  Never put them in a file inside the repository.
- Task Scheduler: "At log on" → `NEXUS_START.bat --no-browser`.
  - The launcher runs the preflight, recovers jobs, starts the worker and the API, and checks health.
  - It rotates `NEXUS_HOME\logs\*.log` above 20 MiB, keeping 5 generations.
- `NEXUS_STOP.bat` stops gracefully: terminate, wait 10 s, kill. Durable jobs resume from their checkpoint.
- Expose it with Tailscale Serve, as in §4.1 step 5.

### 4.3 Reverse-proxy requirements, for any proxy
- Terminate TLS. Forward `Host`, `X-Forwarded-For`, `X-Forwarded-Proto: https` and `X-Forwarded-Host`.
- Forward only to `127.0.0.1:8765`.
- Request body limit at least `NEXUS_MAX_UPLOAD_BYTES`, or lower if you want a stricter edge limit.
- Do not add CORS headers. NEXUS never grants cross-origin requests.
- Health check: `GET /api/v1/health/ready` returns 200 when ready, 503 when not. `GET /api/v1/health/live` checks liveness.

## 5. Single instance (supported model)
Several parts of the control plane exist once per process:
- the brute-force counters;
- the remote-job poller;
- the alert loop and scheduler thread;
- SQLite as the single state store.

Running two API processes on one `NEXUS_HOME` would double the attempts an attacker gets and poll Kaggle twice.
So:
- production takes an OS lock on `NEXUS_HOME/run/api.lock`. The kernel releases it when the process dies, so it can never go stale.
- `serve` always runs uvicorn with one worker.
- the separate `nexus worker` process (durable job ledger) is supported. It claims jobs atomically in SQLite.

Horizontal scaling (several API replicas, a shared rate-limit store) is **not supported** and not needed for one owner.
Tested by `test_single_instance_lock_refuses_a_second_api_process`, using a real second process.

## 6. Lifecycle, restart and recovery
| Event | What happens | Evidence |
|---|---|---|
| Start | gates → lock → lifespan `service.start` audit → durable jobs recovered (`jobs.recover`) → remote jobs adopted by id (no second Kaggle push) → transfers marked interrupted/resumable | `test_lifecycle_*`, `test_control_plane_restart_adopts_the_running_remote_job_without_a_second_push` |
| SIGTERM / SIGINT | uvicorn stops accepting; in-flight requests get up to 30 s; lifespan exit stops poller, alert loop and scheduler ticks; `service.stop` audited; marker removed; lock released | `test_lifecycle_audits_start_stop_and_detects_unclean_restart` |
| Crash, kill -9, power loss | next start finds `run/api.running` → `service.unclean_restart` audit + owner notification; jobs resume from the last checkpoint | same test; `test_worker_killed_mid_job_resumes_without_repeating_stages` |
| Database | schema migrations run on first connection (`db.schema_version`, migration 2 tested on a pre-cloud database) | `test_recovery` |
| Scheduled work | scheduler state is persisted; a restart re-plans without duplicating | `test_restart_persistence`, `test_stop_cancels_safely_and_restart_replans` |

## 7. Observability: what the operator sees
Where to look:
- **Studio → Alerts / Notifications**
- `GET /api/v1/alerts?check=true`
- `GET /api/v1/security/audit`
- `GET /api/v1/health` (owner)

The alert loop runs every 5 minutes, raises each condition once per 6 hours, and clears it when the condition resolves.
Alert text passes through `vault.redact`.

| Condition | Signal | Action in the alert |
|---|---|---|
| Failed jobs (≥3 of one task in 24 h) | alert `fails:<task>`, event `job.failed` | read the job log before scheduling more |
| Worker loss | event `worker.lost`, alert `lost:` | jobs resume from checkpoint; repeated losses → provider |
| Storage failure / failed transfer | event `storage.failed`, alert `transfer:` | resume from Drive & storage → Transfers |
| Storage quota ≥80 % / ≥90 % | alert `storage:<cid>:warning/error` (provider figure) | next destination by rules; nothing deleted |
| Drive needs reauthorisation | alert `reauth:<cid>` | reconnect (device code) |
| GPU quota ≥80 % | alert `quota:`, labelled provider-reported or **estimate** | GPU jobs pause when exhausted |
| Backup failed / incomplete / overdue | alert `backup:failed`, `backup:stale` (and `backup:none` in production) | fix destination, `nexus backup` |
| Upload rejected (ingest) | event `ingest.rejected` (notification) | reason in the event |
| Auth failures / lockouts | audit `auth.fail`, `auth.locked`; alert at ≥20 failures/hour or any lockout | check client addresses; rotate token if leaked |
| Disk pressure on `NEXUS_HOME` | alert `disk:home:warning/error`; `/health.disk.low` | free space |
| Service restart / unclean stop | audit `service.start/stop/unclean_restart`; notification on unclean | check log |
| Not ready | `/api/v1/health/ready` 503; `/api/v1/health.readiness.failing_gates` | fix the named gate |

External monitoring:
- Any free uptime checker, or the proxy's own health check, can poll `/api/v1/health/ready`. It reveals only the verdict.
- No third-party error-reporting SaaS is used. Errors stay in the service log and the audit table.

## 8. Request limits and uploads
- JSON bodies are limited to 8 MiB; multipart bodies to 20 GiB. The limit applies to the declared `Content-Length` and to streamed (chunked) bodies, and returns 413.
- Legacy `/api/upload`:
  - accepts video, audio and image types only (415 otherwise);
  - keeps only the base name of the file (no path traversal);
  - stages each upload in its own temporary folder, always cleaned up.
- `/api/v1/storage/ingest/upload` additionally enforces the job's `max_bytes`, the content checks and the declared rights.

## 9. Owner steps before going live (none can be done by an agent)
1. Approve the host and the exposure: Tailscale Serve, which is private, is recommended.
2. Create the secrets on the host (§2).
3. Do the offline vault-key backup (`ACCOUNT1_DISASTER_RECOVERY.md` §2).
4. Publish the OAuth consent screen (`ACCOUNT1_OAUTH_PRODUCTION_CHECKLIST.md`).
5. Run `python -m nexus production-check` on the host and confirm it prints `READY`.
6. Run one backup + verify + restore drill on the host (`ACCOUNT1_DISASTER_RECOVERY.md` §5).
