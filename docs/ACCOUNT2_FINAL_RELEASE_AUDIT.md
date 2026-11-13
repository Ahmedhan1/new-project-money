# Account 2 final release audit (cinematic engine, PR #6)

Classifications: REAL-VERIFIED · AUTOMATED-TESTED · VISUALLY-INSPECTED · HUMAN-REVIEW-PENDING · GPU-READY-UNVERIFIED ·
BLOCKED-BY-OWNER · NOT-IMPLEMENTED. Machine: shared 4-vCPU cloud container, no GPU. Details and numbers:
`docs/ACCOUNT2_QUALITY_REPORT.md`; evidence files under `docs/results/`.

## 1. HEADs
- Start of this pass: `9929512` (PR #6 head at the previous report).
- Final: the PR #6 head that contains this file (the SHA is given in the PR description; the last code commit before
  the documentation commits is `f697355`). 22+ commits pushed in this pass, no force-push, no history rewrite, nothing
  merged into `main`.

## 2. The four workstreams
| Agent | Branch head | Merged into Account 2 | Status |
|---|---|---|---|
| A renderer/editing | `4a82c82` | `26fa1c1` | AUTOMATED-TESTED + REAL-VERIFIED (bench, Demo 3) |
| B director/visual | `f7c21a6` | `c9719dd` (+ API/UI integration `026edb7`, fixes `f418e37`, `96c36fe`) | AUTOMATED-TESTED + REAL-VERIFIED (Demos 1, 2, 4) |
| C audio | `83bd24f` | `e8658c1` (+ renderer integration `0a7110c`) | AUTOMATED-TESTED; HUMAN-REVIEW-PENDING |
| D generation/VFX | `2023ff6` | `c1abdba` | AUTOMATED-TESTED; video GPU-READY-UNVERIFIED |
All four branch heads are ancestors of the Account 2 head (`git merge-base --is-ancestor`); no unmerged commits remain.
Agent branches are kept (not deleted).

## 3. Renderer acceptance - AUTOMATED-TESTED, REAL-VERIFIED
Timeline editing, multi-track compositing, undo/redo and version restore (API + clean-clone E2E), EDL CMX3600 + OTIO
round trips, 58 transitions, keyframes (opacity/rotation/position/scale), keyframed masks, chroma key, freeze frames,
split screen, credits, speed ramps, optical-flow slow motion, piece cache, resumable render, export verification.
Cache keys: burn spec per piece + audio key incl. gain/pan/ducking mode/speech spans (tests in `test_a2_*.py`).
Bench: one-clip edit 35 s -> ~4 s, caption edit 34 s -> ~2 s, warm 0.4 s. Fixed in this pass: Windows file-URI paths
(OTIO, generated media), LUT test names, refused cache renames on Windows (`f697355`), resume-test race.

## 4. Audio acceptance - AUTOMATED-TESTED; HUMAN-REVIEW-PENDING
Dialogue chain v2 (denoise, de-hum, EQ, compression, speaker balance, room tone, loudness), de-reverb (WPE) and
de-essing available per material, clipping repair, per-item gain/pan, keyframe music ducking, SFX/ambience, J/L cuts,
A/V sync (drift bug found and fixed), loudness/true-peak QA. Five benchmark files to -16 LUFS +-0.1, TP <= -1.9 dBTP.
Real-render regression test kept (`test_c2_audio_qa.py`, `test_a2_integration.py`). **No person has listened.**

## 5. Director acceptance (Agent B) - AUTOMATED-TESTED, REAL-VERIFIED, VISUALLY-INSPECTED
14 editorial profiles; explainable plan (id, reason, confidence per decision); overrides through API and browser
(`ui_director_check.json`); story order with standalone openings and finished endings; duplicate takes; speaker turns;
visual B-roll with licence + named-subject gates; repeated-visual penalties; reference-style extraction and transfer.
B-roll on the labelled set: precision strict 10/18 = 56% (was 5/18), lenient 17/18 = 94%; recall strict 10/28 = 36%
(was 5/28), lenient 17/28 = 61%. Demo 1 set: 8/15 on-topic without the subject gate, 12/12 with it.

## 6. Demos 1, 2, 3
- Demo 1 cinematic documentary: 228.3 s, 1920x1080, 30 fps, H.264/AAC, from NASA "Mars - The Search Begins" (public
  domain); director plan with 3 chapters and 242 decisions, 9 B-roll inserts, grade, push-ins, crossfades, dialogue
  clean-up, ducked music, captions, credits; render with QA 396 s; -14.17 LUFS, TP -1.91 dBTP; QA PASSED_AUTOMATED_CHECKS;
  quality 91.6 "needs_work" (visual 84.7 < 85: soft, blocky, dark 1971 film). REAL-VERIFIED + VISUALLY-INSPECTED;
  storytelling HUMAN-REVIEW-PENDING. Three earlier runs were rejected after inspection (too short, off-topic B-roll,
  bordered-inset cutaways + title card).
- Demo 2 shorts: 8 distinct 1080x1920 shorts (12-52 s), 8/8 QA passed, max source overlap 0.6%, 8/8 finished endings,
  face-centred crop, captions in the YouTube Shorts safe zone, -14 LUFS. Weak titles on some (K-23). REAL-VERIFIED +
  VISUALLY-INSPECTED.
- Demo 3 advanced editing: 53 s, QA passed, frames inspected. REAL-VERIFIED + VISUALLY-INSPECTED.

