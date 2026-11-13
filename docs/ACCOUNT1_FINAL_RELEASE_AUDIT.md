# Account 1 — final release audit (production-closure round, 2026-11-13)

Scope: Account 1 (cloud platform and production infrastructure) on `work/account-1-cloud-platform`, PR #4, and its
integration status with Accounts 2 and 3.

This audit makes no production-readiness claim for NEXUS as a whole. Nothing has been deployed, merged or exposed
publicly.

**KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER.** This round made no Kaggle change, requested no rotation, and did no quota
work. The residual exposure is recorded in §9 as a fact, not as an action.

## 0. Verdicts
| Scope | Verdict |
|---|---|
| **Account 1** | **READY FOR OWNER DEPLOYMENT.** Engineering is complete and verified (§2–§8). What remains is owner-controlled (§10): approve a host, inject the secrets, make the offline vault-key backup, publish the OAuth consent screen, and run `production-check` plus one backup/restore drill on the host. |
| Three-account release | **Engineering unblocked.** Account 3 fixed the `batch_llm` manifest (`7572acb`, D-17); the merged tree at the current exact heads passes Linux 862/0/12 and a live production-mode smoke (§7.1). Not yet run on Windows: GitHub Actions is not starting any job for the whole repository (account-level, §7.6). |
| NEXUS as a whole | **Not READY FOR PRODUCTION**, by definition of the owner-only steps: no approved host, no injected secrets, OAuth consent screen unpublished, no merge. Engineering-side it is READY FOR OWNER DEPLOYMENT. |

## 1. Verified PR state (re-fetched from GitHub)
| | Value |
|---|---|
| PR #4 | **merged by the owner** into `claude/hopeful-allen-luubcy` (merge commit `b54c952`, tree identical to `f9d8f11`): https://github.com/Ahmedhan1/new-project-money/pull/4 |
| Base | PR #3 (`claude/hopeful-allen-luubcy` → `main`), draft, unmerged |
| Code head audited | `73c0023` (production hardening `34a5ba1`, manifest-credential refusal `dafefe3`, test-literal fix `73c0023`). Later commits are docs/state only. |
| Reviews | 0 reviews, 0 review threads |
| Merged / deployed | nothing. No force-push, no branch deletion, no change to an Account 2/3 branch, no billing. |

## 2. Account 1 CI
Runs [43407933476](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933476) (CI) and
[43407933450](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933450) (Windows clean machine), both on
`73c0023a43e8b3e186b9fc0d28c8b402718e2376`. GitHub reports every job as **success**.

