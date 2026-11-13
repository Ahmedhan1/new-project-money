# Account 3 - provider benchmarks (measured here)

Run `bench-20261112T174119`, 2026-11-12, 4 vCPU / 15 GB RAM / no GPU, machine shared with other agents (timings
vary with their load; each provider ran alone under `jobs.heavy_lock`). Evidence with every output, score and
back-translation: `docs/results/account3/models_bench.json`. Routing demo: `docs/results/account3/models_routing_demo.json`.

Reproduce:

    NEXUS_HOME=/home/user/new-project-money/.nexus .venv/bin/python -m nexus.intelligence.model_router bench
    # or scripts/account3/models_bench.py; --rescore <json> re-scores stored outputs without generating

## Providers under test (both real, neither is a hosted API)

| Provider | Model | Runtime | Weights | sha256 |
|---|---|---|---|---|
| `local-llamacpp` (in-process) | `qwen3-4b-q4km` | llama-cpp-python 0.3.36, n_ctx 8192, 4 threads | `Qwen3-4B-Q4_K_M.gguf` (Apache-2.0) | (installed by `nexus models fetch`) |
| `owner-openai-compatible` (HTTP, 127.0.0.1) | `qwen3-0.6b-q8` | `python -m llama_cpp.server` (same library), n_ctx 4096, 4 threads | `Qwen/Qwen3-0.6B-GGUF` `Qwen3-0.6B-Q8_0.gguf` (Apache-2.0, 639,446,688 bytes) | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |

Both run with Qwen3's `/no_think` switch (reasoning trace off), which is how the router calls them.

## Results (scores 0..1; n = number of items)

| Capability (n) | Qwen3-4B Q4_K_M in-process | Qwen3-0.6B Q8_0 server |
|---|---|---|
| json: validity + extraction (2) | 1.00 | 1.00 |
| reasoning: short exact-answer questions (6) | **0.33** (2/6) | **0.00** (0/6) |
| summarization: keyword recall, <= 40 words (1) | 0.72 | 0.80 |
| translation de/fr/ar/zh/ru/hi (6) | **0.93** | **0.83** |
| arabic: Egyptian vs Gulf markers (2) | 0.20 | 0.20 |
| writing: title constraints (1) | 0.80 | 0.80 |
| overall (18 items) | 0.64 | 0.50 |
| failures | 0 | 0 |
| mean latency per item | 5.5 s | 3.2 s |
| mean output tokens/s | 4.1 | 8.1 (11-15 tok/s in an earlier run with less load) |

Per-language translation (score / chrF of the MADLAD back-translation vs the English source):

| | de | fr | ar | zh | ru | hi |
|---|---|---|---|---|---|---|
| 4B | 1.00 / 0.89 | 1.00 / 0.84 | 1.00 (Cyrillic letters leaked into one word: script share 0.96) | 0.85 / 0.47 | 1.00 / 0.95 | 0.72 / 0.31 ("museum" became "evaluation") |
| 0.6B | 0.97 | 0.90 | 0.74 ("children under nine", "separate prices") | 0.87 | 0.97 (English "twelve" left in) | 0.53 (largely meaningless) |

## What this means for routing
- Measured scores are stored as `ai_quality.<cap>` (x10) and **replace the catalogue estimate only when n >= 5**
  (`router.MIN_MEASURED_ITEMS`); here that is reasoning and translation. Smaller samples are shown in
  `route_explain()` as "bench X on n=k: not used".
- Reasoning: the 4B model with thinking off scores 3.3/10 -> below the bar (4) for `deep_research`,
  `reference_interpretation`, `quality_review`, `analytics_interpretation`. Those tasks now return
  `NoProviderAvailable` unless an owner GPU worker or an authorised hosted free tier (public data only) is configured.
  This is a measured limitation, not a bug; see "Proposals" below.
- The 0.6B server is ~2x faster and good enough for JSON extraction, summaries and Latin/Cyrillic translation, but
  scores 0/6 on reasoning, so the router no longer gives it `general`/`content_scoring` work.
- Neither model writes recognisable Egyptian or Gulf dialect when asked (no marker words; the 4B "Gulf" answer
  even used the Egyptian "مفيش"). Dialect work should stay with MADLAD's `arz` target (nexus.speech.mt) or a human.

## Method and limits (honest)
- Automatic scorers only (bench.py docstring): no LLM-as-judge, no human rating. Small fixed set: one run, one
  temperature (0.2); differences of a single item are noise.
- Back-translation uses MADLAD-400 3B sentence by sentence; MT errors lower the score of a correct translation
  (zh: "free of charge" vs "enter for free" is penalised by chrF).
- Dialect markers are a short hand-made word list (`bench.EGY`, `bench.GULF`); absence of markers does not prove a
  text is MSA, presence of one marker does not prove fluent dialect.
- Hosted providers were not benchmarked (no keys here, by design). An owner with keys runs the same command with
  `--providers groq` after adding a small adapter loop (integration note in ACCOUNT3_MODEL_COMPARISON.md).

## Proposals
1. Run heavy reasoning tasks with Qwen3 thinking enabled (drop `/no_think` for `light: False` tasks) and re-measure;
   it costs latency (reasoning traces of hundreds of tokens at ~4-5 tok/s) - needs the owner's decision on latency
   vs. local-only quality. `nexus/llm.py` is not owned by agent B.
2. Ministral 3 3B/8B (Apache-2.0) or Gemma 4 E4B (Apache-2.0) as the next local candidates for the same benchmark.