## 7. Demo 4 reference style - REAL-VERIFIED (measured both sides)
Reference: NASA explainer (public domain). New piece: the KSC walk-and-talk (different source, its own words and
pictures). 22 metrics measured on reference and on the rendered output with the same analyser. Reproduced: interrupt
spacing (4.75 vs 5.18 s), on-screen text share (0.59 vs 0.63), end card, hard/gradual cut mix, contrast, cutaway rate;
colour moved toward the reference (colourfulness 67 -> 48 vs 18). Not reproduced: long takes (source limit, K-24), the
11 s silent intro, music accents, speaking rate. No frame, word or sound of the reference is used.

## 8. Generation - GPU-READY-UNVERIFIED
Wan 2.1 T2V-1.3B (8.19 GB) and Wan 2.2 TI2V-5B (24 GB): valid jobs, deferred RESOURCE_EXHAUSTED here; Account 1 worker
task `a2_wan_video` (GPU required, refuses CPU). The classifier labels a forged zoomed still `animated-still`.
Image generation on CPU (BK-SDM-Tiny, ~30 s/image) REAL-VERIFIED. No paid compute was used.

## 9. Enhancement / VFX - AUTOMATED-TESTED
Upscale (lanczos, Real-ESRGAN - invents detail, never recovers it), interpolation, denoise, deblock, film look, tone
map, background replacement, tracking, particles, depth of field; benchmarks in `ACCOUNT2_MODEL_BENCHMARKS.md`.

## 10. Tests
- Account 2 branch, full fast suite: **346 passed, 6 skipped, 0 failed** (the 6 skips include the cross-account tests,
  which need Accounts 1/3, and model-gated tests).
- Three-account local merge: 687 passed, 4 failed -> after fixes only Account 1's cookie/CSRF test fails (it fails on
  Account 1's own branch too).
- `ruff check .` clean.

## 11. Linux and Windows CI
Fixed in this pass: SciPy, onnxruntime and opencv-python-headless declared in `requirements-ci.txt` (runtime imports);
Windows-only bugs found once collection worked. Green on Linux and Windows at `a52a24a` (run 148), `012a436` (run 152)
and at the code-final head `010d29f`: CI run 43400716347 (ubuntu-latest success, windows-latest success). Release
check (clean clone of `010d29f`, fresh NEXUS_HOME, ruff + fast suite + hygiene): PASS (`docs/results/release_check.json`;
its first attempt failed only the two launcher tests because the container had < 2 GB free disk).

## 12. Windows E2E
The "Windows end-to-end (clean machine)" workflow passed on every push in this pass (runs 76-90; at `010d29f`: run
43400716242), and its script now also covers the workspace page, split + undo/redo, draft preview, final render and verified export.

## 13. Clean clone
Fresh clone + fresh venv from `requirements-ci.txt` + empty non-ASCII `NEXUS_HOME`: app starts, 13/13 E2E steps pass
(`docs/results/a2_clean_clone_e2e.json`). Earlier fresh-clone fast-suite run: 307 passed, 5 skipped.

## 14. Audio human review
`docs/results/a2_audio_review/REVIEW.md` + `review.json`: 6 dialogue before/after pairs and a programme-mix sample with
measurements and automated artefact notes. **PENDING OWNER REVIEW** (audio regenerated locally; media is not in Git).

## 15. Owner actions
1. Review PR #6 and the demos (storytelling, pacing, cutaways, titles) - HUMAN-REVIEW-PENDING.
2. Listen to the audio package and fill the verdicts - BLOCKED-BY-OWNER.
3. Run NEXUS_SETUP / NEXUS_START on your own Windows PC (K-1) - BLOCKED-BY-OWNER.
4. Authorise a free GPU worker for one Wan run (A2-GPU) - BLOCKED-BY-OWNER; no billing.
5. Decide the merge order (recommended A1 -> A2 -> A3, then `integration/contracts_v1/apply.sh`).

## 16. Shared contracts (Accounts 1/2/3)
Account 2 schemas in `nexus/contracts/media.py`; `apply.sh` from Account 3 works unchanged; Account 2 code reads
`contracts.media.VERSION`. Verified on the merged tree: Account 3 EditorialPlan/RenderJobManifest validate with Account
2's validator; the approved script reaches the renderer verbatim (Account 3's test); Account 1's worker renders Account 2
steps and stores the master with a matching SHA-256; GPU jobs wait without a GPU; QualityReport and
GeneratedMediaManifest validate through `validate_any`; an Account 2 ArtifactRef enters Account 3 publishing and stays
non-public until owner approval. Evidence: `docs/results/a2_three_account_check.json`.

## 17. Known defects and owners
| Defect | Owner |
|---|---|
| Publishing ignores QualityReport; no AI-content disclosure mapping (K-26) | Account 3 |
| Cookie/CSRF test fails with httpx 0.28 | Account 1 |
| B-roll can still be plausible-but-wrong without a named subject (K-18) | Account 2 |
| Shorts titles sometimes fragments (K-23) | Account 2 |
| Reference transfer bounded by the source's own shot lengths (K-24); text share is a heuristic (K-25) | Account 2 |
| Demo 1 visual score under the bar because of the source film | source material |
| Nobody has listened to processed audio (K-27) | owner |
| Generated video never run (A2-GPU) | owner (GPU authorisation) |
| Launchers never run on the owner's PC (K-1) | owner |

## Verdict
**READY FOR REVIEW** - every code-level acceptance criterion passes on `010d29f` (Linux CI, Windows CI, Windows E2E,
release check, clean-clone E2E, demos 1-4 rendered and inspected, Demo 5 honestly GPU-READY-UNVERIFIED). This is not a
production-ready claim: human creative review, audio listening, the owner's Windows PC and a real GPU run are pending.
