# Account 3 - Market intelligence, trends and content opportunities (agent C)

Branch `work/account-3-c-market` (from bd71690). Package `nexus/intelligence/market/`. Sources and their first-party
facts: `docs/ACCOUNT3_RESEARCH_SOURCES.md`. Evidence: `docs/results/account3/market_*.json`.

## What it does

```
topic + niche + language
  -> gather()        adapters (Wikipedia search/extracts, langlinks, pageviews x languages, GDELT, RSS, YouTube,
                     Google Trends, Openverse, NASA) - each: ok | skipped (host not declared) | disabled | error
  -> build_brief()   ResearchBrief: claims cross-checked across >= 2 sources, conflicts, open questions, angles
  -> build_opportunity()  ContentOpportunity: 10 factors, each {value|None, orientation, evidence, confidence, method}
  -> to_production_brief()  request body for Account 2's create pipeline
trend_monitor(watchlist)  pageviews last 7 d vs previous 28 d -> emerging / rising / stable / falling / unknown
```

| Module | Content |
|---|---|
| `sources.py` | `Ctx` (network/clock/settings seam; polite 1 s spacing, one 45 s retry on 429), adapters, `host_declared()` (same rule as `costguard.check_host`, without logging a block), `window_stats()` |
| `brief.py` | deterministic claim extraction, AI extraction via router (`data_extraction`, `deep_research`, privacy `public`, inside `jobs.heavy_lock()` + `llm.unload()`), cross-check, conflict finder, angles |
| `scoring.py` | factor functions, `score()`, `build_opportunity()` |
| `niches.yaml` / `niches.py` | 15 verticals: seeds, Wikipedia categories, editorial monetisation tier + sensitive keywords, platform-fit priors, preset; custom niches |
| `pipeline.py` | `gather`, `analyse`, `trend_monitor`, `to_production_brief` |

Usage:
```python
from nexus.intelligence.market import Ctx, analyse, trend_monitor, watchlist
rep = analyse(Ctx(), "James Webb Space Telescope", "science", "en")      # brief, opportunity, production_brief, adapters
tm = trend_monitor(Ctx(), watchlist(["science", "arabic_content"]))
```
Settings (`NEXUS_HOME/settings.json`, key `market`): `rss_feeds` (list of URLs; their hosts must be declared),
`google_trends_alpha_access` (bool, owner statement), `niches_file` / `custom_niches`. YouTube key: env
`NEXUS_KEY_YOUTUBE` or vault `ai/youtube` (same lookup as the model router; never logged - `Ctx.calls` drops `key`).

## Rules this implements

- **Demand without permission**: the Wikipedia Action API `prop=pageviews` (PageViewInfo, same backend as the
  Pageviews REST API, last 60 days) is served by the already-declared `en/ar/de.wikipedia.org` hosts, so a real daily
  demand signal works today. When `wikimedia.org` is declared, the REST API (`agent=user`, bots filtered) is preferred
  automatically. Report field `via` says which one was used. Note: the Action API path is not filtered to `agent=user`.
- **Missing data is never imputed**: a factor without data has `value: null`, `confidence: 0` and is excluded from the
  score; `opportunity.confidence` = confidence-weighted share of the factor weights that are known. `unknown_factors`
  lists them. Competition without YouTube is `unknown`, not "low".
- **No predictions**: the score is a 0-100 confidence-weighted mean of known factors (weights in `scoring.WEIGHTS`).
  Demand is observed Wikipedia readership, a proxy for interest, not for video views. Monetisation is an editorial rule
  set (niche tier minus sensitive-keyword penalty), not platform policy and not a revenue figure.
- **Claims**: a claim is `verified` only when >= 2 sources contain it (all its significant numbers, or >= 60 % of its
  content words for same-language sources without numbers). `independence` says whether the supporting sources are
  different publishers or e.g. two Wikipedia editions (not independent - often translations). AI claims are re-checked
  against the cited source text; failures go to `rejected_claims`. Conflicts (same subject, different number for the
  same unit) are marked `needs human check`. Without an AI provider the brief says `claims: deterministic fallback
  (NoProviderAvailable ...)` and still works.
- **Licences**: every item has `licence`; research text is cited, not copied; visual counts are search hits, not
  checked assets.

### Factors

| Factor | Orientation | Data | Without data |
|---|---|---|---|
| demand | higher better | pageviews: 0.7 x log-level (prev-28-day mean) + 0.3 x trend (last 7 / prev 28) | null |
| freshness | higher better | GDELT raw article counts 7 d vs 28 d; else matching RSS headlines (conf 0.3) | null |
| competition | higher **worse** | YouTube top results: median views (log) + result count, recency count | null ("unknown") |
| differentiation | higher better | heuristic: competitor titles not repeating the topic phrase + source disagreements + languages with readership | low confidence |
| evidence | higher better | sources, verified share, independent-publisher claims | null if no text |
| production_cost | higher **worse** | Openverse (commercial+modification licences) + NASA hit counts, research depth | null |
| monetisation | higher better | `niches.yaml` tier and sensitive keywords (rule) | always (conf 0.35) |
| platform_fit | higher better | niche priors per platform | always (conf 0.35) |
| localisation | higher better | pageviews of the same article in ar/de/fr/hi/zh/ru (via langlinks); hosts not declared listed in `skipped` | null |
| creative_potential | higher better | angles, verified numbers, disagreements (heuristic) | always (conf 0.3) |

