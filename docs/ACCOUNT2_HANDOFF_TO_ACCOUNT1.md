# Account 2 → Account 1 technical handoff (cloud workers, storage, scheduler)

Account 2 (cinematic engine, PR #6, branch `work/account-2-cinematic-engine`) runs its own work locally by default and
uses Account 1's remote compute only through Account 1's documented extension points. **No Account 1 file is edited.**
Verified against Account 1 head `b2517b2` (PR #4) on a local, unpushed three-account merge (A1 → A2 → A3, then
`integration/contracts_v1/apply.sh` from Account 3's branch at `24b3f71`).

## 1. Contracts
- Shared package layout (agreed, `docs/SHARED_CONTRACTS_ARCHITECTURE.md` on Account 3's branch): Account 1 keeps
  `nexus/contracts/cloud.py` (moved from `nexus/contracts.py` by `apply.sh`), Account 2 owns `nexus/contracts/media.py`
  (on this branch already), the shared `nexus/contracts/__init__.py` comes from `apply.sh`.
- On the merged tree `from nexus.contracts import ProductionJobManifest` (Account 1) and `contracts.make("RenderJobManifest", ...)`
  (Account 2) both work; `contracts.VERSION` keeps Account 1's meaning (`"v1"`). Account 2 code reads its own version as
  `contracts.media.VERSION` (fixed in `nexus/reference.py`, the only place that read the package-level name).
- Evidence: `test_contracts_shared.py`, `test_contracts.py` (A1), `test_contracts_media.py` (A2), `test_intel_handoff.py`:
  41 passed on the merged tree.

## 2. Account 2 worker tasks (Account 1's `worker_kit/tasks/` route)
| Task | Files | TaskSpec (registered with `nexus.cloud.compute.tasks.register`) | Status |
|---|---|---|---|
| `a2_render_steps` | `nexus/cloud/worker_kit/tasks/a2_render_steps.py`, compiler `nexus/timeline/remote_render.py` | `gpu="no"`, `resumable=True` | AUTOMATED-TESTED through `LocalProvider` |
| `a2_wan_video` | `nexus/cloud/worker_kit/tasks/a2_wan_video.py` | `gpu="required"`, `min_vram_gb=8.19`, `internet=True` | GPU-READY-UNVERIFIED |

- Registration runs when `nexus.workflows` is imported (API server and job worker both import it); on a branch without
  Account 1's package it is a no-op.
- `a2_render_steps`: Account 2 compiles a timeline of plain cuts into FFmpeg argument lists (`{in:<artifact name>}`,
  `{work}` placeholders) because the worker script cannot import the NEXUS renderer. One checkpoint per step, a `master`
  artifact, and `matches_expectation` (geometry and duration against Account 2's expectation) in the metrics. Richer
  timelines (effects, layers, captions, music) raise `NotRemoteRenderable` and render locally.
- **Inputs are matched by artifact name**: register each input with the name `worker_steps()` returns (`src_00.mp4`, ...).
- `a2_wan_video` refuses to run without CUDA (no CPU stand-in) and returns the GPU name, which Account 2's classifier
  requires before it labels anything `generated-video`.

## 3. Verified on the merged tree (`tests/fast/test_a2_cross_account.py`, 4 passed; skipped on branches without A1/A3)
1. Account 1's dispatcher ran Account 2's compiled steps on its local worker: 4 steps, 4 checkpoints,
   `matches_expectation: true`, master stored as an Account 1 `ArtifactManifest` whose SHA-256 equals Account 2's `ArtifactRef`;
   duration equals Account 2's own render within 2 frames.
2. A Wan job (`requires.gpu = true`) waits queued with 0 attempts when no GPU worker is authorised.
3. Rich timelines are refused for remote plain workers.
4. Account 2 QualityReport / GeneratedMediaManifest validate through `validate_any`.

## 4. Requests to Account 1 (owned by Account 1; not changed by Account 2)
- Two Account 1 fast tests fail on the merged tree and also on Account 1's own branch (from Account 3's
  `integration/contracts_v1/RESULT.md`; to re-check on `b2517b2`): `test_cloud_compute::test_safe_production_mode_gpu_jobs_wait_queued...`
  (fixed in `633cc8b` per Account 1's log) and `test_cloud_security::test_cookie_session_and_csrf` (httpx 0.28 cookie jar).
- Consider exposing `a2_render_steps` progress (`checkpoints_new`) in Studio's job view; Account 2's UI only polls its own jobs.
- When an authorised GPU worker exists, run `a2_wan_video` once (Wan 2.1 T2V-1.3B, 33 frames, 480p) and send the
  result + `WorkerExecutionResult` to Account 2: that is the only way Demo 5 moves from GPU-READY-UNVERIFIED to verified.
