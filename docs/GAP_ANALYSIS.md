# Gap Analysis — product vision vs. what actually exists (2026-11-11)

Status words: **verified** = works end-to-end with test evidence · **partial** = runs, with stated limits ·
**thin** = a module exists but the capability is far from the vision · **missing** = nothing usable ·
**blocked** = needs something only the user can grant. Evidence = test + result files in `docs/results/`.
Acceptance criteria and next actions for every gap are in `nexus/registry/tasks.yaml` (task ids in the last column).

| # | Domain | What exists (evidence) | Real weakness / what is missing | Status | P | Task |
|---|---|---|---|---|---|---|
| 1 | Video editing | Analysis, silence/repeat removal, proposal → timeline → render, undo/redo/branches, QA auto-fix (Test A, `A_edit.json`) | No multi-track compositing, no keyframes, no B-roll insertion; UI editor is form-based, not a timeline | partial | 2 | QLT-3 |
| 2 | Video remixing | 3 differently structured versions from several sources, LLM plan (Test B) | Selection is transcript-driven only; no visual similarity or shot-quality scoring | partial | 3 | — |
| 3 | Video generation from zero | Research → script → review → stills + narration + music → render (Test D, see TEST_REPORT) | Visuals are stock stills/cards with Ken Burns — a narrated slideshow, not generated video; 4B LLM is fragile | thin | 1 | QLT-1, GEN-1, GEN-2 |
| 4 | Image generation | none (licensed stock search + gradient cards) | No generative model at all | missing | 2 | GEN-1 |
| 5 | Storyboarding | Storyboard contact sheet from chosen stills | Made after asset selection — not a planning tool; no shot types/camera notes | thin | 3 | QLT-1 |
| 6 | 2D animation | Manim scenes from specs (Test E) | Template scenes only (chart/timeline/kinetic); no character animation | partial | 3 | — |
| 7 | 3D animation | Blender headless from spec (Test E, `E_3d.png`) | One template scene; CPU Cycles is slow (3 s/frame default) | thin | 3 | GEN-2 |
| 8 | Motion graphics | Pillow→FFmpeg titles, lower thirds, charts, kinetic text (Test E) | Small preset set; repetitive across videos | partial | 3 | QLT-2 |
| 9 | VFX | none (UI page says "not built") | Keying, masks, background removal, compositing | missing | 3 | GEN-3 |
| 10 | Audio production | Denoise/EQ/compress, two-pass loudness to −14 LUFS, ducking (Test A: −14.35 LUFS, TP −1.86) | No source separation (dubbing mutes original music/effects), no SFX library | partial | 3 | QLT-5 |
| 11 | Music & sound design | Procedural synth pads (`audio/music.py` says "usable bed, not composed music") | No real music, no sound effects | thin | 3 | QLT-5 |
| 12 | Voiceover | Piper TTS en/ar/de, per-language pacing | Voices are intelligible but synthetic; Arabic weakest; no emotion/prosody control | partial | 3 | QLT-5 |
| 13 | Dubbing | Translate → TTS → fit to slot (speed/stretch) → ASR back-check (Test C) | No lip-sync, no voice matching, original M&E lost | partial | 3 | QLT-5 |
| 14 | Translation | MADLAD-400 + independent LLM back-translation review, number/foreign-word checks (Test C) | Only en/ar/de wired; quality judged by automated proxies, no native review | partial | 3 | — |
| 15 | Subtitles | Cue generation, timing fix, reading-speed/overlap QA, burn-in, RTL shaping | No styled/animated captions; SRT/VTT sidecar export not verified | partial | 3 | QLT-2 |
| 16 | Video enhancement | none exposed (FFmpeg vidstab installed) | Stabilise, upscale, interpolate, denoise controls | missing | 3 | GEN-3 |
| 17 | Film production | `series.film_plan`: scenes, characters, shot list (Test F — **not yet passed**) | A plan document, not a produced film | thin | 2 | TST-F |
| 18 | Episodic series | Bible, season plan, continuity-checked episodes, shared style (Test F — **not yet passed**) | Episodes are narrated stills; character consistency is textual only | thin | 2 | TST-F, GEN-1 |
| 19 | Documentary | Chronological remix variant (Test B) | No archive research, interview structuring or citation overlays | thin | 3 | QLT-2 |
| 20 | Content summaries | Highlights goal in editor (Test A/B) | No dedicated "summarise this long video" workflow with chaptering | thin | 3 | — |
| 21 | AI research | Wikipedia retrieval with URLs; grounding + number-traceability checks | One source type; top hit can be off-topic (D: "Trained immunity" outranked "Vaccine"); no claim-level citations | thin | 2 | RES-1, QLT-1 |
| 22 | Content calendars | `factory.schedule`, calendar docs in DB | Filled manually; no trend input | thin | 3 | RES-1 |
| 23 | Multi-agent orchestration | Staged workflows with independent review steps; durable job ledger (tests/fast) | "Agents" are pipeline stages, not autonomous planners — adequate for now | partial | 4 | JOB-2 |
| 24 | Five videos/day capacity | Capacity planner from measured throughput, per-profile, masters ≠ language versions (Test G) | Measured on a cloud VM, not the user's PC; stage timings only now being recorded | partial | 2 | CAP-1 |
| 25 | Multi-channel management | Channel records in DB, UI page "partial" | No per-channel branding/queues | thin | 4 | — |
| 26 | Publishing preparation | Per-platform packages: cuts, titles, descriptions, thumbnails (Test G) | Metadata quality unreviewed | partial | 3 | — |
| 27 | Platform integrations | none (deliberate: packages only) | Upload APIs need the user's OAuth apps | blocked | 3 | PUB-1 |
| 28 | Analytics | none | Needs platform API access | blocked | 3 | ANA-1 |
| 29 | Monetization | Manual revenue ledger | No real data, no intelligence | blocked | 3 | ANA-1 |
| 30 | GitHub integration | Repo is source of truth; CI; checkpoint/push engine; read-only catalog re-verification | No Releases yet; CI first run to be confirmed | partial | 2 | CI-1 |
| 31 | Free resource discovery | 113-entry research catalog + `evolution_check` (read-only) | No automated benchmarking of candidates | partial | 4 | EVO-1 |
| 32 | Federated compute | Worker interface, routing, failover test with a **simulated** GPU (Test I) | No real remote provider connected; Kaggle/Colab are adapters only | thin | 3 | GEN-2 |
| 33 | Persistent storage | Storage adapter (local + rclone), provider registry, caps (tests/fast) | Only local destination exercised for real | tested | 1 | STO-1, STO-3 |
| 34 | Backup & recovery | Verified encrypted snapshots, restore + relocation + reference check, drill (tests/fast D, E) | Manual trigger; no off-machine copy yet | verified (local) | 1 | STO-3, STO-4 |
| 35 | Windows startup | Launchers + setup script; Python launcher tested incl. Windows CI | `.bat`/`.ps1` never run on real Windows; some deps unproven there | implemented | 1 | WIN-1 |
| 36 | User experience | Single-page UI: 20+ pages, job table with stages/resume, storage page | No onboarding, no preview player/timeline, errors are terse | partial | 2 | UX-1 |
| 37 | Quality assurance | 5-stage QA (pre-production, assets, editing, technical, language) with targeted auto-fix | Creative quality is not measured (rubric missing); no before/after artefacts | partial | 1 | QLT-1, QLT-4, TST-R |
| 38 | Security | Local-only bind, cost guard on all egress, secret/media scan at checkpoint + CI, encrypted remote backups | No API auth token, no dependency audit | partial | 2 | SEC-1 |
| 39 | Performance | Segment render cache, model unload, stage timing metrics | No proxy previews, no batch scheduling; numbers are from a 4-core VM | partial | 2 | CAP-1 |
| 40 | Controlled self-improvement | Read-only upstream re-verification; nothing self-modifies | No benchmark → proposal loop | thin | 4 | EVO-1 |

## Reading this honestly
- **Strong**: editing pipeline, technical QA, localization checks, zero-spend enforcement, and (new) recoverability.
- **The central product gap** is visual generation (#3, #4, #7, #18): today "create from zero" is a narrated slideshow of
  licensed stills. Closing it needs local image generation benchmarked on CPU (GEN-1) and an authorised free GPU for
  video models (GEN-2 — blocked on a user account).
- **Second gap**: creative quality is unmeasured (#37). Test D passed its automated gates while producing an 18-second,
  one-sentence-per-scene video with zero images — fixed this session, but it shows the gates check form, not substance.
- **Blocked on the user**: remote backup account (STO-3), free GPU account (GEN-2), platform OAuth (PUB-1/ANA-1), a run on
  real Windows (WIN-1).
