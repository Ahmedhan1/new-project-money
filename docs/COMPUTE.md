# Free compute federation plan

Rule: the system must keep working when every remote resource is gone. Remote free GPUs speed things up; they are never required.

Priority order used by the router (`nexus/compute.py`): local machine → officially permitted free GPU session → slower CPU path → defer.

## Sources

| Source | What it gives | Card needed | Limits (check your account; they change) | What NEXUS may do | What NEXUS will not do | State in this build |
|---|---|---|---|---|---|---|
| Your own computer | CPU, RAM, disk | no | none | everything | — | **used for all tests** |
| Kaggle Notebooks | T4×2 or P100, ~30 GB RAM, 12 h per session | no (phone verification for GPU/internet) | weekly GPU-hour quota shown in the account (historically about 30 h) | submit a script through the official API (`kaggle kernels push`), poll status, download output | second accounts, quota evasion, mining, persistent serving | adapter written, **not tested** (no account token here) |
| Google Colab free | T4 when available | no | undisclosed dynamic limits, idle and lifetime timeouts | generate a notebook that **you** open and run by hand | any automation, keep-alive tricks, SSH/remote desktop, serving | hand-off generator written, not an automated worker |
| GitHub Actions | CPU runners | no for public repos | monthly minutes | run the test suite | render videos | not set up |
| Hugging Face Spaces ZeroGPU | short GPU bursts | no | small daily quota, demo-oriented | nothing | batch production | rejected |
| Hugging Face Hub | model downloads | no | fair use | download weights once, cache locally | — | **used** |
| Wikimedia Commons / Wikipedia / Openverse | images, facts | no | per-IP rate limits (we saw HTTP 429 with `Retry-After: 22` and Openverse 504s) | serial, cached requests with a descriptive User-Agent; back off when told | parallel scraping, ignoring `Retry-After` | **used** |

Free tiers that ask for a payment method (most cloud "free credits") are excluded by the Cost Guard manifest rule
`payment_method_required: false`.

## Worker registration

Each worker reports provider, availability, CPU, RAM, GPU/VRAM, free disk, frameworks, maximum job length, quota text and policy
restrictions (`GET /api/compute`). The local worker also recommends model sizes for the detected hardware.

## Routing table

| Task | Needs GPU | Order |
|---|---|---|
| cuts, encoding, audio, packaging, analysis, translation, 2D animation | no | local |
| transcription, local LLM, 3D rendering | no | local → Kaggle |
| image generation, video generation, upscaling | yes | Kaggle → local GPU if present → CPU path if one exists → defer |

## Failure handling (acceptance test I)

A job is a list of named steps. After each step its result is written to the job's checkpoint. If the worker disappears
(`WorkerLost`: session ended, quota exhausted, push refused) the federation records the event, excludes that worker and
continues from the first unfinished step on the next eligible worker. Finished steps are never repeated. If no eligible
worker exists and a CPU path is not acceptable, the job is marked `deferred` with the reasons. It is never sent to a paid service.

Measured in the test: a simulated remote GPU died after 3 of 6 scenes; scenes 4–6 and assembly ran locally; the 6 scenes were
rendered exactly once each; output validated.

The remote worker in that test is simulated. A real Kaggle round trip has not been run.

## What a free GPU session would be used for first

1. Benchmark Wan 2.1 T2V 1.3B (Apache-2.0 weights, ~8 GB VRAM) for 5-second scene generation.
2. Benchmark FLUX.1-schnell (Apache-2.0) for scene stills and thumbnails.
3. Benchmark Chatterbox Multilingual (MIT) as a properly licensed Arabic voice.
4. Benchmark ACE-Step (Apache-2.0) for music.

Until those numbers exist the capacity planner labels the generative profile "unmeasured estimate".
