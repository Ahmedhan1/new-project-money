# Account 2 quality report (measured)

Machine for every number below: shared, noisy 4-vCPU cloud container, 15.7 GB RAM, **no GPU**; other agents' jobs ran
alongside, so timings vary 10-40 % between runs. Media: public-domain NASA (images.nasa.gov) and Prelinger/Internet
Archive items, provenance per file in `$NEXUS_HOME/benchmarks/<id>/provenance.json` (`scripts/benchmarks/media.yaml`).
Labels: REAL-VERIFIED (ran on real media, output checked), AUTOMATED-TESTED (fast tests on rendered output),
VISUALLY-INSPECTED (frames looked at), HUMAN-REVIEW-PENDING, GPU-READY-UNVERIFIED, NOT-IMPLEMENTED.

## 1. Renderer and editing (Agent A) - REAL-VERIFIED + AUTOMATED-TESTED
`docs/results/a2_render_bench.json` (64.5 s, 16-clip timeline, 1280x720, captions, crossfades, cutaways, music):
| Scenario | Before (c1e2d90) | After | Note |
|---|---|---|---|
| cold render | 111 s (51 s on a quiet run) | 37.5 s (quiet run) | piece pipeline, no full-programme re-encode |
| one-clip edit | ~35 s | ~4 s | only that clip's segment + pieces |
| caption edit | ~34 s | ~2 s | only pieces the cue touches |
| warm (no change) | 79 s | 0.4 s | everything cached |
- A/V drift found by Agent C (+5/+44/+82 ms at hard cuts, AAC priming) fixed in 93f851b; every clip within one frame
  (`tests/fast/test_c2_audio_qa.py`, real rendered audio).
- Cache keys cover every output-affecting setting tested: per-piece burn spec (cues, overlays, layers, geometry, encoder),
  audio key (segment audio digests, items incl. gain/pan, music + ducking mode + speech spans, target LUFS, codec).
- Multi-track layers, opacity/rotation/position keyframes, tracked masks, chroma, perspective, freeze, split layouts,
  credits, speed ramps, optical-flow slow motion, 58 transitions, resumable render (checkpoint per stage), EDL CMX3600 and
  OTIO round trips: pixel/frame tests in `tests/fast/test_a2_*.py`, `test_fx.py`.
- Windows: OTIO file-URI and LUT file-name bugs fixed after CI exposed them (a52a24a).

## 2. Audio (Agent C) - REAL-VERIFIED measurements, HUMAN-REVIEW-PENDING
`docs/results/a2_audio_acceptance.json` (whole files, dialogue chain v2, target -16 LUFS):
| Source | Material | LUFS before -> after | True peak after | Speech-band SNR before -> after |
|---|---|---|---|---|
| interview | interview | -27.2 -> -16.1 | -1.93 dBTP | 27.8 -> 33.0 dB |
| panel | studio | -21.4 -> -16.0 | -1.88 | 43.6 -> 40.8 |
| podcast | interview | -22.2 -> -16.0 | -1.88 | 37.5 -> 36.0 |
| walktalk | outdoor | -31.3 -> -16.1 | -1.91 | 22.0 -> 34.0 |
| ad (1948) | archival | -19.0 -> -16.0 | -1.97 | 22.5 -> 29.7 |
- DeepFilterNet evaluated and rejected (licence/runtime, `docs/results/a2_denoise_compare.json`); noisereduce kept for
  archival material. Per-item gain/pan and keyframe ducking measured on a real render: left-right > 20 dB for a hard-panned
  SFX, music 4+ dB lower under speech than in pauses (`tests/fast/test_a2_integration.py`).
- Listening package: `docs/results/a2_audio_review/` (6 before/after dialogue pairs + programme mix, REVIEW.md).
  **Status: PENDING OWNER REVIEW. No person has listened to any processed audio.**

## 3. Director and visual intelligence (Agent B) - REAL-VERIFIED + AUTOMATED-TESTED
- Visual model: SigLIP base patch16-224 int8 ONNX (Apache-2.0), 150-240 ms/frame on 2 threads, 685 MB peak RSS
  (`docs/results/a2_visual_bench.json`).
