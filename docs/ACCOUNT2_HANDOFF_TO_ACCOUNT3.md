# Account 2 → Account 3 technical handoff

Account 2 (cinematic engine) provides the **analysis, editing, rendering, audio and visual-processing engine**.
Account 3 (research, AI provider routing, localisation, publishing, growth analytics) builds on it and must not
re-implement it. Branch: `work/account-2-cinematic-engine` (PR #6, draft, not merged).

## 1. The contract you talk to
`nexus/contracts/__init__.py` + `docs/ACCOUNT2_CONTRACTS.md`. Build documents with `contracts.make(Name, **fields)`,
validate inbound ones with `contracts.validate(doc)`. All documents carry `"schema": "nexus.<Name>/1"`.

| You produce (Account 3 → Account 2) | Document | How Account 2 uses it |
|---|---|---|
| Research findings with media | `MediaAsset` (`rights.basis` required) | imported, analysed, offered to the director as B-roll candidates |
| Scripts / narration text | `EditorialPlan.decisions` (kind `narration`) or plain text to `/api/create` | TTS (mastered to -18 LUFS, cached) and timeline assembly |
| Translations | lines through the Review API (`/api/projects/{id}/review/lines`) | subtitle package + dub; only approved lines count as `commercially_ready` |
| Market-wide style findings from public metadata | `ReferenceStyleProfile.metrics` | director applies structure only (pacing, hook window, density) |

| You consume (Account 2 → Account 3) | Document / endpoint |
|---|---|
| What is in a video | `MediaAnalysis` (scenes, transcript with word timings, speakers, faces, loudness, visual tags) |
| Why the edit looks the way it does | `EditorialPlan` (`GET /api/projects/{id}/plan`): every keep/cut/insert with a reason + confidence; overridable by decision id |
| Finished files | `ArtifactManifest` / `ArtifactRef` (sha256, bytes) from render/export/deliver jobs |
| Can this be published | `QualityReport` (`GET /api/projects/{id}/quality`): stages, score, `human_approval` (always PENDING until a person approves) |
| Rights | `GET /api/projects/{id}/rights` (rights gate refuses undeclared sources); credits text for descriptions |
| Generated media labels | `GeneratedMediaManifest.classification`: `generated-video` only for real model output on a named GPU; animated stills are labelled `animated-still` |

## 2. Endpoints Account 3 will call
Editing/publishing-relevant (all jobs are durable; poll `GET /api/jobs`):
- Direction: `GET /api/director/profiles`, `POST /api/projects/{id}/direct`, `GET /api/projects/{id}/plan`
- Shorts & retention: `POST /api/projects/{id}/shorts`, `GET /api/projects/{id}/retention`, `POST .../retention/apply`, `POST /api/hooks`
- Platforms & delivery: `GET /api/platforms`, `GET /api/platforms/profiles`, `POST /api/projects/{id}/deliver` (16:9 / 4:5 / 9:16 packages, caption.txt, manual upload steps — Account 2 never uploads)
- Export: `GET /api/export/profiles`, `POST /api/projects/{id}/export` (codec/quality/size target, verified)
- Interchange: `GET /api/projects/{id}/edl?fmt=cmx3600|json|otio`
- Review (human corrections): `/api/projects/{id}/review/{speakers,names,lines,cutaways,shorts}`
- Effects/processing: `GET /api/fx/catalog`, `GET /api/effects`, `POST /api/projects/{id}/clips/{clip}/process`
- Models (consent-gated downloads): `GET /api/generation/models`, `GET /api/generation/hardware`, `GET/POST/DELETE /api/models/{id}...`

## 3. What works, measured (details: docs/ACCOUNT2_QUALITY_REPORT.md)
- Renderer: multi-track layers, opacity/rotation/position keyframes, tracked masks, J/L cuts, freeze, split layouts, chroma,
  perspective, speed ramps, 58 verified transitions, captions (classic/boxed/pop/karaoke), resumable rendering, EDL/OTIO.
  Edit-loop speed on a 64 s / 16-clip benchmark: one-clip edit 35 s → ~4 s, caption edit 34 s → ~2 s, warm 0.4 s.
- Audio: dialogue chain v2 with per-material denoise defaults and a speech-band guard; all six benchmark files to -16 LUFS
  ±0.1, true peak ≤ -1.9 dBTP; A/V sync per clip measured (drift bug found and fixed). **No human has listened yet.**
- Enhancement/VFX: upscale (lanczos/Real-ESRGAN), interpolation, denoise, deblock, film look, tone map, background
  replacement, tracking, particles, depth of field. Upscaling never recovers real detail.
- Image generation on CPU: ~30 s per 512² image (BK-SDM-Tiny, OpenRAIL-M).

## 4. What does not exist yet (do not promise it downstream)
- **Generated video**: no GPU here. Manifests for Wan 2.1 / Wan 2.2 defer with `RESOURCE_EXHAUSTED`; run
  `scripts/gpu_video_bench.py` on Account 1's GPU worker to verify (Demo 5 evidence: `docs/results/a2_demos/demo5_generation.json`).
- Lip-sync, a licensed Arabic/Hindi voice, native-speaker translation review, platform upload/analytics (OAuth, Account 3/owner).
- Human creative review of any demo.

## 5. Rules that carry over
- Zero spend: every tool/model is declared in `nexus/registry/components.yaml`; `costguard` refuses undeclared hosts.
- No detection-evasion or re-upload of other people's content; the rights gate blocks undeclared sources.
- Never claim a capability without a test or a measured run; automated QA ≠ approval.