## Hand-off to Account 2 (`to_production_brief`)

Returns the body of `POST /api/create` (`CreateReq{topic, scenes, lang, notes, vertical, preset}` in `nexus/api.py`),
equivalent to `nexus.create.create_video(topic, scenes=, lang=, notes=, preset=)`; the extra key `market` (ids, score,
confidence, angles) is ignored by pydantic/`create_video` and kept for traceability.

| Field | From |
|---|---|
| `topic` | `opportunity.topic` |
| `lang` | `opportunity.language` |
| `notes` | verified claims with `[n]` citations; then single-source claims ("use only with attribution"); then conflicts "DO NOT STATE"; then numbered source list with URL + licence. `create.research(notes=...)` puts notes first in the script-writer corpus. |
| `scenes` | `len(verified claims) + 1`, clamped 3-8 |
| `preset` | niche `preset` (registry/presets.yaml names: scientific, documentary, educational, storytelling, cinematic, commercial, news) |
| `vertical` | true when the niche's best platform prior is Shorts/TikTok |

## Evidence (demo, 2026-11-12)

`scripts/account3/market_demo.py` (real network through `nexus.net`, local Qwen3-4B through the router):
- `docs/results/account3/market_report_jwst_en.json` - English science topic.
- `docs/results/account3/market_report_ai_ar.json` - Arabic-language topic (الذكاء الاصطناعي, ar Wikipedia demand).
- `docs/results/account3/market_trends.json` - trend monitor on a 7-topic watchlist.
- `docs/results/account3/market_niches_check.json` - which configured Wikipedia categories exist.
### Demo results (observed 2026-11-12; Wikipedia readership, not video demand)

| Report | Score / confidence | Demand (mean views/day, Action-API pageviews) | Localisation measured | Claims / verified | AI |
|---|---|---|---|---|---|
| JWST (en, science) | 63.0 / 0.43 | 1,261 last 7 d vs 1,338 prev 28 d (ratio 0.94) | de 151/day, ar 17/day; fr/hi/zh/ru skipped (hosts not declared) | 22 / 6 (3 AI claims rejected by the re-check) | data_extraction: local Qwen3-4B via router; deep_research: no provider (local quality 3.33 < 4 needed) |
| الذكاء الاصطناعي (ar) | 57.8 / 0.42 | 926 vs 815 (ratio 1.14) | de 1,150/day; fr/hi/zh/ru skipped | 18 / 2 | none available at run time -> deterministic fallback (recorded) |

Unknown in both: freshness (GDELT not declared, no RSS configured) and competition (no YouTube key). Openverse and NASA
answered; Wikimedia rate-limited the shared IP repeatedly (HTTP 429) - the demo uses `Ctx(min_interval=3,
rate_limit_retries=4)`. `production_brief` in both files was regenerated offline from the saved opportunity + brief
with the final `to_production_brief()` (claims in the video language only); everything else is the live run.

Trend monitor (`market_trends.json`, 7 topics): Black hole falling (0.66), Large language model falling (0.81),
رمضان rising (1.19), others stable; none met the `emerging` rule (>= 1.5x and >= 20/day) on that day.
`market_niches_check.json`: all configured categories of the 15 niches exist on en/ar Wikipedia.

Known limits seen in the evidence: the automatic support test matches numbers and words, not meaning - e.g. an AI claim
"will be the largest telescope ever sent into space" is "verified" by two older NASA captions written before launch.
That is why notes say "a human verifies before publishing" and why two Wikipedia editions are labelled
"same publisher".

## Tests

`tests/fast/test_intel_market.py` (17 tests, < 1 s, no network): replays trimmed real responses
(`tests/fast/fixtures_market/jwst_en.json`, recorded with `--record`) and synthetic response-shape fixtures
(`shapes.json`, clearly marked, for hosts that could not be recorded). Covers: host rule, adapter statuses, contract
validity, unknown competition, localisation skips, cross-checking, AI claim re-check + NoProviderAvailable fallback,
conflicts, lone-year cross-language rule, window stats without imputation, trend monitor, REST-vs-Action pageviews, GDELT, RSS/Atom, YouTube quota +
key not leaked, Trends disabled, niche DB + custom niches, score/confidence arithmetic.

## Not available / unverified

- GDELT: host not declared and, from this sandbox, the connection is reset (network-blocked too). Freshness is unknown
  in the demo reports unless RSS feeds are configured.
