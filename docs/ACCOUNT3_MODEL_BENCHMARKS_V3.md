# Account 3 — open-weight model benchmark V3 (Kaggle GPUs, 184 items)

**What ran:** 184 auto-scored items, identical for every model, at least 15 per routed capability
(reasoning 19, long_context 15, grounding 15, conflicts 15, json 15, summarization 15, translation 15, dialect_egy 15, dialect_gulf 15, writing 15, strategy 15, instruction 15). Harness unchanged from V2: Account 1's authorised Kaggle worker
(2× NVIDIA T4), Ollama, GGUF **Q4_K_M**, temperature 0, schema-constrained JSON where an item asks for structure,
context 4 096 tokens (8 192 for long-context items). Free Kaggle GPU quota only; no paid endpoint was touched.
Items and scoring rules: `nexus/intelligence/model_router/bench_v3.py` (docstring lists every rule). Raw outputs: `models_bench_v3_raw_a.json`, `models_bench_v3_raw_b.json`, `models_bench_v3_raw_c.json`;
scored: `docs/results/account3/models_bench_v3.json`. Incomplete runs (excluded from the table): none.
Invalid runs (shown last, excluded from routing): qwen3-4b: 79/184 answers start with visible reasoning (thinking switch not honoured; answers truncated at the token cap) - harness issue, not a quality measurement.

Reproduce: `python -m nexus.intelligence.model_router.bench_v3 kernel --models A,B --out DIR --slug SLUG`, push with
`scripts/account3/kaggle_cli.py kernels push -p DIR`, then `scripts/account3/bench_v3_score.py OUT raw_*.json` and
`scripts/account3/bench_v3_report.py`. Scoring needs no model, so anyone can re-score the raw outputs.

## Results (mean 0–1 per capability, [95 % interval]; latency = mean s/item)
| Model | Params (B) | Overall | reasoning | long_context | grounding | conflicts | json | summarization | translation | dialect_egy | dialect_gulf | writing | strategy | instruction | dialect | latency s | tok/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5-9b | 9.7 | 0.84 | 0.79 [0.60–0.98] | 0.87 [0.69–1.00] | 0.97 [0.92–1.00] | 1.00 [1.00–1.00] | 0.97 [0.90–1.00] | 0.86 [0.81–0.90] | 0.96 [0.91–1.00] | 0.60 [0.40–0.80] | 0.24 [0.16–0.32] | 0.96 [0.93–1.00] | 1.00 [1.00–1.00] | 0.87 [0.69–1.00] | 0.42 [0.30–0.54] | 2.17 | 22.3 |
| glm-4.7-flash | 31.2 | 0.835 | 0.84 [0.67–1.00] | 0.93 [0.80–1.00] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.86 [0.82–0.91] | 0.97 [0.91–1.00] | 0.35 [0.20–0.49] | 0.28 [0.17–0.39] | 0.94 [0.88–1.00] | 0.98 [0.95–1.00] | 0.87 [0.69–1.00] | 0.31 [0.22–0.40] | 0.93 | 60.0 |
| qwen3.5-4b | 4.7 | 0.829 | 0.84 [0.67–1.00] | 0.87 [0.69–1.00] | 0.95 [0.88–1.00] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.89 [0.84–0.93] | 0.95 [0.90–1.00] | 0.51 [0.33–0.68] | 0.20 [0.20–0.20] | 0.95 [0.91–0.99] | 0.99 [0.97–1.00] | 0.80 [0.59–1.00] | 0.35 [0.25–0.46] | 1.3 | 44.2 |
| deepseek-r1-0528-qwen3-8b | 8.2 | 0.829 | 0.95 [0.84–1.00] | 0.93 [0.80–1.00] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.97 [0.92–1.00] | 0.82 [0.75–0.89] | 0.95 [0.89–1.00] | 0.43 [0.24–0.61] | 0.25 [0.14–0.37] | 0.83 [0.69–0.97] | 0.92 [0.86–0.98] | 0.87 [0.69–1.00] | 0.34 [0.23–0.45] | 22.36 | 23.0 |
| ministral-3-14b | 13.9 | 0.82 | 0.79 [0.60–0.98] | 0.87 [0.69–1.00] | 0.95 [0.88–1.00] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.86 [0.81–0.91] | 0.96 [0.92–1.00] | 0.45 [0.29–0.62] | 0.31 [0.19–0.42] | 0.96 [0.91–1.00] | 0.87 [0.80–0.93] | 0.84 [0.67–1.00] | 0.38 [0.28–0.48] | 3.35 | 17.2 |
| nemotron-nano-9b-v2 | 8.9 | 0.805 | 0.84 [0.67–1.00] | 0.80 [0.59–1.00] | 0.96 [0.90–1.00] | 1.00 [1.00–1.00] | 0.98 [0.95–1.00] | 0.91 [0.87–0.94] | 0.90 [0.76–1.00] | 0.36 [0.20–0.52] | 0.23 [0.14–0.31] | 0.85 [0.68–1.00] | 0.95 [0.92–0.99] | 0.87 [0.69–1.00] | 0.29 [0.20–0.39] | 2.58 | 22.8 |
| qwen3-4b-think | 4.0 | 0.588 | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.75 [0.53–0.97] | 0.80 [0.59–1.00] | 0.60 [0.34–0.86] | 0.83 [0.75–0.91] | 0.51 [0.26–0.76] | 0.08 [0.00–0.21] | 0.09 [0.00–0.20] | 0.40 [0.14–0.66] | 0.19 [0.00–0.39] | 0.69 [0.46–0.92] | 0.09 [0.00–0.17] | 42.15 | 43.5 |
| qwen3-4b | 4.0 | **invalid** 79/184 answers start with visible reasoning (thinking switch not honoured; answers truncated at the token cap) - harness issue, not a quality measurement | 0.42 [0.19–0.65] | 0.80 [0.59–1.00] | 0.79 [0.63–0.94] | 1.00 [1.00–1.00] | 1.00 [1.00–1.00] | 0.54 [0.47–0.62] | 0.20 [0.05–0.35] | 0.05 [0.00–0.16] | 0.04 [0.00–0.12] | 0.73 [0.58–0.87] | 0.95 [0.92–0.99] | 0.03 [0.00–0.10] | 0.05 [0.00–0.11] | 2.01 | 45.9 |