| Job | Conclusion | Fast tests | Steps |
|---|---|---|---|
| lint-and-fast-tests (ubuntu-latest), [job 131826894522](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933476/job/131826894522) | success | **427 passed, 0 failed, 5 skipped** (432 collected) | lint, state validation, fast tests, secret/media scan: all success |
| lint-and-fast-tests (windows-latest), [job 131826894751](https://github.com/Ahmedhan1/new-project-money/actions/runs/43407933476/job/131826894751) | success | **427 passed, 0 failed, 5 skipped** (432 collected) | same steps: all success |
| windows-e2e (clean machine): setup, start, E2E, status, backup, stop | success | n/a | |

Notes on the intermediate pushes:
- CI on `34a5ba1` and `dafefe3` was **cancelled** by the concurrency group when the next commit was pushed. It did not
  fail.
- Their Windows E2E runs succeeded.
- `34a5ba1` would have failed the secret-scan test (a token-shaped literal in a new test file). The local integration
  run caught it first, and `73c0023` fixed it (§7.1).

### Test-count reconciliation
- Every environment collects the same tests. The differences are environment-dependent skips only.
- Linux CI skips:
  - TTS voice (1);
  - voice models (2);
  - `test_languages` glyphs for zh and hi (no runner font).
- Windows CI skips:
  - TTS voice (1);
  - voice models (2);
  - hi glyphs (1);
  - rclone (1).
- Dev container skips:
  - TTS voice (1);
  - voice models (2);
  - rclone (1).
- The local full run on this round's code: **428 passed, 4 skipped** (432 collected). It adds the 16 production tests
  to the earlier 412/4.
- Method:
  - CI prints no skip reasons;
  - the skips were identified by mapping each `s` to `pytest --collect-only -q` order (reconciled in the previous round,
    unchanged).

## 3. Production security audit (whole Account 1 branch)
| Area | Result | Evidence |
|---|---|---|
| Authentication on every protected route | every `/api/v1` route depends on `require_owner` / `require_worker` except 6 documented public routes (contracts, session, live, ready, studio auth/logout, OAuth callback). Legacy `/api/*`, `/`, `/docs`, `/redoc` and `/openapi.json` are owner-only; the docs are not served in production. | `test_every_v1_route_requires_auth`, `test_route_inventory_detects_unprotected`, `test_api_docs_are_owner_only_in_development` |
| Session lifecycle | HMAC-signed `s1.<issued>.<sig>` value, 12 h TTL, future-dated values refused, rotating the token ends every session, logout clears the cookie | `test_cookie_session_and_csrf` |
| Cookie attributes | HttpOnly, SameSite=Strict, Path=/, Max-Age; Secure on https or behind a proxy; never the token itself | `test_session_cookie_attributes` |
| CSRF | cookie-authenticated unsafe methods need `X-NEXUS-CSRF: 1` **and** a same-origin Origin/Referer (`NEXUS_PUBLIC_ORIGIN`, https-only in production); bearer clients are not CSRF-exposed | `test_cookie_session_and_csrf`, `test_duplicate_session_cookies_are_decided_by_signature_not_parser_order` |
| Duplicate cookies (Account 2's report) | explained and closed (§3.1) | same |
| CORS | no CORS middleware: cross-origin requests are never granted, and the custom CSRF header forces a preflight | code review; `test_loopback_cross_origin_post_refused` |
| Host validation | loopback mode accepts only loopback Host names (DNS rebinding); proxy headers make a request remote | `test_dns_rebinding_host_refused`, `test_proxied_loopback_is_remote` |
| SSRF / redirects | ingestion resolves and validates every hop (private, link-local and metadata ranges refused; at most 5 redirects, each re-validated); outbound hosts go through the Cost Guard allow-list | `test_ingest.py` (71), `ingest_ssrf_live.json` (34/34 refused) |
| Rate limiting | 10 failures / 5 min per client → 429 + audit. Behind the proxy the client comes from `X-Forwarded-For` only for `NEXUS_TRUSTED_PROXIES`; spoofing is ignored. Single process enforced. | `test_rate_limit_after_failures`, `test_rate_limit_identity_behind_trusted_proxy`, `test_single_instance_lock_refuses_a_second_api_process` |
| Request / upload size | JSON 8 MiB, multipart 20 GiB, including chunked bodies (413); legacy upload: type allow-list (415), base name only, private temp folder; ingest adds `max_bytes` and content checks | `test_body_and_upload_limits`, `test_ingest.py` |
| Worker authentication | per-worker `nxw_` tokens (256 bits), only SHA-256 stored in the vault, rotate/revoke, rate limited and audited | `test_worker_token_hashing` |
| Vault | Fernet at rest, files 0600 / dir 0700, wrong key fails closed (`undecryptable` counted, health not ok), production key injected and not on disk | `test_recovery`, `test_production_gates_*` |
| Secret redaction | every stored secret value and token-shaped string is redacted from events, audit, alerts and logs; job manifests carrying a credential are refused | `test_audit_strips_secrets`, `test_alerts_for_backup_auth_and_disk`, `test_job_manifest_with_a_credential_is_refused`, `test_no_secrets.py` |
| Audit coverage | auth failures and lockouts, sessions, worker tokens, connections and OAuth, secrets, scans, remote dispatch, service start/stop/unclean restart, vault-key export | `test_rate_limit_after_failures`, `test_lifecycle_*`, `test_offline_vault_key_export_and_verify` |
| Errors without leaks | auth errors are generic; production-gate messages name checks and fixes, never values; `/health/ready` returns only the verdict | `test_production_gates_fail_closed_and_name_fixes_without_values`, `test_readiness_probe_reveals_only_the_verdict` |

Fixed in this round (with tests):
- duplicate-cookie order dependence;
- per-proxy rate-limit lockout;
- missing body and upload limits;
- unguarded `/docs` and `/openapi.json`;
- credentials accepted in job manifests;
- a hosted deployment silently running in dev mode.

Accepted residual risks: `ACCOUNT1_SECURITY_MODEL.md` §7.2 (no FFmpeg fuzzing, Codespaces header assumption, in-memory
counters reset on restart, CSP `unsafe-inline` for the single-file UI, the Kaggle residual exposure).

### 3.1 The cookie/CSRF discrepancy
Account 2's audit reported `test_cookie_session_and_csrf` failing with httpx 0.28 on an earlier Account 1 commit.

Reproduced 2026-11-13 with httpx 0.28.1 and Starlette 1.7.0:
- httpx keeps the server-set cookie (domain `testserver.local`) next to a manually set one, and sends both:
  `nexus_session=GOOD; nexus_session=FORGED`.
- The old test's "forged cookie → 401" therefore depended on which value the server's cookie parser kept.

Conclusions:
- **Test failure:** environment-dependent; it came from the test's ambiguous input.
- **Security issue:** none. In every case the server accepted only a value with a valid HMAC, and that client really
  held a valid session.
- **Earlier report:** outdated. The test was fixed in `2ac7f96`, and Account 3's `RESULT.md` r2 confirms it passes.
- **Server:** now decides deterministically. Every presented value is checked, so a planted sibling can neither grant
  access nor lock the owner out. A regression test sends both orders explicitly.
- **Current head:** passes on Linux and Windows CI (§2).

## 4. Deployment readiness (`ACCOUNT1_PRODUCTION_DEPLOYMENT.md`)
| Item | Status |
|---|---|
| Explicit production mode, different from development | `NEXUS_ENV=production`, fail-closed, 12 gates; tested with a real subprocess (refuses, then serves with docs off) |
| Service startup / supervision | `nexus serve` (systemd unit and Windows Task Scheduler in the runbook, with `production-check` as ExecStartPre); `NEXUS_START.bat` for the owner PC |
| Graceful shutdown | uvicorn 30 s drain; lifespan stops poller, alerts and scheduler, audits, releases the lock; durable jobs resume from checkpoints |
| Health / readiness | `/api/v1/health/live`, `/api/v1/health/ready` (200/503, verdict only), `/api/v1/health` (owner, full detail including failing gates) |
| Persistent configuration | `NEXUS_HOME` (database, vault ciphertext, settings, storage rules); secrets only through the host's secret store |
| Database init / migrations | schema migrations on first connection; migration 2 tested on a pre-cloud database |
| Secure secret loading | token, vault key and backup passphrase from environment / secret store; key file in `NEXUS_HOME` blocks production |
| TLS / reverse proxy | loopback bind enforced; TLS terminates in the proxy; HSTS on https; `X-Forwarded-*` interpreted only for trusted proxies; uvicorn `proxy_headers=False` |
| Logging / rotation | service logs size-rotated (20 MiB × 5); journald under systemd; audit table retention 90 days / 20,000 rows |
| Monitoring / error reporting | alerts + notifications + audit (§7 of the runbook); external uptime check on `/health/ready`; no third-party SaaS |
| Restart recovery | tested: control-plane restart adopts the running Kaggle job without a second push; unclean stop detected |
| Scheduled jobs | scheduler persisted and re-planned on restart; backup schedule via timer/cron + overdue alert |
| Single vs multi instance | **single instance, enforced** (OS lock); multi-instance not supported (D-13) |
| Host | **not chosen.** Recommended zero-cost target: owner machine + Tailscale Serve (private). Public exposure only with explicit approval. |

## 5. Backup and recovery (`ACCOUNT1_DISASTER_RECOVERY.md`)
| Item | Status | Evidence |
|---|---|---|
| Encrypted backups | AES-256-GCM (scrypt) for remote destinations; vault entries are Fernet ciphertext in every backup | `test_encrypted_backup_roundtrip_and_wrong_passphrase`, `test_backup_carries_vault_ciphertext_never_the_key` |
| Key separation | `vault.key` is never collected; a snapshot containing one is flagged at restore; raw destination bytes checked for both key and credential | same |
| Integrity verification | every object re-read and checksummed before success; corruption and missing objects detected | `test_backup_verify_restore_and_reopen_project`, `test_corruption_and_missing_objects_are_detected` |
| Restore + validation | new empty folder only; checksums per file; references re-opened; vault decryptability reported (no key / right key / wrong key) | `test_backup_carries_vault_ciphertext_never_the_key` |
| Backup history | `last_backup.json` + `backup` documents in the database; `last_restore_drill.json` | `nexus status` |
| Safe failure | destinations independent; free-allowance cap respected; failed, incomplete or overdue backups alert | `test_one_destination_down_does_not_lose_data`, `test_free_allowance_cap_is_never_exceeded`, `test_alerts_for_backup_auth_and_disk` |
| Offline vault-key backup | `vault-key export` (never printed; refuses `NEXUS_HOME`, the repository, backup roots; never overwrites) + `verify` + fingerprint | `test_offline_vault_key_export_and_verify` |
| Real Drive restore / revoke / failover | **not run.** Owner-authorised procedure documented. | — |

## 6. OAuth readiness (`ACCOUNT1_OAUTH_PRODUCTION_CHECKLIST.md`)
- Engineering is complete:
  - device flow and web flow (PKCE, single-use state, https redirect);
  - `drive.file` + `openid` + `email` only;
  - refresh tokens only in the vault;
  - revoke on disconnect;
  - reauth alerts.
- Google's facts were re-checked from Google's pages on 2026-11-13:
  - Testing-mode refresh tokens expire after 7 days;
  - `drive.file` is non-sensitive, so verification is not mandatory;
  - `drive.file` is allowed in the device flow.
- **Production OAuth readiness is not claimed.** The owner must publish the consent screen and reconnect A/B/C.

## 7. Cross-account integration (exact SHAs; separate from Account 1's own CI)
### 7.1 Current run (final review, 2026-11-13), local branch `integration/heads-f9d8f11`, not pushed

**Result artifact:** `docs/results/integration/heads-f9d8f11-d979df1-62414bf.json` (full SHAs, merged tree
`b962b4e`, counts taken from the JUnit report, its sha256, skip reasons, environment, what was not run).
**Reproduce:** `scripts/integration/integrate_heads.sh <A1> <A2> <A3> <dir> <branch>`; a second run produced the
identical tree. PR #3 itself does **not** contain Account 2 or 3: this is a local candidate tree, and the release gate
(Windows + hosted CI on a pushed integration branch, owner merge order) is **not passed**.
| Input | SHA |
|---|---|
| Account 1 | `f9d8f11` (= PR #3 head `b54c952` tree; code identical to `73c0023`) |
| Account 2 | `d979df1e390abc48e79a7e6645229968a5b94107` (`work/account-2-cinematic-engine`, unchanged) |
| Account 3 | `62414bf` (`work/account-3-intelligence-growth`, final closure with the `batch_llm` fix) |

Order: fresh worktree at A1 → merge A2 → merge A3 → `apply.sh`, scripted, with the same keep-both resolutions as before
(`tasks.yaml` union; `api.py` A1 gates + A3 router; `workflows.py` both; `index.html` deep-link `go(...)`; generated
docs/state files Account 1's side). No conflict markers remain.

Results (Linux, Python 3.13, venv from the merged `requirements-ci.txt`):
- **Focused contract tests** (`apply.sh`): **42 passed**.
- **Full fast suite: 862 passed, 0 failed, 12 skipped** (skips are optional assets only).
- **Cross-account `batch_llm`:** `test_intel_batch_llm_integration.py` + `test_intel_batch_manifest.py` +
  `test_production.py` + `test_cloud_security.py`: 61 passed. Account 3's generated manifests validate against
  `ProductionJobManifest/v1`, run on Account 1's real `Dispatcher` and return every item; the old shape is refused
  with a named cause.
- **Live production-mode smoke on the merged tree** (real `python -m nexus serve`, throwaway secrets generated in the
  shell, never written to a file):
  - unconfigured `NEXUS_ENV=production` → exit 1, `ProductionConfigError`, each failing gate prints its fix;
  - fully configured → `production-check` READY (12/12), `/api/v1/health/ready` 200 `{"ready":true}`;
  - owner routes (`/api/v1/health`, `/storage/connections`, `/compute/jobs`): 401 anonymous, 200 with the token;
    `/docs` 401; no `Server` header;
  - second API process on the same home → refused (`single_instance`);
  - SIGTERM → "Application shutdown complete", `service.start`/`service.stop` audited, run marker removed
    (exit status 143 is uvicorn 0.54 re-raising the signal after a clean stop);
  - backup to an off-home destination verified; restore drill 3/3 files verified;
  - token, vault key and stored credential plaintext: **0 occurrences** in logs and backup objects;
  - wrong `NEXUS_VAULT_KEY` → `vault_readable` FAIL, NOT READY.

### 7.2 Superseded runs (not evidence for current heads)
| Run | A1 | A2 | A3 | Result |
|---|---|---|---|---|
| production closure | `73c0023` | `d979df1` | `fd71471` | 766 passed, 12 skipped (A3 manifest still old shape) |
| closure round | `e9ad5e9` | `a52a24a` | `fd71471` | 748 passed, 12 skipped |
| Account 3 r2 | `2ac7f96` | `cc836de` | `5976a49` | 746 passed, 8 skipped |
| final-closure morning | `2ac7f96` | `9929512` | `24b3f71` | 628 passed / 27 failed with CI deps (Account 2's undeclared packages, since fixed) |

### 7.3 Account 2 (latest head `d979df1`)
No open item for Account 1. Account 2's report of a failing Account 1 cookie/CSRF test is explained and closed (§3.1).

### 7.4 Account 3 (head `62414bf`)
**Closed.** The mismatch (`params.models` / item count / `runtime: ollama`) is fixed on Account 3's side
(`7572acb`: one job per model, prompts inline, old shape refused) and verified on the merged tree (§7.1). Account 1
edited no Account 3 file. Account 2 also has a newer follow-up branch (`work/account-2-asset-library-storage`, PR #7);
it was not part of this directive's pinned heads and is not covered by this run.

### 7.5 Remaining integration steps (owner-gated)
1. Windows CI and the Windows clean-machine E2E on the merged tree: need runners (§7.6) and the owner's go-ahead to
   push an integration branch.
2. Merge order A1 → A2 → A3 → `apply.sh`: owner decision. Stale results are not reused; rerun §7.1 at whatever heads
   are current when the owner merges.

### 7.6 GitHub Actions outage (account-level)
Since 2026-11-13 ~10:50 UTC every job in the repository (Account 1, 2 and 3 PRs alike) is marked failed 2–5 s after
creation with no runner assigned and no log. This is the Actions minutes/spending limit or a GitHub-side problem,
not code. The last run that executed (`73c0023`, same code as today's Account 1 head) passed on Linux and Windows.
No billing setting was touched; fixing it is an owner action (§10).

## 8. Production acceptance matrix
| Requirement | Test(s) | Linux | Windows |
|---|---|---|---|
| Authentication and authorization | `test_bearer_ok_and_wrong`, `test_every_v1_route_requires_auth`, `test_remote_denied_without_token`, `test_production_gates_fail_closed…` (no token → 503) | ✔ | ✔ |
| CSRF | `test_cookie_session_and_csrf`, `test_duplicate_session_cookies…`, `test_loopback_cross_origin_post_refused` | ✔ | ✔ |
| Session expiry | `test_cookie_session_and_csrf` (expired value 401), `test_duplicate_session_cookies…` | ✔ | ✔ |
| Vault access | `test_recovery`, `test_secrets_endpoint_never_returns_values`, `test_production_gates_pass_with_complete_configuration` (wrong key → gate fails) | ✔ | ✔ |
| Backup / restore | `test_storage_backup.py` (9), `test_backup_carries_vault_ciphertext_never_the_key` | ✔ | ✔ |
| Interrupted storage transfers | `test_local_resumable_crash_and_resume`, `test_drive_resumable_upload_survives_crash` | ✔ | ✔ |
| Duplicate artifact prevention | `test_store_dedupe_manifest_and_move`, `test_idempotency_key_returns_existing_job` | ✔ | ✔ |
| Worker-loss recovery | `test_lost_attempt_without_result_is_retried`, `test_chunked_transcode_resumes_from_checkpoint`, `test_worker_killed_mid_job_resumes_without_repeating_stages`; live `kaggle_resume.json` | ✔ | ✔ |
| Control-plane restart recovery | `test_control_plane_restart_adopts_the_running_remote_job_without_a_second_push`, `test_lifecycle_audits_start_stop_and_detects_unclean_restart`; live demo F | ✔ | ✔ |
| Scheduler recovery | `test_restart_persistence`, `test_stop_cancels_safely_and_restart_replans` | ✔ | ✔ |
| Storage failure | `test_failover_to_next_allowed_destination_and_opt_in_replication`, `test_drive_quota_exceeded`, `test_one_destination_down_does_not_lose_data`, `test_storage_exhaustion_pauses` | ✔ | ✔ |
| Rate limiting | `test_rate_limit_after_failures`, `test_rate_limit_identity_behind_trusted_proxy` | ✔ | ✔ |
| Invalid upload rejection | `test_body_and_upload_limits`, `test_ingest.py` | ✔ | ✔ |
| Clean installation | Windows clean-machine E2E (`windows-e2e.yml`: setup, start, E2E, status, backup, stop); `scripts/release_check.py` (fresh clone, Linux) | ✔ | ✔ |
| Production startup | `test_production_startup_refuses_then_serves_without_docs`, `test_single_instance_lock_refuses_a_second_api_process`, `test_readiness_probe_reveals_only_the_verdict` | ✔ | ✔ |
| Graceful shutdown | `test_lifecycle_audits_start_stop_and_detects_unclean_restart`; Windows E2E `NEXUS_STOP.bat` | ✔ | ✔ |
| Alerts | `test_alerts_*` (quota estimate labelled, resolution), `test_alerts_for_backup_auth_and_disk` | ✔ | ✔ |
| Log rotation | `test_service_log_rotation` | ✔ | ✔ |

✔ means the test ran and passed in the CI run of §2 for that OS. No test was removed, skipped on purpose or weakened.
`test_cloud_security.py` gained one public-route entry (`/api/v1/health/ready`) in its documented allow-list.

## 9. Security status
- No secrets, tokens or account identities in Git. The repository scan is clean in CI.
- **KAGGLE: CLOSED — ACCEPTED AS-IS BY OWNER.** Residual exposure, recorded as a fact:
  - the token once shared in a chat is the one still configured;
  - anyone who saw that chat could use it within the account's own Kaggle quota until the owner chooses to expire it.
  
  NEXUS's controls are unchanged: vault-only storage, redaction, never sent in a kernel. Not an action item.

## 10. Owner actions still required (none can be done by an agent)
1. Approve the host and the exposure. Recommended: own machine + Tailscale Serve, private. Funnel/public only by
   explicit approval.
2. On the host, inject `NEXUS_API_TOKEN` (≥32 characters, generated there), `NEXUS_VAULT_KEY` and
   `NEXUS_BACKUP_PASSPHRASE`, and set `NEXUS_PUBLIC_ORIGIN` and `NEXUS_TRUSTED_PROXIES`.
3. Offline vault-key backup: `python -m nexus vault-key export --to <removable drive>`, then `vault-key verify`.
   Then remove `NEXUS_HOME/secrets/vault.key` from the host.
4. Configure an off-disk backup destination (second disk, or an authorised Drive with the passphrase).
5. Publish the Google OAuth consent screen ("In production") and reconnect Drive A/B/C.
6. On the host, run `python -m nexus production-check` until it prints READY. Then run one backup + verify + restore
   drill.
7. Optional, touches real accounts: a real Drive revoke, a Drive backup/restore and a forced failover.
8. Merge decisions and order (A1 → A2 → A3 → `apply.sh`). Nothing merges without the owner.
9. Restore GitHub Actions runner availability (Settings → Billing / Actions), then re-run CI on PR #3 and on the
   integration branch. No agent changes billing.

## 11. Outstanding limitations
- No permanent host. Nothing has been tested on the owner's own machine or on an approved host.
- Real Drive revocation, Drive backup/restore and forced Drive failover: fake-server and local tests only.
- A real Kaggle quota-exhaustion message has never been seen. It is never forced (Kaggle closed).
- Owner-GPU / Colab pull worker: protocol tests only.
- The three-account merged tree has not run on Windows or in the clean-machine E2E: GitHub Actions is down for the
  account (§7.6). Linux and a live production-mode smoke passed.
- Rate-limit counters reset on restart. Horizontal scaling is not supported (single instance by design).
- No FFmpeg input fuzzing. The CSP allows inline script for the single-file UI.
- Drive gives 15 GB per account, shared with Gmail/Photos. Three accounts are not unlimited storage.

## 12. Zero-cost
No paid API, paid GPU, billing or payment method. One Kaggle account, with no rotation or quota workaround. No public
deployment. The recommended host costs nothing (the owner's machine + Tailscale Personal plan).
