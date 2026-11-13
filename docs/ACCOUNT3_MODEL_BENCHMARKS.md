# Account 3 — open-weight model benchmarks

> **Current benchmark: V3** (`docs/ACCOUNT3_MODEL_BENCHMARKS_V3.md`). It has 184 items, at least 15 per capability, and 8
> models ran on the authorised free Kaggle T4 worker on 2026-11-13 (kernels a/b/c, every item answered, no errors).
> V2 below is kept as history.
>
> What V3 shows, and its limits:
> - **No routed task has a best model that is separable from the second.** Every 95 % interval overlaps. The six valid
>   non-thinking runs score 0.805–0.84 overall, which is a tie on this item set.
> - **Ceiling effects.** `conflicts` is 1.00 for every valid model and translation (chrF-gated) is about 0.95. Those
>   items no longer discriminate, so harder items are needed before choosing between models.
> - **Dialect is weak everywhere.** Egyptian marker scores are at most 0.60 and Gulf at most 0.31, and both are lexicon
>   proxies. Dialect output always requires a native reviewer.
> - **qwen3-4b (no thinking) is an invalid run, not a weak model.** 79 of 184 answers start with visible reasoning
>   ("Okay, so I need to…"): Ollama did not honour the thinking switch, and the answers were cut at the token cap.
>   `bench_v3.valid_run` now detects this and excludes such runs from routing. The V2 row for qwen3-4b below
>   (summarization 0.0, instruction 0.0) has the same signature and should be read the same way.
> - **qwen3-4b-think** returned 68/184 empty answers: it spent its budget thinking. It counts as valid (under the 50 %
>   rule) but is degraded.
> - These are rule-based scores on one fixed item set. They are not production-grade quality claims. Critical tasks keep
>   `requires_human_review`, and low-scoring outputs stay drafts.
>
> Account 1 `batch_llm` path:
> - `bench_v2.account1_manifest` and `bench_v3 account1` now emit one ProductionJobManifest per model, with the prompts
>   inline (D-17).
> - That path has been tested on Account 1's real dispatcher with the echo engine only.
> - These V3 numbers come from A3's own Kaggle kernels (Ollama, GGUF), not from batch_llm, whose transformers engine
>   loads fp16 weights. Models above 24 GiB fp16 (ministral-3-14b, glm-4.7-flash) are not batch_llm-eligible.

## V2 (Kaggle GPUs, 2026-11-12)

**What ran:** 36 auto-scored items, identical for every model (`nexus/intelligence/model_router/bench_v2.py`), on
Account 1's Kaggle worker (2× NVIDIA T4, 15 GB each) through Ollama with GGUF **Q4_K_M** weights, temperature 0,
JSON-schema constrained output where an item asks for structure, context 4 096 tokens (8 192 for long-context items).
Translations were back-translated locally with MADLAD-400 (Apache-2.0) and scored on meaning, numbers and script.
Raw outputs: `docs/results/account3/models_bench_v2_raw_*.json`; scored: `models_bench_v2.json`.
Reproduce: `python -m nexus.intelligence.model_router.bench_v2 kernel --models ... --out DIR --slug SLUG`, push with
`scripts/account3/kaggle_cli.py kernels push -p DIR`, then `scripts/account3/bench_v2_score.py` and
`scripts/account3/bench_v2_report.py --profiles`. Cost: free Kaggle GPU quota only; no paid endpoint was touched.

Capabilities: structured JSON, reasoning (multi-step arithmetic/logic), summarization, translation (de/fr/zh/ru/hi/ar),
dialect (Egyptian + Gulf Arabic), writing, long context (~2 700-word document, 3 needle questions), fact-grounding
(answer only from given sources, refuse the unsupported), instruction following, creative strategy; source synthesis
with a planted 48 m vs 52 m conflict is scored under grounding.

