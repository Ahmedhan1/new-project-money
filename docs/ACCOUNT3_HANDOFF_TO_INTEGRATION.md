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

### Requests that remain (status checked 2026-11-13 against A1 `919f158` / A2 `d979df1`; Account 3 edits no other branch)
| # | To | Request | Status |
|---|---|---|---|
| A1-1 | Account 1 | `test_cloud_compute.py` read `["job"]["id"]`; `dispatcher.submit` returns `job_id` | **done** by Account 1 (`633cc8b`); passes on run r2 |
| A1-2 | Account 1 | `test_cookie_session_and_csrf` kept the valid cookie next to the forged one (httpx 0.28) | **done** by Account 1 (`2ac7f96`); passes on run r2 |
| A1-3 | Account 1 | `batch_llm` job kind for Kaggle (no persistent serving) so batch-only models become usable | **added** by Account 1 (`2ac7f96`). Account 3's side was incompatible (`params.models`, items as a count, `runtime: ollama`); **fixed** (D-17): one job per model, prompts inline. Tested on Account 1's real dispatcher + worker script (echo engine) in run r3. A real-model run with A3 items is not done |
| A1-4 | Account 1 | Declare `www.wikidata.org`; keep `api.gdeltproject.org` reachable from the owner's PC | Wikidata **declared** (`2ac7f96`); GDELT open |
| A2-1 | Account 2 | Fact graphics: keep the `scene_plan.key_fact` figure as the headline in `mograph.kinetic_text` | open (not re-checked on `d979df1`) |
| A2→A3 K-26 | Account 3 | Publishing ignored QualityReport and carried no AI-content disclosure (Account 2 KNOWN_ISSUES K-26) | **fixed** (D-18): release gate reads Account 2's QualityReport (A2 validator on the merged tree) and maps GeneratedMediaManifest.classification to each platform's AI field |
| A2-2 | Account 2 | Asset ranking: prefer a candidate whose title names the scene's subject (Demo 10 run 4: Philae temple shown for the dam's construction) | **open defect, Account 2 owns it.** Account 2 reports semantic B-roll ranking (`14ca114`); not re-verified with Demo 10. Account 3 did not modify Account 2 code |
| OWNER | owner | Google OAuth client → first private upload (PUB-1) and analytics import (ANA-1); YouTube Data API key (A3-COMPETITION); native reviewers for ar-EG/ar-Gulf/hi/zh (A3-NATIVE); merge approval (A3-INT) | blocked by owner authorization |

### PR #5 base reconciliation (2026-11-13, release sign-off)
PR #5 targets `claude/hopeful-allen-luubcy`, which contains PR #4 (Account 1 `f9d8f11`). GitHub reported the PR
`dirty` (98 ahead, 54 behind, merge base `c93847d`). The base was merged into `work/account-3-intelligence-growth`
(`5034c8d`), with no force-push. Conflicts and resolutions:
| File | Resolution |
|---|---|
| `nexus/api.py` | Account 1's production profile, lifespan, security headers, owner guard and body limit kept intact; Account 3's router mount follows them (A3 routes `/api/a3/*` fall under Account 1's owner guard) |
| `nexus/ui/index.html` | Account 1's deep-link `go(...)` moved after `intelligence.js` so Account 3 pages exist when it runs (as in r3) |
| `nexus/registry/tasks.yaml` | both task lists kept; 83 unique ids |
| `docs/DECISIONS.md` | both kept; D-11..D-15 exist once per account, a note says so; nothing renumbered |
| `docs/MASTER_ROADMAP.md`, `docs/AGENT_WORK_LOG.md` | both kept |
| `NEXUS_STATE.json`, `NEXUS_HANDOFF.md` (state block), `docs/results/fast_tests.json` | generated: regenerated by `state.write_state()` and the test run, not hand-merged |

The base's later commit `a889cd8` (Account 1 final review: docs, state, ACC1-INTEGRATION task) was merged the same way
(`4486de4`); only the generated state block conflicted, and it was regenerated.

Auto-merged without conflict: `components.yaml`, `tests/fast/conftest.py`, `.gitignore`. Account 1's `nexus/cloud/`,
security, vault and Studio files are byte-identical to the base.

### Integration evidence
`integration/contracts_v1/RESULT.md` records the exact SHAs, each conflict resolution and the results of every run.
- **Run r4b (current)**:
  - inputs: base `a889cd8` (contains A1 `f9d8f11`), A2 `d979df1`, A3 `fae6d38`;
  - full fast suite **867 passed, 8 skipped, 0 failed**;
  - focused cross-account run 209 passed;
  - `apply.sh` 42 passed, idempotent;
  - local Linux only.
- Run r3 (superseded) (A1 `919f158`, A2 `d979df1`, A3 `f1835b5`): full fast suite 850 passed, 8 skipped, 0 failed; `apply.sh` 42 passed. It includes the batch_llm
  dispatcher round trip, the QualityReport → publishing handoff with Account 2's validator, and the disclosure gates.
- Run r2 (earlier heads): full fast suite 746 passed, 8 skipped, 0 failed; `apply.sh` focused tests 41 passed.
- Run r1 (earlier heads): full suite 538 passed, 3 skipped, 3 failed; focused tests 53 passed.
Linux and Windows CI have not run on a merged tree, and GitHub Actions jobs currently do not start at all. Windows has not been re-validated since CI on `e9b1754`.
