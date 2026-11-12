"""Source adapters for market intelligence. Each returns a SourceResult dict:

    {"adapter", "status": ok|skipped|disabled|error, "reason", "items": [Item...], "data": {...}, "calls": [...]}

An Item is normalised: {"source", "url", "date", "licence", "title", "text"?, "kind"}.
Every HTTP call goes through `nexus.net` (Cost Guard). An adapter whose host is not declared in
registry/components.yaml is *skipped* with the reason and the integration request - it never bypasses the guard.
Adapters take a `Ctx`, so tests replay recorded JSON fixtures without network.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ... import config, costguard, net

WIKI_LICENCE = "CC BY-SA 4.0 (Wikipedia) - facts cited, text not copied"
PV_LICENCE = "Wikimedia pageview statistics (AQS docs: CC BY-SA 4.0) - aggregated counts"
LOCALISATION_LANGS = ("ar", "de", "fr", "hi", "zh", "ru")
YT_SEARCH_COST = {"search_bucket_calls": 1, "legacy_units": 100}  # see docs/ACCOUNT3_RESEARCH_SOURCES.md
YT_VIDEOS_COST = {"units": 1}


def host_declared(url: str) -> tuple[bool, str]:
    """Same rule as costguard.check_host, without logging a block (we ask before calling, we do not probe)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in costguard.PAID_HOSTS:
        return False, f"{host} is a known paid endpoint"
    for cid, c in costguard.manifest().items():
        if host in c.get("network_hosts", []) and not costguard.violations(c):
            return True, cid
    return False, f"host {host} is not declared in nexus/registry/components.yaml (integration request in docs/ACCOUNT3_MARKET_INTELLIGENCE.md)"


def _net_text(url: str, params: dict | None = None) -> str:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with tempfile.TemporaryDirectory() as d:
        return net.download(url, Path(d) / "feed.xml", timeout=20, retries=2).read_text(encoding="utf-8", errors="replace")


