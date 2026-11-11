# Open-Source Discovery Report

113 projects and services evaluated. Repository licence, last push, archive flag and stars were read from the live GitHub API on 2026-11-11; model-weight licences were read from the model cards. Entries without a GitHub repository were checked on their own sites.

**Decisions:** 20 deployed and tested in this build, 18 selected for the next phase, 43 candidates, 32 rejected.

- **DEPLOYED** = installed here, exercised by the acceptance tests.
- **NEXT** = chosen, not yet integrated.
- **CANDIDATE** = viable alternative, not chosen now.
- **REJECTED** = licence, maintenance or fit problem (reason given).

## Licence classes (kept separate)

### Fully free and open-source (78)

FFmpeg, PySceneDetect, OpenTimelineIO, MLT Framework, Kdenlive, Shotcut, OpenShot, LosslessCut, Olive, MoviePy, PyAV, yt-dlp, Wan 2.1, Wan 2.2, CogVideoX, Mochi 1, ComfyUI, Diffusers, stable-diffusion.cpp, faster-whisper, Whisper, whisper.cpp, WhisperX, sherpa-onnx, Silero VAD, Subtitle Edit, Piper (piper1-gpl), Piper (original), eSpeak NG, Chatterbox, Qwen3-TTS, Kokoro, MADLAD-400 on CTranslate2, OPUS-MT, Argos Translate, llama.cpp, llama-cpp-python, Ollama, Manim Community, manim (3b1b), Blender, Motion Canvas, three.js, lottie-web, OpenToonz, Krita, Synfig, Pencil2D, Inkscape, python-audio-separator, Demucs, Spleeter, DeepFilterNet, RNNoise, Pedalboard, pyloudnorm, ACE-Step, Audacity, OpenCV, vid.stab, Real-ESRGAN, Real-ESRGAN ncnn, Practical-RIFE, RIFE ncnn, SAM 2, rembg, GFPGAN, SQLite, FastAPI, Tauri, Electron, LangGraph, Prefect, Celery, Valkey, PostgreSQL, Docker Engine (Moby), Local machine

### Free, but with commercial restrictions on the weights (6)

LTX-Video, HunyuanVideo, FramePack, Stable Video Diffusion, FLUX, Stable Audio Open

### Source-available (not open source) (4)

Remotion, Ultralytics YOLO, Redis, n8n

### Research-only / non-commercial (6)

F5-TTS, Coqui TTS / XTTS, SeamlessM4T, AudioCraft / MusicGen, CodeFormer, Wav2Lip

### Free-tier hosted services (13)

Wikimedia Commons, Openverse, Wikipedia API, Pexels, Pixabay, Freesound, Kaggle Notebooks, Google Colab free, GitHub Actions, Hugging Face Spaces (ZeroGPU), Lightning AI Studios free, Hugging Face Hub, ModelScope

### Paid options - excluded (6)

Runway / Pika / Kling / Luma / Sora / Veo, ElevenLabs / Play.ht / Azure-Google-AWS TTS, OpenAI / Anthropic / Gemini APIs, RunPod / Vast.ai / Lambda / Modal / Replicate / fal, Shutterstock / Getty / Artlist / Epidemic Sound, DeepL API / Google Translate API

## Full matrix by area

