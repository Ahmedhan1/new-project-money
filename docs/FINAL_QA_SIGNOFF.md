# Final QA, performance and release validation (independent QA: Account 3)

Date 2026-11-13. **Tested SHAs.**
- Round 1: candidate `9752d91`.
- Round 2: `4a3dd6f`.
- **Round 3 (current): `a2096606d0908fc7e040eecd8030e6f47d049b7f`**, which is `integration/final-release` with PR #9,
  the PR #5 follow-up (`80e04fc`) and Account 1's QA-D1 to D10 fixes.

Every finding names the SHA it was reproduced on. A defect is marked fixed only when it is reproduced as gone on a
tested SHA, never from code inspection. Machine-readable summary: `docs/results/final_qa_summary.json`. Defects:
`docs/results/final_qa/defects.json`. Round 3 evidence: `docs/results/final_qa/round3/`.

Status words: **PASS** (tested and verified with evidence), **TESTED** (automated checks pass; live or human validation
remains), **BLOCKED** (needs an owner credential, resource or approval), **DEFERRED** (accepted for later), **FAIL** (a
reproducible defect remains). Independent results are listed separately and never added together.

## Verdict (round 3, `a209660`): NOT READY FOR RELEASE — no open FAIL on Linux; release BLOCKED on owner actions

**All ten QA defects (QA-D1 to D10) are independently verified as fixed on `a209660`**, and QA-D11 (A3) is integrated.
Nothing open remains a FAIL on Linux. The release still cannot be signed off as production-ready:
- **Windows (BLOCKED).** The deliverable is a Windows single-file product, but `NEXUS_VIDEO_AI_Setup.exe` has not been
  built or installed, and GitHub Actions starts no job, so there is no CI, no Windows E2E and no update/rollback test.
- **Wan 2.2 720p I2V (not verified).** There is no successful run after the VAE change; 720p T2V is evidenced.
- **Owner-only validation (BLOCKED)**, listed in §9b: provider OAuth, native-language review, real analytics, the audio
  and creative review, and merge approval.

### Round 3 results on `a209660` (local Linux; separate runs, never added together)
| Check | Result | Evidence (`round3/`) |
|---|---|---|
| `ruff check .` | clean | `summary_a209660.txt` |
| Full fast suite | **981 passed, 9 skipped, 0 failed** of 990 (579.0 s). The same 9 skips as before (optional assets). It includes Account 1's new QA regression tests | junit sha256 `50036371…175d` |
| Cross-account and contract set (15 files, including the QA-D11 test) | **157 passed** | junit `1e58d1b1…5c71` |
| `apply.sh` twice | 42 passed, then 42 passed, 0 files changed | — |
| Break-test (`scripts/qa/break_probe.py`) | **36 passed, 0 failed**. QA-D1 (owner token accepted after 11 anonymous requests), D6 (logout revokes a copied cookie), D7 (raw-token cookie refused) and D8 (plain error, one run, stage `failed`) all pass. The earlier protections still hold | `break_probe_a209660.json` |
| QA-D2, damaged `nexus.db` | `nexus doctor` and `nexus serve` print "database … is damaged … `python -m nexus restore <new folder>`" and exit 3, with no traceback. Minor leftover: `serve` prints "NEXUS running at …" before it stops | `startup_checks_a209660.txt` |
| QA-D3, Asset Library | payload 0.26 s median (was 17.6 to 17.8 s). A registry edit is seen on the next call; 16/16 identical results from concurrent callers; a caller mutating its result does not affect the next caller | `assetlib_fix_check_a209660.json` |
| QA-D4, full disk (40 MB tmpfs, 18 MB upload ×2) | HTTP 507 with a plain message, no file written, no project created, server healthy. The space check reserves about 287 MB for an 18 MB upload, so a write failure *after* the check passes could not be produced live; that path is covered by Account 1's unit tests only | — |
| QA-D5, navigation race | Storage → Home shows Home | `nav_race_a209660.txt` |
| QA-D9, doctor without FFmpeg | motion graphics and RNNoise reported `[BAD] … needs ffmpeg` | `startup_checks_a209660.txt` |
| QA-D10, E2E export assertion | the clean-machine E2E on Linux passes **18/18**, and the export step now requires `verify_ok: True` | `clean_machine_e2e_a209660.json` |
| QA-D11, `a3refs` | renders without `[object Object]` | `ui_all_pages_a209660.json` |
| UI, all 31 pages, real Chromium (3 s per page) | 0 page errors, 0 console errors, 0 HTTP 5xx, no blank page. At 1.2 s per page, run while the full suite loaded the CPU, three pages were still loading; at 3 s all render | `ui_all_pages_a209660.json` |
| QA-W4 | `wan21-t2v-1.3b` pinned (by Account 1; the code was not re-run on a GPU) | — |