@dataclass
class Ctx:
    """Network + clock + settings seam. Production uses nexus.net; tests pass fixture replayers."""
    fetch_json: Callable[..., dict] = field(default=lambda url, params=None: net.get_json(url, params, timeout=25, retries=2, max_wait=20))
    fetch_text: Callable[..., str] = field(default=_net_text)
    host_ok: Callable[[str], tuple[bool, str]] = field(default=host_declared)
    today: dt.date = field(default_factory=lambda: dt.date.today())
    settings: dict = field(default_factory=lambda: (config.settings().get("market") or {}))
    youtube_key: Callable[[], str | None] = field(default=lambda: _youtube_key())
    calls: list = field(default_factory=list)
    min_interval: float = 2.0  # polite serial requests (Wikimedia API etiquette); tests set 0
    rate_limit_wait: float = 45.0  # patient retries after a 429 (we back off and wait, we do not evade limits)
    rate_limit_retries: int = 1
    _last: float = 0.0

    def get(self, url: str, params: dict | None = None, cost: dict | None = None, text: bool = False):
        ok, why = self.host_ok(url)
        if not ok:
            raise HostNotDeclared(why)
        safe = {k: v for k, v in (params or {}).items() if k != "key"}  # never record an API key
        self.calls.append({"url": url, "params": safe, "cost": cost or {}})
        for attempt in range(self.rate_limit_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                return self.fetch_text(url, params) if text else self.fetch_json(url, params)
            except net.RemoteUnavailable as e:
                if attempt >= self.rate_limit_retries or not self.rate_limit_wait or not ("rate limited" in str(e) or "HTTP 429" in str(e)):
                    raise
                self.calls[-1]["retried_after_rate_limit_s"] = self.rate_limit_wait
                time.sleep(self.rate_limit_wait)


class HostNotDeclared(RuntimeError):
    pass


def _youtube_key() -> str | None:
    from ..model_router.router import api_key
    return api_key("youtube")


def result(adapter: str, status: str = "ok", reason: str = "", items: list | None = None, data: dict | None = None) -> dict:
    return {"adapter": adapter, "status": status, "reason": reason, "items": items or [], "data": data or {}}


def _guard(adapter: str, fn):
    """Run an adapter body; turn 'host not declared' into skipped, network failures into error. Never raises."""
    try:
        return fn()
    except HostNotDeclared as e:
        return result(adapter, "skipped", str(e))
    except (net.RemoteUnavailable, costguard.CostGuardError, OSError, ValueError, KeyError, ET.ParseError) as e:
        return result(adapter, "error", f"{type(e).__name__}: {str(e)[:200]}")


def item(source: str, url: str | None, date: str | None, licence: str, title: str, kind: str, text: str | None = None, **extra) -> dict:
    d = {"source": source, "url": url, "date": date, "licence": licence, "title": title, "kind": kind}
    if text:
        d["text"] = text
    d.update(extra)
    return d


def wiki_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ------------------------------------------------------------------ Wikimedia
def wiki_search(ctx: Ctx, topic: str, lang: str = "en", max_pages: int = 2) -> dict:
    """Search + intro extracts (facts). Ranking reuses nexus.create.rank_hits (K-4)."""
    from ...create import rank_hits

    def run():
        api = f"https://{lang}.wikipedia.org/w/api.php"
        hits = ctx.get(api, {"action": "query", "list": "search", "srsearch": topic, "format": "json", "srlimit": 8})
        raw = hits.get("query", {}).get("search", [])
        titles = (rank_hits(topic, raw) if lang == "en" else [h["title"] for h in raw])[:max_pages]
        if not titles:
            return result("wiki_search", "ok", f"no {lang}.wikipedia hits", data={"lang": lang, "titles": []})
        pages = ctx.get(api, {"action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1, "inprop": "url",
                              "titles": "|".join(titles), "format": "json", "redirects": 1})
        items = []
        for pg in pages.get("query", {}).get("pages", {}).values():
            ex = (pg.get("extract") or "").strip()
            if ex:
                items.append(item(f"wikipedia:{lang}", pg.get("fullurl") or wiki_url(lang, pg["title"]), pg.get("touched", "")[:10] or None,
                                  WIKI_LICENCE, pg["title"], "facts", ex[:4000], lang=lang))
        order = {t: i for i, t in enumerate(titles)}
        items.sort(key=lambda x: order.get(x["title"], 99))
        return result("wiki_search", items=items, data={"lang": lang, "titles": [i["title"] for i in items]})
    return _guard("wiki_search", run)


def wiki_extract(ctx: Ctx, title: str, lang: str) -> dict:
    """Intro extract of a known title (e.g. the Arabic article found through langlinks)."""
    def run():
        api = f"https://{lang}.wikipedia.org/w/api.php"
        pages = ctx.get(api, {"action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1, "inprop": "url",
                              "titles": title, "format": "json", "redirects": 1})
        items = [item(f"wikipedia:{lang}", pg.get("fullurl") or wiki_url(lang, pg["title"]), pg.get("touched", "")[:10] or None,
                      WIKI_LICENCE, pg["title"], "facts", (pg.get("extract") or "")[:4000], lang=lang)
                 for pg in pages.get("query", {}).get("pages", {}).values() if pg.get("extract")]
        return result("wiki_extract", items=items, data={"lang": lang})
    return _guard("wiki_extract", run)


def wiki_langlinks(ctx: Ctx, title: str, lang: str = "en", targets=LOCALISATION_LANGS) -> dict:
    def run():
        d = ctx.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "prop": "langlinks", "titles": title,
                                                                "lllimit": 500, "format": "json", "redirects": 1})
        links = {}
        for pg in d.get("query", {}).get("pages", {}).values():
            for ll in pg.get("langlinks", []):
                if ll.get("lang") in targets:
                    links[ll["lang"]] = ll.get("*") or ll.get("title")
        return result("wiki_langlinks", data={"from": f"{lang}:{title}", "links": links,
                                               "missing": [t for t in targets if t not in links]})
    return _guard("wiki_langlinks", run)


