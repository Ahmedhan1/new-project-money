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

## Research, publishing and analytics sources (lead summary of streams A, C, E)

Facts and first-party URLs: `docs/ACCOUNT3_RESEARCH_SOURCES.md`, `docs/ACCOUNT3_PLATFORM_CAPABILITIES.md`,
`docs/ACCOUNT3_CREATOR_INTELLIGENCE.md`. Declared hosts: `nexus/registry/components.yaml` (cost guard).

| Tool | What for | Cost / card | Status here |
|---|---|---|---|
| Wikipedia Action API (en, ar, de, fr, hi, zh, ru) | facts, per-wiki readership (`prop=pageviews`) | free, no key | used (demo reports) |
| Wikimedia Pageviews REST API | daily demand per article | free, no key | declared; 429 rate limits from the shared sandbox IP |
| GDELT DOC 2.0 | news momentum | free, no key; commercial terms unverified | declared as adapter; connection reset from the build sandbox |
| NASA Image & Video Library, Openverse | available licensed visuals (production-difficulty signal) | free | used |
| RSS/Atom feeds | owner-chosen news/industry feeds | free | parser built; owner adds feeds + declares their hosts |
| YouTube Data API v3 / Analytics API | public metadata, competition, private uploads, owner analytics | owner's own Google Cloud project, default free quota, no billing | declared; needs the owner's key / OAuth |
| Google Trends API | search demand | limited alpha (access by application) | adapter disabled; never scraped |
| TikTok Research API | - | academic/approved researchers only | not built |
| TikTok Content Posting, Instagram/Facebook Graph | publishing | free, need approved apps; TikTok unaudited apps post private only | gated adapters, never run |
| Snapchat Marketing API | - | ads-oriented, no organic posting | manual export package |
