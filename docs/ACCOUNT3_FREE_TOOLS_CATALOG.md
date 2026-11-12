# Account 3 - free tools catalogue

Checked 2026-11-12. Zero-spend rule: a tool is listed as usable only if it works without a payment method. Other
agents may append sections for their areas below the AI part.

## AI (text models and runtimes) - agent B

Full facts and sources: `docs/ACCOUNT3_AI_MODEL_CATALOG.md`. Routing config: `nexus/intelligence/capability_registry/providers.yaml`.

| Tool | What for | Cost / card | Privacy class allowed | Status here |
|---|---|---|---|---|
| llama-cpp-python (MIT) + Qwen3-4B GGUF (Apache-2.0) | default local model, all tasks | free, local | private | installed, measured |
| llama-cpp-python `[server]` + Qwen3-0.6B GGUF (Apache-2.0) | second local provider via the OpenAI protocol; fast light tasks | free, local | internal (could be private on the same machine; kept at internal because an owner server can be remote) | installed (extras `sse-starlette`, `starlette-context`, `pydantic-settings`), measured |
| Ollama (MIT) | owner-run server, `http://localhost:11434/v1` | free, local | internal | documented, not installed |
| vLLM / SGLang (Apache-2.0) | owner GPU worker (e.g. Account 1's Kaggle worker) | free software; GPU time per the owner's account | internal | documented, not run (no GPU here) |
| MADLAD-400 3B on CTranslate2 (Apache-2.0) | machine translation; also the benchmark's back-translation scorer | free, local | private | installed (nexus.speech.mt) |
| Google Gemini API free tier | strong hosted model for public data | free tier, no billing account; free-tier prompts are used to improve Google products | public only | adapter + config; owner must authorise + add key |
| Groq free plan | fast hosted gpt-oss / Qwen3.8-27B | free plan; card requirement not stated (unverified) | public only | adapter + config; owner must authorise + add key |
| OpenRouter `:free` models | fallback hosted models | free, 50 requests/day without purchased credits | public only | adapter + config; non-`:free` ids refused |
| Mistral Experiment plan | hosted Mistral models | phone verification, no card (help centre; unverified on re-check) | public only | adapter + config |
| Cerebras | - | needs a payment method for the $5 trial credit | - | **excluded** (never routed) |
| Hugging Face Inference Providers | - | free users get no monthly credits | - | **excluded** (never routed) |
