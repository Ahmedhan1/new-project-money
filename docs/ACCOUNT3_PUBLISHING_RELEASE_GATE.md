# Account 3 — publishing release gate, AI-content disclosure, provider readiness

Code: `nexus/intelligence/publishing/release_gate.py`, `engine.py`, `adapters/`, `capabilities.py`, `owner_validation.py`.
Tests (no network): `tests/fast/test_intel_release_gate.py`, `test_intel_publishing.py`, `test_intel_owner_validation.py`.
Status words follow `docs/FINAL_FEATURE_MATRIX.md`. **No platform flow is live-verified**: every adapter has run only
against recorded or mock responses.

Release sign-off: on the three-account tree (runs r4 and r4b), the gate was checked against Account 2's real code:
- reports from Account 2's own `qa.approve()`;
- Account 2's own validator;
- `classify.manifest()` and `contracts.artifact()`.

The result was `test_intel_release_gate.py` 44 passed, 0 skipped, and `test_intel_a2_publishing_integration.py` 3
passed (`integration/contracts_v1/RESULT.md`).

## 1. What must be true before anything is uploaded
A finished render does not make a publishable result. `engine.enqueue()` and `engine.run_one()` both call
`release_gate.evaluate()`. `run_one` checks again because the file or the evidence can change after queueing. Every
result is stored in `manifest.release_check`, and every evaluation goes into `manifest.history`.

| Check | Blocks any upload | Blocks public / unlisted only |
|---|---|---|
| Final output | file missing, not a local file, or sha256 differs | — |
| Account 2 QualityReport (`nexus.QualityReport/1`, produced by `nexus/qa.py approve()`) | no report; invalid report (A2's own validator on the merged tree); report for another output; status `BLOCKED`, `NEEDS_FIXES` or unknown; any failed check of severity `critical` | `human_approval` does not start with `APPROVED` (A2 writes `PENDING …`) |
| AI-content disclosure | AI provenance contradicts an owner "no AI" attestation; the platform has no private mode and no supported field (Facebook /videos) | unknown provenance; YouTube without the owner's realism decision; Snapchat needs a manual label |
| Language (per output) | voice licence not cleared; voice consent problems; review ids that do not exist; invalid dialect | native review required and not approved **for the same variety**; translation QA, terminology or OCR/glyph check failed; localized metadata without a language record |
| Owner approval (unchanged) | — | `approval == "approved_by_owner"` |

Private or draft remains the default. Owner approval never bypasses the gate. Quality findings, `known_issues`, the
human-approval text and the review state are copied into `release_check` so the publishing record keeps them.
Evidence is attached later with `engine.attach_release()` (API `POST /api/a3/publish/{id}/release`, check with
`GET …/release`). Typical attachments are a QualityReport after human review, the owner's disclosure decision, or the
language record after native review.

## 2. AI-content disclosure model
Provenance becomes categories:

| Source | Category |
|---|---|
| `GeneratedMediaManifest.classification = generated-video` | fully_ai_generated |
| `generated-image`, `animated-still` | ai_generated_imagery |
| `edited-source` | ai_assisted_editing |
| `procedural` | none by itself |
| audio `tts` | ai_narration |
| audio `dub`, `voice_clone`, `voice_conversion`, `generated_music` | synthetic_audio |
| edits `ai_postprocessing` / audio `ai_enhanced` | human_ai_postprocessing |
| no records, empty records, only non-AI records without an owner attestation, unknown values | **unknown** (disclosed as AI) |

Content is labelled non-AI only when the owner attests it (`no_ai_attested_by: "owner"`) **and** no AI provenance exists.
An attestation that contradicts AI records blocks the upload.

Platform mapping (fields read on the first-party pages on 2026-11-13; nothing inferred):

| Platform | Field | Human decision | Adapter sets it | Unknown provenance |
|---|---|---|---|---|
| YouTube / Shorts | `status.containsSyntheticMedia` (videos.insert/update): "realistic Altered or Synthetic content" | yes: realism is a judgement. Default flag true; the owner may set false with a reason | yes | private upload with flag true; public blocked until the owner decides |
| TikTok | `post_info.is_aigc` (optional bool) | no | yes | flag true; public blocked |
| Instagram | `is_ai_generated` on the media container | no | yes | blocked (no private API mode) |
| Facebook | `is_ai_generated` exists only on the Reels publish step; the adapter uses `/videos` | — | **no** | AI or unknown content refused; manual upload with Meta's label |
| Snapchat Spotlight | none (manual upload) | yes | — | the package tells the owner to label by hand |

Whether a platform makes disclosure mandatory is policy, not API. Only YouTube's field description states its scope.
For the others, NEXUS sets the flag for every AI category (conservative).

## 3. Language readiness per output
A record looks like `{"language", "dialect", "translation_qa", "terminology", "ocr_glyph", "voice": {"licence_ok", "consent_problems"}, "review_items"}`.
- Native review is required for every non-source language whose readiness is not reviewed (≥ 20 lines at ≥ 90 %
  acceptance). It is always required for ar-EG, ar-Gulf, hi and zh, whatever the score.
- `ar` + `EG` gives `ar-EG`, and `ar` + `Gulf` gives `ar-Gulf`. A review recorded for one variety never clears the other.
  Unknown dialects are refused.
- States:
  - `ready`;
  - `draft` (a failed check);
  - `draft_needs_native_review`, with `reviewer_approval` one of not_submitted, pending, rejected, invalid or approved.
- No reviewer means the output stays a draft and can be uploaded **privately** only. Approval comes only from
  `language/review.record()` with a reviewer label. Nothing in the gate can create it.

## 4. Provider readiness (`capabilities.readiness()`, `GET /api/a3/platforms/readiness`)
| Platform | Classes | Evidence |
|---|---|---|
| YouTube, Shorts | automated-test verified · implemented, not live-tested · **requires owner authorization** | recorded responses: resumable upload, resume after 5xx, thumbnail, quota ledger, disclosure flag, analytics import; mock owner validation |
| TikTok | implemented, not live-tested · **requires owner authorization** (approved app; unaudited apps post SELF_ONLY) | request shape incl. `is_aigc` |
| Instagram Reels / Feed | implemented, not live-tested · **requires owner authorization** (no private mode: publish only after owner approval) | request shape incl. `is_ai_generated` |
| Facebook | implemented, not live-tested · **requires owner authorization**; AI content not supported by the adapter | refusal tested |
| Snapchat Spotlight | manual export only | package + MANUAL_UPLOAD.md |
| Real-provider verified | **none** | — |

The following are unchanged and tested:
- private/draft default;
- owner approval for public;
- idempotency key (same artifact, platform, connection and metadata, so never a second upload);
- resumable YouTube upload and session recovery;
- retry with backoff (max 5);
- blocked states that are never auto-retried;
- quota ledger;
- credentials only as vault references.

Tokens never enter the DB, logs or reports.
The YouTube owner path is in `docs/ACCOUNT3_YOUTUBE_OWNER_VALIDATION.md`.