def wiki_pageviews(ctx: Ctx, title: str, lang: str = "en", days: int = 60) -> dict:
    """Daily user pageviews. Preferred: Wikimedia Pageviews REST API (wikimedia.org, agent=user). Fallback: the wiki's
    own Action API `prop=pageviews` (PageViewInfo, same backend, last <=60 days) on the already-declared wiki host."""
    end = ctx.today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    article = urllib.parse.quote(title.replace(" ", "_"), safe="")
    rest = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia/all-access/user/{article}/daily/"
            f"{start:%Y%m%d}00/{end:%Y%m%d}00")

    def run():
        series: dict[str, int | None] = {}
        if ctx.host_ok(rest)[0]:
            d = ctx.get(rest)
            for it in d.get("items", []):
                ts = it["timestamp"]
                series[f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"] = int(it["views"])
            via, url = "pageviews-rest", rest
        else:
            api = f"https://{lang}.wikipedia.org/w/api.php"
            d = ctx.get(api, {"action": "query", "prop": "pageviews", "titles": title, "pvipdays": min(days, 60),
                              "format": "json", "redirects": 1})
            for pg in d.get("query", {}).get("pages", {}).values():
                for day, v in (pg.get("pageviews") or {}).items():
                    series[day] = None if v is None else int(v)
            via, url = "action-api-prop-pageviews", f"{api}?action=query&prop=pageviews&titles={urllib.parse.quote(title)}"
        series = dict(sorted(series.items()))
        it = item(f"pageviews:{lang}", url, end.isoformat(), PV_LICENCE, title, "demand")
        return result("wiki_pageviews", items=[it], data={"lang": lang, "title": title, "via": via, "series": series,
                                                           "note": "the action API counts all agents; the REST path counts agent=user"
                                                           if via != "pageviews-rest" else "agent=user"})
    return _guard("wiki_pageviews", run)


def window_stats(series: dict, today: dt.date, recent: int = 7, base: int = 28) -> dict:
    """Mean daily views of the last `recent` days vs the `base` days before. Missing days are excluded, not zero-filled."""
    end = today - dt.timedelta(days=1)
    def mean(a, b):
        vals = [series.get((end - dt.timedelta(days=i)).isoformat()) for i in range(a, b)]
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals) if vals else None), len(vals)
    r, rn = mean(0, recent)
    b, bn = mean(recent, recent + base)
    ratio = (r / b) if (r is not None and b) else None
    return {"recent_mean": r, "recent_days": rn, "base_mean": b, "base_days": bn, "ratio": ratio,
            "coverage": (rn + bn) / (recent + base)}


# ------------------------------------------------------------------ news
def gdelt_timeline(ctx: Ctx, query: str, timespan: str = "35d") -> dict:
    """GDELT DOC 2.0 raw article-count timeline (news momentum)."""
    def run():
        q = f'"{query}"' if " " in query else query
        d = ctx.get("https://api.gdeltproject.org/api/v2/doc/doc", {"query": q, "mode": "timelinevolraw", "format": "json",
                                                                    "timespan": timespan})
        series = {}
        for s in d.get("timeline", []):
            for p in s.get("data", []):
                day = f"{p['date'][:4]}-{p['date'][4:6]}-{p['date'][6:8]}"
                series[day] = series.get(day, 0) + int(p.get("value", 0))
        it = item("gdelt", f"https://api.gdeltproject.org/api/v2/doc/doc?query={urllib.parse.quote(q)}&mode=timelinevolraw",
                  ctx.today.isoformat(), "GDELT open data - link to publisher; article text not stored", query, "news-volume")
        return result("gdelt_timeline", items=[it], data={"series": dict(sorted(series.items())), "timespan": timespan})
    return _guard("gdelt_timeline", run)


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def parse_feed(xml_text: str, feed_url: str) -> list[dict]:
    """RSS 2.0 and Atom with the stdlib. Headline + link + date only (publisher copyright)."""
    root = ET.fromstring(xml_text)
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        out.append(item(f"rss:{urllib.parse.urlparse(feed_url).hostname}", (it.findtext("link") or "").strip() or None,
                        _parse_date(it.findtext("pubDate")), "publisher copyright - headline + link only",
                        (it.findtext("title") or "").strip(), "headline", re.sub(r"<[^>]+>", " ", it.findtext("description") or "")[:400].strip()))
    for e in root.findall("a:entry", ns):
        link = e.find("a:link", ns)
        out.append(item(f"rss:{urllib.parse.urlparse(feed_url).hostname}", link.get("href") if link is not None else None,
                        _parse_date(e.findtext("a:updated", namespaces=ns) or e.findtext("a:published", namespaces=ns)),
                        "publisher copyright - headline + link only", (e.findtext("a:title", namespaces=ns) or "").strip(), "headline",
                        re.sub(r"<[^>]+>", " ", e.findtext("a:summary", namespaces=ns) or "")[:400].strip()))
    return out


def rss_feeds(ctx: Ctx, query: str, feeds: list[str] | None = None) -> dict:
    """Configured feeds (settings.json market.rss_feeds); items whose title/summary mention the query terms."""
    feeds = feeds if feeds is not None else list(ctx.settings.get("rss_feeds") or [])
    if not feeds:
        return result("rss_feeds", "skipped", "no feeds configured (settings.json market.rss_feeds)")
    terms = [w for w in re.findall(r"\w{3,}", query.lower())]
    items, per_feed = [], {}
    for f in feeds:
        r = _guard("rss_feeds", lambda f=f: result("rss_feeds", items=parse_feed(ctx.get(f, text=True), f)))
        per_feed[f] = r["status"] if r["status"] == "ok" else f"{r['status']}: {r['reason']}"
        for it in r["items"]:
            hay = (it["title"] + " " + it.get("text", "")).lower()
            if terms and all(t in hay for t in terms):
                items.append(it)
    ok = any(v == "ok" for v in per_feed.values())
    return result("rss_feeds", "ok" if ok else "skipped", "" if ok else "no feed readable", items, {"feeds": per_feed})


