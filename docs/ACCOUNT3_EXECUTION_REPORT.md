# Account 3 — execution report (2026-11-12)

Branch `work/account-3-intelligence-growth` → draft PR #5 (base: release candidate `claude/hopeful-allen-luubcy`,
`c93847d`). Nothing was merged into `main` or into Account 1/2 branches.

## How the work was done
The lead built the shared foundation first (contracts v1, the model router, the API/UI mount points), pushed it, then
ran **six parallel sub-agents** (Opus-class) in separate git worktrees, each owning a disjoint set of paths and
committing to its own branch: A creator intelligence, B models & routing, C market intelligence, D localisation,
E publishing & analytics, F growth. All six finished and pushed; the lead reviewed each diff (scope check: only owned
paths), ran its tests, merged it with `--no-ff`, and applied the integration requests (manifest hosts, job wiring,
contract fixes). No agent ran out of usage this time; every branch arrived complete with tests and evidence.

## Feature matrix (Account 3)
States: **V** verified end to end in the product · **I** implemented + tested, not proven on real data/accounts ·
**P** partial · **N** not implemented · **B-perm** blocked by provider permission/owner credentials ·
**B-quota** blocked by free-tier limits · **H** needs human review.

| Capability | State | Evidence / gap |
|---|---|---|
| Model router: privacy classes, quotas, breaker, schema validation, cache, fallbacks | V | `tests/fast/test_intel_router.py`; failover between two real local providers (`models_routing_demo.json`); AI Models page in Chromium |
| Works without Gemini/Groq | V | every demo here ran with no hosted API |
| Hosted free tiers (Gemini, Groq, OpenRouter `:free`, Mistral) | I / B-perm | adapters + config; need owner authorisation + key. Cerebras and Hugging Face **excluded** (card / no free credits) |
| Model benchmark (18 auto-scored items, 2 local models) | I | `models_bench.json`; small sample — 4B reasoning 3.3/10 → heavy tasks run as labelled drafts (D-11) |
| Open-weight GPU models (vLLM/SGLang on Account 1's worker) | N / B-perm | documented; Kaggle forbids persistent serving → batch job kind proposed to Account 1 |
| Creator reference profiles (measured + interpreted, confidence) | I | 4 public-domain NASA/JPL references (`creators_profiles.json`); on-screen text and music not measured |
| Reference comparison + abstract style + do-not-copy | I | `creators_compare.json`; no causation claims (tested) |
| Pattern library for 9 formats (MrBeast-style, El Daheeh-style, documentary…) | P / H | every claim cited or marked hypothesis; El Daheeh/Shorts claims secondary/unverified; no lawful references for challenge/Arabic formats in this environment |
| Market research: sources, demand in 7 languages, opportunity score with evidence | **V** | real job through the UI/API → report "Aswan High Dam" (score 56, confidence 0.41); `market_report_*.json` |
| News momentum (GDELT), competition (YouTube) | B-perm | GDELT resets connections from the sandbox; YouTube needs the owner's key — reported as unknown, never guessed |
| Original strategy: script + storyboard with originality/grounding gates → Account 2 | **V** | real run: report → Qwen3-4B script (originality ok, grounding ok) → `create-from-idea` job submitted (see Demo 4) |
| 15-language matrix with verified licences | I | `matrix.yaml` (sources + date per licence); no cleared voice for MSA/ar-EG/ar-Gulf/tr/ko |
| Translation QA catching real omissions/additions | **V** | real MT on 8 languages: invented "completed in 2005", "111 m wide", reversed Arabic digit groups, "High Dam" mistranslated — all flagged (`language_demo.json`) |
| Egyptian vs Gulf Arabic distinct | P | classifier tells them apart (EG 0.95 on Egyptian text); **generation** is not distinct: only 3/8 adapted lines differ, final Gulf text still classifies as MSA (K-15) |
| Dubbing gated by licence + consent | I | uncleared voices refused (tested); voice-over rendered for de/fr/ru/es; zh voice failed in Piper |
| SRT / VTT / ASS, full vs condensed | I | exporters + QA; RTL and CJK fonts |
| Platform capability matrix (7 destinations, first-party sources) | I | `ACCOUNT3_PLATFORM_CAPABILITIES.md`; YouTube quota re-checked by the lead (upload = own 100/day bucket) |
| Publishing engine (private default, owner approval, idempotent, resumable job) | I | 22 tests; durable `publish-upload` job wired |
| Real private upload | B-perm | needs the owner's Google OAuth client (PUB-1) |
| Analytics import (YouTube Analytics API, Studio/TikTok/IG/FB CSV) | I / B-perm | CSV mappings not checked against real exports; no owner data here (ANA-1) |
| Experiments, statistics, learning loop, recommendations | I | numerics checked vs scipy; public CC BY 4.0 dataset only — **not the owner's channel** |
| Account 3 UI (research, references, script studio, language, publishing, analytics, AI models) | V | all pages driven in Chromium without errors; research job started from the UI |
| Contracts v1 shared with Accounts 1/2 | I | `nexus/intelligence/contracts.py`; maps to Account 1 `AIProviderCapability/v1` and Account 2 `ReferenceStyleProfile` |

## Acceptance demonstrations
| Demo | Result |
|---|---|
| 1 Reference intelligence | **Partial**: 4 comparable profiles (science explainer, interview, launch montage, promo) from public-domain media. Challenge / Arabic educational / vlog formats need owner-supplied, lawfully held references |
| 2 Market research | **Done**: sourced reports (en science topic, Arabic-demand topic, Aswan High Dam via the UI); competition/news unknown (permissions) |
| 3 AI router | **Done** for local providers: two runtimes, failover, privacy routing, quota/limit gates, no Gemini/Groq needed. Hosted providers not exercised (no keys) |
| 4 Original content strategy → Account 2 | **Done**: see below |
| 5 Multilingual | **Done with findings**: 8 languages translated + QA'd, licensed voice-over for de/fr/ru/es; Gulf dialect generation weak; no native review yet |
| 6 Platform packaging | **Done** (packages for all 7 destinations, limits checked); claims cited to first-party docs |
| 7 Publishing | **Blocked** on owner OAuth: the adapter stops with the exact steps; nothing uploaded |
| 8 Performance learning | **Blocked** on owner analytics; statistics demonstrated on a public dataset, clearly labelled |

### Demo 4 detail (real run, this machine) — `docs/results/account3/demo4_strategy_to_production.json`
Research job `job_5b44ec38c6` (Aswan High Dam, history; score 56, confidence 0.41) → strategy job `job_ed38aa3fc3`:
Qwen3-4B wrote 4 scenes citing claim ids; originality ok (0 % shared 6-grams), grounding ok (every number sourced) →
Account 2 `create-from-idea` job `job_2ce2cebdc1` rendered a 46.3 s video (research 24 s, script 308 s, assemble 58 s,
render+QA 77 s) that **passed automated QA**. Frames were then inspected by eye (`demo4_frames.jpg`) and showed defects
automated QA did not catch:
- wrong image: an 18th-century Russian map ("Belgorod governorate 1745") for the *Aswan* Governorate — lexical match (Account 2);
- the credits card renders as empty boxes: an Arabic name in the attribution put the whole line in an Arabic-only font (Account 2);
- 2 of 4 scenes are plain title cards (Account 2 visuals);
- clickbait hook "the dam has a secret", unsupported by the research (Account 3) → **fixed**: `strategy.framing` now blocks
  submission of sensational framing that no sourced claim supports (regression test from this run);
- Account 2 paraphrases the approved script instead of using it verbatim (integration request `create_video(script=...)`).
Verdict: the chain research → original strategy → production works end to end; the output is not publishable without
the Account 2 fixes above and a human review.

## Bugs found by running the integrated system (fixed, with regression tests)
- **Deadlock**: the job runner holds the CPU lock; the research brief takes it again for the local LLM → the worker
  waited on itself (18 min, found with py-spy on the real job). `heavy_lock` is now re-entrant per thread.
- Network-bound uploads waited behind renders → `jobs.handler(kind, heavy=False)`.
- Account 1's vault rejects `/` in names → router reads `ai.<provider>`.
- The lead's own manifest text had the outdated YouTube upload quota (1600 units) → corrected from Google's page.
- Measured 3.3/10 reasoning removed every provider for four heavy tasks → soft quality bar with labelled drafts (D-11).
- A filtered test run overwrote the fast-suite result record → only complete runs are recorded.

## What only the owner can unblock
1. Google Cloud project (no billing) + OAuth client → first private YouTube upload (PUB-1) and Analytics import (ANA-1).
2. A YouTube Data API key → competition signals in market research and public metadata for references.
3. Authorise one hosted free tier (or a GPU worker via Account 1) → heavy AI tasks above draft quality.
4. Lawfully held reference videos for the challenge / Arabic educational / vlog formats.
5. Native reviewers for ar-EG / ar-Gulf / hi / zh lines (Language Studio + Review page).

## Cross-account notes
- Account 1 (`nexus/contracts.py`) and Account 2 (`nexus/contracts/`) collide on the import name `nexus.contracts`.
- Account 2: accept an approved `ContentScript` verbatim in `create_video` (today passed as notes/direction).
- Account 1: `batch_llm` job kind for the Kaggle worker (persistent serving is not allowed there).

---

# V2 — Intelligence, real-world quality & final integration (2026-11-12, later session)

Branch `work/account-3-intelligence-growth`, draft PR #5. Nothing merged into `main` or into Account 1/2 branches.
Every claim below links to evidence in the repository; "by eye" means frames were inspected, not only automated QA.

## Demo 4 defects → gates → Demo 10 (real research, real render)
| Demo 4 defect | Gate (tested) | Demo 10 evidence |
|---|---|---|
| 18th-century Russian map for modern Aswan | `visual_context`: geo_mismatch, historical_in_modern, old_map | the exact "Belgorod governorate 1745" + 6 other historical maps refused before download |
| credits rendered as empty boxes | `fonts` glyph-coverage runs + `typography` missing-glyph gate (critical) + OCR read-back | OCR recall ar/en/zh/ru/de 1.0, hi 0.84; a hieroglyph without a font blocks the render |
| 2 of 4 scenes were title cards | `scene_plan`: licensed photo → fact graphic → flagged card; > 25 % blocks | run 4: 4/4 licensed photos, 0 cards |
| sensational hook "the dam has a secret" | `strategy.framing` + script review + handoff refusal | refused at handoff |
| approved script paraphrased | `create_video(script=...)` verbatim | narration identical in all four runs |
| (new) unsupported dubbing | readiness + dubbing gate | ar-Gulf dubbing refused (no cleared voice); readiness 31.5 "draft_needs_native_review" |
| (new) duplicate publishing | idempotency key | second create returns the same private draft |

Iterations, each found by **inspecting frames by eye** and fixed with a regression test (`docs/results/account3/demo10_run{1..4}_*`):
1. run 1 — fact-graphic labels broken ("Constructed between 1960 and the dam was", "…it will func"); Giza pyramids for
   an Aswan sentence (same country) → `key_fact` rewrite, `place_mismatch`.
2. run 2 — a real "High Dam Burg, Aswan" photo refused (place words compared as phrases) → distinctive place words.
3. run 3 — Philae temple for "The Aswan High Dam…"; "Flag of Aswan Governorate" accepted → `subject_unverified`, `emblem`.
4. run 4 (final) — the real Aswan High Dam on scene 1, all automated stages pass, render 61 s.
   **Residual, by eye:** scene 2 (construction narrative) shows Philae temple — its visual query names no structure, so
   the subject rule does not apply. Fix belongs in asset ranking (request A2-2), not in a stricter gate.
Also found on the real research and fixed: place resolver mapped "Kourou" → Guinea, "Upper Egypt" → Ghana/Gambia/Guyana,
"New Valley" → Colombia/Honduras/Italy.

## Parts 3–9
- **Models (Part 3):** 36-item benchmark on Kaggle T4×2, 8 models measured (`docs/ACCOUNT3_MODEL_BENCHMARKS.md`):
  DeepSeek-R1-Qwen3-8B 0.90 (slow), Nemotron-Nano-9B 0.84 (2.6 s), Ministral-3-14B 0.83, GLM-4.7-Flash 0.80,
  Qwen3.5-4B 0.80; dialect ≤ 0.52 everywhere. Task routing derived from measurements; critical tasks below the bar set
  `requires_human_review`. Qwen3.6-35B/3.8-27B not measured (Ollama mmproj pull failure; cause isolated).
- **Creators V2 (Part 4):** 14 measured dimensions incl. OCR text share, colour, motion; weighted blends report conflicts.
- **Market V2 (Part 5):** credibility, cross-publisher verification, recency, geography, language demand, coverage and an
  uncertainty interval (Aswan: 57.1 in [51.2, 61.6], 3/23 claims cross-confirmed → "indicative only").
- **Languages (Part 6):** readiness per language (8 dimensions), terminology memory, native-review workflow + API.
  No language is publishable without native review; Gulf Arabic still classifies as MSA.
- **Publishing (Part 7):** **blocked** — no Google OAuth client exists; nothing uploaded.
- **Analytics (Part 8):** first-party ledger joining platform analytics to production facts and experiment arms; no
  owner data exists yet.
- **Integration (Part 9):** one `nexus.contracts` package. Local three-account merge (run r1):
  - **full fast suite: 538 passed, 3 skipped, 3 failed**. Two failures were Account 1 test bugs that also failed on
    Account 1's own branch (later fixed there). One was an Account 3 test literal (fixed).
  - **Focused** contract and handoff tests: 53 passed.

  The two figures come from different test sets and are not added together. The Strategy Handoff was validated by
  Account 2's own validator.

## Verification (V2, superseded)
At the time: fast suite 363 passed, 3 skipped. CI was red from `9fc6cfb` (undeclared `pycountry`) and green from `ba777cf`.
Current numbers are in the "Final production closure" section below.


# Final hardening and production closure (2026-11-13)

## Zero spend (D-16)
The router used to refuse a hosted provider only when `payment_method_required is True`. A string such as `unverified`
(Groq, Mistral) passed once the provider was authorised and a key added. The gate now fails closed, and
`costguard.check_host` re-runs it. No hosted provider was used. Tests: `test_zero_spend_fail_closed.py`.

## Account 1 batch_llm contract (D-17)
Before the fix, `account1_manifest` sent a model list, items as a count and `runtime: ollama`. Account 1's worker fails
on that shape with `TypeError: 'int' object is not iterable`. Now:
- one job per model, carrying the HF repo and the actual prompt list;
- the engine is transformers, vllm or echo;
- a params-hash idempotency key;
- Kaggle only;
- the old shape is refused with a named cause.

On the merged tree, `test_intel_batch_llm_integration.py` runs on Account 1's real dispatcher and worker script (fake
Kaggle CLI, echo engine). It checks:
- validation against the pydantic contract;
- execution;
- verified artifacts with the right ids and counts;
- the simulated label;
- idempotency;
- old-shape failure.

