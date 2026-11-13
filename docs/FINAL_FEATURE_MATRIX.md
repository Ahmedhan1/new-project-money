# Final feature matrix — Account 3 (intelligence, research, strategy, languages, publishing, growth)

Status categories (used everywhere in Account 3 docs):
- **Verified**: ran end to end on real data in this repository, with evidence.
- **Tested**: automated tests pass; it ran on real inputs only partially, or not at all.
- **Implemented, not verified on a real account**: the code exists and is tested, but no owner account or data exists to prove it.
- **Partial**: works with stated gaps.
- **Blocked by owner authorization**: cannot proceed without the owner.
- **Pending integration**: needs the owner-approved three-account merge.

No publishing provider is real-provider verified. A mock or recorded-response test is never counted as a live result.
Evidence paths are relative to `docs/results/account3/` unless shown otherwise. Updated 2026-11-13.

| Capability | Status | Evidence / limit |
|---|---|---|
| Model router: privacy classes, quotas, breaker, schema validation, failover | Verified | `tests/fast/test_intel_router.py`; two local providers on real prompts |
| Zero-spend gate (fails closed; hosted provider needs verified `payment_method_required: false`; no automatic paid fallback) | Tested | `tests/fast/test_zero_spend_fail_closed.py` (network trapped). Groq/Mistral refused; no hosted provider has been used |
| Hosted free tiers (OpenRouter `:free`, Gemini free tier) | Blocked by owner authorization | needs `authorized: true` + key; Gemini also needs an owner `daily_requests` limit |
| Account 1 `batch_llm` manifests (one job per model, prompts inline, old shape refused) | Tested; dispatcher round trip on the merged tree | `tests/fast/test_intel_batch_manifest.py`; `test_intel_batch_llm_integration.py` ran on Account 1's real dispatcher and worker script with the `echo` engine (simulated label checked), `integration/contracts_v1/RESULT.md` run r3. A real-model batch_llm run with A3 items has **not** been done |
| Open-weight benchmark V2 (36 items, 8 models) | Verified, small sample | `models_bench_v2.json`; 3–6 items per capability, not conclusive |
| Open-weight benchmark V3 (184 items, ≥ 15 per capability, Kaggle T4×2) | see `docs/ACCOUNT3_MODEL_BENCHMARKS_V3.md` | run status per kernel and results in that file; scores are rule-based proxies, not production-grade quality |
| Mandatory human review: critical tasks below the bar, every dialect output | Tested | `Result.requires_human_review`; `bench_v3.routing` forces review for dialect adaptation |
| Market research V1 + V2 assessment (provenance, dates, cross-source, geography, credibility, confidence interval) | Verified | `market_v2_assessments.json`, Demo 10 research |
| Research degradation (host down / undeclared / no key → error / skipped / disabled, factor unknown) | Tested | `tests/fast/test_intel_research_degradation.py` |
| Competition / saturation | Blocked by owner authorization | needs the owner's YouTube Data API key; reported as unknown, never guessed |
| GDELT freshness | Partial | adapter tested; reachability from the owner's machine **not verified** (connection reset from the dev sandbox) |
| Original strategy + originality / grounding / framing gates | Verified | Demo 10 (handoff approved; sensational variant refused) |
| Strategy Handoff → Account 2 (EditorialPlan, RenderJobManifest, verbatim script) | Verified on a local merge / Pending integration | validated by Account 2's own validator on run r3 |
| Visual context gate | Partial | wrong-subject image remains when the query names no structure (Account 2 ranking, request A2-2) |
| Glyph coverage + OCR read-back (ar, en, zh, hi, ru, de) | Verified | Demo 10 gates; ja/ko have no OCR packs |
| Language readiness, terminology memory, native-review workflow | Tested | `language_readiness.json`; no native reviewer has reviewed a line |
| Per-output language gate (dialect distinct, same-variety review, draft-only without review) | Tested | `tests/fast/test_intel_release_gate.py` |
| Native-language review (ar-EG, ar-Gulf, hi, zh) | Blocked by owner authorization | the owner must supply reviewers; all non-English output is `draft_needs_native_review` |
| Egyptian / Gulf Arabic generation | Partial | best V3 dialect proxy ≤ 0.6 (Egyptian), ≤ 0.31 (Gulf); always reviewed |
| Dubbing gated by licence and consent | Verified | ar-Gulf refused; de/fr/ru/es rendered |
| Lip-sync | Not implemented | no cleared model |
| Publishing release gate: Account 2 QualityReport, checksum, critical findings, human QA approval | Tested | `test_intel_release_gate.py`; Account 2 validator used on the merged tree |
| AI-content disclosure (7 categories; YouTube / TikTok / Instagram fields; Facebook refuses AI content; unknown → AI) | Tested | `docs/ACCOUNT3_PUBLISHING_RELEASE_GATE.md`; fields read on first-party pages 2026-11-13 |
| Publishing engine (private default, owner approval, idempotent, resumable, backoff, quota, blocked states) | Implemented, not verified on a real account | recorded-response tests; nothing uploaded |
| Provider readiness: YouTube | Implemented, not verified on a real account · Blocked by owner authorization | `capabilities.readiness()`; OAuth client + one private upload needed |
| Provider readiness: TikTok, Instagram, Facebook | Implemented, not verified on a real account · Blocked by owner authorization | approved developer apps needed |
| Provider readiness: Snapchat | Manual export only | no organic upload API |
| First private YouTube upload | Blocked by owner authorization | `owner_validation youtube --i-authorize-a-private-upload` (mock-tested only; PUB-1) |
| Analytics import (API + CSV), data-origin labels, normalisation, duplicates, reconciliation, experiments | Implemented, not verified on a real account | `test_intel_analytics_quality.py`; no owner channel data (ANA-1); CSV maps unverified against real exports |
| Shared contracts package (A1 + A2 + A3) | Pending integration | `integration/contracts_v1/RESULT.md` run r3 on A1 `919f158`, A2 `d979df1`; awaits the owner's merge approval |
