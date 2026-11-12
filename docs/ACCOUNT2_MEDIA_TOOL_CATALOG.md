# Account 2 media tool catalogue: generation, enhancement, VFX, depth

Research for Agent D (Account 2), read on **2026-11-12**. It extends `nexus/registry/generation.yaml` (the machine-readable
matrix, 51+ entries) with the enhancement/VFX tools and re-checks the generation entries.

**How each fact was checked**
- **Code licence / maintenance**: GitHub search API (`gh api search/repositories?q=repo:<owner/name>` → `license.spdx_id`,
  `pushed_at`, stars, `archived`), plus the raw `LICENSE` file where the API says `NOASSERTION`.
- **Weights licence / size**: Hugging Face API (`/api/models/<repo>?blobs=true` → `cardData.license`, `gated`, file
  sizes) and the card text (`/raw/main/README.md`). Size = the files NEXUS would fetch, not the whole repo.
- **Speed/VRAM**: `measured here` = on this machine (shared 4 vCPU, 16 GB RAM, no GPU; numbers are noisy, other agents
  were running); `card` = what the model card says (not reproduced here); `unverified` = neither.
- Anything marked **UNVERIFIED** could not be read (gated repo, 401, no licence file) and is treated as *not allowed*.

Raw query output kept outside Git (scratchpad); the facts are copied below.

## Licence classes
| Class | Meaning in NEXUS | Default |
|---|---|---|
| **FOSS** | code OSI-licensed **and** weights allow commercial use without registration, revenue cap or territory limit (OpenRAIL use restrictions are passed on) | usable |
| **Restricted weights** | commercial use only under conditions (revenue cap, MAU cap, registration, attribution, territory) | refused unless the caller accepts the conditions |
| **Research / non-commercial** | weights CC BY-NC, "non-commercial", "research", or a card saying deployed use is out of scope | refused unless `allow_noncommercial=True` (personal work) |
| **Excluded** | licence excludes the owner's territory, or no readable licence, or needs paid compute | never used |

