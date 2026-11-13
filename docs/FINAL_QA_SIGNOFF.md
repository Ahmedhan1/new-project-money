# Final QA, performance and release validation (independent QA: Account 3)

Date 2026-11-13. Machine-readable summary: `docs/results/final_qa_summary.json`. Defects: `docs/results/final_qa/defects.json`.
Status words: **PASS** (tested and verified with evidence), **TESTED** (automated checks pass; live or human validation
remains), **BLOCKED** (needs an owner credential, resource or approval), **DEFERRED** (accepted for later), **FAIL** (a
reproducible defect remains). Independent results are listed separately and never added together.

## Verdict: NOT READY FOR RELEASE

The integrated product works end to end on Linux:
- a fresh clone installs and starts;
- the real-Chromium UI loads every page;
- a SIGKILL mid-job recovers;
- path, symlink and upload attacks are refused;
- 953 of 962 fast tests pass, and the other 9 are skipped (optional assets only).

The release is still not ready, for two reasons:
- **Four major defects are open.** Each has a reproduction and a fix recommendation:
  - QA-D1: anonymous traffic locks the owner out;
  - QA-D2: a damaged database stops startup with a traceback;
  - QA-D3: the Asset Library takes 17.7 s per request;
  - QA-D4: a full disk during upload leaves a truncated file and an orphan project.
- **Windows is unverified.** GitHub Actions starts no job, so no CI run, Windows E2E or Windows installer exists for
  this candidate.

## 1. Inputs and environment
| Input | SHA |
|---|---|
| Integration candidate `integration/final-release` (Account 1) | `9752d919858abdd434501bd13d3a8274a9f41cca` |
| Release candidate `claude/hopeful-allen-luubcy` | `813967e524ea7e7372653e1b081fb2b92f14b2de` |
| Account 3 PR #5 in the candidate | `0c0fd7d`; QA fix after the candidate: `80e04fc` |
| Account 2 PR #6 / PR #7 in the candidate | `d979df1` / `387b9da` (PR #7's later head `516d94d` is docs only) |

Environment: Linux 6.18 x86_64 cloud container, 4 vCPU, 15 GiB RAM, Python 3.13.18, FFmpeg 6.1.1, Node 22.23.4,
Chromium through Node Playwright. No GPU and no Windows. Tests ran in an untouched detached worktree of the candidate,
each with a fresh temporary `NEXUS_HOME`. The QA branch's own changes were tested in a separate run.

## 2. Test programme (candidate `9752d91`)
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
| QA-D1 | major | open | A1 | anonymous requests count as failed logins; the owner's valid token then gets 429 for the window |
| QA-D2 | major | open | A1 | a damaged `nexus.db` gives a traceback at startup and in doctor, with no recovery hint (`nexus restore` works) |
| QA-D3 | major (perf) | open, fix measured | A2 module / A1 | `/api/library` takes 17.7 s per request (the YAML is parsed 183× per call); the cache fix gives 0.004 s with identical output |
| QA-D4 | major (data) | open | A1 | a full disk during upload gives HTTP 500, an orphan project and a truncated copy |
| QA-D5 | minor | open | A1/A2 UI | a late async page render overwrites the page the user moved to |
| QA-D6 | minor | open | A1 | logout does not revoke a copied session cookie (valid up to 12 h) |
| QA-D7 | minor | open | A1 | a legacy cookie holding the raw token is accepted with no expiry |
| QA-D8 | minor | open | A1 | a failed job's error carries a traceback and absolute paths; undecodable input is retried 3×; the stage stays `running` |
| QA-D9 | minor | open | A1 | without FFmpeg, doctor still reports "motion graphics (Pillow + FFmpeg)" as OK |
| QA-D10 | minor (test) | open | A1/A2 | the E2E export step reads keys export() never returns, so it passes when they are None |
| QA-D11 | minor | **fixed** `80e04fc` (PR #5) | A3 | Creator References printed `[object Object]` |
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

## 6. Wan 2.2 TI2V-5B: independent review of Account 1's evidence (TESTED)
| Check | Finding |
|---|---|
| Model identity | `Wan-AI/Wan2.2-TI2V-5B-Diffusers` at revision `b8fff7315c768468a5333511427288870b2e9635` in every record. The worker selects it by explicit key, with no fallback path; the 1.3B entry is separate and unpinned (QA-W4). No 14B model and no paid provider appear |
| Real GPU run | 3 Kaggle jobs (`rjob_3ea658eeb829`, `rjob_c1ad61c90bb8`, `rjob_e03072b99fd5`). Accelerator "Tesla T4 x2"; the environment probe shows 2 × T4 (sm 7.5, 14.56 GiB), torch 2.11 / CUDA 13.0, diffusers 0.35.2. Not echo, not CPU, not a still |
| Per-GPU use | recorded separately. GPU0 peak 12.6 GB alloc, 13.0 GB smi; GPU1 9.9 / 14.2 GB (smoke); sharded run 7.9 / 12.8 GB. Utilisation max 100 % on both, mean 13–38 % (download and load dominate a 7–8 min session) |
| Output validity | ffprobe: h264, 672×384, 24 fps, 25 frames, 1.04 s; local SHA-256 equals the worker's; no static pairs, not black, not flat. Tiled vs reference decode: 44.55 dB PSNR |
| Motion and consistency | the contact sheets show a stable scene with a moving subject (T2V) and small coherent motion (I2V). Two different motion numbers are recorded for the same clip (14.67 vs 5.78; QA-W3) |
| Prompt adherence | weak at this size: the "fox" is an unrecognisable dark shape and the camera does not track; the I2V cat does not turn or blink (QA-W2) |
| Not verified | the 720p acceptance runs. The report still has `__WAN_VERDICT__`, `__WAN_720_ROWS__` and `__GATE__` placeholders. The MP4 files are not in the repository, so QA could not re-hash or watch them (QA-W1) |

Verdict: real generation of valid moving video on the owner's Kaggle T4×2 is **evidenced at smoke size**. The 720p
acceptance and the speed/quality comparison are **not done**.

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

## 10. For Account 1 (integration owner)
Merge `work/final-qa-performance` (QLT-1 improvements, rubric, regression tests, QA scripts and evidence) and PR #5
`80e04fc`. Then fix or accept the items below, and re-run the full suite and `scripts/qa/break_probe.py`:
- QA-D1, QA-D2 and QA-D4 (owner: Account 1);
- QA-D3 (Account 2, through Account 1);
- QA-D5 to QA-D10.

The release needs, in addition:
- Actions restored by the owner;
- the Windows plan in §8 executed;
- the Wan 720p acceptance and the report placeholders resolved;
- the owner's approval.
