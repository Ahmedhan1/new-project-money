# Account 3 - AI model and provider catalogue

Checked: **2026-11-12** (agent B, branch `work/account-3-b-models`). Routing facts live in
`nexus/intelligence/capability_registry/providers.yaml`; this document is the full table with sources.

Rules for reading this table
- **verified** = read on the first-party page cited in the row on 2026-11-12. **unverified** = not stated on a
  first-party page I could reach; do not rely on it. **est.** = my estimate, not a measurement.
- Memory figures marked *est.* use the rule of thumb "Q4 GGUF ~ 0.6 GB per billion parameters + context"; only the
  two models in "Measured here" were actually run.
- Free-tier numbers change often. The owner's console is authoritative; the router additionally honours
  owner-set limits (`settings.json ai_providers.<id>.daily_requests / daily_tokens`).
- Zero-spend: any service that requires a payment card is **never routed** (`payment_method_required: true`), and a
  service without any free quota is **never routed** (`free_quota: none`).

## 1. Measured here (this 4-CPU / 15 GB machine, no GPU)

| Provider id | Model id | Weights | Licence | Commercial | Size | Context used | Notes |
|---|---|---|---|---|---|---|---|
| `local-llamacpp` | `qwen3-4b-q4km` | Qwen/Qwen3-4B-GGUF Q4_K_M | Apache-2.0 (verified, HF card) | yes | 2.5 GB file | 8192 | in-process llama-cpp-python 0.3.36 |
| `owner-openai-compatible` | `qwen3-0.6b-q8` | Qwen/Qwen3-0.6B-GGUF `Qwen3-0.6B-Q8_0.gguf` (639,446,688 bytes) | Apache-2.0 (verified, HF card + LICENSE file) | yes | 0.64 GB | 4096 | llama-cpp-python's OpenAI-compatible server on 127.0.0.1 |

Scores, latency and tokens/s: `docs/ACCOUNT3_PROVIDER_BENCHMARKS.md`, evidence `docs/results/account3/models_bench.json`.

## 2. Open-weight model families (self-hostable)

Source for ids, dates and licence tags: Hugging Face Hub API `https://huggingface.co/api/models/<repo>` (card
`license` field), queried 2026-11-12. Context/RAM not listed in the API are marked unverified/est.

