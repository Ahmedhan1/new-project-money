# Account 3 - research and demand sources (agent C, market intelligence)

Checked 2026-11-12. "Verified" = read on the first-party page listed (or exercised live from this machine, said so).
"Unverified" = taken from secondary sources or not re-checked. All calls go through `nexus/net.py`, so every host must
be declared in `nexus/registry/components.yaml` (Cost Guard). Status column says whether the host is declared on
branch `work/account-3-intelligence-growth` (bd71690).

| Source | What it gives us | Endpoint | Key / cost | Host declared? | Adapter |
|---|---|---|---|---|---|
| Wikipedia Action API (search + extracts + langlinks) | facts, citations, titles of the same article in other languages | `https://{lang}.wikipedia.org/w/api.php` | free, no key | en, ar, de: yes. fr, hi, zh, ru: **no** | `wiki_search`, `wiki_langlinks` |
| Wikipedia Action API `prop=pageviews` (PageViewInfo extension) | daily pageviews of an article for the last 1-60 days (same data as the Pageviews REST API, served by the wiki host) | `https://{lang}.wikipedia.org/w/api.php?action=query&prop=pageviews&pvipdays=60` | free, no key | en, ar, de: yes | `wiki_pageviews` (fallback path) |
| Wikimedia Pageviews REST API (AQS) | daily per-article pageviews, `agent=user` filters known bots, any date range since 2015-07 | `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{article}/daily/{start}/{end}` | free, no key; descriptive User-Agent required | **no** (`wikimedia.org`) | `wiki_pageviews` (preferred path) |
| GDELT DOC 2.0 API | news coverage volume timeline (`mode=timelinevolraw`, raw article counts + `norm`), article lists | `https://api.gdeltproject.org/api/v2/doc/doc` | free, no key | **no** | `gdelt_timeline` |
| RSS / Atom feeds | recent headlines from a configurable list of publishers (freshness) | per feed | free | only if the feed host is declared | `rss_feed` (stdlib `xml.etree`) |
| YouTube Data API v3 `search.list` + `videos.list` | competition: number of recent results, publish dates, view counts of top results (public metadata only) | `https://www.googleapis.com/youtube/v3/...` | free quota, needs the owner's API key (`NEXUS_KEY_YOUTUBE`) | **no** (`www.googleapis.com`) | `youtube_competition` (off without key + host) |
| Google Trends API (alpha) | search interest | not public | allow-listed alpha testers only | n/a | `google_trends` - always `disabled` unless the owner states access; **no scraping of trends.google.com** |
| TikTok Research API | - | - | academic / non-profit researchers only | n/a | **not built** (see below) |
| Openverse API | count of CC-licensed images for the topic = visual material available | `https://api.openverse.org/v1/images/` | anonymous, no key | yes | `openverse_count` |
| NASA Image and Video Library | count of NASA images/videos for the topic | `https://images-api.nasa.gov/search` | no key | yes | `nasa_count` |

## Facts and their sources

- **PageViewInfo `prop=pageviews`**: "the number of daily pageviews for each of the last pvipdays days", `pvipdays`
  1-60 (default 60). Verified: https://www.mediawiki.org/wiki/Extension:PageViewInfo . Exercised live on
  ar.wikipedia.org from this machine on 2026-11-12 (returns `{date: count|null}`; `null` = no data for that day; requests
  with a generic User-Agent get a Wikimedia error page, `config.USER_AGENT` is accepted). Because these numbers come
  from the same Pageviews backend, a 7-vs-28-day comparison fits inside the 60-day window - this is why the demand signal
  works today **without** a new host.
- **Pageviews REST API**: data licence CC BY-SA 4.0 / code MIT-0 per the reference page footer
  (https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html). Endpoint shape
  exercised live with curl on 2026-11-12 (`.../per-article/en.wikipedia/all-access/user/Black_hole/daily/20261001/20261005`
  returned `items[].views`). Rate limits: unverified (Wikimedia's API etiquette asks for a descriptive User-Agent and
  serial requests).
- **GDELT DOC 2.0**: endpoint `https://api.gdeltproject.org/api/v2/doc/doc`; modes `artlist`, `timelinevol`,
  `timelinevolraw` (raw article counts with `norm`), `timelinetone`; `timespan` e.g. `1w`, `3m`; searches a rolling
  3-month window. Verified: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ . Rate limits / terms: not stated on
  that page (unverified). From this sandbox the host reset the connection (2026-11-12), so GDELT is also
  network-blocked here, independent of the Cost Guard.
- **YouTube Data API quota**: the current first-party page says "Projects that enable the YouTube Data API have a
  default quota allocation of 100 `search.list` calls, 100 `videos.insert` calls, and 10,000 units per day combined
  for all other endpoints"; `search.list` "Each call costs 1 quota" in its own 100/day bucket; `videos.list` costs 1 unit.
  Verified 2026-11-12: https://developers.google.com/youtube/v3/determine_quota_cost . NOTE: older documentation
  (and the task brief) gave `search.list = 100 units` from the shared 10,000 pool. The adapter records **both**: the
  call against the 100/day search bucket and the legacy-equivalent 100 units, so a budget computed either way is
  conservative. Using the API needs a Google Cloud project; enabling the API itself does not require billing
  (unverified for every account type - the owner must confirm no card is requested, otherwise it is refused under the
  zero-spend rule).
- **Google Trends API**: alpha, "a limited set of developers", access by application form. Verified:
  https://developers.google.com/search/apis/trends and https://developers.google.com/search/blog/2025/07/trends-api .
  No public endpoint is documented for non-testers, so the adapter cannot be implemented against a spec; it reports
  `disabled` with this reason. pytrends (unofficial scraper) is archived and scraping is against Google's terms - not used.
- **TikTok Research API**: applicants must be affiliated with academic or non-profit research institutions and "be
  independent of commercial interests". Verified: https://developers.tiktok.com/products/research-api/ . A commercial
  creator does not qualify, so nothing is built around it; TikTok demand is `unknown` in every report.
- **Openverse / NASA**: licences and quota facts as declared in `nexus/registry/components.yaml` (entries `openverse`,
  `nasa-media`); both are already used by `nexus/assets.py` / `nexus/broll.py`.

## Licence handling per item

Every normalised item carries `licence`:
- Wikipedia text: `CC BY-SA 4.0 - facts cited, text not copied`.
- Pageview counts: `Wikimedia pageview statistics (AQS docs: CC BY-SA 4.0) - aggregated counts`.
- GDELT: `GDELT open data - link to publisher; article text not stored`.
- RSS: `publisher copyright - headline + link only`.
- YouTube: `YouTube API Services terms - public metadata only, no download`.
- Openverse: the per-asset CC licence string returned by the API (`license` + `license_version`).
- NASA: `NASA media guidelines - check item rights`.
