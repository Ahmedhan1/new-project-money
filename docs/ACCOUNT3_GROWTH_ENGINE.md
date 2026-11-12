# Account 3 growth engine (agent F): experiments, statistics, learning loop, recommendations

Branch `work/account-3-f-growth` (from bd71690). Package `nexus/intelligence/growth/`, tests
`tests/fast/test_intel_growth.py` (14 tests, < 1 s, no network/models), demo `scripts/account3/growth_demo.py`,
evidence `docs/results/account3/growth_demo.json`.

Status: **implemented + tested** (fast tests). Demo run on a real public dataset and on labelled synthetic fixtures.
**Not verified** against the owner's channel: there is no channel analytics in this environment.

## Modules

| module | what it does |
|---|---|
| `stats.py` | normal/t distributions (numpy + math only, checked against scipy to 1e-9/1e-5), power calculation (two proportions, two means), two-proportion z-test, Welch t-test, percentile bootstrap CI (seeded), Holm correction, `verdict()` with minimum-sample guard. Verdicts: `winner`, `inconclusive`, `insufficient_data` (observational patterns use `association` instead of `winner`). Effect + CI always reported. |
| `experiments.py` | `ExperimentManifest` registry: 14 single variables (hook, title, thumbnail, length, structure, opening, caption_style, density, pacing, editing_profile, music, publish_time, localisation, cta), one-variable rule enforced, `min_samples` from a power calculation (stored with its assumptions and sample unit), status lifecycle planned -> running -> collecting -> concluded / inconclusive / stopped (illegal transitions raise), assignment plan (balanced, seeded block shuffle so variant is not confounded with publish order; YouTube title/thumbnail tests use the native Test & compare on one upload), `record_platform_result` (owner-entered YouTube outcome winner / preferred / inconclusive), `evaluate()` at equal days-since-publish with Holm across arms. |
| `normalise.py` | reads `AnalyticsRecord` (agent E's importers): metric aliases (views/view_count, ctr in % or share, average_view_duration, ...), derived rates (ctr, like_rate, avg_view_pct, views_per_day, log_views, retention_Xs), age from `meta.published` vs record date, `at_age()` (one snapshot per video near day N), `segment()` by platform/niche/language/format, tiny `load_csv()` for demos. |
| `learning.py` | `mine_patterns()` (binary feature x segment, Welch + bootstrap median, Holm over all tests, `robust_to_outliers` flag, min 30 videos per side), `weak_openings()` (relative drop at 3/5/15 s vs leave-one-out channel median + max(2 MAD, 5 pts); `no_baseline` under 5 other videos), `loss_segments()` (where the curve falls > 2x the video's median loss rate, mapped to timeline clips, speech share and the failed checks of `nexus.growth.retention_report` that overlap those seconds). |
| `recommend.py` | `GrowthRecommendation` documents with evidence (record ids, numbers, CI) and confidence; `next_experiments()` (power-calculated from the observed effect); `from_experiment()`; `narrate()` optional router phrasing (`analytics_interpretation`), deterministic text always kept in `text`, router output only in `narrative`; `NoProviderAvailable` -> recorded deterministic fallback. |

### Account 2 hand-off (target "account2")

`rec["production"]` = `{director_profile, director_overrides, preset_overrides?, retention_fixes, instructions, hook_max_s?}`:
`director_overrides` merge into `nexus.director.get_profile(name, overrides)` (keys validated against
`registry/direction.yaml` by `recommend.check_production`), e.g. weak opening -> `{"opening": "cold_open"}` +
fixes `trim_lead, remove_lead_fade, add_hook_text` + `hook_max_s` = platform `first_speech_max`; loss over a static
shot -> `shot_len` x0.75 of the profile, `add_punch_ins`, instructions "faster"; low speech share / dead air ->
`pause_tolerance` <= 0.5 + `tighten_pauses`. Target "account3" carries `packaging` (title/thumbnail/publish time/localisation/CTA).

## Demo results (`docs/results/account3/growth_demo.json`)

Part 1 - **public dataset, not the owner's channel**: 3,951 rows; 1,568 used (videos 3-30 days old at an
assumed 2026-05-11 snapshot). Title features vs log10(1+views/day), Holm over 5 tests:
all-caps word +0.104 (95% CI +0.027..+0.181, Holm p=0.042, but median-difference CI includes 0 -> flagged not
robust); question mark, number, exclamation, long title: inconclusive. Like rate: all five features show
associations (e.g. question titles +1.07 pts, CI +0.64..+1.50). Segmented (8 categories x 2 formats x 5 features =
78 tests): 64 insufficient_data, 12 inconclusive, 2 associations (news long-form titles with numbers -0.198;
AI long-form with "!" +0.475, n=30 vs 142). All 8 recommendations are low-confidence (0.25-0.35) "test this"
items for account3; one next experiment planned (title, YouTube Test & compare). One recommendation was phrased
through the router (local qwen3-4b, under `jobs.heavy_lock`, model unloaded after); the narrative slightly
mangled a phrase, which is why `text` stays the authoritative deterministic sentence.
Runtime 115 s with the LLM call, ~3 s without.

Part 2 - **synthetic fixtures**: 3 vs 5 clicks out of 10 impressions -> `insufficient_data`, status stays
`collecting`; 250 vs 350 clicks per 5,000 impressions x 4 uploads -> `winner` B (CI excludes 0); YouTube
"preferred" -> recorded as inconclusive; weak opening flagged at 3 s; loss at 10-18 s localised to clip 1 with the
"longest static stretch" check -> account2 production dict that passes `check_production`.

## Limitations

- Observational patterns are associations; confounders (channel size, topic, thumbnail, promotion) are not controlled.
- Public dataset: search-sampled cross-section, one snapshot per video, collection date not stated (assumed).
- Per-upload experiments on a single channel are slow: CTR at 5% needs ~8,000 impressions per arm to see +20%.
- `evaluate()` compares each variant with the control only (Holm across those); no variant-vs-variant tests.
- Proportion tests treat impressions/views as independent trials (pooled across uploads); upload-level clustering
  widens real uncertainty - prefer per-video metrics (Welch) when there are enough uploads.
- Retention-loss localisation is descriptive; a drop next to a clip does not prove the clip caused it.

## Integration requests (for the lead)

1. Agent E: please emit `meta.published` (or `metrics.age_days`) on every `AnalyticsRecord`, plus `meta.niche/language/format`
   and `metrics.retention_curve` (`[[t_s, share], ...]` or `{"ratio": [[elapsed_ratio, share], ...]}` with `duration_s`), so
   equal-age comparison and opening analysis work without guessing.
2. Contracts: consider adding `meta: D?` to `AnalyticsRecord` and `production: D?` / `packaging: D?` to `GrowthRecommendation`
   (currently carried as extra keys, allowed by the "readers ignore unknown keys" rule).
3. Account 2: accept `GrowthRecommendation.production` as overrides for `director.get_profile` / `presets.get` and the
   retention fix ids for `growth.apply_retention_pass`; only after the owner accepts the recommendation (status `accepted`).
4. API/UI (not owned here): a form for the owner to enter YouTube Test & compare outcomes -> `experiments.record_platform_result`.
5. `nexus/registry/tasks.yaml`: register GRW tasks for "experiments on real channel data" (blocked on OAuth / analytics import).

## Research notes (sources)

- YouTube "Test & compare" thumbnails: up to three thumbnails; each gets a result based on **watch time share**;
  outcomes include "Winner", "Preferred" (likely best, gap not large enough to declare a winner) and
  "Inconclusive". https://support.google.com/youtube/answer/13861714
- YouTube "A/B test titles & thumbnails": up to 3 titles / title+thumbnail combinations, the one with the highest
  watch time is shown to all viewers at the end. https://support.google.com/youtube/answer/16391400
  (Read via search summary on 2026-11-12; exact current UI wording unverified.)
  Consequence for NEXUS: such tests are a platform feature; NEXUS only records the result the owner enters,
  it never fakes the split or re-computes YouTube's verdict.
- Public dataset used for the demo: "YouTube Video Metadata", Tisha Nawani, Mendeley Data V1 (2026-05-12),
  DOI 10.17632/4gf2nhbr9d.1, licence **CC BY 4.0** (read from the Mendeley public API `data_licence` field),
  https://data.mendeley.com/datasets/4gf2nhbr9d/1 . 3,951 videos collected with the YouTube Data API v3 across
  search queries. File `data.csv` 5.7 MB, sha256 56c938bc9c69a368929c320f54e3d32ec06b916945c0b74abdb57719929abc8e
  (the demo refuses to run on a different file). Stored in `.nexus/a3_data/mendeley/` (not in Git); only derived
  statistics and a few video ids are committed. Attribution: changes = derived title features and views/day.
- Kaggle "Trending YouTube Video Statistics" (datasnaek) was considered; its CC0 status was only seen in a
  secondary source and download needs a Kaggle login, so it was not used.