## 1. Image generation
| Tool | Repo / docs | Code lic. (GH) | Weights lic. (HF) | Class | Fetch size | VRAM / RAM | CPU | Speed | Maintained |
|---|---|---|---|---|---|---|---|---|---|
| SD 1.5 (+ LCM-LoRA) | huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5, latent-consistency/lcm-lora-sdv1-5 | generative-models MIT; diffusers Apache-2.0 | CreativeML OpenRAIL-M + OpenRAIL++-M (LoRA) | FOSS (use restrictions) | 2.27 GB fp16 subset | ~4 GB VRAM unverified / 6 GB RAM measured | yes | **measured here: 30.7 s / 512² at 4 steps** (`docs/results/generation_bench.json`) | weights frozen 2022; diffusers active (pushed 2026-11-12) |
| BK-SDM-Tiny (distilled SD 1.5) | huggingface.co/nota-ai/bk-sdm-tiny | BK-SDM repo NOASSERTION (licence file not read) | CreativeML OpenRAIL-M (card read) | FOSS (use restrictions) | **1.06 GB fp16** (unet 0.65 + text enc 0.25 + VAE 0.17) | <4 GB RAM | yes | measured here: see `docs/ACCOUNT2_MODEL_BENCHMARKS.md` | weights 2023, not updated |
| segmind tiny-sd | huggingface.co/segmind/tiny-sd | — | CreativeML OpenRAIL-M | FOSS | 1.06 GB (pickle `.bin` only, no safetensors) | — | yes (unmeasured) | unmeasured | 2023 |
| SDXL 1.0 base | huggingface.co/stabilityai/stable-diffusion-xl-base-1.0 | MIT | OpenRAIL++-M | FOSS | 6.9 GB | 8–12 GB VRAM (unverified) | slow (unmeasured) | unverified | frozen 2023 |
| SDXL-Turbo / SD-Turbo | stabilityai/sdxl-turbo, sd-turbo | MIT | `sai-nc-community` / Stability Community Licence (card: "for commercial use refer to stability.ai/license") | Restricted (revenue cap, registration) | 6.9 / 2.6 GB | — | SD-Turbo yes | unmeasured | 2024 |
| SD 3.5 Medium / Large / Large Turbo | stabilityai/stable-diffusion-3.5-* | sd3.5 MIT | `stabilityai-ai-community` (gated: auto) | Restricted (USD 1 M revenue cap) | 5–17 GB | 10–24 GB | no | unverified | 2024 |
| FLUX.1 [schnell] | black-forest-labs/FLUX.1-schnell (repo gated "auto"; card 401 without login) | flux Apache-2.0 | Apache-2.0 (HF tag) | FOSS | ~34 GB bf16 (GGUF Q4 ~7 GB) | 12–24 GB | slow | unverified | 2024 |
| FLUX.1 [dev] | black-forest-labs/FLUX.1-dev | Apache-2.0 | FLUX.1-dev Non-Commercial | Research / NC | — | — | — | — | — |
| FLUX.2 klein 4B, Z-Image-Turbo, Qwen-Image-2512, Sana 600M / Sprint 0.6B | see registry | Apache-2.0 (Sana GH) | Apache-2.0 (HF tags; Sana's Gemma-2 text encoder adds Gemma Terms) | FOSS (Sana: Gemma terms) | 7–30 GB | 12–16 GB+ | no/slow | unverified | active 2026 |
| ComfyUI | github.com/Comfy-Org/ComfyUI | **GPL-3.0** (pushed 2026-11-12, 146 k stars) | n/a | FOSS but copyleft | — | — | runs | — | very active |
| stable-diffusion.cpp | github.com/leejet/stable-diffusion.cpp | MIT (pushed 2026-11-12) | n/a (runs GGUF of the above) | FOSS | — | — | yes | unmeasured | active |
| ControlNet (SD 1.5 v1.1) | lllyasviel/ControlNet; HF lllyasviel/control_v11p_sd15_canny | Apache-2.0 (pushed 2024-02) | OpenRAIL (HF tag) | FOSS (use restrictions) | 0.72 GB fp16 per control (1.45 fp32) | +1–2 GB | slow | unmeasured | archived-in-practice (no push since 2024) |
| IP-Adapter | tencent-ailab/IP-Adapter; HF h94/IP-Adapter | Apache-2.0 | Apache-2.0 (FaceID variants depend on InsightFace models - non-commercial) | FOSS (not FaceID) | 0.04–0.1 GB + 2.5 GB image encoder | +1 GB | slow | unmeasured | 2024 |
| LoRA (diffusers/peft) | huggingface/diffusers | Apache-2.0 | **each LoRA has its own licence** - check per file | depends | MBs | — | yes | — | active |
| Inpaint / outpaint | HF stable-diffusion-v1-5/stable-diffusion-inpainting; diffusers/stable-diffusion-xl-1.0-inpainting-0.1 | Apache-2.0 | OpenRAIL-M / OpenRAIL++-M | FOSS | 2.1 / 6.9 GB fp16 subset | unverified | slow | unmeasured | 2023-2024 |

## 2. Video generation (none of it runs on this CPU; all GPU numbers are card claims, UNVERIFIED here)
| Model | HF repo | Code lic. (GH) | Weights lic. (HF) | Class | Repo size (all files) | VRAM (card) | Speed (card) | Maintained |
|---|---|---|---|---|---|---|---|---|
| Wan 2.1 T2V 1.3B | Wan-AI/Wan2.1-T2V-1.3B-Diffusers | Wan2.1 Apache-2.0 (pushed 2026-03) | Apache-2.0 | FOSS | 26.9 GB (fp32 + bf16; fetch subset ~17 GB unverified) | **8.19 GB** | "5 s 480P on an RTX 4090 in about 4 min" | yes |
| Wan 2.1 T2V/I2V 14B | Wan-AI/Wan2.1-T2V-14B, -I2V-14B-480P | Apache-2.0 | Apache-2.0 | FOSS | 64–77 GB | 24 GB+ with offload | card table only | yes |
| Wan 2.2 TI2V 5B | Wan-AI/Wan2.2-TI2V-5B-Diffusers | Wan2.2 Apache-2.0 (pushed 2026-09) | Apache-2.0 | FOSS | 31.8 GB | "at least 24 GB (RTX 4090)" with offload + t5_cpu | 720P@24 fps | yes |
| Wan 2.2 T2V/I2V A14B (MoE) | Wan-AI/Wan2.2-T2V-A14B | Apache-2.0 | Apache-2.0 | FOSS | 117.5 GB | 80 GB single GPU or multi-GPU | — | yes |
| LTX-Video 2B 0.9.5 | Lightricks/LTX-Video | Apache-2.0 (pushed 2026-11) | per-version files: 0.9–0.9.5 "Open RAIL-M"; 0.9.6+ "LTX-Video Open Weights License 0.X" | 0.9.5: FOSS; 0.9.6+: Restricted | 236 GB repo (one checkpoint ~6–9 GB) | ~8–12 GB unverified | "30 FPS 1216×704 faster than real time" (H100) | yes |
| LTX-2 | Lightricks/LTX-2 | Apache-2.0 | `ltx-2-community-license-agreement` | Restricted | 293 GB | — | — | yes (2026-10) |
| HunyuanVideo / 1.5 | tencent/HunyuanVideo, tencent/HunyuanVideo-1.5 | NOASSERTION; LICENSE.txt = Tencent Hunyuan Community Licence | `tencent-hunyuan-community`: "DOES NOT APPLY IN THE EUROPEAN UNION, UNITED KINGDOM AND SOUTH KOREA", >100 M MAU needs a licence | **Excluded** (territory) | 37 / 346 GB | 1.5: "minimum 14 GB with offloading" | step-distilled 8–12 steps | yes |
| CogVideoX 2B | zai-org/CogVideoX-2b | CogVideo Apache-2.0 | Apache-2.0 | FOSS | 12.8 GB | "diffusers FP16 from 4 GB" (all optimisations) | "A100 ~90 s / H100 ~45 s" for 6 s @ 8 fps, 50 steps | low activity (2025-11) |
| CogVideoX 5B | zai-org/CogVideoX-5b | Apache-2.0 | "The CogVideoX LICENSE" (registration / scale limits) | Restricted | 20 GB | "from 5 GB" | "A100 ~180 s" | low |
| Mochi 1 preview | genmo/mochi-1-preview | Apache-2.0 | Apache-2.0 | FOSS | 124 GB | "22 GB bf16 + offload; 42 GB full quality" | — | yes |
| SVD / SVD-XT | stabilityai/stable-video-diffusion-img2vid-xt | MIT | Stability Community Licence | Restricted | 30 GB | — | — | frozen 2024 |
| AnimateDiff (motion adapter v1-5-2) | guoyww/animatediff-motion-adapter-v1-5-2 | AnimateDiff Apache-2.0 (2024-07) | **no licence tag on the HF repo** (UNVERIFIED); Lightning variant OpenRAIL-M | Excluded until read | 3.4 GB | — | **measured here: CPU output = noise** (K-19, `docs/results/t2v_cpu_bench.json`) | stale |
| Kandinsky 6 Lite, MiniMax-H3, others | see registry | — | MIT / territory-excluded | see registry | — | — | — | — |

Remote execution is the only route to these models: `nexus.generate.remote` emits `RenderJobManifest`s with
`requirements {gpu, vram_gb, models}` for Account 1's scheduler; `scripts/gpu_video_bench.py` measures them on a GPU.

## 3. Enhancement
| Tool | Repo / model | Code lic. | Weights lic. | Class | Size | CPU | Speed | Maintained |
|---|---|---|---|---|---|---|---|---|
| Real-ESRGAN (PyTorch) | github.com/xinntao/Real-ESRGAN | BSD-3-Clause (pushed 2024-08, 37 k stars) | BSD-3 (weights in the repo's releases; GitHub releases are not an allowed download source here) | FOSS | x4plus 67 MB; general-x4v3 4.9 MB | yes, slow | measured here (ONNX path below) | low activity |
| Real-ESRGAN general-x4v3 safetensors | huggingface.co/OzzyGT/RealESRGAN_general_x4v3 (mirror by a diffusers maintainer; card links the BSD-3 licence) | BSD-3 | BSD-3 (HF tag) | FOSS (mirror) | **4.86 MB** | **yes** | measured: `ACCOUNT2_MODEL_BENCHMARKS.md` | — |
| Real-ESRGAN-ncnn-vulkan | github.com/xinntao/Real-ESRGAN-ncnn-vulkan | MIT (raw LICENSE read; API says NOASSERTION) | BSD-3 | FOSS | 50 MB binary | needs a Vulkan device (lavapipe CPU Vulkan not installed here → not measured) | — | 2024-05 |
| RIFE / Practical-RIFE | github.com/hzwer/Practical-RIFE, nihui/rife-ncnn-vulkan | MIT / MIT | MIT (weights on the author's Google Drive; HF mirrors e.g. `t9749/rife-4.25-lite-onnx` MIT, third-party) | FOSS (mirror provenance unverified) | 12–21 MB | slow | not measured here (FFmpeg minterpolate measured instead) | Practical-RIFE active (2026-08) |
| FILM | google-research/frame-interpolation | Apache-2.0, **archived** | Apache-2.0 | FOSS | — | slow | — | archived |
| FFmpeg minterpolate (mci) | ffmpeg.org/ffmpeg-filters.html#minterpolate | LGPL/GPL (build) | n/a | FOSS | — | yes | measured here | active |
| vid.stab | github.com/georgmartius/vid.stab | LGPL-2.1+ (raw LICENSE; API NOASSERTION) | n/a | FOSS | — | yes | already in renderer (`stabilize`) | active (2026-08) |
| Denoise: hqdn3d, nlmeans, bm3d | FFmpeg filters (all present in this build: `ffmpeg -filters`) | LGPL/GPL | n/a | FOSS | — | yes (bm3d/nlmeans slow) | measured here | — |
| Deblock (`deblock`), sharpen (`unsharp`, `cas`) | FFmpeg | LGPL | n/a | FOSS | — | yes | measured here | — |
| LUTs (`lut3d`, .cube) | FFmpeg; NEXUS `nexus/fx/lut.py` | — | each LUT file's own licence | FOSS (built-ins generated by NEXUS) | — | yes | renderer | — |
| HDR→SDR tone mapping | FFmpeg `zscale` + `tonemap` (hable/mobius/reinhard); `libplacebo` (needs Vulkan) | LGPL | n/a | FOSS | — | yes | measured on a synthetic PQ clip | — |
| GFPGAN / CodeFormer (faces) | TencentARC/GFPGAN; sczhou/CodeFormer | GFPGAN Apache-2.0 + bundled NVIDIA StyleGAN2 parts; CodeFormer S-Lab 1.0 | HF repo for GFPGAN returned 401; CodeFormer S-Lab (non-commercial) | Excluded / NC | — | — | — | — |

Upscaling never recovers real detail: it interpolates plausible texture. NEXUS labels such clips `edited-source`, and
the benchmark reports PSNR/SSIM only for a downscale→upscale round trip where the original exists.

## 4. VFX: segmentation, background removal, tracking
| Tool | Repo / model | Code lic. | Weights lic. | Class | Size | CPU | Speed | Maintained |
|---|---|---|---|---|---|---|---|---|
| SAM 2 / 2.1 (hiera-tiny) | github.com/facebookresearch/sam2; HF facebook/sam2.1-hiera-tiny | Apache-2.0 (pushed 2026-05, 20 k stars) | Apache-2.0 | FOSS | 0.15 GB (tiny) | slow (needs torch; video predictor is GPU-oriented) | unmeasured | active |
| rembg | github.com/danielgatis/rembg | MIT (pushed 2026-11-06) | per model: u2net/u2netp/u2net_human_seg Apache-2.0 (U-2-Net repo), isnet Apache-2.0 (DIS), BiRefNet MIT, **BRIA RMBG non-commercial** | FOSS except BRIA | u2netp 4.6 MB, u2net 176 MB | yes | — | active |
| U²-Net / U²-Netp ONNX | xuebinqin/U-2-Net (Apache-2.0); HF BritishWerewolf/U-2-Netp (Apache-2.0 tag, third-party export) | Apache-2.0 | Apache-2.0 | FOSS (mirror) | 4.6 MB | yes | — | 2024 |
| MODNet (portrait matting) | github.com/ZHKKKe/MODNet; HF **Xenova/modnet** (ONNX, HF staff export) | Apache-2.0 | Apache-2.0 | FOSS | **25.9 MB** fp32 | **yes** | measured: `ACCOUNT2_MODEL_BENCHMARKS.md` | 2024-05 |
| MediaPipe Selfie Segmentation | github.com/google-ai-edge/mediapipe; HF **onnx-community/mediapipe_selfie_segmentation** (ONNX) | Apache-2.0 (pushed 2026-11-12) | Apache-2.0 | FOSS | **0.46 MB** | **yes** | measured | active |
| BiRefNet / BiRefNet-lite | github.com/ZhengPeng7/BiRefNet; HF onnx-community/BiRefNet_lite-ONNX | MIT | MIT | FOSS | 224 MB (lite fp32) | slow | unmeasured | active |
| BRIA RMBG 1.4 / 2.0 | briaai/RMBG-* | — | `bria-rmbg-*` (non-commercial without a paid agreement) | Research / NC | — | — | — | — |
| OpenCV trackers (CSRT, KCF, MIL, Nano, ViT) | github.com/opencv/opencv(_contrib) | Apache-2.0 | n/a (Nano/ViT use small ONNX files) | FOSS | — | yes | CSRT/KCF are in **opencv-contrib** only; the installed `opencv-python` 5.0 ships MIL/Nano/ViT | active |
| Optical flow (Lucas-Kanade, Farnebäck, DIS) | OpenCV core | Apache-2.0 | n/a | FOSS | — | yes | measured (tracker) | active |

## 5. Depth
| Model | HF repo | Weights lic. | Class | Size | CPU | Notes |
|---|---|---|---|---|---|---|
| Depth Anything V2 **Small** | depth-anything/Depth-Anything-V2-Small; ONNX: onnx-community/depth-anything-v2-small | **Apache-2.0** | FOSS | 99 MB fp32 ONNX (27 MB int8) | **yes, measured** | code repo Apache-2.0 (pushed 2026-03) |
| Depth Anything V2 Base / Large | depth-anything/Depth-Anything-V2-Base / -Large | **CC BY-NC 4.0** | Research / NC | 0.36 / 1.25 GB | — | never default |
| MiDaS DPT-Hybrid | Intel/dpt-hybrid-midas | Apache-2.0 | FOSS | 0.46 GB | slow | isl-org/MiDaS MIT, **archived** |
| Depth Pro | apple/DepthPro | Apple AMLR (research) | Research / NC | 1.8 GB | — | — |

## 6. Prompt adherence (for benchmarks only)
| Model | Weights lic. | Class | Note |
|---|---|---|---|
| OpenAI CLIP ViT-B/32 | code MIT; card: "**Any** deployed use case of the model - whether commercial or not - is currently out of scope" | Research | not used |
| LAION CLIP-ViT-B-32-laion2B-s34B-b79K (open_clip, 0.6 GB) | MIT (HF tag) | FOSS | used by `scripts/gpu_video_bench.py --clip` (open_clip repo NOASSERTION; its LICENSE is MIT-style, read) |

## 7. Particles, light effects, film look
No model needed: procedural NumPy/Pillow generators in `nexus/vfx` (particles: dust, snow, embers, bokeh; light leaks;
lens flare) produce alpha videos (`qtrle`/`png` in MOV) and FFmpeg filters do the film look (noise grain, gblur glow,
sub-pixel gate weave). All FOSS, all CPU; measured in the benchmark document. These are labelled `procedural`.

## Summary table
| Capability | Best licence-clean choice | Class | Runs here (CPU)? |
|---|---|---|---|
| Text-to-image | SD 1.5 + LCM (2.3 GB) / BK-SDM-Tiny (1.06 GB) | FOSS (OpenRAIL) | yes, measured |
| Text/image-to-video | Wan 2.1 1.3B (8 GB VRAM), Wan 2.2 5B (24 GB), CogVideoX-2B (4 GB+) | FOSS | **no** — GPU-ready, UNVERIFIED |
| Upscale | Real-ESRGAN general-x4v3 (ONNX built locally from 4.9 MB safetensors) | FOSS | yes, measured |
| Frame interpolation | FFmpeg minterpolate (RIFE on GPU) | FOSS | yes (minterpolate), RIFE unmeasured |
| Denoise / deblock / sharpen / tone map | FFmpeg hqdn3d, nlmeans, deblock, unsharp/cas, zscale+tonemap | FOSS | yes, measured |
| Background removal | MediaPipe selfie (0.46 MB), MODNet (26 MB) | FOSS | yes, measured |
| Tracking | OpenCV LK optical flow + template re-detection | FOSS | yes, measured |
| Depth / DoF / 2.5D parallax | Depth Anything V2 Small ONNX | FOSS | yes, measured (parallax = animated still) |
| Particles / light leaks / flares / film look | procedural | FOSS | yes |
