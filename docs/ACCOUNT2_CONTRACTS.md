# Account 2 integration contract (v1)

Code: `nexus/contracts/__init__.py` (`make`, `validate`, `check`, `artifact`). Tests: `tests/fast/test_contracts.py`.
Every document has `"schema": "nexus.<Name>/1"`. Readers must ignore unknown keys and refuse unknown major versions.
Media is never inlined: files travel as **ArtifactRef** `{uri, sha256?, bytes?, media_type?, role?}`; `uri` is
`file://` locally or whatever Account 1's storage returns (e.g. `rclone:remote/path`, `s3://...`). Account 2 never
writes Account 1's storage directly — it hands ArtifactRefs to the scheduler.

## Who produces / consumes what
| Document | Producer | Consumer | Purpose |
|---|---|---|---|
| MediaAsset | Account 2 (import), Account 3 (research finds) | all | a source/produced file with `rights.basis` in owned/licensed/public-domain/commentary/generated |
| MediaAnalysis | Account 2 (`media.analyze`, `media.understand`, diarisation, faces, visual) | Account 2 director, Account 3 (scripts, localisation) | scenes, transcript with word timings, speakers, faces, loudness, visual tags |
| EditorialPlan | Account 2 director | UI review, Account 3 (titles, metadata) | every keep/cut/insert decision with a reason; editable before render |
| TimelineEDL | Account 2 | renderer, UI, export (OTIO/EDL) | `timeline` = renderer dict (docs/TIMELINE.md); `edl` = flat event list |
| RenderJobManifest | Account 2 | Account 1 scheduler | job kind, inputs, settings, outputs, `requirements` {gpu, vram_gb, ram_gb, est_s, models} |
| RenderExecutionRequest | Account 1 scheduler | worker (any provider) | manifest + `environment` {provider, gpu, vram_gb, cpu, workdir, storage} |
| RenderExecutionResult | worker | Account 1 → Account 2 | status queued/running/done/failed/cancelled/deferred, outputs, metrics, error {code, message, retryable}, checkpoint |
| EffectManifest | Account 2 (VFX/enhance) | renderer, UI | effects available on this machine + their parameters + cost class |
| AudioMasterManifest | Account 2 audio | QA, reports | chain applied, before/after LUFS/peak/noise |
| GeneratedMediaManifest | Account 2 generation | QA, publishing (Account 3) | model + licence + compute + `classification` (`generated-video` only for real model output; `animated-still` otherwise) |
| QualityReport | Account 2 QA | UI, Account 3 publishing gate | stages/checks, score, `human_approval` |
| ReferenceStyleProfile | Account 2 (from a lawfully supplied reference) | director; Account 3 may supply `metrics` from public metadata | structure only: pacing, hooks, density — never footage or script |
| ArtifactManifest | any producer | Account 1 storage | list of ArtifactRefs produced by a job |

## Execution model
1. Account 2 builds a `RenderJobManifest` (same for local or remote) and states `requirements`.
2. Account 1 chooses a provider and sends a `RenderExecutionRequest`. The worker runs
   `python -m nexus.contracts.run <request.json>` (to be provided by Account 2 once Account 1's worker contract lands) or the
   in-process equivalent; local execution uses the same path, so no code knows which provider ran it.
3. The worker returns a `RenderExecutionResult`. Interrupted jobs return `status: failed` + `error.retryable: true` +
   the last `checkpoint` (finished segments/stages); a retry passes the checkpoint back in the request manifest.

Error codes (`error.code`): `INPUT_MISSING`, `RIGHTS_REFUSED`, `RESOURCE_EXHAUSTED` (RAM/VRAM/disk), `MODEL_UNAVAILABLE`,
`TIMEOUT`, `PROVIDER_LOST`, `QA_FAILED`, `INTERNAL`.

## Boundaries
- Account 1: worker orchestration, storage, scheduler, account connections, remote dashboard. Not touched by Account 2.
- Account 3: research, provider routing, localisation, publishing, growth analytics. Account 2 provides the analysis and
  editing engine those use; it does not implement trend discovery or uploading.
- No component may download multi-GB weights onto the owner's Windows PC automatically (`requirements.models` lets the
  scheduler route such jobs to a remote GPU worker instead).