# ------------------------------------------------------------------ competition
def youtube_competition(ctx: Ctx, query: str, lang: str = "en", region: str | None = None, n: int = 25) -> dict:
    """Public metadata of the top results (count, recency, views). Off unless the owner configured NEXUS_KEY_YOUTUBE."""
    key = ctx.youtube_key()
    if not key:
        return result("youtube_competition", "disabled", "no YouTube Data API key (env NEXUS_KEY_YOUTUBE or vault ai/youtube)")

    def run():
        base = "https://www.googleapis.com/youtube/v3/"
        p = {"part": "snippet", "q": query, "type": "video", "maxResults": n, "relevanceLanguage": lang[:2], "key": key}
        if region:
            p["regionCode"] = region
        s = ctx.get(base + "search", p, cost=YT_SEARCH_COST)
        ids = [x["id"]["videoId"] for x in s.get("items", []) if x.get("id", {}).get("videoId")]
        stats = {}
        if ids:
            v = ctx.get(base + "videos", {"part": "statistics,snippet", "id": ",".join(ids), "key": key}, cost=YT_VIDEOS_COST)
            stats = {x["id"]: x for x in v.get("items", [])}
        items = []
        for vid in ids:
            x = stats.get(vid, {})
            sn = x.get("snippet", {})
            st = x.get("statistics", {})
            items.append(item("youtube", f"https://www.youtube.com/watch?v={vid}", (sn.get("publishedAt") or "")[:10] or None,
                              "YouTube API Services terms - public metadata only, no download", sn.get("title", ""), "competitor",
                              views=int(st["viewCount"]) if "viewCount" in st else None, channel=sn.get("channelTitle")))
        return result("youtube_competition", items=items, data={"total_results_estimate": s.get("pageInfo", {}).get("totalResults"),
                                                                 "recent_cutoff": (ctx.today - dt.timedelta(days=90)).isoformat(),
                                                                 "quota": {"search": YT_SEARCH_COST, "videos": YT_VIDEOS_COST if ids else {}}})
    return _guard("youtube_competition", run)


def google_trends(ctx: Ctx, query: str) -> dict:
    """The official Google Trends API is an allow-listed alpha with no public spec. Never scrape trends.google.com."""
    if not ctx.settings.get("google_trends_alpha_access"):
        return result("google_trends", "disabled", "Google Trends API is a limited alpha (developers.google.com/search/apis/trends); "
                      "owner has not declared access; scraping trends.google.com is not allowed")
    return result("google_trends", "disabled", "owner declared alpha access, but the alpha API spec is not public - an adapter "
                  "will be written against the spec the owner receives (integration request)")


# ------------------------------------------------------------------ visual material (production difficulty)
def openverse_count(ctx: Ctx, query: str) -> dict:
    def run():
        d = ctx.get("https://api.openverse.org/v1/images/", {"q": query, "page_size": 20, "license_type": "commercial,modification"})
        items = [item("openverse", r.get("foreign_landing_url"), None,
                      f"{(r.get('license') or '?').upper()} {r.get('license_version') or ''}".strip(), r.get("title") or query, "visual")
                 for r in d.get("results", [])]
        return result("openverse_count", items=items, data={"result_count": d.get("result_count")})
    return _guard("openverse_count", run)


def nasa_count(ctx: Ctx, query: str) -> dict:
    def run():
        d = ctx.get("https://images-api.nasa.gov/search", {"q": query, "media_type": "image,video", "page_size": 20})
        col = d.get("collection", {})
        items = []
        for it in col.get("items", [])[:20]:
            m = (it.get("data") or [{}])[0]
            items.append(item("nasa", f"https://images.nasa.gov/details/{m.get('nasa_id')}", (m.get("date_created") or "")[:10] or None,
                              "NASA media guidelines - check item rights", m.get("title", ""), "visual",
                              (m.get("description") or "")[:600]))
        return result("nasa_count", items=items, data={"total_hits": col.get("metadata", {}).get("total_hits")})
    return _guard("nasa_count", run)