## Publishing release gate (D-18), disclosure, provider readiness, owner validation
- **Release gate.** No upload without Account 2's QualityReport for the exact file (PASSED, no critical finding,
  checksum). Public also needs human QA approval, same-variety native review and the disclosure decision.
- **Disclosure.** Seven categories. Fields read on the first-party pages: YouTube `containsSyntheticMedia`, TikTok
  `is_aigc`, Instagram `is_ai_generated`. Facebook /videos refuses AI content. Unknown provenance is treated as AI.
- **Provider readiness.** No platform is live-verified. YouTube is ready for owner authorization; TikTok, Instagram and
  Facebook need approved apps; Snapchat is manual.
- **Owner validation.** `owner_validation youtube` makes one private upload, mock-tested only. Docs:
  `ACCOUNT3_PUBLISHING_RELEASE_GATE.md`, `ACCOUNT3_YOUTUBE_OWNER_VALIDATION.md`.

## Analytics (D-19) and research
- Analytics records carry a data origin; simulated rows never conclude experiments.
- Dates and time zones are normalised without guessing.
- Duplicates and invalid values are reported.
- Rows are reconciled against the Total row and the API.
- Research sources that are down, undeclared or keyless produce explicit error/skipped/disabled results and unknown
  factors.

## Benchmark V3
- 184 items, at least 15 per capability. 8 models × 184 answers ran on the authorised free Kaggle worker with no errors.
- qwen3-4b (no thinking) is an invalid harness run (reasoning leaked into the answers) and is excluded.
- No best model is separable from the second.
- Dialect is weak (Gulf ≤ 0.31). Details: `ACCOUNT3_MODEL_BENCHMARKS_V3.md`.

## Not done (owner or other accounts)
- The first private YouTube upload and analytics on real data (OAuth client).
- The YouTube competition key.
- Native reviewers.
- A real-model batch_llm run with A3 items.
- GDELT reachability from the owner's PC.
- A2-1 and A2-2 (Account 2).
- Merge approval.

Test, CI and integration numbers for the final commit: `integration/contracts_v1/RESULT.md` (run r3) and PR #5.