| Family | Representative repo ids (newest first) | Released | Licence (card) | Commercial use | Inputs -> outputs | RAM/VRAM | Notes |
|---|---|---|---|---|---|---|---|
| Qwen3.8 | `Qwen/Qwen3.8-27B` (+FP8), `Qwen/Qwen3.8-Flash-Next` (180B), `Qwen/Qwen3.8-2.4T-A95B` | 2026-08 | 27B: Apache-2.0; 2.4T: "other / qwen3.8-max" | 27B yes; 2.4T unverified (custom licence) | image+text -> text | 27B: ~17 GB Q4 est.; needs GPU | 27B is also on Groq/Cerebras |
| Qwen3.6 | `Qwen/Qwen3.6-35B-A3B`, `Qwen/Qwen3.6-27B` | 2026 | Apache-2.0 | yes | image+text -> text | 35B-A3B: ~21 GB Q4 est. (3B active: CPU-feasible on 32 GB) | MoE |
| Qwen3 (small) | `Qwen/Qwen3-0.6B`, `Qwen3-1.7B`, `Qwen3-4B` (+ `-GGUF`) | 2025 | Apache-2.0 | yes | text -> text | 0.6B Q8 0.64 GB, 1.7B Q8 1.83 GB, 4B Q4 2.5 GB (file sizes, verified) | the two local models above |
| Qwen3 Next / Coder | `Qwen/Qwen3-Next-80B-A3B-Instruct-GGUF`, `Qwen/Qwen3-Coder-Next` | 2025-12 / 2026-01 | Apache-2.0 | yes | text -> text | ~48 GB Q4 est. | |
| DeepSeek V4 | `deepseek-ai/DeepSeek-V4.1-Flash-1017`, `DeepSeek-V4.1-Flash`, `DeepSeek-V4-Pro(-0813)` | 2026-04 .. 2026-10 | MIT | yes | text -> text (`V4-Flash-Vision-Exp`: image) | multi-GPU (size not checked) | Mistral's model page also lists "DeepSeek V4.1 Flash 1017, MIT, 1M context" |
| DeepSeek V3.2 | `deepseek-ai/DeepSeek-V3.2`, `-Speciale` | 2025-11/12 | MIT | yes | text -> text | multi-GPU | |
| Mistral | `mistralai/Mistral-Large-4-1T-A52B`, `Mistral-Medium-3.5-128B`, `Leanstral-1.5-119B-A6B`, `Shieldstral-1.0-3B`, `LIDstral-Arabic`; Small 4 / Ministral 3 (3B/8B/14B) | 2026 | Large 4: "Mistral Ecosystem Open Weight License"; Medium 3.5: other; Small 4, Ministral 3, Leanstral, Shieldstral: Apache-2.0 (docs.mistral.ai models page + HF tags) | Apache ones yes; others unverified | text (some vision) | Ministral 3B ~2 GB Q4 est. | Ministral 3 3B/8B: good small multilingual candidates for this CPU box (not tested) |
| Llama | `meta-llama/Llama-4-Scout-17B-16E-Instruct`; `Llama-3.3-70B-Instruct`; `Llama-3.2-1B/3B` | 2024-2025 | Llama 4 / 3.3 / 3.2 Community Licence (gated, manual approval) | yes **with conditions** | Llama 4: image+text -> text | Scout 109B total: GPU | Llama 4 licence (dev.meta.ai/llama/llama4/license, effective 2025-04-05): >700M MAU needs a separate Meta licence; "Built with Llama" must be displayed; derived models must be named starting "Llama"; Acceptable Use Policy applies. No EU-specific clause found in the Llama 4 text (the multimodal EU restriction of Llama 3.2 not re-checked). |
| NVIDIA Nemotron | `nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB`, `Nemotron-3.5-Bolt-74B-A9B`, `Nemotron-Labs-Dory1.0-4B-GGUF`, `Nemotron-3.5-Super-VL-120B-A12B` | 2026-10 | OpenMDW-1.1 (openmdw.ai/license/1-1) | unverified (licence text not read) | text; Super-VL: image+text | E4B: "4GB" variant | Dory 4B GGUF is a CPU candidate |
| GLM (Z.ai) | `zai-org/GLM-5.3` (753B), `GLM-5.2`, `GLM-5.1`, `GLM-5`, `GLM-4.7-Flash` | 2025-12 .. 2026-08 | GLM-5.3: "other / glm-5.3" on HF, MIT per docs.mistral.ai; GLM-5.2 and older: MIT | <=5.2 yes; 5.3 unverified (sources disagree) | text -> text | multi-GPU | |
| Gemma 4 | `google/gemma-4-E2B/E4B/12B/31B/26B-A4B-it-assistant` (+ QAT q4_0) | 2026-04 .. 06 | Apache-2.0 tag, licence link ai.google.dev/gemma/docs/gemma_4_license | yes (per tag; licence page not read) | any-to-any (E4B) | E2B/E4B are phone/CPU class | older Gemma (`gemma` licence) is gated |
| Aya / Cohere | `CohereLabs/tiny-aya-fire/earth/water`, `tiny-aya-*-thinker`, `North-Small-Translate-1.0`; `North-Mini-Code-1.0` | 2026 | tiny-aya, North-Translate: **CC-BY-NC-4.0** (gated auto); North-Mini-Code: Apache-2.0 | **no** for CC-BY-NC models | text -> text | tiny-aya 3.3B: ~2 GB Q4 est. | strong multilingual, but non-commercial: allowed for evaluation only |
| gpt-oss | `openai/gpt-oss-20b`, `gpt-oss-120b` | 2025 | Apache-2.0 (per Groq/Cerebras catalogues; HF card not re-read) | yes | text -> text | 20B ~13 GB est. | hosted free on Groq |

## 3. Runtimes (all serve an OpenAI-compatible `/v1/chat/completions`; the router's one adapter covers them)

| Runtime | Licence | How it serves | JSON/structured output | Verified |
|---|---|---|---|---|
| llama.cpp / llama-cpp-python | MIT | `python -m llama_cpp.server --model x.gguf --port 8765` (needs `sse-starlette`, `starlette-context`, `pydantic-settings`) | `response_format: json_object` (+ schema grammar) | **run here** 2026-11-12 (0.3.36) |
| Ollama | MIT | `http://localhost:11434/v1/`; `/v1/chat/completions`, `/v1/models`, `response_format` supported (docs.ollama.com/api/openai-compatibility) | yes | docs 2026-11-12; binary not installed here |
| vLLM | Apache-2.0 | `vllm serve <model>` (docs.vllm.ai/en/latest/serving/online_serving/) | page re-checked did not mention `response_format` -> unverified | docs 2026-11-12 |
| SGLang | Apache-2.0 | `python3 -m sglang.launch_server --model-path <m>`; examples use `http://127.0.0.1:30000/v1`; structured outputs (JSON/regex/EBNF) documented (docs.sglang.io/basic_usage/openai_api_completions.html) | yes | docs 2026-11-12 |
| Transformers | Apache-2.0 | library; `transformers serve` not checked | n/a | runtime licences per upstream LICENSE files, not re-fetched today |

