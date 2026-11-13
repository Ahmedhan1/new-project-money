# Account 2 model benchmarks: generation, enhancement, VFX (Agent D)

Measured 2026-11-12 on the build container: **shared 4 vCPU, 16 GB RAM, no GPU**. Five agents shared the CPU;
heavy runs were serialised with `flock /tmp/nexus-heavy.lock`, but unlocked test suites still ran next to them, so the
numbers are noisy (the first BK-SDM image took 238 s under contention vs 29-33 s uncontended). Raw data:
`docs/results/account2_generation/`, `docs/results/account2_enhance/bench.json`, `docs/results/account2_vfx/bench.json`
(+ the JPEG crops/strips next to them, all looked at). Reproduce: `python -m nexus.enhance.bench`,
`python -m nexus.vfx.bench`, `scripts/gpu_video_bench.py` (GPU).

## 1. Classification of every generation capability
| Capability | Status here | Evidence |
|---|---|---|
| Text-to-image, SD 1.5 + LCM-LoRA (2.27 GB, OpenRAIL-M) | **works here** | earlier run: 30.7 s / 512² at 4 steps (`docs/results/generation_bench.json`) |
| Text-to-image, BK-SDM-Tiny (1.06 GB, OpenRAIL-M) | **works here** | 29-33 s / 512² at 8 steps, 72 s at 20 steps, 3.6 GB RSS (below) |
| Text-to-image, FLUX.1 schnell / Z-Image / SDXL | GPU-ready, **UNVERIFIED** | registry tiers only |
| Text-to-video (Wan 2.1 1.3B, Wan 2.2 5B, CogVideoX-2B) | GPU-ready, **UNVERIFIED** | `scripts/gpu_video_bench.py` written + its CPU parts tested; never run on a GPU |
| Image-to-video (Wan 2.2 TI2V 5B) | GPU-ready, **UNVERIFIED** | same |
| Video-to-video (Wan 2.1 V2V pipeline) | GPU-ready, **UNVERIFIED** | same |
| Text/image-to-video on CPU | **not feasible** | K-19: CPU AnimateDiff = noise (`docs/results/t2v_cpu_bench.json`) |
| 2.5D parallax from a still | works here, labelled **animated-still** (never generated video) | 1.9-3.5 s per 3 s clip at 720p |
| Ken Burns on a still | works here, **animated-still** | existing `generate.animate_still` |
| Particles / light leaks / lens flares | works here, **procedural** | ~1 s per 2 s of 720p RGBA |
| Upscale, interpolate, denoise, deblock, sharpen, film look, tone map | works here, **edited-source** | below |
| Background removal / replacement, tracking, depth of field | works here, **edited-source** | below |

The guard (`nexus/generate/classify.py`) grants `generated-video` only if: a registry video model is named, nothing is
simulated or still-derived, the frames are **not** one still under a global zoom/pan, and the compute record names a GPU.
Measured: the Ken Burns clip gives `still_motion` verdict `global-transform-of-one-still` and is downgraded to
`animated-still` even when a worker claims a Wan model made it. Depth parallax is **not** caught by the frame test
(verdict `independent-motion`, residual 5.7-12.3 vs motion 6.3-10.8) - provenance and the GPU requirement are what keep
it labelled `animated-still`. Limitation: a real generated slow pan over a distant landscape can be downgraded to
`animated-still` (conservative direction).

## 2. CPU image generation (BK-SDM-Tiny, `docs/results/account2_generation/`)
| Config | s / image (512²) | Note |
|---|---|---|
| DPM-Solver++ 4 steps, CFG 7.5 | 238 (contended, first run) | lighthouse missing from the image |
| 8 steps | **29.0 / 32.5** (uncontended), 114 contended | coherent: lighthouse on a cliff, bicycle at a brick wall, bowl of oranges |
| 20 steps | 71.9 / 210 (contended) | lighthouse duplicated |
| SD 1.5 LCM-LoRA on BK-SDM-Tiny | **does not load** | `size mismatch ... [64, 2560, 3, 3] vs [64, 1920, 3, 3]` - pruned UNet |
Download 1.06 GB in 17.9 s, load 3.3 s, peak RSS 3.64 GB (torch 2.15 CPU, fp32). Verdict: usable for backgrounds; no
faster than SD 1.5 + LCM (which needs 4 steps without CFG). SD 1.5 + LCM stays the CPU default; BK-SDM-Tiny is the
smaller download (registry `bk-sdm-tiny`, diffusers block wired into the worker).

