# Account 1 — final release audit (refreshed 2026-11-13, closure round)

Scope: Account 1 (cloud platform) on `work/account-1-cloud-platform`, PR #4. This audit covers Account 1's own code and
its integration status with Accounts 2 and 3. It is **not** a production-readiness claim for NEXUS as a whole.

**Kaggle: CLOSED — ACCEPTED AS-IS BY PROJECT OWNER** (owner decision, 2026-11-13). No further Kaggle changes are
requested or planned. The residual credential exposure is recorded in §9.

## 1. Verified PR state (re-fetched from GitHub, 2026-11-13)
| | Value |
|---|---|
| PR #4 | open, **draft**, unmerged: https://github.com/Ahmedhan1/new-project-money/pull/4 |
| Base | `claude/hopeful-allen-luubcy` @ `c93847d` (PR #3, draft). PR #3 is fully contained in Account 1's branch. |
| Code head of this audit | `e9ad5e9`. Code is unchanged since `9ea4a73`; `bb94742` and `e9ad5e9` are docs/state only. This closure round adds docs/state commits only. |
| Reviews | 0 reviews, 0 review threads |
| Merged into `main` | nothing. No merge, force-push, branch deletion or change to an Account 2/3 branch. |

## 2. Account 1 CI on the newest code head
Run [43389988132](https://github.com/Ahmedhan1/new-project-money/actions/runs/43389988132) (CI) and run 43389988073
(Windows end-to-end), both on `e9ad5e90211cd8758895b776e5e3e71b3c6f67cb`. GitHub reports every job as **success**.

| Job | Conclusion | Fast tests | Steps |
|---|---|---|---|
| lint-and-fast-tests (ubuntu-latest), [job 131767364365](https://github.com/Ahmedhan1/new-project-money/actions/runs/43389988132/job/131767364365) | success | **411 passed, 0 failed, 5 skipped** (416 collected) | lint, state validation, fast tests, secret/media scan: all success |
| lint-and-fast-tests (windows-latest), [job 131767364767](https://github.com/Ahmedhan1/new-project-money/actions/runs/43389988132/job/131767364767) | success | **411 passed, 0 failed, 5 skipped** (416 collected) | same steps: all success |
| windows-e2e (clean machine), job 131767312704 | success | n/a | |

Earlier code heads: `2ac7f96` was green (runs 43385684356 and 43385684363). The CI run on `9ea4a73` was **cancelled**
because `e9ad5e9` was pushed right after it; it did not fail.

### Test-count reconciliation
All three environments collect the same 416 tests. The differences are environment-dependent skips only:
| Environment | Result | Skipped tests |
|---|---|---|
| Linux CI (Ubuntu runner, Python 3.12) | 411 passed, 5 skipped | `test_audio_post::test_clean_speech…` (no TTS voice), `test_diarize_faces` ×2 (no voice models), `test_languages::…glyphs…[zh]` and `[hi]` (runner has no matching font) |
| Windows CI (Python 3.12) | 411 passed, 5 skipped | TTS voice (1), voice models (2), `test_languages…[hi]` (no font), `test_storage_backup::test_backend_contract[rclone]` (rclone not installed on Windows CI) |
| Dev container, clean clone (`scripts/release_check.py` at `9ea4a73`; Python 3.13; Noto fonts installed) | 412 passed, 4 skipped | TTS voice (1), voice models (2), rclone (1, not installed here) |
| Dev container, fresh venv from `requirements-ci.txt` only | 412 passed, 4 skipped | same as above, so Python packages are not the cause; the runner's fonts and tools are |

Method: CI runs `pytest -q` without `-rs`, so it prints no skip reasons. The CI skips were identified by mapping each
`s` in the progress output to the collected test order (`pytest --collect-only -q`, 416 items, same order).
`NEXUS_STATE.json` had shown `411 passed, 4 skipped`. That came from a local run made before the control-plane-restart
test was added (415 tests). It is regenerated in this round from a full local run on the current code.

## 3. Verified Account 1 capabilities
Each row is backed by implementation plus tests or recorded evidence. "Live" means a real provider was used.
| Capability | Fast tests | Live evidence |
|---|---|---|
| Kaggle remote execution (one account), private kernels, artifact SHA-256 verification, idempotency | `test_cloud_compute.py` | `kaggle_probe.json`, `kaggle_transcode.json` (T4 x2, NVENC) |
| Safe quota handling: provider pause, no rotation, GPU jobs wait queued, never paid fallback | `test_cloud_compute.py`, `test_scheduler.py` | simulated quota message only (§6) |
| Dispatching, priorities, cancellation, retries, checkpoints, resume after worker loss and after a control-plane restart | `test_cloud_compute.py` (incl. `test_control_plane_restart_adopts_the_running_remote_job_without_a_second_push`) | `kaggle_resume.json`, `lead_demos.json` (control-plane SIGKILL) |
| Google Drive A/B/C: device flow, encrypted refresh tokens, isolation, resumable upload, SHA-256, move, delete | `test_cloud_storage.py` (fake server) | `storage_drive_live.json`, `storage_drive_live_isolation.json` (3 real accounts) |
| Drive → Kaggle → Drive with no download to the owner's PC | — | Demo E in `lead_demos.json` |
| Storage routing (rules, failover, replication, reserve) | `test_cloud_storage.py` | real routing; failover between real Drives not forced |
| Link ingestion + SSRF protections | `test_ingest.py` | `ingest_live.json`, `ingest_ssrf_live.json` |
| Scheduler controls (daily target, pause/resume, stop-after-current, stop, budgets) and alerts | `test_scheduler.py`, `test_cloud_security.py` | `lead_demos.json` (demo F) |
| Remote Studio + observability | `test_studio_api.py`, `tests/e2e/test_studio_browser.py` | dev host only |
| Vault, owner auth, sessions, CSRF, headers, worker tokens, audit, route-auth inventory | `test_cloud_security.py` (20), `test_no_secrets.py` | `security_recovery.json` (7/7) |
| `batch_llm`: Kaggle-only routing, limits (items, tokens, prompt chars, weight size before download, time budget), checkpointed parts, joined results | `test_cloud_batch_llm.py` (8, simulated `echo` engine) | `batch_llm_live.json`: Qwen2.5-0.5B-Instruct, Tesla T4 x2, 8/8 items, job `rjob_877fa89eb0ac` |
| Cost Guard: `www.wikidata.org` declared; GDELT left to Account 3 | `test_costguard_research_hosts.py` (4) | reachability not verified from the owner's PC |

## 4. Account 1 code changes (all rounds since the start of the final closure)
| Commit | Change |
|---|---|
| `2ac7f96` | `batch_llm` worker task, TaskSpec, dispatcher join step, `wikidata` component, deterministic cookie/CSRF test, live script and evidence |
| `9ea4a73` | control-plane-restart regression test; Account 3 handoff §7–8; tasks; decisions D-11, D-12 |
| `bb94742`, `e9ad5e9` | audit, acceptance demo L, handoffs, state |
| this round | docs/state only: Kaggle closure, test-count reconciliation, latest-head integration, PR #5 interface notice |

## 5. Evidence types, kept separate
| Type | What it proves | Where |
|---|---|---|
| Fast tests (dev container) | logic, with fake services | `scripts/release_check.py` → 412 passed, 4 skipped |
| CI (GitHub, Linux + Windows) | the same suite on clean runners | §2 |
| Clean-machine Windows E2E | install + launch on a fresh Windows runner | `windows-e2e` job |
| Browser E2E | real Chromium against the Studio | `tests/e2e/test_studio_browser.py` (dev container) |
| Real Kaggle | real GPU jobs | `kaggle_*.json`, `batch_llm_live.json`, `lead_demos.json` |
| Real Google Drive | three real accounts | `storage_drive_live*.json`, Demo E |
| Fake services only | revoke flow, quota message, pull-worker protocol | `test_cloud_storage.py`, `test_cloud_compute.py` |
| Not done on the owner's machine | deployment, host, Wikidata/GDELT reachability, real owner-GPU worker | — |

## 6. Live vs simulated
| Capability | Live | Simulated only |
|---|---|---|
| Kaggle probe / transcode / NVENC / checksums | yes | |
| Worker-loss resume, control-plane-crash resume | yes | also fast tests |
| `batch_llm` with a real model | yes | `echo` engine in fast tests |
| Drive A/B/C, isolation, resumable upload, move, delete; Drive → Kaggle → Drive | yes | |
| Drive token revocation | no | fake server |
| Drive backup/restore | no | local drill only |
| Forced failover between real Drives | no | logic tests |
| Kaggle quota-exhaustion message | no (never forced; Kaggle closed) | fake CLI message |
| Owner GPU / Colab pull worker | no | protocol tests |
| Permanent public HTTPS | no | |

## 7. Cross-account integration (exact SHAs; separate from Account 1's own CI)
### 7.1 Latest-head run (this round), local branch `integration/latest-heads-check`, not pushed
| Input | SHA |
|---|---|
| Account 1 | `e9ad5e90211cd8758895b776e5e3e71b3c6f67cb` |
| Account 2 | `a52a24ac93689ebac917f9e9ac0452ab9bc2f297` |
| Account 3 | `fd71471df08dc82e262176487651c04c947fa9f8` (its `integration/contracts_v1/apply.sh`) |

Order: A1 → merge A2 → merge A3 → `apply.sh`.

Conflicts, all resolved by keeping both sides:
- `nexus/registry/tasks.yaml` (85 unique ids);
- `nexus/api.py` (A1 security middleware + A3 router);
- `nexus/workflows.py`;
- `nexus/ui/index.html` (A1's deep-link `go(...)` placed after `intelligence.js`).

Generated docs/state files took Account 1's side. `apply.sh` moved `nexus/contracts.py` → `nexus/contracts/cloud.py`
and `test_contracts.py` → `test_contracts_cloud.py`. Account 2 already ships `media.py`.

Results:
- **Focused contract tests** (from `apply.sh`): 42 passed.
- **Ruff:** clean.
- **Full fast suite** in a fresh venv built only from the merged `requirements-ci.txt` (Linux, Python 3.13):
  **748 passed, 0 failed, 12 skipped**. All skips are optional assets: opentimelineio 1, TTS/Piper voices 2, voice
  models 2, SigLIP ONNX 3, tesseract 3, rclone 1.
- **Not run on the merged tree:** Windows, the clean-machine E2E, renders with real models. Those run only once the
  owner approves the merge and it is pushed.

### 7.2 Earlier runs (superseded; not evidence for the current heads)
| Run | A1 | A2 | A3 | Result |
|---|---|---|---|---|
| Account 1, 2026-11-13 morning | `2ac7f96` | `9929512` | `24b3f71` | 663 passed with extra packages; 628 passed / 27 failed or error with CI deps (Account 2's missing `scipy`, `cv2`, `onnxruntime`), since fixed by Account 2 |
| Account 3 r2 | `2ac7f96` | `cc836de` | `5976a49` | 746 passed, 8 skipped, 0 failed (base `main`, per Account 3's `integration/contracts_v1/RESULT.md`) |

### 7.3 Account 2 status (PR #6, head `a52a24a`)
CI on `a52a24a`, run 43391530347: Linux **success**, Windows **success** (339 passed, 11 skipped, 0 failed), and
`windows-e2e` **success**. The four Windows failures reported on `cc836de` (OTIO path normalisation, the two
generation-classification tests, the LUT file name) no longer fail. `a52a24a` replaces the `file://` slicing with
`url2pathname`, removes `:` from the LUT test names, and declares `scipy`, `onnxruntime` and `opencv-python-headless`
(commits `a443238`, `39d0fac`). **No open Account 2 blocker for Account 1.**

### 7.4 Account 3 status (PR #5, head `fd71471`, CI green)
**Interface mismatch still open.** `bench_v2.account1_manifest()` sends
`params: {"models": [...], "items": <count>, "runtime": "ollama"}`. Account 1's `batch_llm` needs one `params.model`
per job and the prompts themselves: a JSONL input artifact (`params.items_input`) or `params.items` as a list.

A job in the current shape fails safely (status `failed`, error recorded), but the message
(`TypeError: 'int' object is not iterable`) doesn't name the cause. Verified by running the worker.

Account 3 was notified on PR #5:
- [contract](https://github.com/Ahmedhan1/new-project-money/pull/5#issuecomment-6847899922)
- [correction of the quoted error message](https://github.com/Ahmedhan1/new-project-money/pull/5#issuecomment-6847919100)

Account 1 did not edit Account 3's files. No integration test exercises this path, so the green integrated suite does
not cover it.

### 7.5 Integration path
1. Account 3 fixes `account1_manifest` (handoff §7).
2. Rerun §7.1 at the then-current exact heads, including Windows.
3. The owner approves the merge order A1 → A2 → A3 → `apply.sh`.

Account 1 does not adopt the package layout on its own branch (D-12).

## 8. Test results (Account 1 branch alone)
| Check | Result |
|---|---|
| Ruff | clean (CI and local) |
| CI fast suite, Linux / Windows (`e9ad5e9`) | 411 passed, 0 failed, 5 skipped each |
| Clean-clone release check (`9ea4a73`, dev container) | 412 passed, 0 failed, 4 skipped; hygiene pass |
| Security / storage + ingestion / compute + scheduler + Studio API / contracts / batch_llm | 29 / 96 / 38 / 32 / 8 passed |
| Browser Studio E2E | 1 passed (dev container) |
| Windows clean-machine E2E | success (CI) |
| Type checks | not configured in this repository |

No test was removed, skipped on purpose or weakened.

## 9. Security status
- No secrets, tokens or account identities in Git. The repository scan is clean in CI. The Kaggle account id was
  redacted from the new evidence before push, and history was checked.
- **Kaggle credential: CLOSED — ACCEPTED AS-IS BY PROJECT OWNER.** Residual exposure: the Kaggle token that was shared
  in a chat earlier is the one still configured. It worked for the live `batch_llm` job on 2026-11-13. Anyone who saw
  that chat could use it within the account's own Kaggle quota until the owner chooses to expire it. NEXUS's own
  controls are unchanged: vault-only storage, redaction, the token never travels in a kernel, scans in CI. This is
  recorded as a fact, not as an action.

## 10. Genuine owner-side actions (none done; not verifiable from here)
1. Back up `NEXUS_HOME/secrets/vault.key` offline. Without it, the vault (Drive tokens included) can't be decrypted.
2. Publish the Google OAuth consent screen. In Testing mode, refresh tokens expire after 7 days.
3. Choose and approve a permanent host (recommendation: owner machine + Tailscale Funnel), then test on that machine.
4. Optional, needs the owner's approval because it touches real accounts: real Drive token revocation, a Drive
   backup/restore run, and a forced failover between real Drives.
5. Optional: attach a real owner-GPU / Colab pull worker, if that capability is wanted.
6. Merge decisions and order (A1 → A2 → A3 → `apply.sh`). Nothing merges without the owner.

## 11. Zero-cost limits
No paid API, paid GPU, billing or payment method. One Kaggle account, with no rotation or quota bypass. Nothing has been
deployed publicly. The control plane has only run in the development container. Drive gives 15 GB per account, shared
with Gmail/Photos; three accounts are not unlimited storage.

## 12. Remaining items and ownership
| Item | Owner |
|---|---|
| `account1_manifest` → `batch_llm` contract (§7.4) | Account 3 |
| New integration run at the next exact heads, including Windows | Account 1 / Account 3 (both have run it before), after the Account 3 fix |
| Vault-key backup, OAuth production mode, host, owner-machine test, optional real-account checks | Owner |
| Merge approval | Owner |

## 13. Verdict
**READY FOR REVIEW** (Account 1 only).

All Account 1 code-level checks pass:
- CI green on Linux and Windows at `e9ad5e9`;
- clean-clone suite;
- browser E2E and Windows clean-machine E2E;
- live Kaggle and Drive evidence.

The latest-head three-account integration passes on Linux. The one open cross-account item belongs to Account 3. It is
precisely isolated, fails safely today, and has a documented fix that Account 3 has been notified about. This is not a
statement that NEXUS is production-ready: there is no permanent host and nothing has been tested on the owner's machine.
