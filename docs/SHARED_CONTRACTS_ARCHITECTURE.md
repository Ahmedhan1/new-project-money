# Shared contracts architecture (v1)

**Problem.** Account 1 shipped `nexus/contracts.py` (pydantic models) and Account 2 shipped `nexus/contracts/__init__.py`
(dict schemas). With both in one tree, Python imports the package and Account 1's module silently disappears:
every `from nexus.contracts import ProductionJobManifest` fails. Renaming imports independently in each branch would
just move the collision.

**Agreed layout** (verified on branch `integration/contracts-v1`, which merges all three accounts; not merged into `main`):

| Module | Owner | Tag form | Contents |
|---|---|---|---|
| `nexus/contracts/cloud.py` | Account 1 | `contract: "<Name>/v1"` (pydantic, `extra="forbid"`) | storage, compute, quotas, events, ingestion, AI provider capability |
| `nexus/contracts/media.py` | Account 2 | `schema: "nexus.<Name>/1"` (dict schema) | render/edit/audio/VFX/generation manifests, QualityReport, EditorialPlan |
| `nexus/intelligence/contracts.py` | Account 3 | `schema: "nexus.<Name>/1"` (dict schema) | research, scripts, storyboards, localisation, publishing, analytics, StrategyHandoff |
| `nexus/contracts/__init__.py` | shared | — | re-exports, `registry()`, `validate_any()` |

The files were **moved, not rewritten**: `cloud.py` is Account 1's module byte-for-byte except two path strings in its
docstring; `media.py` is Account 2's `__init__.py` unchanged.

## Rules
1. Each owner edits only its own module. A breaking change adds a new major (`/v2`, `/2`) next to the old one.
2. Tags are unique across the whole system: `registry()` raises on a duplicate and the fast suite calls it.
   The same *name* may exist in two forms (`ArtifactManifest/v1` and `nexus.ArtifactManifest/1`); both stay addressable.
3. Readers ignore unknown optional keys (dict schemas); Account 1's models stay strict (`extra="forbid"`).
4. `validate_any(doc)` accepts every form, so a consumer never needs to know which account produced a document.
5. Backward compatibility: every name the two original modules exported is re-exported from `nexus.contracts`.
   `VERSION` / `new_id` keep Account 1's meaning (the `/api/v1/contracts` endpoint reports them); Account 2's
   `media.VERSION == 1` is used internally by its validator.

## Cross-account flows that now validate end to end
- **Account 3 → Account 2: Strategy Handoff** (`nexus/intelligence/handoff.py`, contract `nexus.StrategyHandoff/1`).
  It holds Account 2's `nexus.EditorialPlan/1` (one explainable *insert* decision per scene, with claim ids and sources)
  and `nexus.RenderJobManifest/1` (requirements for Account 1's scheduler), plus the `create-from-idea` params whose
  `script` is used **verbatim** by `create.create_video(script=...)`. Validated with Account 2's own validator
  (`tests/fast/test_intel_handoff.py`). An unapproved handoff (originality, grounding or framing failed) is refused.
- **Account 3 → Account 1:** `ProviderCapability` maps to `AIProviderCapability/v1`; batch model jobs use
  `ProductionJobManifest/v1` (`bench_v2.account1_manifest`).
- **Account 2 → Account 3:** `QualityReport` gates publishing; `GeneratedMediaManifest.classification` is carried into
  platform disclosure.

## Tests
- `tests/fast/test_contracts_cloud.py` — Account 1's original suite (schemas committed in `docs/contracts/`).
- `tests/fast/test_contracts_media.py` — Account 2's original suite.
- `tests/fast/test_contracts_shared.py` — both APIs from one import, no tag collision, a real document from each
  account through `validate_any`, `extra="forbid"` still enforced.
- `tests/fast/test_intel_router.py` (Account 3 contracts), `tests/fast/test_intel_handoff.py` — Account 3.

## Merge order proposed to the owner
`main` → merge `work/account-1-cloud-platform` → merge `work/account-2-cinematic-engine` → merge
`work/account-3-intelligence-growth` → `bash integration/contracts_v1/apply.sh` (stages the restructure; review with
`git diff --cached`, then commit). Nothing is merged into `main` without the owner's approval.

`apply.sh` checks its preconditions (all three accounts merged, no other unresolved conflicts) and handles both states
of Account 2: heads before `a443238` still keep their schemas in `nexus/contracts/__init__.py` and ship
`tests/fast/test_contracts.py` (add/add conflict with Account 1, split by owner). From `a443238` on, Account 2 ships
`nexus/contracts/media.py` and `tests/fast/test_contracts_media.py` itself, and only Account 1's module and test are
moved. A second run changes nothing. Recorded runs, exact SHAs and results: `integration/contracts_v1/RESULT.md`.