- B-roll on a hand-labelled set (28 narration sentences = opportunities; 18 inserts each; one non-blind labeller):
| Method | Precision strict (R/inserts) | Precision lenient (R+P) | Recall strict (R/opportunities) | Recall lenient |
|---|---|---|---|---|
| lexical (before) | 5/18 = 28% | 10/18 = 56% | 5/28 = 18% | 10/28 = 36% |
| visual (SigLIP) | 10/18 = 56% | 17/18 = 94% | 10/28 = 36% | 17/28 = 61% |
- Extended with Demo 1 (Mars narration, NASA library + the film's own shots), labelled by subject agreement:
  without a named-subject gate 8/15 cutaways on-topic (Earth storm, Moon, Apollo, a title card got in); with
  `broll_subject=["Mars"]` 12/12 on-topic (`docs/results/a2_broll/demo1_subject_gate.json`). This is subject agreement,
  not "shows exactly what is said".
- Director coherence on six media types (`docs/results/a2_director/`): standalone openings, finished endings, duplicate
  takes collapsed, nothing padded; every decision has an id, reason and confidence; manual overrides verified through
  the API and the browser (`docs/results/a2_demos/ui_director_check.json`: a Drop correction removed the sentence, 8 -> 7).
- Defects found by frame inspection and fixed: in-film cutaways rendered as a bordered inset window (now full-frame);
  title-card shots used as cutaways (now excluded in both the director and the B-roll library).

## 4. Generation, enhancement, VFX (Agent D)
- Image generation on CPU: BK-SDM-Tiny (OpenRAIL-M) ~30 s per 512x512 image - REAL-VERIFIED.
- Text-to-video / image-to-video: Wan 2.1 T2V-1.3B (8.19 GB VRAM), Wan 2.2 TI2V-5B (24 GB): contract-valid jobs, deferred
  with RESOURCE_EXHAUSTED here; Account 1 worker task `a2_wan_video` refuses CPU - **GPU-READY-UNVERIFIED**.
- Classification guard: a forged "Wan output" made from a zoomed still is labelled animated-still (residual 2.1 vs motion
  27.4); `generated-video` requires model output, independent motion and a named GPU - AUTOMATED-TESTED.
- Enhancement/VFX (upscale lanczos/Real-ESRGAN, interpolation, denoise, deblock, film look, tone map, background
  replacement, tracking, particles, depth of field): pixel tests in `tests/fast/test_d2_*.py`; benchmarks in
  `docs/ACCOUNT2_MODEL_BENCHMARKS.md`. Real-ESRGAN invents plausible detail; it never recovers lost detail.

## 5. Demos
| Demo | Result | Evidence |
|---|---|---|
| 1 Cinematic documentary | 228 s 1080p, QA passed, quality 91.6 (visual 84.7: source film limits), frames inspected; see section 6 | `docs/results/a2_demos/demo1_*` |
| 2 Shorts factory | 8 distinct 9:16 shorts (12-52 s) from the ISS interview; 8/8 QA passed; max pairwise source overlap 0.6%; 8/8 end on a finished sentence; face-centred crop; captions inside the YouTube Shorts safe zone (margin 710 px, need 672); -13.9..-14.5 LUFS, TP <= -1.68 dBTP; quality 90.1-92.7. Weak: some titles are fragments ("One of the things that caves") | `demo2_shorts.json`, `demo2_contact.jpg` |
| 3 Advanced editing | 53 s, 11 capabilities in one timeline, QA passed, frames inspected (mask region corrected after inspection) | `demo3_advanced.json`, `demo3_contact.jpg` |
| 4 Reference style | explainer structure applied to the walk-and-talk; output re-measured with the same analyser (22 metrics). Reproduced: pattern-interrupt spacing (4.75 vs 5.18 s), on-screen text share (0.585 vs 0.626), end card, hard/gradual mix (0.82/0.18 vs 0.60/0.20), contrast (0.193 vs 0.209), cutaway rate (0.4 vs 0.67/min); colour moved toward the reference (colourfulness 67 -> 48 vs 17.6). Not reproduced: long takes (16.9 s median vs 6.9 s - the source's own median shot is 8.2 s), silent 11 s intro (first speech 0.1 s), music accents, speaking rate (the source speaker talks at 175 wpm) | `demo4_reference.json`, `demo4_contact.jpg` |
| 5 AI video generation | GPU-READY-UNVERIFIED (see section 4) | `demo5_generation.json` |

## 6. Demo 1 (filled from docs/results/a2_demos/demo1_documentary.json)
**"Red Planet: The First Close Look"** - 228.3 s (3:48), 1920x1080, 30/1 fps, h264/aac, 133.37 MB.
- Source: NASA "Mars - The Search Begins" (public domain; first 900 s, `documentary_long` in media.yaml); B-roll from the
  NASA library (public domain) and the film's own speech-free shots; music: procedural bed generated locally.
- Director: `scientific_documentary`, target 225 s -> 228.3 s from 900 s
  of source; cold open; 3 chapters (0, 82, 183 s); 242 explained decisions
  (`demo1_editorial_plan.json`, a valid EditorialPlan).
- B-roll: 13 planned, 9 inserted after duplicate removal, 24.5% of the runtime,
  every library picture names Mars (subject gate); reasons with SigLIP score and licence per insert.
- Post: documentary grade + vignette, slow push-in keyframes on the 4 longest shots, crossfades at topic changes,
  dialogue clean-up (dialogue_v2_ffmpeg) on every narration clip, music ducked under speech by keyframes, burned
  captions, title, rolling credits.
- Render with QA: 396 s for 35 segments / 41 pieces (total script 541 s incl. B-roll
  search and frame extraction). QA **PASSED_AUTOMATED_CHECKS**, no known issues.
- Loudness -14.17 LUFS, LRA 2.54, true peak -1.91 dBTP.
- Quality score 91.6 (needs_work): visual 84.7 (bar 85; failed: sharpness, blockiness, exposure -
  the 1971 film scan is soft, blocky and has deep blacks; no grade can recover detail), editing 94.2
  (caption speed p90 ~20 chars/s, longest line ~42 chars), audio 92.0 (level steps at a few cuts in the
  original optical track), localisation 100.0.
- Frames inspected (`demo1_contact.jpg`, 12 frames). Earlier runs were rejected: run 1 was 162 s (the 6-min excerpt had
  176 s of usable narration; nothing padded); run 2 had Earth/Moon/Apollo cutaways (fixed with the subject gate); run 3
  showed in-film cutaways as bordered insets and the film's title card as a cutaway (both fixed in the director).
- **HUMAN-REVIEW-PENDING**: storytelling, pacing and the mix have not been judged by a person.

## 7. What automated QA cannot say
Storytelling, emotional arc, whether a cutaway shows exactly what is said, voice naturalness, translation accuracy and
whether a short will perform are not measured (`quality.not_measured` in every report). Every demo is
**HUMAN-REVIEW-PENDING**.