## 3. Enhancement (`docs/results/account2_enhance/`)
### Upscale x2, round trip (downscale x2 with area, upscale back, compare with the original; 1 s, 8 frames)
| Source | nearest | bicubic | lanczos + unsharp (baseline) | Real-ESRGAN x4v3 (ONNX) | ESRGAN s/frame (input) |
|---|---|---|---|---|---|
| ad 320x240 (1948 film) | 29.47 / 0.924 | 30.41 / 0.931 | **30.77 / 0.934** | 26.53 / 0.929 | 0.44 (160x120) |
| interview 1280x720 | 32.97 / 0.956 | **35.83 / 0.974** | 35.65 / 0.971 | 30.32 / 0.938 | 5.96 (640x360) |
| broll 1920x1012 | 28.89 / 0.919 | 30.65 / 0.938 | **31.09 / 0.943** | 29.07 / 0.896 | 4.13 (960x506) |
(PSNR dB / SSIM.) The baseline beats nearest everywhere (+1.3 to +2.7 dB) and matches bicubic. Real-ESRGAN **loses** on
PSNR/SSIM: it is GAN-trained and invents edges/texture instead of reproducing the missing pixels. By eye
(`ad_native2x_crops.jpg`) it gives the cleanest line art on the 1948 cartoon; on faces/clouds it smooths texture.
**Upscaling never recovers real detail.** Native 2x of the ad: lanczos 0.014 s/frame, ESRGAN 1.26 s/frame. ESRGAN on a
720p source (5120x2880 intermediate) was skipped as impractical here (~30 s/frame estimated, not measured).

### Other ops (s/frame, realtime factor; 4 s segment unless noted)
| Op | ad 320x240 | interview 720p | broll 1920x1012 |
|---|---|---|---|
| minterpolate mci 2x | 0.022 (0.76x) | 0.265 (0.06x) | 0.538 (0.03x, 1 s) |
| minterpolate blend 2x | 0.003 (5.7x) | 0.007 (2.3x) | 0.010 (1.7x) |
| hqdn3d | 0.005 (6.4x) | 0.014 (2.3x) | 0.017 (1.9x) |
| nlmeans | 0.102 (0.33x) | 0.628 (0.05x) | 1.096 (0.03x, 1 s) |
| deblock | 0.005 (6.8x) | 0.013 (2.5x) | 0.015 (2.2x) |
| CAS sharpen | 0.005 (6.2x) | 0.018 (1.8x) | 0.026 (1.3x) |
| film look (grain+halation+weave) | 0.007 (4.5x) | 0.023 (1.5x) | 0.046 (0.73x) |
| HDR(PQ)->SDR hable | 0.008 | 0.017 | 0.031 |

Denoise quality (noise `alls=18` added to the clean segment; PSNR/SSIM vs clean): ad noisy 22.66/0.507 -> hqdn3d
23.87/0.567 -> **nlmeans 31.59/0.890**; interview 22.72 -> 23.85 -> **29.94/0.821**; broll 24.14 -> 25.43 -> **30.89/0.856**.
hqdn3d is the fast light clean-up; nlmeans is the one that removes real noise (15-30x slower).
Tone map: a PQ (BT.2020, 1000 nits) conversion of the SDR segment, tone-mapped back with hable, is a plausible SDR image
(SSIM 0.83-0.92) but not the original (PSNR 15-19 dB: hable compresses highlights and darkens mid-tones) - judged by eye
for real HDR footage, which none of the benchmark media is. An SDR source is returned untouched.

## 4. VFX (`docs/results/account2_vfx/`)
| Op | Result |
|---|---|
| Background replacement, MediaPipe selfie (0.46 MB) | interview 720p: **0.060 s/frame (16.6 fps)**; leaks: NASA logo/flag partly kept as foreground (`interview_background_replacement.jpg`) |
| Background replacement, MODNet (25.9 MB) | **0.346 s/frame (2.9 fps)**; clean silhouette incl. hair; recommended for quality |
| Tracking (LK flow + template) | broll cloud patch 120 frames: 0.011 s/frame, 0 lost; walktalk head 180 frames: 0.006 s/frame, 0 lost, box stays on the head (`*_tracking.jpg`). Synthetic test: mean error < 4 px |
| Depth Anything V2 Small (ONNX 99 MB) | 1.36-2.51 s per frame (518 px inference); plausible maps (ISS near, Earth far; speaker separated from backdrop) |
| Depth of field | 0.06-0.75 s per frame after depth |
| 2.5D parallax, 3 s 1280x720 | 1.9-3.5 s per clip incl. depth; subtle convincing drift on interview; edges stretch on strong moves |
| Particles (dust/snow/embers/bokeh), light leak, flare, 2 s 720p RGBA | 0.9-1.4 s each |
Overlays composite correctly (`interview_overlays.jpg`); the default flare path crosses the face - choose the path per shot.

## 5. GPU protocol (not run)
`scripts/gpu_video_bench.py`: T2V (Wan 2.1 1.3B, Wan 2.2 5B, CogVideoX-2B), I2V (Wan 2.2 5B), V2V (Wan 2.1); records
time, peak VRAM/RAM, resolution, frames, frame-to-frame SSIM, Farnebäck warp error, flicker, still-motion verdict,
classification and an optional CLIP score (LAION ViT-B/32, MIT). `--request` executes a `RenderExecutionRequest` and
returns a `RenderExecutionResult` (deferred without CUDA - tested). Card claims (unverified): Wan 2.1 1.3B 8.19 GB VRAM,
~4 min per 5 s 480p on an RTX 4090; Wan 2.2 5B needs 24 GB with offload; CogVideoX-2B from 4 GB, ~90 s on A100.
