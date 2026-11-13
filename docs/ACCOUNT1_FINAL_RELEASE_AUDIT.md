# Account 1 — final release audit (2026-11-13)

Scope: Account 1 (cloud platform) on `work/account-1-cloud-platform`, PR #4. The audit covers Account 1's code and its
integration with Accounts 2 and 3. It does **not** say the platform is production-ready.

## 1. Verified PR state (refreshed from GitHub, 2026-11-13)
| | Value |
|---|---|
| PR #4 | open, draft, https://github.com/Ahmedhan1/new-project-money/pull/4 |
| Base | `claude/hopeful-allen-luubcy` @ `c93847d` (PR #3, draft, base `main`). PR #3 is fully contained in Account 1's branch. |
| Head at start | `b2517b2` (CI Linux + Windows success; 0 reviews, 0 review threads, 0 comments) |
| Code head of this audit | `9ea4a73`. Later commits on the branch are docs/state only. |
| PR #5 (Account 3) | head `24b3f71`, CI success (Linux, Windows, windows-e2e) |
| PR #6 (Account 2) | head `9929512`, **CI failure** on Linux and Windows on its own head (missing `cv2` / `onnxruntime`; see §7). windows-e2e success. |
| Merged into `main` | nothing. No merge, no force-push, no branch deleted, no Account 2/3 branch changed. |

## 2. Account 1 features (complete)
Remote GPU compute on one Kaggle account: dispatcher, idempotency, priorities, checksum verification, checkpoints, resume
after worker loss and after a control-plane crash, quota pause, and safe production mode. Pull-worker protocol for an
owner GPU or Colab. Multi-account Google Drive A/B/C: device/web OAuth, encrypted refresh tokens, resumable upload,
isolation, storage router with failover and replication. SSRF-safe link ingestion. Scheduler with daily target,
pause/resume, stop-after-current, stop, and budgets. Remote Studio: jobs, storage, alerts, GPU quota. Security: vault,
owner auth, cookie session + CSRF, headers, audit, worker tokens, route-auth inventory. Contracts `/v1`. New in this
round: the `batch_llm` worker task and the Wikidata Cost Guard declaration.

## 3. Code changes in this round
| Commit | Change |
|---|---|
| `2ac7f96` | `nexus/cloud/worker_kit/tasks/batch_llm.py` (new worker task); `compute/tasks.py` TaskSpec `batch_llm` (gpu required, `providers=("kaggle",)`, resumable); `compute/dispatcher.py` joins verified parts of all attempts (`_join_batch_llm`, `_record_local`); `registry/components.yaml` `wikidata`; `tests/fast/test_cloud_batch_llm.py` (8), `tests/fast/test_costguard_research_hosts.py` (4); `test_cookie_session_and_csrf` made deterministic; `scripts/acceptance/batch_llm_live.py`; live evidence `docs/results/batch_llm_live.json` |
| `9ea4a73` | `test_control_plane_restart_adopts_the_running_remote_job_without_a_second_push` (new regression test); Account 3 handoff §7–8; tasks `ACC1-BATCH-LLM`, `ACC1-INTEGRATION`; decisions D-11, D-12 |
| docs commit(s) after `9ea4a73` | this audit, acceptance results, handoff/state refresh (`python -m nexus checkpoint`) |

The `job_id` bug listed in Account 3's `integration/contracts_v1/RESULT.md` was fixed in `633cc8b` and is still fixed at
`9ea4a73`: `tests/fast/test_cloud_compute.py` reads `r["job_id"]`. Account 3's RESULT.md lists it as open. That file is
on Account 3's branch, so Account 3 should mark both Account 1 rows resolved (§7).

## 4. `batch_llm` worker evidence
- **Design:** one HF model over a finite JSONL of prompts → JSONL results, then the process exits. No endpoint, port or
  background process. Limits: `max_items` (hard cap 5000), `max_new_tokens` (hard cap 4096), `max_prompt_chars`,
  `max_model_gb` (checked from Hub metadata **before** download; an unknown size is refused), and `time_budget_s`
  (bounded resume). Part files are checkpointed and checksum-verified, and the control plane joins them. A per-item error
  never fails the job. Weights are never downloaded outside a Kaggle kernel unless the owner sets
  `NEXUS_BATCH_LLM_ALLOW_HERE=1` on a machine of their choice. Routing never picks local, `user_gpu` or Colab.
- **Live (real Kaggle GPU, real model):** job `rjob_877fa89eb0ac`, `Qwen/Qwen2.5-0.5B-Instruct` (Apache-2.0,
  988,097,824 bytes downloaded inside the kernel), **Tesla T4 x2**, engine `transformers`, 8/8 items OK, 2 parts joined
  (`batch_llm_results.jsonl` sha256 `b566e7ce…714783`), task 44.7 s (model load 38.0 s), 22.5 completion tokens/s at
  batch 4, peak VRAM 766 MB, 1 attempt, wall 80 s. Evidence: `docs/results/batch_llm_live.json`. The account id is
  redacted to `<kaggle-user>`.
- **Quality caveat (for Account 3):** the 0.5B model got the arithmetic, the JSON extraction and the Arabic translation
  right. It translated "Friday" as "jeudi" and added an invented clause to the summary. This benchmark proves the
  pipeline, not model quality.
- **Fast tests (simulated engine `echo`, labelled `simulated: true`):** 8 passed. They cover end-to-end through the fake
  Kaggle CLI, worker loss → resume skipping finished parts, limits and per-item errors, JSONL input artifact, refusal
  outside Kaggle, oversized or unknown-size models, time budget, and safe production mode.

## 5. Research hosts / Cost Guard
| Host | Use (Account 3 code) | Declaration | Reachability |
|---|---|---|---|
| `www.wikidata.org` | `quality/visual_context._wikidata_country` (`wbsearchentities`, `wbgetentities`) | **added by Account 1** (`wikidata`, zero cost, CC0) | dev sandbox: HTTP **429** to a generic User-Agent (needs a descriptive UA). Owner PC: **not verified** |
| `api.gdeltproject.org` | `market/sources.gdelt_timeline` | Account 3's `gdelt-doc` entry on PR #5; not duplicated | dev sandbox: connection reset. Owner PC: **not verified** |

Tests: `test_costguard_research_hosts.py` checks that component ids are unique, that Wikidata is allowed and SPARQL is
not, that the GDELT host has at most one owner and is zero-cost when declared (passes on A1 alone and after the merge),
and that paid hosts stay blocked. No paid service and no new outbound host beyond `www.wikidata.org`.

## 6. Cookie security regression test
`test_cookie_session_and_csrf` now clears the cookie jar before every negative case, asserts the jar holds exactly the
cookie under test, and checks four cases: forged → 401, valid cookie with one signature character changed → 401,
expired → 401, no credentials → 401. A positive control follows: the genuine cookie → 200. Server code is unchanged
(`nexus/cloud/security.py`). Result: 20/20 in `test_cloud_security.py` on httpx 0.28.1, the version Account 3 reported
as ambiguous.

## 7. Shared-contract integration at exact heads
Isolated worktree (local branch `integration/contracts-v1-check`, **not pushed**): A1 `2ac7f96` → merge A2 `9929512` →
Account 3's `integration/contracts_v1/apply.sh` (from `24b3f71`) → merge A3 `24b3f71`.
- **Conflicts and resolutions:**
  - `nexus/registry/tasks.yaml`: kept both sides; 78 unique ids.
  - `tests/fast/test_contracts.py` (add/add): split by `apply.sh` into `test_contracts_cloud.py` and
    `test_contracts_media.py`.
  - `nexus/api.py`: keep both (A1 security middleware + A3 router).
  - `nexus/workflows.py`: keep both (A1/PR3 job kinds + A3 imports).
  - `nexus/ui/index.html`: keep both. The deep-link `go(...)` boot moved after `intelligence.js`, replacing A3's
    `go("home")`, so `/#review` still works.
  - Generated docs/state files: took Account 1's side; regenerated per branch.
- **Layout:** `nexus/contracts/cloud.py` (A1, moved), `nexus/contracts/media.py` (A2, moved), shared
  `nexus/contracts/__init__.py` (re-exports, `registry()`, `validate_any()`), and `nexus/intelligence/contracts.py`
  (A3). `ProductionJobManifest`, `ArtifactManifest` (`/v1` and `nexus.ArtifactManifest/1`) and `RenderJobManifest`
  validate through `validate_any`. Account 3's Strategy Handoff validates with Account 2's own validator.
- **Focused tests** (shared, cloud, media contracts, handoff, batch_llm, cost guard, security): **73 passed**. Ruff clean.
- **Complete integrated fast suite:**
  - With this container's packages (CI requirements plus `scipy`, `cv2`, `onnxruntime`, Noto fonts as in CI):
    **663 passed, 0 failed, 9 skipped**. Skips are optional tools: opentimelineio, Piper voices, tesseract, rclone.
  - With **CI's declared dependency set only** (clean venv from `requirements-ci.txt`): **628 passed, 23 failed,
    4 errors, 8 skipped**.

| Failures | Owner | Exact cause | Fix (verified locally) |
|---|---|---|---|
| 13 (`test_c2_dialogue` collection, `test_c2_audio_measure`, `test_c2_audio_qa`, `test_c2_sound_design`) | **Account 2** | `scipy` imported by `nexus/audio/*` and tests; not in `requirements-ci.txt` or `requirements.txt` | add `scipy` |
| 11 (`test_d2_vfx`, `test_d2_generation_arch`, `test_d2_enhance`) | **Account 2** | `cv2` not in `requirements-ci.txt` (only via `scenedetect[opencv-headless]` in `requirements.txt`) | add `opencv-python-headless` |
| 1 (`test_d2_enhance::test_onnx_writer…`) | **Account 2** | `onnxruntime` not declared | add `onnxruntime` |

With those three packages in the clean venv, the affected files give **62 passed, 1 skipped**. The same missing
modules make PR #6 red on its own head, so this is not an integration defect. Account 1 did not edit Account 2's files.
The fix is Account 2's (handoff below). Before the CI font install, one Account 3 test
(`test_mixed_script_text_gets_a_font_per_run_and_no_boxes`) failed in this container on Account 3's branch alone as well.
It needs `fonts-noto-core`, which CI installs, and passes once that is installed.
- **Account 3 → Account 1 contract note:** `bench_v2.account1_manifest` sends `params.models`, a count in `params.items`
  and `runtime: "ollama"`. `batch_llm` takes one `model` and the prompts themselves (handoff §7). The manifest still
  validates as `ProductionJobManifest/v1`; the params need Account 3's change.
- **Integration path:** (1) Account 2 adds `scipy`, `opencv-python-headless` and `onnxruntime` to
  `requirements-ci.txt`. (2) Account 3 marks its RESULT.md Account 1 rows resolved. (3) Rerun this integration at the
  new heads. (4) The owner approves merge order A1 → A2 → A3 → `apply.sh`. Account 1 does not commit the package layout
  on its own branch (D-12).

## 8. Test and CI results (Account 1 branch)
| Check | Result |
|---|---|
| Ruff | clean |
| Full fast suite, clean clone (`scripts/release_check.py`, `9ea4a73`) | **412 passed, 0 failed, 4 skipped**; hygiene pass |
| Security (`test_cloud_security`, `test_no_secrets`, `test_costguard_research_hosts`) | 29 passed |
| Cloud storage + ingestion (`test_cloud_storage`, `test_ingest`) | 96 passed |
| Compute + scheduler + Studio API (`test_cloud_compute`, `test_scheduler`, `test_studio_api`) | 38 passed |
| Contracts (`test_contracts`) | 32 passed |
| batch_llm (`test_cloud_batch_llm`) | 8 passed |
| Browser Studio E2E (`tests/e2e/test_studio_browser.py`, Playwright Chromium) | 1 passed |
| Type checks | not configured in this repository (no mypy/pyright config or CI step) |
| Linux CI / Windows CI | see PR #4 checks on the newest head (recorded in the PR description) |
| Integrated suite at exact heads | §7 |

The 4 skips on Account 1 alone are optional tools (rclone, Piper voices, tesseract). No test was removed or weakened.

## 9. Live vs simulated evidence
| Capability | Live (real provider) | Simulated only |
|---|---|---|
| Kaggle GPU probe / transcode / NVENC / checksums | yes (T4 x2) | |
| Worker-loss resume, control-plane-crash resume | yes (Demo D/F) | also fast tests (fake Kaggle CLI) |
| `batch_llm` with a real model | yes (`rjob_877fa89eb0ac`) | `echo` engine in fast tests |
| Drive A/B/C connect, isolation, resumable upload, move, delete | yes (3 real accounts) | |
| Drive → Kaggle → Drive without PC download | yes (Demo E, 144.6 MB output) | |
| Drive token revocation | no | fake server |
| Drive backup/restore | no | local drill only |
| Forced real failover between Drives | no | logic tests |
| Kaggle quota-exhaustion message | no (never forced, by rule) | fake CLI "429 ... weekly GPU quota" |
| Owner GPU / Colab pull worker | no | protocol tests |
| Permanent public HTTPS | no | |
| Wikidata / GDELT from the owner's PC | no | dev sandbox only (429 / reset) |

## 10. Security status and owner actions
- No secrets in Git: secret scan clean. The Kaggle account id was removed from the new evidence before push, and branch
  history was checked. OAuth client JSON and tokens live only in the encrypted vault under `NEXUS_HOME/secrets` (not in
  Git).
- **Owner actions, not done and not verifiable from here:**
  1. Revoke the Kaggle tokens that were shared in chat, create **one** new token, and store it directly:
     `python -c "from nexus import vault; import getpass; vault.put('kaggle_primary', getpass.getpass('Kaggle token: '), {'provider':'kaggle'})"`.
     The old token in this dev container still worked on 2026-11-13, so it has not been revoked yet.
  2. Back up `NEXUS_HOME/secrets/vault.key` offline.
  3. Publish the OAuth consent screen to production. In Testing mode, refresh tokens expire after 7 days.

## 11. Zero-cost deployment limits
No paid API, paid GPU, billing activation or payment method. One Kaggle account, with no rotation or quota bypass.
Nothing has been deployed publicly. The control plane runs only in this development container. The recommended host
(owner machine + Tailscale Funnel) stays a recommendation until the owner authorises it. Kaggle has a weekly GPU quota
and session limits, and Drive gives 15 GB per account shared with Gmail/Photos. Three accounts are not unlimited storage.

## 12. Remaining blockers and ownership
| Blocker | Owner |
|---|---|
| `requirements-ci.txt` missing `scipy`, `opencv-python-headless`, `onnxruntime` (PR #6 CI red; integrated suite red on CI deps) | Account 2 |
| RESULT.md still lists the two fixed Account 1 failures; `account1_manifest` params need the `batch_llm` schema; descriptive Wikidata User-Agent | Account 3 |
| Merge approval and order (A1 → A2 → A3 → `apply.sh`), never into `main` without approval | Owner |
| Token rotation, vault-key backup, OAuth production mode, host choice | Owner |
| Real quota message, real pull worker, real Drive revoke/backup/failover | Owner approval (touches real accounts) / provider |

## 13. Verdict
**READY FOR REVIEW** (Account 1 only).

All Account 1 code-level acceptance criteria pass:
- the fast suite in a clean clone;
- security, storage, compute, scheduler, contracts and `batch_llm` tests;
- the browser E2E;
- the live `batch_llm` run on Kaggle.

The three-account integration passes at exact heads when Account 2's dependencies are present. The one remaining
blocker belongs to Account 2: three undeclared CI dependencies. It is isolated to exact files and causes, and the fix
was verified locally. Account 2 still has to make that change on its own branch, so the integrated merge waits for it
and for the owner's approval. This is not a production-readiness claim: there is no permanent host, the tokens still
need rotating, and the remaining live checks above have not been done.
