# Final feature matrix — Account 3 (intelligence, research, strategy, languages, publishing, growth)

States: **V** verified end to end on real data · **I** implemented + tested, not proven on real accounts/data ·
**P** partial · **B** blocked (reason) · **H** needs human review by design. Evidence paths are in `docs/results/account3/`.

| Capability | State | Evidence / limit |
|---|---|---|
| Model router (privacy, quotas, breaker, schema, failover, no paid endpoints) | V | `test_intel_router.py`; two local providers |
| Open-weight benchmark on remote GPUs + measured task routing | V (8 models) / P | `models_bench_v2.json`; Qwen 27B/35B not measured |
| Mandatory human review for critical tasks below the bar | I | `Result.requires_human_review` |
| Market research V1 + V2 assessment | V | `market_v2_assessments.json`, Demo 10 research |
| Competition / saturation | B (owner YouTube key) | reported unknown, never guessed |
| Original strategy + originality/grounding/framing gates | V | Demo 10 (handoff approved; sensational variant refused) |
| Strategy Handoff → Account 2 (EditorialPlan, RenderJobManifest, verbatim script) | V | Demo 10 runs 1–4; Account 2 validator |
| Visual context gate (geography, period, maps, emblems, place, subject) | V / P | Demo 10 run 4; residual wrong-subject image when the visual names no structure (A2-2) |
| Glyph coverage + OCR read-back (ar, en, zh, hi, ru, de) | V | Demo 10 gates; ja/ko without OCR packs |
| Scene visual plan (no silent title cards) | V | run 4: 0 cards |
| Creator reference intelligence V2 (14 dims, blends) | V (4 public-domain refs) / P | challenge/Arabic/vlog formats need owner references |
| Language readiness, terminology memory, native review | I / H | `language_readiness.json`; no reviewer yet |
| Gulf Arabic generation | P / H | classifies as MSA; best model 0.52 |
| Dubbing gated by licence and consent | V | ar-Gulf refused; de/fr/ru/es rendered |
| Lip-sync | N | no cleared model |
| Publishing engine (private default, approval, idempotent, resumable) | I | duplicate refused in Demo 10 |
| First private YouTube upload | B (owner OAuth client) | PUB-1 |
| Analytics import + first-party ledger + experiments | I / B | no owner data (ANA-1) |
| Shared contracts package (A1 + A2 + A3) | V (local merge) | `integration/contracts_v1/RESULT.md`; awaits owner merge approval |