### Earlier rounds (history)
In round 1 (`9752d91`) and round 2 (`4a3dd6f`), the same programme found and reproduced QA-D1 to D10. Those results
and the defect descriptions follow below, unchanged, as the record of what was fixed.

## 1. Inputs and environment
| Input | SHA |
|---|---|
| Integration candidate, round 1 | `9752d919858abdd434501bd13d3a8274a9f41cca` |
| **Integration head, round 2** (current base of PR #9) | **`4a3dd6f8e30c6766cddb18ec88afe1230be7b846`** |
| Release candidate `claude/hopeful-allen-luubcy` | `813967e524ea7e7372653e1b081fb2b92f14b2de` |
| Account 3 PR #5 in the candidate | `0c0fd7d`; QA fix after the candidate: `80e04fc` |
| Account 2 PR #6 / PR #7 in the candidate | `d979df1` / `387b9da` (PR #7's later head `516d94d` is docs only) |

Environment: Linux 6.18 x86_64 cloud container, 4 vCPU, 15 GiB RAM, Python 3.13.18, FFmpeg 6.1.1, Node 22.23.4,
Chromium through Node Playwright. No GPU and no Windows. Tests ran in an untouched detached worktree of the candidate,
each with a fresh temporary `NEXUS_HOME`. The QA branch's own changes were tested in a separate run.

## 2. Test programme

### Round 2, on `4a3dd6f`
Each line is a separate run; the counts are never added together. Evidence is in `docs/results/final_qa/round2/`.
| Check | Result |
|---|---|
| `ruff check .` | clean |
| Full fast suite | **953 passed, 9 skipped, 0 failed** of 962 (579.7 s), the same skips as round 1. junit sha256 `c1f16b90…1dc4` |
| Cross-account and contract set | **156 passed** (junit `7b482f19…111e`) |
| `apply.sh` twice | 42 passed, then 42 passed, 0 files changed |
| Break-test | **30 passed, 5 failed**: the same five as round 1 (QA-D1, D6, D7, D8 ×2) |
| UI, all 31 pages, real Chromium | 0 page errors, 0 console errors, 0 HTTP 5xx. `a3refs` still prints `[object Object]`: the fix `80e04fc` is **not yet in the integration branch**. `assets` and `dubbing` were blank at 1.2 s |
| Navigation race (QA-D5) | reproduced: Storage → Home still shows "Storage & Backup" |
| `/api/library` (QA-D3) | 17.8 s |
| QA-D2 (damaged DB), QA-D4 (disk full) | not re-run. None of `nexus/db.py`, `api.py`, `project.py`, `__main__.py`, `cloud/security.py`, `jobs.py`, `ui/index.html` or `assetlib` changed between `9752d91` and `4a3dd6f` (0 files). Status stays open as reproduced in round 1 |

QA branch after merging `4a3dd6f` (`572eba3` plus these docs), separate run: **961 passed, 9 skipped, 0 failed** (578.6 s).
junit sha256 `a927f170…968f`.

### Round 1, on candidate `9752d91`
| Check | Result | Evidence |
|---|---|---|
| `ruff check .` | clean | `raw/` |
| Full fast suite | **953 passed, 9 skipped, 0 failed** of 962 (588.7 s). Skips: opentimelineio 1, local voices 4, SigLIP ONNX 3, one archive test superseded on this tree 1 | junit sha256 `0b68257f…44bf4`, `raw/candidate_fast_summary.txt` |
| Per area (slices of that run) | security/auth/API 154; jobs/recovery 31; cloud compute/storage 62; renderer/director/audio/VFX/export 268 (8 skipped); assets/archive/disk 61 (1 skipped); Account 3 283; contracts 35; other 68. All 0 failed | `fast_suite_areas.json` |
| Cross-account and contract tests (separate run) | **156 passed, 0 skipped** (14 files) | junit sha256 `b9061ab6…d270` |
| `integration/contracts_v1/apply.sh` twice | 42 passed, then 42 passed, 0 code files changed: idempotent | `raw/apply_sh_runs.txt` |
| Browser: `tests/e2e/test_studio_browser.py` | 1 passed (real Chromium, 15 Studio sections, scheduler) | — |
| Browser: every main-UI page (QA script) | 31 pages, 0 console errors, 0 HTTP 5xx; 1 page error (QA-D5); `a3refs` printed `[object Object]` (QA-D11, fixed); `assets` blank for 17.7 s (QA-D3) | `ui/ui_all_pages_candidate.json` |
| Account 2 UI check script | **not run**: it needs the Python Playwright package, which this environment does not have (Account 1 reports 11/11) | — |
| Fresh clone → new venv → `python -m nexus serve` | install 47 s (40 packages); cold start 1.10 s; clean-machine E2E script on Linux **18/18 PASS**: non-ASCII paths, Arabic/Chinese captions, director, render, export, assets, disk, archive, backup | `raw/fresh_clone_e2e_report.json` |
| Production profile | unconfigured `production-check` exits 1 (6 required gates fail closed). The configured 12/12 READY is Account 1's evidence and was not re-run | — |
| Windows E2E / installer | **not run** (no Windows machine; Actions starts no job) | — |

QA branch (candidate plus the QA changes in §5), separate run: **961 passed, 9 skipped, 0 failed** of 970 (576.2 s). That is the candidate's 953 plus 8 new QLT-1 tests, with the same 9 skips. junit sha256 `37a946ac…b199`; ruff clean.

## 3. Break-testing (live server, disposable home and test token)
`scripts/qa/break_probe.py`: **30 passed, 5 failed** (`break_probe.json`). Each probe group used its own loopback address,
so one group's lock-out could not hide another group's results.
- **Refused as expected:**
  - anonymous API access, system stop and `/docs`;
  - wrong, tampered and expired (TTL + 2 min) sessions;
  - CSRF without the header, and a foreign `Origin`;
  - five path-traversal forms, a symlink escape and project-id traversal (all 404);
  - `.exe` uploads (415), and a `../` filename (reduced to its base name, nothing written outside);
  - a 9 MiB JSON body (413);
  - brute force (429 after 10 failures).
- **Failed:** QA-D1 (major), QA-D6, QA-D7 and QA-D8 (two probes).
- **Crash:** a SIGKILL during analysis of a 60 s video, then a restart. The first 200 came after 1.1 s, and the job was
  marked "worker lost at stage 'analyze'", resumed, and finished `done`.
- **Corrupt video:** the job ended `failed` with the cause "moov atom not found" (it was not stuck), but see QA-D8.
- **Missing FFmpeg:** `nexus doctor` says `[BAD] ffmpeg not found on PATH -> run NEXUS_SETUP`, but see QA-D9.
- **Damaged database:** the server import and `nexus doctor` crash with a traceback (QA-D2). `nexus restore <new dir>`
  recovers the home: 3 projects, media references OK, and doctor reports OK on the restored home.
- **Disk exhaustion:** home on a 40 MB tmpfs. The second 18 MB upload returns HTTP 500 and leaves an orphan project
  with a truncated 4.6 MB file (QA-D4). `/api/health` stays 200.
- **Not tested:** an interrupted model download, an update rollback, and uninstall. These need the Windows installer or
  multi-GB downloads.

## 4. Defects
Full reproductions, files and fixes are in `docs/results/final_qa/defects.json`.

| Id | Severity | Status | Owner | Summary |
|---|---|---|---|---|
| QA-D1 | major |  **fixed, verified `a209660`**  | A1 | anonymous requests count as failed logins; the owner's valid token then gets 429 for the window |
| QA-D2 | major |  **fixed, verified `a209660`**  | A1 | a damaged `nexus.db` gives a traceback at startup and in doctor, with no recovery hint (`nexus restore` works) |
| QA-D3 | major (perf) |  **fixed, verified `a209660`** (0.26 s)  | A2 module / A1 | `/api/library` takes 17.7 s per request (the YAML is parsed 183× per call); the cache fix gives 0.004 s with identical output |
| QA-D4 | major (data) |  **fixed, verified `a209660`** (refusal path)  | A1 | a full disk during upload gives HTTP 500, an orphan project and a truncated copy |
| QA-D5 | minor |  **fixed, verified `a209660`**  | A1/A2 UI | a late async page render overwrites the page the user moved to |
| QA-D6 | minor |  **fixed, verified `a209660`**  | A1 | logout does not revoke a copied session cookie (valid up to 12 h) |
| QA-D7 | minor |  **fixed, verified `a209660`**  | A1 | a legacy cookie holding the raw token is accepted with no expiry |
| QA-D8 | minor |  **fixed, verified `a209660`**  | A1 | a failed job's error carries a traceback and absolute paths; undecodable input is retried 3×; the stage stays `running` |
| QA-D9 | minor |  **fixed, verified `a209660`**  | A1 | without FFmpeg, doctor still reports "motion graphics (Pillow + FFmpeg)" as OK |
| QA-D10 | minor (test) |  **fixed, verified `a209660`**  | A1/A2 | the E2E export step reads keys export() never returns, so it passes when they are None |
| QA-D11 | minor | **fixed, verified `a209660`** (`80e04fc` integrated) | A3 | Creator References printed `[object Object]` |
| QA-D12 | major (content) | **fixed** (QA branch) | A3 | a series bible with two records on one line lost the second location (the real Test F bible) |
| QA-D13 | major (content) | **fixed** (QA branch) | A3 | QLT-1: fictional-name image queries, repeated searches, off-bible locations only reported |
| QA-D14 | minor | deferred | A3 | non-Latin research ranking stays in the search engine's order; the measured variant did not help |
| QA-O1 | optimisation | deferred | A2 | a warm re-render spends about 90 % of its time re-running QA on identical output |

## 5. Quality and performance work (QA branch `work/final-qa-performance`)
All changes are in Account 3-scope files: `nexus/series.py`, the `produce` image step in `nexus/create.py`, the new
`nexus/intelligence/quality/rubric.py`, tests and `scripts/qa/`. No Account 1 infrastructure file and no
`nexus/assetlib`, `nexus/api.py` or `nexus/workflows.py` file was edited.

| Item (QLT-1) | Baseline (candidate code) | After (QA branch) | Evidence |
|---|---|---|---|
| Bible parsing, real Test F output | 1 location parsed | 2 | `qlt1_measure.json` |
| Image searches across the 2 Test F episodes | 8 (one per scene) | 2 (one per location; repeats reuse the image, marked `reuse_key`) | same |
| Scene queries containing fictional names | 8 of 8 | 6 of 8. The remaining 6 are "Lunar Reef", whose look text the model cut off; its name is the only query left | same |
| Live Commons, Seabrook Bay | "Seabrook Bay" → Seabrook, Texas shops (0 relevant) | "sprawling estuary fog draped" → ladder rung "fog draped" (1 relevant) | sample of **1 location**: indicative only |
| Off-bible locations | reported (minor check) | repaired to the best-matching bible location; each repair recorded on the scene; the writer's mistake stays visible as its own check | `test_qa_qlt1.py` |
| Research ranking by topic | already done for Latin script (K-4) | non-Latin variant measured, **not adopted** (QA-D14) | `qlt1_ranking_experiment.json` |
| Scored review rubric | none | `rubric.score()`: hook, story arc, scene relevance, repeated visuals, audio balance. Missing evidence is `None`, never guessed. Fixed thresholds (0.7 overall, 0.4 floor, at least 3 known dimensions). On 4 real Demo 10 runs it flags the run with a title-card fallback (`needs_review`) and passes the others | `rubric_demo10.json` |

Wiring the rubric into the approval step belongs in Account 2's `qa.approve()`. The recommendation is to call
`rubric.gate()` there. Account 2's minor "duplicate stills" check should accept images marked `reused_for`.

Performance (`perf_summary.json`, `perf_assetlib.json`):

| Area | Measured |
|---|---|
| Asset Library | **17.7 s** baseline; proposed fix **0.004 s** with identical output (open: QA-D3) |
| Archive browser, disk dashboard | 5 ms, 6 ms |
| Tools, dashboard, projects, system status | 230 ms, 4 ms, 2 ms, 3 ms |
| Cold start | 1.10 s |
| Restart after SIGKILL | 1.11 s |
| 9.6 s 720p render | cold 9.4 s; warm (5/5 segments from cache) 6.5 s, of which 0.4 s is render and ~6 s is QA and the quality score (QA-O1). One warm run took 60 s; 3 further runs did not reproduce it |

## 6. Wan 2.2 TI2V-5B: independent review of Account 1's evidence (as of `a209660`)
| Check | Finding |
|---|---|
| Model identity | `Wan-AI/Wan2.2-TI2V-5B-Diffusers` at revision `b8fff7315c768468a5333511427288870b2e9635` in every successful record. The worker selects it by explicit key, with no fallback path; the 1.3B entry is separate and unpinned (QA-W4). No 14B model and no paid provider appear |
| Real GPU | Kaggle T4 × 2 on every job (sm 7.5, 14.56 GiB each), with per-GPU memory and utilisation recorded. Not echo, not CPU, not a still |
| **720p T2V** (`rjob_620150232fa9`) | **succeeded**: 1280×704, 121 frames at 24 fps, 30 steps, h264; the local SHA-256 equals the worker's (`26c351fd…2e1f`). Generation 2,835 s, decode 1,264 s (own tiling), job 4,455 s (74 min). Peak allocated GPU0 12.9 GB / GPU1 9.7 GB; mean utilisation 34 % / 61 %. No static pairs, not black, not flat. Contact sheet: a recognisable red fox trotting through snow in a sunlit birch forest, with the camera moving alongside, temporally consistent and on prompt |
| **720p I2V** | **not verified.** `rjob_5b6d09efb6d1` failed with CUDA OOM on GPU0. `i2v720.json` is that same failed attempt. The VAE-placement change in `4a3dd6f` has **no successful post-fix run yet**, so there is no I2V MP4 to inspect |
| **I2V after the VAE fix, smoke size** (`rjob_a3611e323cb4`, in `a209660`) | **succeeded** at 672×384, 25 frames. This is a new job: different timings and hash (`77736937…`), and the memory profile changed as the fix intends (GPU0 peak 5.2 GB vs 12.8 GB before). It used the same seed as the pre-fix run, so the frame statistics are nearly identical (decoded motion 1.369 vs 1.366). **Motion is very low**: mean decoded frame difference 1.37, against 3.97 for the 720p T2V clip, and the cat does not turn or blink as prompted. The record replaced the pre-fix `smoke_i2v.json` (the earlier job `rjob_c1ad61c90bb8` is still in git history) |
| Smoke runs (672×384, 25 f) | T2V and I2V succeeded. Prompt adherence was weak at that size (QA-W2) |
| MP4 files | not in the repository. `tasks.yaml` cites `media/t2v_1280x704_121f_24fps.mp4`, which does not exist in the tree. QA could inspect only the contact sheets and the ffprobe and hash records, not the video itself (QA-W1) |
| Motion metrics | two different numbers per clip: worker 13.74 vs local decoded 3.97 at 720p (QA-W3) |

Verdict: **720p T2V is evidenced as real, valid, moving and on-prompt** (from records and the contact sheet).
**I2V works mechanically after the fix at smoke size, but with very little motion. 720p I2V is still not verified**: it
needs a successful post-fix 720p run, and its MP4 inspected for real motion. Speed: about 74 min of T4×2 time per 5 s 720p
clip.

## 7. Real providers (evidence review; QA has no owner credentials and re-ran nothing live)
**Google Drive Daily Archive** (`docs/results/a2_storage/daily_archive_live.json`, 12/12): TESTED.
- It used the existing `drive_a` connection (drive_a, drive_b and drive_c healthy by provider-reported quota) and a
  disposable 20.85 MB generated test pattern, in an isolated temp copy of the home that was deleted afterwards.
- Interrupted upload: the drop was injected client-side, which is fault injection on a real provider. The upload
  resumed at byte 8,388,608 in the same session.
- Integrity:
  - a provider SHA-256 check;
  - an independent byte-identical download, with a matching Drive md5;
  - registry kind `master` with retention `archive`;
  - local deletion only after a fresh re-verification;
  - retrieve gives an identical SHA-256.
- No owner media: only test files were created (3 left in `NEXUS/*/prj_fc2f8d061c`).
- drive_c is at 13.5 of 15 GB (QA-W5).

**Kaggle**: the accelerator actually assigned to the owner's account is recorded per job (T4 × 2). The Kaggle
configuration was not changed. Mock evidence (`FakeKaggleCLI`, the echo engine, Drive fakes in the fast suite) is kept
apart from these live records.

## 8. Windows single-file product (BLOCKED)
`NEXUS_VIDEO_AI_Setup.exe` has not been built. There is no Windows builder, and Actions starts no job. Nothing about
installation, the shortcut, first launch, update, uninstall or rollback can be claimed.

Exact verification plan, once the owner restores Actions:
1. Run `windows-installer.yml` (workflow_dispatch) on the final SHA. Record the artifact's SHA-256 and
   `build_manifest.json`.
2. On a clean Windows 11 VM with no Python on PATH (`where python` finds nothing), run the installer per-user without
   admin. Check the desktop and Start-menu shortcuts.
3. Launch from the shortcut. One browser tab should open at `http://127.0.0.1:8765` within 30 s. Check that the System &
   Updates page shows the API, worker and scheduler running.
4. Settings: set the token. Check the vault file exists under `%LOCALAPPDATA%\NEXUS\data` with no token in plain text,
   and that Kaggle and Drive show connection status.
5. Run `scripts/acceptance/windows_e2e.py` against it (with the QA-D10 fix): create, edit, render, export, assets, disk,
   archive and backup.
6. Back up, then run the restore drill from the UI.
7. Update to a newer build. Check that the data is kept and that a SHA-256 mismatch is refused. Roll back.
8. Uninstall: the program is removed and the data is kept unless the box is ticked. Reinstall, and check the data is
   picked up.

Pass means every step is recorded with screenshots and exit codes.

## 9. Account 3 deliverable (PR #5)
PR #5 is ready for Account 1's integration. It holds the A3 code in the candidate (`0c0fd7d`) plus one QA fix
(`80e04fc`, QA-D11).
- Preserved: the `batch_llm` contract, the release gate (QualityReport, checksum, critical findings, human approval),
  AI disclosure, and private-by-default publishing.
- Language status: native review is BLOCKED (no reviewer). Every non-English output stays a draft.
- Owner-only items:
  - the YouTube OAuth client and one private upload;
  - the YouTube Data API key;
  - native reviewers;
  - an analytics export.

## 9b. Owner-only validation (BLOCKED / OWNER ACTION; never inferred from local Linux tests)
| Item | Status |
|---|---|
| YouTube OAuth client and one private upload (`owner_validation`) | BLOCKED, owner action |
| TikTok, Instagram, Facebook developer apps | BLOCKED, owner action |
| YouTube Data API key (competition research) | BLOCKED, owner action |
| Real analytics import (one owner export) | BLOCKED, owner action |
| Native-language review: ar-EG, ar-Gulf, hi, zh | BLOCKED, owner action (every output stays a draft) |
| Audio listening review and creative review (a human watches the renders; the QLT-1 rubric does not replace this) | BLOCKED, owner action |
| GitHub Actions restored; Windows installer built and installed | BLOCKED, owner action |
| Merge approvals (PR #8 into the RC, PR #3 into `main`) | owner decision. QA does not merge |

## 9c. Re-test protocol once Account 1 reports fixes
A finding closes only when it is reproduced as gone on the new exact integration SHA:
1. Re-run `ruff`, the full fast suite and the cross-account set, and run `apply.sh` twice.
2. `scripts/qa/break_probe.py` against a live server: QA-D1, D6, D7 and D8 must pass.
3. QA-D2: corrupt `nexus.db` (WAL checkpointed first). Expect a plain message pointing to `nexus restore`, and doctor
   showing `[BAD] database`.
4. QA-D4: put the home on a 40 MB tmpfs and upload 18 MB twice. Expect HTTP 507, no partial file and no orphan project.
5. QA-D3: time `/api/library` (target under 0.5 s). Edit the registry and check the next call reflects it. Call it
   from 16 threads and check every result is identical. `scripts/qa/bench_assetlib.py` already does all three for the
   proposed fix: 17.6 s → 0.004 s, the reload is seen after an edit, and 16/16 results are identical.
6. QA-D5: `scripts/qa/ui_nav_race.js` (Storage → Home must show Home), and `ui_all_pages.js` over every page.
7. QA-D11: `a3refs` must show hook text once PR #5 `80e04fc` is integrated. Keep `tests/fast/test_intel_ui_pattern_library.py`.

## 10. For Account 1 (integration owner)
Round 3 (`a209660`): QA-D1 to D10 are verified fixed and QA-D11 is integrated. To merge from this branch: the round 2
and round 3 report, the evidence under `docs/results/final_qa/round2|round3/`, and the updated QA scripts (break probe
reads stage runs; the UI check takes `WAIT_MS`; the Asset Library check covers invalidation, threads and mutation).
None of it touches product code.

Still open before a production release:
- Owner: restore GitHub Actions, then build the Windows installer and run the §8 plan.
- Account 1 / Account 2: a successful post-fix **720p** I2V run with real motion, and its MP4 kept for inspection.
- Cosmetic: `nexus serve` should not print "NEXUS running at …" when the database check then stops it.
- Owner: the items in §9b, and merge approval.