## Results (0–1 per capability; latency = mean seconds per item; VRAM per GPU after load)
| Model | Params (B) | Licence | Overall | json | reasoning | summarization | translation | dialect | writing | long_context | grounding | instruction | strategy | latency s | tok/s | VRAM | Run |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-4b | 4.0 | Apache-2.0 | **0.419** | 1.0 | 0.333 | 0.0 | 0.523 | 0.001 | 1.0 | 0.667 | 0.7 | 0.0 | 0.833 | 1.69 | 62.5 | 3159 MiB + 0 MiB | ok |
| qwen3-4b-think | 4.0 | Apache-2.0 | **0.784** | 1.0 | 1.0 | 1.0 | 0.478 | 0.52 | 0.0 | 1.0 | 0.65 | 1.0 | 0.333 | 30.77 | 55.4 | 3159 MiB + 0 MiB | 10/36 empty answers |
| qwen3.5-4b | 4.7 | Apache-2.0 | **0.799** | 1.0 | 0.833 | 0.8 | 0.979 | 0.32 | 0.8 | 0.667 | 1.0 | 0.667 | 1.0 | 1.39 | 55.6 | 3855 MiB + 0 MiB | ok |
| qwen3.5-9b | 9.7 | Apache-2.0 | **0.793** | 1.0 | 0.75 | 0.8 | 0.979 | 0.52 | 0.8 | 0.667 | 1.0 | 0.667 | 1.0 | 1.97 | 31.0 | 6403 MiB + 0 MiB | ok |
| deepseek-r1-0528-qwen3-8b | 8.2 | MIT | **0.898** | 1.0 | 0.917 | 0.7 | 0.979 | 0.52 | 1.0 | 1.0 | 1.0 | 1.0 | 0.667 | 24.67 | 29.7 | 5267 MiB + 0 MiB | 2/36 empty answers |
| ministral-3-14b | 13.9 | Apache-2.0 | **0.833** | 1.0 | 0.75 | 1.0 | 0.979 | 0.32 | 1.0 | 1.0 | 1.0 | 1.0 | 0.833 | 3.64 | 18.4 | 9303 MiB + 0 MiB | ok |
| nemotron-nano-9b-v2 | 8.9 | NVIDIA Open Model License (commercial use allowed under its terms) | **0.842** | 1.0 | 0.833 | 0.975 | 0.882 | 0.31 | 0.8 | 1.0 | 1.0 | 1.0 | 1.0 | 2.63 | 25.5 | 6317 MiB + 0 MiB | ok |
| qwen3.6-35b-a3b | 36.0 | Apache-2.0 | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **invalid**: not run (pull failed) |
| glm-4.7-flash | 31.2 | MIT | **0.804** | 1.0 | 0.75 | 0.8 | 0.979 | 0.32 | 1.0 | 1.0 | 1.0 | 0.667 | 1.0 | 3.03 | 60.7 | 8889 MiB + 9133 MiB | ok |
| qwen3.8-27b | 27.8 | Apache-2.0 | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **invalid**: not run (pull failed) |

Every licence listed allows commercial use (Apache-2.0, MIT, NVIDIA Open Model License under its terms). Base-model
context windows are 32k–256k; this run capped them at 4k/8k to fit two T4s, so long-context results are for ≤ 8k.

## Findings
- **DeepSeek-R1-0528-Qwen3-8B** is the strongest overall but slow (≈ 25 s/item: it reasons before answering).
- **Ministral-3-14B** and **GLM-4.7-Flash** are the best fast generalists (GLM ≈ 3 s/item, ≈ 60 tok/s).
- **Qwen3.5-4B** matches the 9B models at ≈ 1.4 s/item — the best small model. Qwen3-4B without thinking is weak (empty
  summaries, instruction failures); with thinking it is slow and leaves 10/36 answers empty.
- **Dialect is the open problem:** no model exceeds 0.52 on Egyptian/Gulf Arabic. Dialect output always needs native
  review (consistent with the language readiness matrix, K-15).
- **Nemotron-Nano-9B-v2** is the best fast all-rounder once run correctly (≈ 2.6 s/item). Its first run returned 35/36
  empty answers — hidden reasoning used the whole token budget — so that run was discarded as a harness error and
  re-run with the model card's `/no_think` system switch (kernel `nexus-a3-bench-v2-c`). Lesson recorded: an empty
  answer is a harness failure to investigate, never a score.