### Editing & media engine

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | LGPL-2.1+/GPL-2+ | - | 2026-11-11 | 67,051 | **DEPLOYED** | Core encode/filter/analysis engine; every render and technical QA check runs through it |
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | BSD-3-Clause | - | 2026-11-10 | 5,436 | **DEPLOYED** | Shot detection; chosen over FFmpeg scdet for adaptive detectors and a Python API |
| [OpenTimelineIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO) | Apache-2.0 | - | 2026-11-10 | 2,070 | **NEXT** | Export NEXUS timelines to Kdenlive/Resolve for manual finishing |
| [Kdenlive](https://github.com/KDE/kdenlive) | GPL-3.0 | - | 2026-11-11 | 6,090 | **NEXT** | Recommended manual finishing editor (reads OTIO/MLT); not embedded |
| [MLT Framework](https://github.com/mltframework/mlt) | LGPL-2.1 | - | 2026-11-11 | 1,874 | **CANDIDATE** | Alternative render engine (melt); FFmpeg filtergraphs cover current needs |
| [Shotcut](https://github.com/mltframework/shotcut) | GPL-3.0 | - | 2026-11-11 | 15,654 | **CANDIDATE** | Alternative manual editor |
| [OpenShot](https://github.com/OpenShot/openshot-qt) | GPL-3.0 | - | 2026-11-10 | 6,767 | **CANDIDATE** | Simpler editor; libopenshot less capable than MLT |
| [LosslessCut](https://github.com/mifi/lossless-cut) | GPL-2.0 | - | 2026-11-09 | 45,938 | **CANDIDATE** | GUI for stream-copy cuts; FFmpeg already does this headlessly |
| [Olive](https://github.com/olive-editor/olive) | GPL-3.0 | - | 2024-12-05 | 9,153 | **REJECTED** | No commits for ~2 years; unstable alpha |
| [MoviePy](https://github.com/Zulko/moviepy) | MIT | - | 2026-08-26 | 15,137 | **REJECTED** | Slower than direct filtergraphs; adds nothing over our renderer |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause | - | 2026-11-08 | 3,333 | **REJECTED** | Installed as a dependency but bypassed: its API change broke faster-whisper decoding, we decode with the ffmpeg binary |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | - | 2026-11-04 | 206,943 | **REJECTED** | Downloading from platforms generally violates their ToS; NEXUS asks the user to upload files they own |

### AI video generation

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Wan 2.1](https://github.com/Wan-Video/Wan2.1) | Apache-2.0 | Apache-2.0 (1.3B T2V runs in ~8 GB VRAM) | 2026-03-05 | 17,417 | **NEXT** | First video model to benchmark on a free Kaggle GPU session; cannot run on the CPU-only machine |
| [Wan 2.2](https://github.com/Wan-Video/Wan2.2) | Apache-2.0 | Apache-2.0 (5B TI2V needs ~24 GB without offload) | 2026-09-21 | 18,552 | **CANDIDATE** | Higher quality but heavier than free 16 GB GPUs comfortably allow |
| [LTX-Video](https://github.com/Lightricks/LTX-Video) | Apache-2.0 | LTX open-weights licence: free below a revenue threshold - verify current text | 2026-11-02 | 11,365 | **CANDIDATE** | Fastest open video model; licence terms must be re-read before commercial use |
| [CogVideoX](https://github.com/zai-org/CogVideo) | Apache-2.0 | 2B: Apache-2.0; 5B: CogVideoX licence (restricted) | 2025-11-04 | 13,197 | **CANDIDATE** | Only the 2B weights are freely commercial |
| [Mochi 1](https://github.com/genmoai/mochi) | Apache-2.0 | Apache-2.0 | 2026-10-22 | 3,767 | **CANDIDATE** | Needs far more VRAM than free tiers offer |
| [HunyuanVideo](https://github.com/Tencent-Hunyuan/HunyuanVideo) | Tencent Hunyuan Community | Tencent community licence: excludes EU/UK/South Korea and has usage limits | 2026-10-08 | 12,759 | **REJECTED** | Territorial restriction makes it unsafe for a German/European channel |
| [FramePack](https://github.com/lllyasviel/FramePack) | Apache-2.0 | uses HunyuanVideo weights (Tencent licence) | 2025-10-16 | 17,351 | **REJECTED** | Inherits the Hunyuan weight licence |
| [Stable Video Diffusion](https://github.com/Stability-AI/generative-models) | MIT | Stability AI Community Licence (revenue cap, registration) | 2025-12-16 | 27,344 | **REJECTED** | Weight licence has commercial conditions |

### Image generation & workflows

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) | GPL-3.0 | - | 2026-11-11 | 146,357 | **NEXT** | Node workflow runner for image/video models on the GPU worker |
| [Diffusers](https://github.com/huggingface/diffusers) | Apache-2.0 | - | 2026-11-11 | 34,909 | **NEXT** | Scriptable pipelines for the Kaggle batch adapter |
| [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) | MIT | - | 2026-11-10 | 7,984 | **CANDIDATE** | CPU image generation without PyTorch; to benchmark for local stills |
| [FLUX](https://github.com/black-forest-labs/flux) | Apache-2.0 | schnell: Apache-2.0; dev: non-commercial | 2025-07-31 | 26,058 | **CANDIDATE** | Only FLUX.1-schnell weights are usable commercially |

### Speech recognition & subtitles

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | MIT (Whisper weights) | 2026-11-10 | 26,670 | **DEPLOYED** | ASR + dub verification; 0.36x realtime on 4 CPU cores with small/int8 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT | MIT | 2026-10-22 | 10,639 | **DEPLOYED** | Bundled inside faster-whisper (vad_filter) |
| [WhisperX](https://github.com/m-bain/whisperX) | BSD-2-Clause | pyannote diarisation models are gated (free, needs HF account) | 2026-10-22 | 25,215 | **NEXT** | Forced alignment + speaker diarisation (speaker detection is not deployed yet) |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 | per model | 2026-11-11 | 15,885 | **NEXT** | Diarisation and VAD without PyTorch; models on GitHub releases |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | MIT | MIT | 2026-11-10 | 55,101 | **CANDIDATE** | Fallback ASR with no Python dependency |
| [Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit) | MIT | - | 2026-11-11 | 14,959 | **CANDIDATE** | Desktop subtitle editor for manual correction |
| [Whisper](https://github.com/openai/whisper) | MIT | MIT | 2026-08-31 | 112,502 | **REJECTED** | Reference implementation needs PyTorch and is ~4x slower on CPU than faster-whisper |

### Text-to-speech

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Piper (piper1-gpl)](https://github.com/OHF-Voice/piper1-gpl) | GPL-3.0 | per voice: joe CC0, ljspeech PD, thorsten CC0; kareem (Arabic) UNVERIFIED | 2026-11-11 | 6,251 | **DEPLOYED** | Fast CPU neural TTS; 0.07x realtime |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng) | GPL-3.0 | - | 2026-11-08 | 7,024 | **DEPLOYED** | Clear-licence fallback voice (robotic); phonemiser for Piper |
| [Chatterbox](https://github.com/resemble-ai/chatterbox) | MIT | MIT; multilingual model lists Arabic | 2026-11-04 | 27,762 | **NEXT** | Top candidate to replace the unverified-licence Arabic voice; needs PyTorch and a CPU-speed benchmark |
| [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) | Apache-2.0 | Apache-2.0 | 2026-03-17 | 14,369 | **CANDIDATE** | High quality but no Arabic in its listed languages; GPU-oriented |
| [Kokoro](https://github.com/hexgrad/kokoro) | Apache-2.0 | Apache-2.0 | 2025-08-06 | 10,056 | **CANDIDATE** | Excellent English, no Arabic or German |
| [Piper (original)](https://github.com/rhasspy/piper) **(archived)** | MIT | - | 2025-08-26 | 11,323 | **REJECTED** | Archived; development moved to OHF-Voice/piper1-gpl |
| [F5-TTS](https://github.com/SWivid/F5-TTS) | MIT | CC-BY-NC weights | 2026-11-03 | 15,562 | **REJECTED** | Non-commercial weights |
| [Coqui TTS / XTTS](https://github.com/coqui-ai/TTS) | MPL-2.0 | XTTS: Coqui Public Model Licence (non-commercial) | 2024-08-16 | 46,215 | **REJECTED** | Company closed, repo unmaintained, weights non-commercial |

### Translation

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [MADLAD-400 on CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT | Apache-2.0 (google/madlad400-3b-mt) | 2026-11-10 | 4,750 | **DEPLOYED** | Primary translator after a side-by-side test: correct MSA/German and real Egyptian Arabic (arz) |
| [OPUS-MT](https://github.com/Helsinki-NLP/Opus-MT) | MIT | CC-BY-4.0 / Apache-2.0 per pair | 2026-02-23 | 875 | **CANDIDATE** | Smaller and faster but no Egyptian Arabic |
| [Argos Translate](https://github.com/argosopentech/argos-translate) | MIT | MIT/CC0 | 2026-08-08 | 6,644 | **CANDIDATE** | Easy packaging; quality below MADLAD |
| [SeamlessM4T](https://github.com/facebookresearch/seamless_communication) | CC-BY-NC-4.0 | CC-BY-NC | 2026-09-08 | 11,905 | **REJECTED** | Non-commercial |

### Local language models

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | Qwen3-4B GGUF: Apache-2.0 | 2026-11-11 | 134,134 | **DEPLOYED** | Local scripting/planning/review LLM; 6.9 tok/s on 4 cores |
| [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) | MIT | - | 2026-11-08 | 10,673 | **DEPLOYED** | Python binding used by nexus.llm |
| [Ollama](https://github.com/ollama/ollama) | MIT | - | 2026-11-11 | 186,675 | **CANDIDATE** | Easier model management on the user's desktop; same engine |

### Animation & motion graphics

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Manim Community](https://github.com/ManimCommunity/manim) | MIT | - | 2026-11-11 | 42,421 | **DEPLOYED** | 2D explainer/diagram animation, driven from a spec |
| [Blender](https://github.com/blender/blender) | GPL-3.0-or-later | - | 2026-11-11 | 21,883 | **DEPLOYED** | Headless 3D (Cycles CPU); 3.2 s/frame at 640x360/20 samples here |
| [Motion Canvas](https://github.com/motion-canvas/motion-canvas) | MIT | - | 2026-07-02 | 19,773 | **CANDIDATE** | TypeScript motion graphics; would add a Node toolchain |
| [three.js](https://github.com/mrdoob/three.js) | MIT | - | 2026-11-11 | 118,175 | **CANDIDATE** | WebGL scenes for the future desktop UI preview |
| [lottie-web](https://github.com/airbnb/lottie-web) | MIT | - | 2025-09-01 | 32,398 | **CANDIDATE** | Play Lottie animations; asset licences vary per file |
| [OpenToonz](https://github.com/opentoonz/opentoonz) | BSD-3-Clause (modified) | - | 2026-11-10 | 8,174 | **CANDIDATE** | Hand-drawn 2D; GUI tool, hard to automate |
| [Krita](https://github.com/KDE/krita) | GPL-3.0 | - | 2026-11-11 | 10,727 | **CANDIDATE** | Painting/frame animation; GUI tool |
| [Synfig](https://github.com/synfig/synfig) | GPL-3.0 | - | 2026-11-08 | 2,404 | **CANDIDATE** | Vector tweening with a CLI renderer |
| [Pencil2D](https://github.com/pencil2d/pencil) | GPL-2.0 | - | 2026-11-10 | 1,879 | **CANDIDATE** | Simple hand-drawn animation |
| Inkscape | GPL-2.0+ | - | n/a | - | **CANDIDATE** | SVG authoring/CLI export; GitHub mirror is stale, official repo is on GitLab |
| [manim (3b1b)](https://github.com/3b1b/manim) | MIT | - | 2026-09-09 | 96,390 | **REJECTED** | Author's personal fork; Community edition is the documented, packaged one |
| [Remotion](https://github.com/remotion-dev/remotion) | Remotion Licence | free for individuals/small teams, paid company licence | 2026-11-11 | 72,491 | **REJECTED** | Not open source; licence fee beyond a team-size threshold |

### Audio production

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | MIT | per model (MDX/Demucs weights MIT) | 2026-10-29 | 1,487 | **NEXT** | Voice/music separation so dubbing can keep the original music and effects |
| [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) | MIT/Apache-2.0 | MIT/Apache-2.0 | 2026-11-06 | 4,999 | **NEXT** | Better speech denoising than the FFmpeg afftdn filter used now |
| [ACE-Step](https://github.com/ace-step/ACE-Step) | Apache-2.0 | Apache-2.0 | 2026-02-15 | 5,051 | **NEXT** | Open-weight music generation to benchmark on the GPU worker (replaces procedural pads) |
| [Spleeter](https://github.com/deezer/spleeter) | MIT | MIT | 2026-10-28 | 28,525 | **CANDIDATE** | Older separation quality |
| [RNNoise](https://github.com/xiph/rnnoise) | BSD-3-Clause | BSD | 2025-02-22 | 5,961 | **CANDIDATE** | FFmpeg arnndn filter can load its models |
| [Pedalboard](https://github.com/spotify/pedalboard) | GPL-3.0 | - | 2026-11-09 | 6,475 | **CANDIDATE** | Studio effects chain |
| [Audacity](https://github.com/audacity/audacity) | GPL-3.0 | - | 2026-11-11 | 19,028 | **CANDIDATE** | Manual audio repair on the desktop |
| [Demucs](https://github.com/facebookresearch/demucs) **(archived)** | MIT | MIT | 2024-04-24 | 10,407 | **REJECTED** | Archived upstream; use via maintained audio-separator instead |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | MIT | - | 2026-01-04 | 792 | **REJECTED** | FFmpeg loudnorm/ebur128 already measure and normalise |
| [AudioCraft / MusicGen](https://github.com/facebookresearch/audiocraft) | MIT | MusicGen weights CC-BY-NC | 2026-03-03 | 23,848 | **REJECTED** | Non-commercial weights |
| [Stable Audio Open](https://github.com/Stability-AI/stable-audio-tools) | MIT | Stability AI Community Licence | 2026-11-01 | 3,906 | **REJECTED** | Weight licence has commercial conditions |

### Visual enhancement & vision

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [OpenCV](https://github.com/opencv/opencv) | Apache-2.0 | - | 2026-11-11 | 91,532 | **DEPLOYED** | Sharpness/exposure sampling; Haar face cascades are absent in OpenCV 5 (face reframing currently off) |
| vid.stab | GPL-2.0+ | - | n/a | - | **DEPLOYED** | Stabilisation filters are compiled into the installed FFmpeg (not yet exposed in the UI) |
| [rembg](https://github.com/danielgatis/rembg) | MIT | per model | 2026-11-06 | 25,426 | **NEXT** | CPU background removal for cut-outs and thumbnails |
| [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | BSD-3-Clause | BSD-3 | 2024-08-06 | 37,459 | **CANDIDATE** | Upscaling; stale since 2024 and needs a GPU for video |
| [Real-ESRGAN ncnn](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) | MIT | BSD-3 | 2024-05-10 | 2,294 | **CANDIDATE** | Vulkan build for integrated GPUs |
| [Practical-RIFE](https://github.com/hzwer/Practical-RIFE) | MIT | MIT | 2026-08-27 | 1,044 | **CANDIDATE** | Frame interpolation on the GPU worker |
| [RIFE ncnn](https://github.com/nihui/rife-ncnn-vulkan) | MIT | MIT | 2024-01-02 | 1,130 | **CANDIDATE** | Vulkan interpolation |
| [SAM 2](https://github.com/facebookresearch/sam2) | Apache-2.0 | Apache-2.0 | 2026-05-30 | 20,138 | **CANDIDATE** | Video object masks for VFX; GPU |
| [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) | AGPL-3.0 | AGPL or paid enterprise licence | 2026-11-11 | 63,301 | **REJECTED** | AGPL obligations / paid licence for closed use |
| [GFPGAN](https://github.com/TencentARC/GFPGAN) | Apache-2.0 | mixed | 2024-07-26 | 37,709 | **REJECTED** | Unmaintained |
| [CodeFormer](https://github.com/sczhou/CodeFormer) | S-Lab Licence | non-commercial | 2025-11-18 | 18,228 | **REJECTED** | Non-commercial |
| [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) | none (research) | non-commercial | 2025-06-22 | 13,273 | **REJECTED** | Non-commercial; lip-sync therefore NOT available yet |

### Backend & orchestration

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [SQLite](https://github.com/sqlite/sqlite) | Public domain | - | 2026-11-11 | 10,980 | **DEPLOYED** | Project/job/asset/ledger store; no server to run |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | - | 2026-11-11 | 104,624 | **DEPLOYED** | Local backend API + UI host |
| [Tauri](https://github.com/tauri-apps/tauri) | Apache-2.0/MIT | - | 2026-11-10 | 112,789 | **NEXT** | Desktop shell for the web UI (smaller than Electron) |
| [Electron](https://github.com/electron/electron) | MIT | - | 2026-11-11 | 123,860 | **CANDIDATE** | Heavier desktop shell alternative |
| [Prefect](https://github.com/PrefectHQ/prefect) | Apache-2.0 | - | 2026-11-10 | 24,152 | **CANDIDATE** | Workflow scheduler if the built-in queue is outgrown |
| [Celery](https://github.com/celery/celery) | BSD-3-Clause | - | 2026-11-11 | 29,175 | **CANDIDATE** | Distributed queue; needs a broker |
| [Valkey](https://github.com/valkey-io/valkey) | BSD-3-Clause | - | 2026-11-11 | 27,689 | **CANDIDATE** | Open-source Redis fork if a broker is needed |
| [PostgreSQL](https://github.com/postgres/postgres) | PostgreSQL Licence | - | 2026-11-11 | 22,792 | **CANDIDATE** | Only if multi-user; SQLite is enough for one machine |
| [Docker Engine (Moby)](https://github.com/moby/moby) | Apache-2.0 | - | 2026-11-11 | 72,264 | **CANDIDATE** | Optional sandbox for evaluating new repositories |
| [LangGraph](https://github.com/langchain-ai/langgraph) | MIT | - | 2026-11-11 | 44,812 | **REJECTED** | Agent graph framework; our pipelines are deterministic steps with checkpoints, no need |
| [Redis](https://github.com/redis/redis) | RSALv2/SSPLv1/AGPLv3 | - | 2026-11-11 | 77,445 | **REJECTED** | Licence changed; Valkey is the BSD alternative |
| [n8n](https://github.com/n8n-io/n8n) | Sustainable Use Licence | fair-code, restrictions on commercial hosting | 2026-11-11 | 210,200 | **REJECTED** | Not open source |

### Free media & research sources

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Wikimedia Commons](https://commons.wikimedia.org) | per file | PD/CC0/BY/BY-SA accepted; licence read per file | n/a | - | **DEPLOYED** | Primary image source with authoritative per-file licence metadata |
| [Openverse](https://github.com/WordPress/openverse) | MIT (API) | per asset | 2026-11-11 | 604 | **DEPLOYED** | Secondary CC search (anonymous, rate limited; returned 504s during testing) |
| [Wikipedia API](https://www.mediawiki.org/wiki/API) | CC BY-SA 4.0 text | - | n/a | - | **DEPLOYED** | Research source with citations |
| [Pexels](https://www.pexels.com/api/) | Pexels Licence | free commercial use; no standalone redistribution; API key (free account) | n/a | - | **NEXT** | Stock video/photos; needs the user's free API key |
| [Pixabay](https://pixabay.com/api/docs/) | Pixabay Content Licence | free commercial use with restrictions; API key (free account) | n/a | - | **NEXT** | Stock video/music; needs the user's free API key |
| [Freesound](https://freesound.org/docs/api/) | per sound (CC0/BY/BY-NC) | filter to CC0/BY; API key (free account) | n/a | - | **NEXT** | Sound effects library; BY-NC must be filtered out |

### Free compute

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Local machine](https://github.com/n/a) | n/a | - | n/a | - | **DEPLOYED** | Always-available worker; everything in this build runs here |
| [Kaggle Notebooks](https://github.com/Kaggle/kaggle-cli) | Apache-2.0 (client) | free weekly GPU quota, no card | 2026-11-04 | 7,657 | **NEXT** | Batch GPU adapter written, NOT tested (needs the user's account token) |
| [Google Colab free](https://research.google.com/colaboratory/faq.html) | service | interactive only; automation prohibited | n/a | - | **NEXT** | Manual notebook hand-off only |
| [GitHub Actions](https://docs.github.com/actions) | service | free minutes for public repos | n/a | - | **CANDIDATE** | CI for the test suite only - never a render farm |
| [Lightning AI Studios free](https://lightning.ai) | service | monthly free credits; terms change | n/a | - | **CANDIDATE** | Re-check whether a card is required before any use |
| [Hugging Face Spaces (ZeroGPU)](https://huggingface.co/docs/hub/spaces-zerogpu) | service | small daily GPU quota per account; meant for demos | n/a | - | **REJECTED** | Quota and terms do not fit batch production |

### Model sources

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Hugging Face Hub](https://huggingface.co) | per model | public downloads need no account | n/a | - | **DEPLOYED** | Source of all model weights (revision recorded per download) |
| [ModelScope](https://modelscope.cn) | per model | - | n/a | - | **CANDIDATE** | Mirror for Wan/Qwen weights if HF is unreachable |

### Paid services

| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |
|---|---|---|---|---|---|---|
| [Runway / Pika / Kling / Luma / Sora / Veo](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Paid video generation - blocked by the Cost Guard |
| [ElevenLabs / Play.ht / Azure-Google-AWS TTS](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Paid TTS - blocked by the Cost Guard |
| [OpenAI / Anthropic / Gemini APIs](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Metered LLM APIs - blocked by the Cost Guard |
| [RunPod / Vast.ai / Lambda / Modal / Replicate / fal](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Paid GPU compute - blocked by the Cost Guard |
| [Shutterstock / Getty / Artlist / Epidemic Sound](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Paid stock libraries |
| [DeepL API / Google Translate API](https://github.com/n/a) | proprietary | - | n/a | - | **REJECTED** | Free tiers require a card / billing account |