- YouTube: no key on this machine -> competition unknown. Quota facts: see research sources (the current doc gives
  search.list its own 100-calls/day bucket; legacy 100-unit cost also recorded).
- Google Trends: alpha, allow-list only, no public spec -> disabled. TikTok Research API: not available to commercial
  creators -> not built.
- Openverse returned HTTP 504 from this machine during the demo (recorded as `error`, factor computed from NASA only).
- Wikimedia rate-limits this shared IP at times (HTTP 429); the adapter waits once (45 s) and otherwise reports `error`.
- Score weights and the log scales are editorial choices, not fitted to outcomes (there is no outcome data yet; agent F's
  analytics import could calibrate them later).

## Integration requests (for the lead)

1. **Declare demand/news hosts** in `nexus/registry/components.yaml` (all free, no key, no card):
```yaml
  - id: wikimedia-pageviews
    name: Wikimedia Analytics API (Pageviews, AQS)
    kind: service
    capability: [demand-signal]
    source_url: https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html
    license: "data CC BY-SA 4.0 (per AQS docs), code MIT-0"
    free_tier: "free, no key"
    quotas: "unpublished; descriptive User-Agent, serial requests; 429 => back off"
    commercial_use: "yes (aggregate statistics, attribution)"
    usage_restrictions: "no bulk crawling"
    data_retention: "article titles queried are sent to Wikimedia"
    availability: "remote; falls back to prop=pageviews on the wiki host"
    hardware: none
    network_hosts: [wikimedia.org]
    status: deployed
  - id: gdelt-doc
    name: GDELT DOC 2.0 API (news volume timeline)
    kind: service
    capability: [news-signal]
    source_url: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
    license: "GDELT open data; article text belongs to publishers (only counts/links used)"
    free_tier: "free, no key"
    quotas: "not published; NEXUS makes 1 call per topic"
    commercial_use: "unverified - check https://www.gdeltproject.org/about.html#termsofuse"
    usage_restrictions: "do not store article text"
    data_retention: "query terms are sent to GDELT"
    availability: "remote; freshness = unknown when unreachable"
    hardware: none
    network_hosts: [api.gdeltproject.org]
    status: adapter
```
2. **Add Wikipedia language hosts** to the existing `wikipedia` component for localisation demand:
   `fr.wikipedia.org, hi.wikipedia.org, zh.wikipedia.org, ru.wikipedia.org` (same terms as en/ar/de).
3. **YouTube Data API** (only if the owner wants competition data; needs the owner's Google Cloud project and key -
   owner must confirm no billing account is requested):
```yaml
  - id: youtube-data-api
    name: YouTube Data API v3 (search.list, videos.list - public metadata)
    kind: service
    capability: [competition-signal]
    source_url: https://developers.google.com/youtube/v3/determine_quota_cost
    license: "YouTube API Services Terms of Service"
    free_tier: "default quota: 100 search.list calls/day + 10,000 units/day other endpoints"
    quotas: "search.list 1 call of 100/day bucket (legacy docs: 100 units); videos.list 1 unit"
    account_required: true
    commercial_use: "per YouTube API Services Terms; metadata only, no downloads"
    usage_restrictions: "no video download; respect data-retention rules of the API terms"
    data_retention: "query terms sent to Google"
    availability: "remote; competition = unknown without key"
    hardware: none
    network_hosts: [www.googleapis.com]
    status: adapter
```
4. **RSS feeds**: each feed host the owner lists in `settings.json market.rss_feeds` needs a declaration (e.g. one
   `rss-feeds` component with an explicit host list). Proposal: a component per curated feed list, reviewed by a human.
5. **API/UI**: `GET /api/market/opportunity?topic=&niche=&lang=` -> `analyse()`; `GET /api/market/trends?niches=` ->
   `trend_monitor(watchlist(...))`; `POST /api/market/produce` -> `jobs.submit("create-from-idea", to_production_brief(...))`.
   Long runs (AI brief) should go through the job ledger as a `market-research` stage (needs `nexus/workflows.py`).
6. **tasks.yaml**: register "MKT-1 market intelligence (agent C)" as implemented + tested.


## V2 assessment (`market/assess_v2.py`, `GET /api/a3/research/{id}/assessment`)
Evidence-quality layer over a report: publisher credibility tiers, cross-publisher verification (pages of one site
count once), recency from publication dates only (Wikipedia retrieval dates excluded), geography (ISO countries of the
topic and its claims), language demand per Wikipedia edition, competition and saturation (unknown until YouTube supply
is measured - never guessed), evidence coverage (share of score weight with measured evidence) and an uncertainty
interval (score with every unknown factor at its worst and best). Real reports (`market_v2_assessments.json`):
JWST 63.0 in [50.1, 70.6], 22/22 claims single-publisher → "indicative only"; AI (ar) 57.8 in [45.9, 66.5].
The geography step exposed a resolver false positive (Kourou → Guinea), fixed and regression-tested.