- **Not measured: Qwen3.6-35B-A3B and Qwen3.8-27B.** Both fail at `ollama pull` with "file does not exist" while
  fetching the repositories' vision projector (mmproj); the re-run proved it is not disk (944 GB free). Next step:
  download the text GGUF directly and `ollama create` from a Modelfile.
- **Limits:** 3–6 items per capability, so many scores sit at the 1.0 ceiling and differences under ~0.15 are not
  meaningful; translation saturates (0.979 for five models) because back-translation forgives style. A larger item set
  per capability is needed before routing on small differences.

## Task routing (measured; bar = router min quality / 10)
| Task | Best model | Score | Bar | Meets bar | Human review | Next best |
|---|---|---|---|---|---|---|
| deep_research | deepseek-r1-0528-qwen3-8b | 0.972 | 0.4 | yes | critical | nemotron-nano-9b-v2 0.944, glm-4.7-flash 0.917 |
| scriptwriting | ministral-3-14b | 1.0 | 0.4 | yes | critical | deepseek-r1-0528-qwen3-8b 1.0, nemotron-nano-9b-v2 0.933 |
| translation | qwen3.5-4b | 0.979 | 0.3 | yes | critical | qwen3.5-9b 0.979, glm-4.7-flash 0.979 |
| dialect_adaptation | qwen3.5-9b | 0.52 | 0.4 | yes | **required** | deepseek-r1-0528-qwen3-8b 0.52, qwen3-4b-think 0.52 |
| summarization | ministral-3-14b | 1.0 | 0.3 | yes |  | qwen3-4b-think 1.0, nemotron-nano-9b-v2 0.988 |
| title_generation | glm-4.7-flash | 1.0 | 0.3 | yes |  | qwen3-4b 0.916, ministral-3-14b 0.916 |
| hook_generation | glm-4.7-flash | 1.0 | 0.3 | yes |  | qwen3-4b 0.916, ministral-3-14b 0.916 |
| topic_ideation | glm-4.7-flash | 1.0 | 0.3 | yes |  | qwen3-4b 0.916, ministral-3-14b 0.916 |
| audience_classification | nemotron-nano-9b-v2 | 1.0 | 0.3 | yes |  | ministral-3-14b 1.0, deepseek-r1-0528-qwen3-8b 1.0 |
| data_extraction | nemotron-nano-9b-v2 | 1.0 | 0.3 | yes |  | ministral-3-14b 1.0, deepseek-r1-0528-qwen3-8b 1.0 |
| content_scoring | qwen3.5-4b | 0.916 | 0.3 | yes |  | nemotron-nano-9b-v2 0.916, qwen3.5-9b 0.875 |
| reference_interpretation | qwen3.5-4b | 0.916 | 0.4 | yes | critical | nemotron-nano-9b-v2 0.916, qwen3.5-9b 0.875 |
| quality_review | deepseek-r1-0528-qwen3-8b | 0.972 | 0.4 | yes | critical | nemotron-nano-9b-v2 0.944, ministral-3-14b 0.917 |
| story_planning | glm-4.7-flash | 1.0 | 0.4 | yes | critical | qwen3-4b 0.916, ministral-3-14b 0.916 |
| analytics_interpretation | qwen3.5-4b | 0.916 | 0.4 | yes | critical | nemotron-nano-9b-v2 0.916, qwen3.5-9b 0.875 |
| general | qwen3-4b-think | 1.0 | 0.3 | yes |  | deepseek-r1-0528-qwen3-8b 0.959, nemotron-nano-9b-v2 0.916 |

Rules now in the router (`router.CRITICAL`): deep_research, scriptwriting, translation, quality_review,
analytics_interpretation, reference_interpretation and story_planning are **critical** — below the bar the result
carries `requires_human_review = True` and must not be used unreviewed. Dialect adaptation is always reviewed.
**How these models are used:** Kaggle forbids persistent serving, so they run as batch jobs on Account 1's worker
(proposed `batch_llm` job kind) or on an owner GPU running Ollama/vLLM; interactive work stays on the local Qwen3
providers. Measured profiles are in `capability_registry/providers.yaml` (`availability: batch_only`,
`source: bench_v2`), so an owner-served copy is ranked by measurement, not by estimate.
