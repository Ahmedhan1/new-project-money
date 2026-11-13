# Final feature matrix — Account 3 (intelligence, research, strategy, languages, publishing, growth)

Status categories (used everywhere in Account 3 docs):
- **Verified**: ran end to end on real data in this repository, with evidence.
- **Tested**: automated tests pass; it ran on real inputs only partially, or not at all.
- **Implemented, not verified on a real account**: the code exists and is tested, but no owner account or data exists to prove it.
- **Partial**: works with stated gaps.
- **Blocked by owner authorization**: cannot proceed without the owner.
- **Pending integration**: needs the owner-approved three-account merge.

Evidence paths are relative to `docs/results/account3/` unless shown otherwise. Updated 2026-11-13.

| Capability | Status | Evidence / limit |
|---|---|---|
| Model router: privacy classes, quotas, breaker, schema validation, failover | Verified | `tests/fast/test_intel_router.py`; two local providers on real prompts |
| Zero-spend gate (fails closed; hosted provider needs verified `payment_method_required: false`) | Tested | `tests/fast/test_zero_spend_fail_closed.py` (network trapped). Groq and Mistral stay refused (unverified). No hosted provider has been used. |
| Hosted free tiers (OpenRouter `:free`, Gemini free tier) | Blocked by owner authorization | needs `authorized: true` + key; Gemini also needs an owner `daily_requests` limit (no published quota) |
| Open-weight benchmark V2 (36 items, 8 models, Kaggle T4×2) | Verified, small sample | `models_bench_v2.json`; 3–6 items per capability, so differences are not conclusive |
| Open-weight benchmark V3 (184 items, ≥ 15 per capability) | see `docs/ACCOUNT3_MODEL_BENCHMARKS_V3.md` | item set and scorers tested; run status and results are stated in that file |
| Mandatory human review for critical tasks below the bar and for every dialect output | Tested | `Result.requires_human_review`; `bench_v3.routing` forces review for dialect adaptation |
| Market research V1 + V2 assessment | Verified | `market_v2_assessments.json`, Demo 10 research |
| Competition / saturation | Blocked by owner authorization | needs the owner's YouTube Data API key; reported as unknown, never guessed |
| Original strategy + originality / grounding / framing gates | Verified | Demo 10 (handoff approved; the sensational variant was refused) |
| Strategy Handoff → Account 2 (EditorialPlan, RenderJobManifest, verbatim script) | Verified on a local merge / Pending integration | Demo 10 runs 1–4; validated by Account 2's own validator |
| Visual context gate (geography, period, maps, emblems, place, subject) | Partial | Demo 10 run 4; a wrong-subject image remains when the visual query names no structure (Account 2 ranking, request A2-2) |
| Glyph coverage + OCR read-back (ar, en, zh, hi, ru, de) | Verified | Demo 10 gates; ja/ko have no OCR packs |
| Scene visual plan (no silent title cards) | Verified | run 4: 0 cards |
| Creator reference intelligence V2 (14 dimensions, blends) | Partial | 4 public-domain references; the challenge, Arabic and vlog formats need owner references |
| Language readiness, terminology memory, native-review workflow | Tested | `language_readiness.json`; no native reviewer has reviewed a line yet |
| Native-language review (ar-EG, ar-Gulf, hi, zh) | Blocked by owner authorization | the owner must supply reviewers |
| Egyptian / Gulf Arabic generation | Partial | best V2 dialect score 0.52; Gulf output classifies as MSA; always reviewed |
| Dubbing gated by licence and consent | Verified | ar-Gulf refused; de/fr/ru/es rendered |
| Lip-sync | Not implemented | no cleared model |
| Publishing engine (private default, approval, idempotent, resumable) | Implemented, not verified on a real account | duplicate refused in Demo 10; nothing uploaded |
| First private YouTube upload | Blocked by owner authorization | needs a Google OAuth client (PUB-1) |
| Analytics import, first-party ledger, experiments | Implemented, not verified on a real account | no owner channel data (ANA-1) |
| Shared contracts package (A1 + A2 + A3) | Pending integration | `integration/contracts_v1/RESULT.md` run r2 on the current heads of all three branches; awaits the owner's merge approval |
