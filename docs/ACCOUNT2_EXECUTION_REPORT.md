# Account 2 execution report (cinematic production engine)

Scope: editing/rendering, director and visual intelligence, audio post, generation/VFX/enhancement, performance and the
editor workspace. Branch `work/account-2-cinematic-engine`, PR #6 (not merged into `main`). Starting point: the verified
release candidate `c93847d` (PR #3). Final audit: `docs/ACCOUNT2_FINAL_RELEASE_AUDIT.md`; measured results:
`docs/ACCOUNT2_QUALITY_REPORT.md`.

## How the work was run
- Four parallel workstreams in separate git worktrees and branches, one integrator:
  A renderer (`work/account-2-agent-a-renderer`), B director/visual (`...-agent-b-director`), C audio (`...-agent-c-audio`),
  D generation/VFX (`...-agent-d-generation`). Heavy renders were serialised with a shared lock on a 4-vCPU container.
- Integration order A -> C -> D -> B; every merge followed by the full fast suite and integration fixes in the
  integrator's own commits (renderer audio wiring, API/UI for B, Windows fixes).
- Contracts first (`nexus/contracts/media.py`, `docs/ACCOUNT2_CONTRACTS.md`), then a 9-item licensed benchmark set
  (NASA + Prelinger public domain, provenance per file), then features, then demos on that media.

## What exists now (by area)
| Area | Delivered | Evidence |
|---|---|---|
| Renderer | multi-track layers, keyframes, masks, chroma, perspective, freeze, split, credits, speed ramps, slow motion, 58 transitions, piece cache, resume, EDL/OTIO, export verification | `a2_render_bench.json`, Demo 3 |
| Director | 14 profiles, explainable decisions with overrides, coherence rules, reference-style transfer, shorts factory | `a2_director/`, `a2_reference/`, `a2_shorts/`, Demos 1, 2, 4 |
| Visual intelligence | SigLIP (Apache-2.0) embeddings on CPU, scene labels, shot types, duplicates; visual B-roll with licence + subject gates | `a2_visual_bench.json`, `a2_broll/` |
| Audio | dialogue chain v2, per-item gain/pan, keyframe ducking, sound design, sync/loudness QA | `a2_audio_acceptance.json`, `a2_audio_review/` |
| Generation/VFX | model catalogue with licence classes, consent-gated downloads, remote job manifests, CPU image generation, classification guard, enhancement + VFX processors | `ACCOUNT2_MEDIA_TOOL_CATALOG.md`, `ACCOUNT2_MODEL_BENCHMARKS.md`, Demo 5 |
| Workspace | Cinematic Workspace page, models page, director controls (profiles, per-decision corrections, reference, visual install, B-roll subject) - every control drives a real API call | `ui_director_check.json`, clean-clone E2E |
| Cross-account | shared contracts layout, Account 1 worker tasks, Account 3 handoff gaps documented | `a2_three_account_check.json`, handoff docs |

## Defects found by looking at real output (and fixed)
Draft renders blocked by QA geometry; captions past the programme end; AAC drift at hard cuts; demo mask region;
director cutaways drawn as bordered insets; title cards used as cutaways; Earth/Moon B-roll under Mars narration (subject
gate); Windows file-URI paths, LUT file names and refused cache renames; a project endpoint 500 on partial analyses;
a resume-test race under load. Each has a regression test.

## Not done / not claimable
Generated video (no GPU: GPU-READY-UNVERIFIED), human listening and creative review of every demo, the owner's own
Windows PC, Account 3's publishing gate and disclosure (their module), Account 1's cookie test (their module).

## Documents
`ACCOUNT2_CONTRACTS.md`, `ACCOUNT2_MEDIA_TOOL_CATALOG.md`, `ACCOUNT2_MODEL_BENCHMARKS.md`, `ACCOUNT2_RENDERER_ARCHITECTURE.md`,
`ACCOUNT2_EDITING_GUIDE.md`, `ACCOUNT2_QUALITY_REPORT.md`, `ACCOUNT2_HANDOFF_TO_ACCOUNT1.md`, `ACCOUNT2_HANDOFF_TO_ACCOUNT3.md`,
`ACCOUNT2_FINAL_RELEASE_AUDIT.md`.
