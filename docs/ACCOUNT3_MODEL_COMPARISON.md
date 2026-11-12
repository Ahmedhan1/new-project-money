# Account 3 - model comparison, routing policy and Account 1 alignment

Date 2026-11-12. Measured numbers: `docs/ACCOUNT3_PROVIDER_BENCHMARKS.md`. Catalogue: `docs/ACCOUNT3_AI_MODEL_CATALOG.md`.

## 1. Which model for which task (current routing, no hosted key configured)

The router ranks by measured quality once the benchmark has run (`ai_quality.<cap>` metrics, 0-10), else by the
catalogue estimate. Light tasks take the fastest model that clears the bar; heavy tasks take the strongest; ties go to
the more private provider. See the live answer with:

    NEXUS_HOME=... python -m nexus.intelligence.model_router status --privacy internal
    NEXUS_HOME=... python -m nexus.intelligence.model_router explain title_generation --privacy public

| Data class | Who may receive it | Effect today |
|---|---|---|
| `private` (user media, unreleased scripts, client data) | in-process only (`local-llamacpp`) | Qwen3-4B for writing/JSON/summary/translation tasks; the four reasoning-heavy tasks (deep_research, reference_interpretation, quality_review, analytics_interpretation) -> `NoProviderAvailable` (measured reasoning 3.3 < 4) |
| `internal` | in-process + owner-run servers (`owner-openai-compatible`: local llama.cpp server, Ollama, vLLM/SGLang on the owner's GPU) | Qwen3-0.6B server first for light summarization/JSON/title tasks (faster), Qwen3-4B otherwise and as fallback (demo: server stopped -> 4B answered, fallbacks listed) |
| `public` | everything above + authorised hosted free tiers | hosted providers only after owner authorisation + key; Cerebras and HF never |

## 2. Router guarantees (unchanged strictness, plus new gates)
- Privacy gate first; nothing private leaves the process.
- Zero spend: cost guard on every network call; HTTP 402 refused; new static gate refuses any provider with
  `payment_method_required: true` or `free_quota: none` even if the owner authorises it; OpenRouter only `:free` ids.
- Quotas: documented rpm/rpd/tpm/tpd (90 % ceiling for daily ones) **and** owner-set `daily_requests` / `daily_tokens`
  per provider (settings.json), applied to every provider kind.
- Token budget: `estimate_tokens()` (deliberately high: 3.5 chars/token Latin, 1/token CJK, 2.5 chars/token
  Arabic/Devanagari) + `max_tokens` must fit the model context and the remaining token quota.
- `health()` now reports p50/p95 latency over the last 50 successes, last success/failure, last error, tokens today,
  owner limits and whether zero-spend blocks the provider.
- `route_explain(task, privacy)` returns the ranked route, quality source (measured/estimate), licence, locality, all
  exclusion reasons and the list of gates - for the UI.

## 3. Account 1 alignment (`AIProviderCapability/v1`)

`nexus.intelligence.model_router.account1_capabilities()` (CLI: `... model_router capabilities`) returns one
`AIProviderCapability/v1` dict per provider that could ever run (zero-spend-blocked providers are omitted):

| Account 1 field | Filled from |
|---|---|
| `provider`, `connection_id` | registry id; `a3:<id>` (Account 1 may substitute its connection id) |
| `tasks` | router task names whose quality bar at least one of the provider's models clears |
| `models` | catalogue models + owner-declared server models |
| `local` | `hosting in (in_process, owner_worker)` |
| `commercial_use` | `yes` only when every model's licence is Apache-2.0/MIT or marked yes; `no` if any is no; else `unverified` |
| `quotas` | `QuotaStatus/v1` items, `resource: api_requests`, documented rpd/rpm only, `source: documented`, `used` = today's count |

Account 1 only meters and schedules; the router still makes the final eligibility decision per request (keys,
breaker, owner limits), so a capability in this list is "may be able to run", never a promise.

## 4. Registering an owner-run GPU worker (vLLM / SGLang)

**Persistent endpoint (owner's own GPU or any machine the owner controls)**
1. On the GPU machine: `vllm serve Qwen/Qwen3.6-35B-A3B --port 8000 --api-key <random>` or
   `python3 -m sglang.launch_server --model-path Qwen/Qwen3.6-35B-A3B --port 30000`
   (pick an Apache-2.0/MIT model from the catalogue; CC-BY-NC models such as tiny-aya are evaluation-only).
2. Make it reachable only to this machine (SSH tunnel / private network). For a non-localhost host, declare it in the
   cost-guard manifest of a zero-cost component (Account 1 owns `costguard`) - otherwise the adapter refuses the call.
3. `settings.json`:
   `{"ai_providers": {"owner-openai-compatible": {"base_url": "http://127.0.0.1:8000/v1", "models": [{"model": "Qwen/Qwen3.6-35B-A3B", "licence": "Apache-2.0", "commercial_use": "yes", "context": 32768, "speed": "fast", "quality": {"reasoning": 7, "writing": 7, "json": 7, "translation": 7, "arabic": 6, "summarization": 7, "source": "estimate"}}], "daily_tokens": 2000000}}}`
   and the key in `NEXUS_KEY_OWNER_OPENAI_COMPATIBLE`.
4. Run the benchmark against it (`... model_router bench --providers owner-openai-compatible --server-model ...` for
   a local GGUF, or adapt `run_entry` with `base_url`) so the router uses measured quality.

**Kaggle (Account 1's worker)**: Account 1's `docs/COMPUTE.md` lists "persistent serving" as not allowed on Kaggle and
the worker as a batch adapter (`kaggle kernels push`, poll, download). So a Kaggle GPU cannot be an
`owner-openai-compatible` endpoint. The compliant shape is a **batch provider**: Account 3 writes a JSONL of
non-urgent prompts (deep_research, story_planning, quality_review with `privacy <= internal`), Account 1 runs vLLM's
offline `LLM.generate` over it in one Kaggle job, and the outputs come back as an artifact. See integration request 2.

## 5. Integration requests (for the lead)
1. `docs/DECISIONS.md`: record "Cerebras and Hugging Face Inference Providers excluded: card / no free quota (2026-11-12)"
   and "second local provider = llama.cpp server + Qwen3-0.6B".
2. Account 1: a `batch_llm` job kind for the Kaggle worker (input JSONL of `{id, task, system, prompt, max_tokens}`,
   output JSONL `{id, text, usage}`); Account 3 would add a `kind: batch_openai` provider whose `complete()` raises
   `NoProviderAvailable("queued")` and whose results are picked up asynchronously.
3. UI/api.py: expose `route_explain()` and `health()` at e.g. `GET /api/ai/route?task=&privacy=` and `GET /api/ai/health`.
4. `scripts/setup.sh`: optional `pip install sse-starlette starlette-context pydantic-settings` (llama.cpp server extras)
   and the 639 MB Qwen3-0.6B GGUF into `NEXUS_HOME/models/hf/qwen3-0.6b-gguf/` for the second local provider.
6. **Behaviour change to note for all agents**: after the benchmark, the shared `.nexus` DB holds measured reasoning
   3.33 for Qwen3-4B (n=6, thinking off), so the four reasoning-heavy tasks raise `NoProviderAvailable` locally. Owner
   decision needed: (a) enable Qwen3 thinking for `light: False` tasks in `nexus/llm.py` (slower) and re-run the bench,
   (b) lower `min_q` for those tasks, or (c) configure an owner GPU worker / hosted free tier for public data.
5. Model registry (`nexus/models.py`, not mine): register `llm-qwen3-0.6b` (Qwen/Qwen3-0.6B-GGUF, Apache-2.0, sha256 in
   the benchmarks doc) so `python -m nexus models fetch` can install it.