## How to read this (limits)
- Intervals are normal approximations over 15–19 items. Two models whose intervals overlap are **not** shown to differ;
  the routing table marks this per task. These are measurements on one fixed item set, not general rankings.
- Every scorer is a rule, not a judge: it checks format, numbers, facts named in the item, and reference overlap
  (chrF for translation). It cannot see fluency or tone. Dialect scores rest on marker lexicons (`EGY`, `GULF`, `MSA`
  in `bench_v3.py`) and are a proxy only — a native reviewer is mandatory before any dialect text is published.
- Context was capped at 4k/8k tokens to fit two T4s; long-context results are for documents of ~2 000–2 800 words.
- V3 re-scores the 36 V2 items with reference-based translation and the V3 dialect lexicon, so V3 numbers are on their
  own scale; compare models within V3, not V3 with V2.

## Task routing (measured; bar = router min quality / 10)
| Task | Best model | Score | Bar | Meets bar | Human review | Best separable from 2nd? | Next best |
|---|---|---|---|---|---|---|---|
| deep_research | deepseek-r1-0528-qwen3-8b | 0.97 | 0.4 | yes | critical | **no** (intervals overlap) | glm-4.7-flash 0.944, qwen3.5-4b 0.914 |
| scriptwriting | glm-4.7-flash | 0.937 | 0.4 | yes | critical | **no** (intervals overlap) | qwen3.5-9b 0.935, ministral-3-14b 0.916 |
| translation | glm-4.7-flash | 0.966 | 0.3 | yes | critical | **no** (intervals overlap) | qwen3.5-9b 0.965, ministral-3-14b 0.962 |
| dialect_adaptation | qwen3.5-9b | 0.42 | 0.4 | yes | **required** | **no** (intervals overlap) | ministral-3-14b 0.38, qwen3.5-4b 0.353 |
| summarization | qwen3-4b-think | 0.915 | 0.3 | yes |  | **no** (intervals overlap) | glm-4.7-flash 0.899, qwen3.5-4b 0.877 |
| title_generation | qwen3.5-9b | 0.982 | 0.3 | yes |  | **no** (intervals overlap) | qwen3.5-4b 0.971, glm-4.7-flash 0.961 |
| hook_generation | qwen3.5-9b | 0.982 | 0.3 | yes |  | **no** (intervals overlap) | qwen3.5-4b 0.971, glm-4.7-flash 0.961 |
| topic_ideation | qwen3.5-9b | 0.982 | 0.3 | yes |  | **no** (intervals overlap) | qwen3.5-4b 0.971, glm-4.7-flash 0.961 |
| audience_classification | glm-4.7-flash | 0.933 | 0.3 | yes |  | **no** (intervals overlap) | nemotron-nano-9b-v2 0.925, ministral-3-14b 0.922 |
| data_extraction | glm-4.7-flash | 0.933 | 0.3 | yes |  | **no** (intervals overlap) | nemotron-nano-9b-v2 0.925, ministral-3-14b 0.922 |
| content_scoring | deepseek-r1-0528-qwen3-8b | 0.934 | 0.3 | yes |  | **no** (intervals overlap) | qwen3.5-4b 0.915, glm-4.7-flash 0.91 |
| reference_interpretation | deepseek-r1-0528-qwen3-8b | 0.934 | 0.4 | yes | critical | **no** (intervals overlap) | qwen3.5-4b 0.915, glm-4.7-flash 0.91 |
| quality_review | deepseek-r1-0528-qwen3-8b | 0.954 | 0.4 | yes | critical | **no** (intervals overlap) | glm-4.7-flash 0.927, nemotron-nano-9b-v2 0.917 |
| story_planning | qwen3.5-9b | 0.982 | 0.4 | yes | critical | **no** (intervals overlap) | qwen3.5-4b 0.971, glm-4.7-flash 0.961 |
| analytics_interpretation | deepseek-r1-0528-qwen3-8b | 0.934 | 0.4 | yes | critical | **no** (intervals overlap) | qwen3.5-4b 0.915, glm-4.7-flash 0.91 |
| general | deepseek-r1-0528-qwen3-8b | 0.907 | 0.3 | yes |  | **no** (intervals overlap) | glm-4.7-flash 0.855, nemotron-nano-9b-v2 0.855 |

Critical tasks (`router.CRITICAL`) below the bar, or below 0.7, carry `requires_human_review`; dialect adaptation
always does. These models are batch-only (Kaggle forbids persistent serving); interactive work stays local.
