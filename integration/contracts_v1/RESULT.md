# Three-account integration runs (local branches only — nothing pushed, nothing merged into `main` or any account branch)

**Run r4b is the current evidence** (exact final heads). Run r4 is the same recipe on the previous base commit; its
detailed checks below were repeated on r4b with identical counts. Runs r3, r2 and r1 are kept for history. Full-suite results and focused results are reported separately and are never added together.
All results below are **local Linux runs** (this dev container: Linux x86_64, Python 3.13), **not GitHub CI**.

## Run r4b — 2026-11-13, exact final heads (current)

| Input | Branch | SHA |
|---|---|---|
| base | `claude/hopeful-allen-luubcy` (PR #5's base; contains PR #4 and Account 1's final review) | `a889cd8dee1f3a19a99858d1d2a40b93ba217da7` |
| Account 1 | `work/account-1-cloud-platform` (in the base via PR #4; the base's later commit `a889cd8` changes docs/state only) | `f9d8f1131788d5848594bb78c61bff3b86d400e1` |
| Account 2 | `work/account-2-cinematic-engine` | `d979df1e390abc48e79a7e6645229968a5b94107` |
| Account 3 | `work/account-3-intelligence-growth` | `fae6d3842ea7524150949bd3515c2c4439b4b941` (all A3 code; later commits change docs and generated state only) |

Local branch `integration/contracts-v1-r4b` (not pushed), in order:
1. start from the base `a889cd8`;
2. merge A2 `d979df1` (`e9d5a19`);
3. merge A3 `fae6d38` (`44eec9c`);
4. `apply.sh` (`813157b`).

The conflicts and resolutions are the same as in r4 (table below). tasks.yaml ends with 91 unique ids, and the merged
`NEXUS_HANDOFF.md` has 1,415 words.

Results on `813157b` (local Linux, not GitHub CI):
- `apply.sh`: **42 passed**, then 42 again with no change (idempotent).
- Focused cross-account run (same 16 files as r4): **209 passed, 0 skipped** (26 s).
- Full fast suite: **867 passed, 8 skipped, 0 failed** (403 s). The skips are the same 8 optional assets.
- `ruff check .`: clean.

Independent cross-check: Account 1's final review (`a889cd8`, `docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md`) reports 862
passed, 12 skipped, 0 failed on its own merged tree (A1 `f9d8f11`, A2 `d979df1`, A3 `62414bf`). That is a separate run on an
earlier A3 head, so its count is not comparable to the totals above.

## Run r4 — 2026-11-13, base `b54c952` (superseded by r4b only because the base gained a docs commit)

| Input | Branch | SHA |
|---|---|---|
| base | `claude/hopeful-allen-luubcy` (PR #5's base; contains PR #4) | `b54c952` |
| Account 1 | `work/account-1-cloud-platform` (already in the base via PR #4) | `f9d8f1131788d5848594bb78c61bff3b86d400e1` |
| Account 2 | `work/account-2-cinematic-engine` | `d979df1e390abc48e79a7e6645229968a5b94107` |
| Account 3 | `work/account-3-intelligence-growth` | `0d9d1de81ccfc380a771c67f4faad98eb8bb8401` (all A3 code; later A3 commits change docs and generated state only) |

A3 `0d9d1de` contains the previous head `62414bf`, the base merge `5034c8d` and three follow-ups:
- `06def55`: checkpoint-resume integration test;
- `f34d9b8`: data origin in growth recommendation evidence;
- `0d9d1de`: handoff length.

Local branch `integration/contracts-v1-r4` (not pushed), in order:
1. start from the base `b54c952` (A1 `f9d8f11` is an ancestor: checked with `git merge-base --is-ancestor`);
2. merge A2 `d979df1` (`899ab8d`);
3. merge A3 `0d9d1de` (`be76eda`);
4. `apply.sh` (`68a3c68`).

Conflict resolutions:
| Step | Files | Resolution |
|---|---|---|
| A2 | `tasks.yaml`, `DECISIONS.md`, `MASTER_ROADMAP.md` | both sides kept; tasks.yaml has 68 unique ids |
| A2 | `NEXUS_STATE.json`, `NEXUS_HANDOFF.md`, `fast_tests.json` (generated) | base (Account 1) side, as in r2/r3 |
| A3 | `nexus/workflows.py` | both sides kept: Account 2's `export` and `clip-process` handlers, then the Account 3 job-kind imports. Handler list identical to r3 |
| A3 | `NEXUS_STATE.json`, `NEXUS_HANDOFF.md`, `fast_tests.json` (generated) | Account 3 side |

Results on `68a3c68`:
- `apply.sh`: **42 passed**. A second run changed nothing and passed again (42).
- Focused cross-account run: **209 passed, 0 skipped** (24 s). This is a separate run from the full suite. It covers:
  - batch_llm (A3 + A1 tests);
  - QualityReport → publishing;
  - the release gate;
  - owner validation;
  - zero spend;
  - handoff;
  - A2 cross-account;
  - the three contract suites;
  - publishing;
  - analytics;
  - research degradation;
  - growth.
- Full fast suite (`python -m pytest tests/fast -rs`): **867 passed, 8 skipped, 0 failed** (411 s). The skips are
  optional assets not installed here: opentimelineio (1), local TTS/Piper voices (4), SigLIP ONNX files (3).
- `ruff check .`: clean.

**Finding fixed during r4.** The first full run on the merged tree had **1 failure**:
`test_state.py::test_task_registry_and_state_file_are_valid`. Merging Account 2's handoff section brought
`NEXUS_HANDOFF.md` to 1,561 words, over the repository's 1,500-word limit. The fix shortened Account 3's own handoff
section and the blocker texts Account 3 wrote, in A3 `0d9d1de`. The test was not changed. The merged handoff is now
1,461 words.

### batch_llm on Account 1's real dispatcher (simulated execution)
`tests/fast/test_intel_batch_llm_integration.py` (5 tests) uses Account 1's own `FakeKaggleCLI`. The real worker script,
embedded bundle, checksum verification and checkpoints all run. The `echo` engine loads **no model**, so the results
are labelled simulated. Each test passes on `68a3c68`:

| Check | Evidence |
|---|---|
| One model per job, actual prompt items | `params.model == "Qwen/Qwen3-4B"`, `params.items` is the Bench V3 prompt list; validates as Account 1's pydantic `ProductionJobManifest` |
| Input/output manifests, artifact checksums | all 12 items come back in order in `batch_llm_results.*`; `sha256` of the artifact file equals the recorded checksum; 3 parts |
| Idempotency | resubmitting the same manifest returns the same job, `created == False` |
| Checkpoint handling | worker lost after part 0. Attempt 1 is `lost` after running part 0; attempt 2 skips part 0 and runs parts 1–2; checkpoints `[1, 2, 3]`; all 6 items present |
| Kaggle-only routing | a dispatcher without Kaggle finds no candidate; the reason names kaggle |
| No persistent endpoint | summary `persistent_endpoint: false`, `simulated: true`, `engine: echo` |
| Unsupported shapes | old shape (`params.models`, items as a count, `runtime: ollama`) refused by `validate_manifest` with the field named; Account 1's worker also fails it |
| Error handling | per-item errors and limits: Account 1's `test_cloud_batch_llm.py` (8 tests, in the same focused run) |

A real-model batch_llm run with Account 3's items has **not** been done. It is optional and must not change the owner's
Kaggle configuration.

### QualityReport, disclosure and language on Account 2's real code
- `test_intel_a2_publishing_integration.py` (3 tests):
  - a report from Account 2's own `qa.approve()` with status BLOCKED cannot be queued;
  - PASSED allows a **private** upload only while `human_approval` is `PENDING …`;
  - findings are kept in `release_check`;
  - `classify.manifest()` gives `ai_generated_imagery`;
  - `contracts.artifact()` URIs resolve.
- `test_intel_release_gate.py` (44 tests, **0 skipped** here; the Account 2 validator test skips on the A3 branch alone)
  covers:
  - checksum mismatch, a missing output, a file changed after enqueue, critical findings, the human-review gate;
  - the seven disclosure categories; unknown provenance becomes AI; the owner's YouTube realism decision;
  - TikTok `is_aigc` and Instagram `is_ai_generated` in the requests; Facebook refuses AI content; Snapchat is manual;
  - ar-EG and ar-Gulf separated, review ids that do not exist refused, no platform advertised as live-verified.

### Not run (honest gaps)
- **Windows**: no Windows machine or VM is available to this agent, and GitHub Actions jobs do not start (see below).
  Neither the Windows fast suite nor the clean-machine Windows e2e has run on r4 or on the final A3 head.
- **GitHub CI**: every job on every account's branch ends in 1–5 s without running a step. Examples:
  - A3 `211b97f` and `62414bf`;
  - Account 1's runs;
  - Account 2's PR #7 runs at 11:17–11:25 UTC.

  That is a repository- or account-level Actions problem. No billing or settings were changed, and no empty commit or
  re-run was made.
- Real renders, real uploads and real-model batch inference.

## Run r3 — 2026-11-13, earlier heads (superseded by r4: A1 was `919f158`, A3 was `f1835b5`)

| Input | Branch | SHA |
|---|---|---|
| base | `main` (origin) | `ec5af11` |
| Account 1 | `work/account-1-cloud-platform` | `919f158` |
| Account 2 | `work/account-2-cinematic-engine` | `d979df1` |
| Account 3 | `work/account-3-intelligence-growth` | `f1835b5e564099aed507ad2b5cb0c79cd8832bcc` (all A3 code; later A3 commits are docs/state only) |

Local branch `integration/contracts-v1-r3b` (not pushed), in order:
1. merge A1 (`4e6c393`);
2. merge A2 (`301eabc`);
3. merge A3 (`2755d79`);
4. `apply.sh` (`402b15e`).

Conflict resolutions:
- `tasks.yaml`, `DECISIONS.md`, `MASTER_ROADMAP.md` and `AGENT_WORK_LOG.md`: both sides kept. tasks.yaml ends with 87
  unique ids.
- `api.py` and `workflows.py`: both sides kept.
- `index.html`: the A3 script position with A1's deep-link `go(...)`.
- Generated files (`NEXUS_STATE.json`, `NEXUS_HANDOFF.md`, `fast_tests.json`): the Account 3 side. The A2 step took
  Account 1's side, as in r2.

`apply.sh`: **42 passed**. A second run changed nothing and passed again (42).

Cross-account checks on the merged tree (132 passed in one focused run, separate from the full suite):
- **batch_llm** (`test_intel_batch_llm_integration.py`, 4 tests): an A3 manifest validates against Account 1's pydantic
  `ProductionJobManifest`. It runs on Account 1's **real dispatcher and worker script** (fake Kaggle CLI, `echo` engine,
  so no model) and returns all 12 items with checksum-verified artifacts in 3 parts. The summary is labelled
  `simulated: true` with no persistent endpoint. Resubmission returns the same job. The old shape is refused with a
  named cause and fails on the worker.
- **QualityReport → publishing** (`test_intel_a2_publishing_integration.py`, 3 tests):
  - Account 2's own `qa.approve()` reports go into the release gate. BLOCKED cannot be queued; PASSED allows private
    only while human approval is PENDING.
  - Findings are preserved.
  - Account 2's `classify.manifest()` gives the `ai_generated_imagery` disclosure.
  - Account 2's `contracts.artifact()` URI resolves.
- **Shared contracts, Strategy Handoff, GeneratedMediaManifest validation:** `test_contracts_*`, `test_intel_handoff.py`
  and `test_a2_cross_account.py`.
- **Disclosure, owner approval and private-only validation:** `test_intel_release_gate.py`,
  `test_intel_owner_validation.py`, `test_zero_spend_fail_closed.py`.
- **Renderer dispatch:** covered by Account 2's `test_a2_cross_account.py`. It submits Account 2 render tasks through
  Account 1's dispatcher; Account 2 owns that path.

Full fast suite (`python -m pytest tests/fast -rs`) on `402b15e`: **850 passed, 8 skipped, 0 failed** (362 s).
Skips are optional assets not installed here: opentimelineio (1), local TTS/Piper voices (4), SigLIP ONNX files (3).
Later A3 commit `211b97f` changes one analytics function (an unresolvable time zone gives `None`; it was a Windows CI
failure). It is covered by the A3 branch's own tests.

Not run on the merged tree: Windows, the clean-machine e2e, and real renders. CI runs those only after the owner
approves and pushes the merge. The Account 3 branch's own CI (Linux, Windows, Windows clean-machine e2e) is linked from
PR #5.
An earlier r3 attempt on A3 `7572acb` (batch_llm fix only): full suite 779 passed, 8 skipped, 0 failed.

## Run r2 — 2026-11-13, earlier heads (superseded by r3)

| Input | Branch | SHA |
|---|---|---|
| base | `main` (origin) | `ec5af11` |
| Account 1 | `work/account-1-cloud-platform` | `2ac7f96223b1ac1dff802e99aed90bb10de42724` |
| Account 2 | `work/account-2-cinematic-engine` | `cc836de63b33e2daa03ac158422e4888a9976f32` |
| Account 3 | `work/account-3-intelligence-growth` | `5976a49` + the `apply.sh` fixed in `bfd3a94` |

Local branch `integration/contracts-v1-r2` (not pushed): `80e427a` merge A1 → `337b122` merge A2 → `4508750` merge A3
→ `9cc3dc2` apply.sh → `a5d4cab` handoff-file resolution.

Merge conflicts and how they were resolved:
| Step | File | Resolution |
|---|---|---|
| A2 | `nexus/registry/tasks.yaml` | both task lists kept (additive); 63 unique ids |
| A2 | `docs/results/fast_tests.json` | either side; it is regenerated by the next full run |
| A3 | `nexus/api.py`, `nexus/workflows.py` | both sides kept (A1 security middleware + A3 router; A2 export/remote tasks + A3 job kinds) |
| A3 | `nexus/ui/index.html` | A3 position (after `intelligence.js`) with A1's deep-link `go(...)` expression |
| A3 | `tasks.yaml`, `docs/MASTER_ROADMAP.md`, `docs/AGENT_WORK_LOG.md` | both sides kept; 78 unique task ids |
| A3 | `NEXUS_STATE.json`, `docs/results/fast_tests.json` | Account 3 side (generated files) |
| A3 | `NEXUS_HANDOFF.md` | Account 3 side. "Keep both" exceeded the 1,500-word limit that `test_state.py` enforces; this generated memory file is rewritten by `nexus checkpoint` after the merge anyway |

`apply.sh` on the merged tree: Account 2 already ships `nexus/contracts/media.py` and `tests/fast/test_contracts_media.py`
(its `a443238`), so only Account 1's `nexus/contracts.py` → `nexus/contracts/cloud.py` and
`tests/fast/test_contracts.py` → `tests/fast/test_contracts_cloud.py` were moved. The script version recorded for run r1
assumed an add/add test conflict and failed here (missing `test_contracts_cloud.py`); it was fixed (`bfd3a94`) and rerun.

Results:
- **Focused** (apply.sh: shared + cloud + media contract tests + Strategy Handoff): **41 passed**; a second run of
  `apply.sh` changed nothing and passed again (idempotent).
- **Full fast suite** (`python -m pytest tests/fast -rs`) on `a5d4cab`: **746 passed, 8 skipped, 0 failed** (393 s).
  An earlier full run on `9cc3dc2` had 745 passed, 1 failed: `test_state.py`, caused by the handoff-file conflict
  resolution above, not by any account's code.
- Skips (optional assets not installed here): opentimelineio (1), local TTS/Piper voices (4), SigLIP ONNX files (3).
- Not run on the merged tree: Windows, the clean-machine end-to-end job, renders with real models. These run in CI only
  after the owner approves and the merge is pushed.

Reproduce (any machine with the project's venv):
```bash
git switch -c integration/try origin/main
git merge --no-ff origin/work/account-1-cloud-platform
git merge --no-ff origin/work/account-2-cinematic-engine     # resolve as in the table
git merge --no-ff origin/work/account-3-intelligence-growth  # resolve as in the table
bash integration/contracts_v1/apply.sh && git commit -m "Apply shared contracts layout"
python -m pytest tests/fast -q
```

## Run r1 — 2026-11-12, earlier heads (superseded)

Base `9fc6cfb` (Account 3) + Account 1 `76d49db` + Account 2 `ff4d215` + Account 3 `af4b957`; local branch
`integration/contracts-v1` at `983a0a0`.
- **Full fast suite: 538 passed, 3 skipped, 3 failed** before the Account 3 fix. This was never a fully green suite.
- **Focused** (contracts + handoff + readiness + secret scan + Account 2 integration tests): 53 passed after the fix.

| Failing test | Owner | Cause | Status on the current heads |
|---|---|---|---|
| `test_no_secrets::test_tracked_files_contain_no_tokens` | Account 3 | fake `ya29.` literal in a test | fixed in `af4b957` (built at runtime) |
| `test_cloud_compute::test_safe_production_mode_gpu_jobs_wait_queued...` | Account 1 | the test read `["job"]["id"]`; `submit()` returns `job_id` | fixed by Account 1 in `633cc8b`; passes in r2 |
| `test_cloud_security::test_cookie_session_and_csrf` | Account 1 | httpx 0.28 kept the valid cookie next to the forged one | fixed by Account 1 in `2ac7f96` ("deterministic cookie/CSRF test"); passes in r2 |

The Strategy Handoff's EditorialPlan and RenderJobManifest are validated by **Account 2's own validator**
(`nexus.contracts.media.validate`) on the merged tree, not by an Account 3 copy.
