# Account 3 → integration handoff

Branch `work/account-3-intelligence-growth` (PR #5), based on the release candidate `c93847d` (`claude/hopeful-allen-luubcy`,
PR #3) — the same base as Account 1 (PR #4) and Account 2 (`work/account-2-cinematic-engine`).

## Ownership inside Account 3
| Package | Work stream | Branch while in progress |
|---|---|---|
| `nexus/intelligence/contracts.py`, `api.py`, `nexus/ui/intelligence.js`, integration | lead | `work/account-3-intelligence-growth` |
| `model_router/`, `providers/`, `capability_registry/` | B — models & routing | `work/account-3-b-models` |
| `creators/` | A — creator reference intelligence | `work/account-3-a-creator-intel` |
| `market/` | C — market intelligence & opportunities | `work/account-3-c-market` |
| `language/` | D — localisation intelligence | `work/account-3-d-language` |
| `publishing/` | E — platforms, publishing, analytics import | `work/account-3-e-publishing` |
| `growth/` | F — experiments & recommendations | `work/account-3-f-growth` |

Shared files touched by Account 3 (kept to single lines so merges with Accounts 1/2 stay trivial):
`nexus/api.py` (one `include_router` + one route), `nexus/ui/index.html` (one `<script>` tag),
`nexus/costguard.py` (additive `FREE_TIER_HOSTS` + optional `free_tier_provider` argument; default behaviour unchanged).

## Contracts (v1) — `nexus/intelligence/contracts.py`
Conventions follow Account 2's contract: `"schema": "nexus.<Name>/1"`, readers ignore unknown keys and refuse unknown
majors, media travels as ArtifactRef dicts, never inline.

| Document | Producer | Consumer |
|---|---|---|
| ProviderCapability | A3 router catalogue | Account 1 scheduler via `to_account1_ai_capability()` → `AIProviderCapability/v1` |
| ResearchBrief, ContentOpportunity | A3 market | A3 strategy, UI; Account 2 via `StoryboardBrief`/create inputs |
| CreatorReferenceProfile, CreativeStyleProfile | A3 creators | A3 strategy; Account 2 `ReferenceStyleProfile` (director) |
| ContentScript, StoryboardBrief | A3 strategy | Account 2 production (`create_video` / director `EditorialPlan.brief`) |
| LocalizationManifest | A3 language | Account 2 audio/render (dubbing manifests), A3 publishing |
| PlatformMetadata, PublishingManifest | A3 publishing | A3 publishing queue; Account 1 OAuth/secrets (reference ids only) |
| AnalyticsRecord | A3 publishing importers | A3 growth |
| ExperimentManifest, GrowthRecommendation | A3 growth | Account 2 (creative parameters), A3 packaging/scheduling |

Rules enforced by `validate()`: confidence ∈ [0,1]; evidence items need a source; allowed status values;
`PublishingManifest.visibility == "public"` is invalid unless `approval == "approved_by_owner"`.

## Cross-account findings (for the owner / integrator)
1. **Import-name collision**: Account 1 adds `nexus/contracts.py` (module); Account 2 adds `nexus/contracts/__init__.py`
   (package). Python cannot have both; whichever merges second breaks the other's imports. Suggested fix: Account 1 →
   `nexus/cloud/contracts.py` (it already has `nexus/cloud/`), Account 2 keeps `nexus/contracts/`. Account 3 stays out of
   it (`nexus/intelligence/contracts.py`).
2. Account 1's `AIProviderCapability/v1` expects Account 3 to declare providers — implemented by
   `contracts.to_account1_ai_capability()`; the router reads keys from Account 1's vault (`vault.get("ai.<provider>")`)
   when that module is present, otherwise env `NEXUS_KEY_<PROVIDER>`.
3. Remote LLM execution on Account 1's Kaggle GPU: an owner-run OpenAI-compatible server (vLLM/SGLang/llama.cpp server)
   registers as provider `owner-openai-compatible` (`settings.json ai_providers.owner-openai-compatible.base_url` + models
   with licences). Privacy class `internal` may go there; `private` stays in-process.

## Status
All six work streams are merged into `work/account-3-intelligence-growth`. Feature matrix, demos and blockers:
`docs/ACCOUNT3_EXECUTION_REPORT.md`.

## Integration requests to Accounts 1 and 2
| To | Request | Why |
|---|---|---|
| 1 | Rename `nexus/contracts.py` → `nexus/cloud/contracts.py` (or Account 2 renames its package) | import-name collision |
| 1 | Vault secret names `ai.<provider>`, `yt.<account>` (vault regex forbids `/`) | router + publishing read them |
| 1 | `batch_llm` job kind on the Kaggle worker (no persistent serving allowed there) | heavy AI tasks on a free GPU |
| 1 | OAuth connection records the publishing engine can reference by id | uploads |
| 2 | `create_video(script=ContentScript)` to use an approved script verbatim | strategy hand-off |
| 2 | Consume `CreativeStyleProfile` via `creators.to_account2_reference_style()` in the director | style transfer |
| 2 | Apply `production` overrides from accepted `GrowthRecommendation`s only after owner acceptance | growth loop |
| 2 | Render `LocalizationManifest` dubbing lines (voices are licence/consent-gated on our side) | dubbing |
| 2 | Image relevance gate in create/assets (Demo 4: "Belgorod governorate 1745" map shown for the Aswan Governorate) | wrong visuals pass QA |
| 2 | Mixed-script text in overlays/credits: pick a font per run, not per line (Demo 4: credits rendered as tofu) | unreadable credits |

## V2 integration (2026-11-12, session "Intelligence V2")

### Contract collision — resolved by design, verified on a three-account merge
One package `nexus.contracts`: `cloud.py` (Account 1, moved unchanged), `media.py` (Account 2, moved unchanged),
`__init__` re-exporting both APIs + `registry()` (fails on a duplicate tag) + `validate_any()`; Account 3 stays in
`nexus.intelligence.contracts`. Details: `docs/SHARED_CONTRACTS_ARCHITECTURE.md`. Apply after merging Accounts 1 and 2:
`bash integration/contracts_v1/apply.sh`. Result of the local three-account merge: `integration/contracts_v1/RESULT.md`.
This supersedes request 1 in the table above (no module is renamed; both import forms keep working).

### Strategy Handoff → Account 2 (`nexus/intelligence/handoff.py`, `nexus.StrategyHandoff/1`)
- `editorial_plan`: Account 2's `nexus.EditorialPlan/1` — one *insert* decision per scene with reason, claim ids, sources,
  visual and visual query; validated by **Account 2's own validator** on the merged tree.
- `render_job`: Account 2's `nexus.RenderJobManifest/1` — requirements for Account 1's scheduler.
- `create_params.script`: used **verbatim** by `create.create_video(script=...)` (request 2 above: done in
  `nexus/create.py`, a 20-line opt-in path; without `script` Account 2's own writer runs as before).
- Refused when originality, grounding or framing fails (`handoff.submit`).

### Requests that remain
| # | To | Request |
|---|---|---|
| A1-1 | Account 1 | `tests/fast/test_cloud_compute.py:353` reads `["job"]["id"]`; `dispatcher.submit` returns `job_id` |
| A1-2 | Account 1 | `test_cookie_session_and_csrf`: `c.cookies.clear()` before setting the forged cookie (httpx 0.28 keeps both; server behaviour is correct) |
| A1-3 | Account 1 | `batch_llm` job kind: run `bench_v2`-style prompts on the Kaggle worker (Kaggle forbids persistent serving) so the measured batch-only models in `providers.yaml` become usable for non-interactive tasks |
| A1-4 | Account 1 | Declare `www.wikidata.org` (place-resolution fallback) and keep `api.gdeltproject.org` reachable from the owner's PC |
| A2-1 | Account 2 | Fact graphics: label + figure from `scene_plan.key_fact` (fixed here); please keep the figure as the headline in `mograph.kinetic_text` |
| A2-2 | Account 2 | Asset ranking: prefer a candidate whose title names the scene's subject (the gate now refuses wrong-subject images; ranking first would avoid fallbacks) |
| OWNER | owner | Google OAuth client + consent → first private upload (PUB-1) and analytics import (ANA-1); native reviewers for ar-EG/ar-Gulf/hi/zh |
