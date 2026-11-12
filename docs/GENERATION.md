# Image and video generation (GEN-0)

Machine-readable matrix: `nexus/registry/generation.yaml` (51 models, runtimes, free-compute options, hardware tiers; every
licence with the URL read on 2026-11-12). Code: `nexus/generate/`. Tests: `tests/fast/test_generate.py`.

## What NEXUS can honestly produce

| Kind (provenance label) | What it is | Where it runs |
|---|---|---|
| `generated` still | SD 1.5 + LCM-LoRA, 4–6 steps, 512–768 px | CPU (measured: **30.7 s per 512×512 image after warm-up**, 25 s load, 6.2 GB peak RAM on 4 vCPU; `docs/results/generation_bench.json`) |
| `animated_still` | Ken Burns / pan on a still (generated or licensed) | FFmpeg, any machine |
| `motion_graphics` | designed titles, charts, kinetic type | Pillow + FFmpeg / Manim |
| `footage` | the user's or licensed camera footage | — |
| `generated` video | text-to-video / image-to-video | **Not on this machine.** Needs ≥ 8 GB VRAM (Wan 2.1 T2V 1.3B, Apache-2.0) or the owner's GPU machine / an authorised free GPU notebook via the remote folder |

`generate.label(tl)` reports the share of programme time per kind; `generate.guard_claim(text, tl)` refuses descriptions such
as "AI-generated video" for a programme whose generated-video share is zero. A generated still with Ken Burns motion is an
`animated_still` whose `still.kind` is `generated` — never "generated video".

## Recommendation per hardware tier (weights licence must allow commercial use)
| Tier | Images | Video |
|---|---|---|
| CPU only (this build machine) | SD 1.5 + LCM-LoRA (OpenRAIL-M / OpenRAIL++, commercial use allowed with the use restrictions passed on) | none — animated stills + motion graphics |
| 6–8 GB VRAM | SD 1.5 LCM, SDXL Lightning / Sana | Wan 2.1 T2V 1.3B (8.2 GB by its card; offload on 6 GB unmeasured) |
| 12–16 GB | FLUX.2 klein 4B, Z-Image Turbo, FLUX.1 schnell (Apache-2.0) | Wan 2.1 1.3B; LTX-Video 2B 0.9.5 |
| 24 GB+ | Z-Image Turbo, Qwen-Image | Wan 2.2 TI2V 5B (Apache-2.0) |
| Remote (owner-authorised) | FLUX.1 schnell | Wan 2.1 1.3B on a Kaggle T4/P100 (16 GB) |

Refused by default (`commercial_use` no/restricted): FLUX.1 dev, SD 3.5 Medium (revenue-capped), SD-Turbo, SVD, CogVideoX-5B,
HunyuanVideo, CodeFormer and others — usable only with `allow_noncommercial=True` for personal work.

## Free compute (zero-spend rules)
| Option | Card | Verdict |
|---|---|---|
| Owner's own GPU machine via a shared folder | no | **survives** — `RemoteFolder` hand-off (job spec in `outbox/`, artefact + provenance in `inbox/`) |
| Kaggle Notebooks | no | **survives** — official API (`kaggle kernels push`); weekly quota not published; needs the owner's account |
| Google Colab free | no | manual hand-off only (ToS restricts remote control / automation) |
| Hugging Face ZeroGPU | no | rejected for production (5 GPU-minutes/day) |
| Lightning AI, Paperspace | unverified | excluded until a no-card account is confirmed |
| Cloud trials (GCP/AWS/Azure/Modal) | yes | excluded by policy |

Nothing was signed up for. A remote result whose provenance names no real model (e.g. the old `SimulatedRemoteGPU`) can
never be labelled `generated`.

## Enabling image generation on a machine
The heavy stack stays out of the app's own environment. Install torch (CPU or CUDA) + diffusers + transformers + peft into
a separate folder or venv and point NEXUS at it (`settings.json` → `"generation": {"python": ..., "pythonpath": ...,
"hf_home": ...}` or env `NEXUS_GEN_PYTHONPATH` / `NEXUS_GEN_HF_HOME`). Weights (2.3 GB) are fetched once with
`allow_download=True`. `generate.capabilities()` then reports `image: available` from a real probe of that environment.

## Not built yet
Inpainting/outpainting, upscaling (Real-ESRGAN), AI frame interpolation (RIFE), depth parallax, character/reference
consistency (IP-Adapter): models are in the registry with licences; providers are not wired. Character consistency across
scenes is therefore not available; a fixed seed and a shared style prompt are the only consistency NEXUS applies today.