## 4. Hosted APIs (free tiers only)

| Provider id | Host | Free text models (ids) | Rate limits (documented) | Free quota | Card needed | Privacy / training | Status in router |
|---|---|---|---|---|---|---|---|
| `gemini` | generativelanguage.googleapis.com (OpenAI-compatible `/v1beta/openai`) | "Free of charge": `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-2.5-pro/-flash/-flash-lite` (2.5 deprecated, rerouted 2026-12-01). Not free: `gemini-4-carbon`, `gemini-4-argon`, `gemini-3.1-pro-preview` | per project; only shown at aistudio.google.com/rate-limit (not public for these ids) | yes, no billing account needed (rate-limits page) | no | **free tier: "Used to improve our products: Yes"** -> public data only | routable after owner authorisation + key |
| `groq` | api.groq.com/openai/v1 | `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b` (llama-3.x now "Enterprise") | base: 30 RPM, 1K RPD, 8K TPM, 200K TPD per model (page says these are Developer-plan base limits) | yes (free plan; exact numbers in console) | unverified (pricing page silent) | no retention by default; logs up to 30 days for troubleshooting/abuse; ZDR opt-in (console.groq.com/docs/your-data) | routable after authorisation + key |
| `openrouter-free` | openrouter.ai/api/v1 | only `:free` ids, e.g. `liquid/lfm-3-1.7b:free`, `baidu/ernie-5.5-flash-preview:free`, `thinkingmachines/inkling-2-small:free` (list changes) | 20 RPM; 50 RPD (<10 credits ever bought) | yes | no | upstream may log prompts | routable; non-`:free` ids refused |
| `mistral-free` | api.mistral.ai/v1 | Experiment plan; ids unverified (`mistral-small-latest` assumed) | not published; console | yes (Experiment plan) | phone verification, no card per help centre (unverified - page 404 on re-check) | reported: may be used for training | routable after authorisation (public only) |
| `cerebras` | api.cerebras.ai/v1 | `gpt-oss-120b`, `qwen-3.8-27b` | Free Trial: 5 RPM, 90K TPM, 1M TPD | one-time $5 promo credit, 30 days | **yes** ("Add a valid payment method", cerebras.ai/pricing) | not stated | **never routed** (card) |
| `huggingface-free` | router.huggingface.co/v1 | 200+ via providers | - | **none for free users** (pricing page) | credits purchase required | per provider | **never routed** (no free quota) |

## 5. Sources (all fetched 2026-11-12)
- https://console.groq.com/docs/rate-limits , https://console.groq.com/docs/models , https://console.groq.com/docs/your-data
- https://aistudio.google.com/docs/pricing , https://aistudio.google.com/docs/rate-limits
- https://inference-docs.cerebras.ai/support/rate-limits , https://inference-docs.cerebras.ai/models/overview , https://www.cerebras.ai/pricing
- https://openrouter.ai/docs/api-reference/limits , https://openrouter.ai/models?max_price=0
- https://huggingface.co/docs/inference-providers/pricing
- https://docs.mistral.ai/getting-started/models/models_overview/ ; help.mistral.ai Experiment-plan article (search snippet only; page returned 404)
- https://dev.meta.ai/llama/llama4/license/
- https://docs.ollama.com/api/openai-compatibility , https://docs.vllm.ai/en/latest/serving/online_serving/ , https://docs.sglang.io/basic_usage/openai_api_completions.html
- Hugging Face Hub API `https://huggingface.co/api/models?author=<org>` and `/api/models/<repo>` for Qwen, deepseek-ai, mistralai, meta-llama, nvidia, zai-org, google, CohereLabs

## 6. How the owner enables a hosted free provider
1. Create the account yourself (never with a card for this project). 2. Put the key in the environment as
`NEXUS_KEY_<ID>` (e.g. `NEXUS_KEY_GROQ`, `NEXUS_KEY_OPENROUTER_FREE`) or in Account 1's vault under `ai/<id>` - never in Git.
3. In `NEXUS_HOME/settings.json`: `{"ai_providers": {"groq": {"authorized": true, "daily_tokens": 150000}}}`.
4. `python -m nexus.intelligence.model_router status --privacy public` shows which tasks now route there and why.
Private and internal data never go to a hosted provider regardless of these settings.
