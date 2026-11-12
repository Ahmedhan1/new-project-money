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

### Demo 4 detail (real run, this machine)
Research job `job_5b44ec38c6` (Aswan High Dam, history) → strategy job `job_ed38aa3fc3`: Qwen3-4B wrote 4 scenes citing
claim ids; originality ok (0 % shared 6-grams, no banned phrases), grounding ok (every number in a sourced claim) →
Account 2 `create-from-idea` job submitted with the verified facts + approved direction.
Reviewer notes on that script (why the gates are necessary but not sufficient): the hook "the dam has a secret" is
clickbait the facts do not support (grounding checks numbers, not framing); one visual suggests "the logo of the
Egyptian government" (an official emblem — should not be used). Both belong in a human review step before production.

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
