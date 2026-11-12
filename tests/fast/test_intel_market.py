"""Market intelligence (Account 3 / agent C): adapters, brief, scoring, trend monitor, hand-off. No network: recorded
fixtures (tests/fast/fixtures_market/jwst_en.json, trimmed real responses) + synthetic response-shape fixtures."""
import datetime as dt
import json
from pathlib import Path

import pytest

from nexus.intelligence import contracts
from nexus.intelligence.market import brief as brief_mod, niches, pipeline, scoring, sources as src

FIX = Path(__file__).parent / "fixtures_market"
TODAY = dt.date(2026, 11, 12)
SHAPES = json.loads((FIX / "shapes.json").read_text(encoding="utf-8"))
IGNORE = {"key", "format", "redirects", "exintro", "explaintext", "inprop", "srlimit", "lllimit", "page_size", "media_type", "license_type"}


def _norm(url, params):
    return url, json.dumps({k: v for k, v in (params or {}).items() if k not in IGNORE}, sort_keys=True, ensure_ascii=False)


def replay_ctx(name="jwst_en.json", host_ok=None, extra=None, **kw):
    recs = json.loads((FIX / name).read_text(encoding="utf-8"))["records"]
    table = {_norm(r["url"], r["params"]): r["response"] for r in recs}

    def fetch(url, params=None):
        for prefix, resp in (extra or {}).items():
            if url.startswith(prefix):
                return resp(url, params) if callable(resp) else resp
        k = _norm(url, params)
        if k not in table:
            raise src.net.RemoteUnavailable(f"no fixture for {k}")
        return table[k]
    return src.Ctx(fetch_json=fetch, fetch_text=lambda url, params=None: (extra or {})[url], host_ok=host_ok or src.host_declared,
                   today=TODAY, settings=kw.pop("settings", {}), youtube_key=kw.pop("youtube_key", lambda: None), min_interval=0,
                   rate_limit_wait=0, **kw)


@pytest.fixture(scope="module")
def report():
    return pipeline.analyse(replay_ctx(), "James Webb Space Telescope", "science", "en", ai=None, settings={})


def test_host_rule_matches_cost_guard():
    assert src.host_declared("https://en.wikipedia.org/w/api.php")[0]
    ok, why = src.host_declared("https://api.gdeltproject.org/api/v2/doc/doc")
    assert not ok and "not declared" in why
    assert not src.host_declared("https://api.openai.com/v1/x")[0]


def test_report_adapters_are_honest(report):
    a = report["adapters"]
    assert a["wiki"]["status"] == "ok" and a["pageviews"]["status"] == "ok"
    assert a["gdelt"]["status"] == "skipped" and "components.yaml" in a["gdelt"]["reason"]
    assert a["youtube"]["status"] == "disabled" and a["google_trends"]["status"] == "disabled"
    for it in a["wiki"]["items"] + a["nasa"]["items"]:
        assert {"source", "url", "date", "licence"} <= set(it) and it["licence"]


def test_opportunity_contract_and_missing_data(report):
    o = report["opportunity"]
    assert contracts.validate(o) == []
    f = o["factors"]
    assert f["competition"]["value"] is None and f["competition"]["confidence"] == 0  # never guessed
    assert "competition" in o["unknown_factors"] and "freshness" in o["unknown_factors"]
    assert f["demand"]["value"] is not None and f["demand"]["observed"]["base_days"] == 28
    assert f["demand"]["via"] == "action-api-prop-pageviews"
    loc = f["localisation"]
    assert set(loc["observed"]) == {"ar", "de"} and "not declared" in loc["skipped"]["fr"]
    assert 0 < o["confidence"] < 1 and 0 <= o["score"] <= 100
    for k, x in f.items():
        assert set(x) >= {"value", "evidence", "confidence", "method"}, k


def test_brief_cross_checks_claims(report):
    b = report["brief"]
    assert contracts.validate(b) == []
    ver = [c for c in b["claims"] if c["status"] == "verified"]
    assert ver and all(len(c["supported_by"]) >= 2 for c in ver)
    # en + ar Wikipedia share a publisher: that is said, not hidden
    assert any(c["independence"].startswith("same publisher") for c in ver)
    assert "deterministic only" in b["method"][0]
    assert b["angles"] and b["open_questions"] is not None


def test_production_brief_mapping(report):
    p = report["production_brief"]
    assert {"topic", "scenes", "lang", "notes", "vertical", "preset"} <= set(p)
    assert p["preset"] == "scientific" and p["lang"] == "en" and not p["vertical"]
    assert "[1]" in p["notes"] and "Sources:" in p["notes"] and 3 <= p["scenes"] <= 8


def test_ai_claims_are_rechecked_and_fallback():
    s = [{"id": "s1", "source": "wikipedia:en", "url": "u1", "lang": "en", "kind": "facts", "title": "A",
          "text": "The telescope launched in December 2021 with a 6.5 metre mirror. It observes in the infrared."},
         {"id": "s2", "source": "nasa", "url": "u2", "lang": "en", "kind": "visual", "title": "B",
          "text": "Webb has a 6.5 metre primary mirror and launched in 2021 from Kourou."}]
    results = [{"items": [{k: v for k, v in x.items() if k != "id"} for x in s]}]

    def ai(task, prompt, schema):
        if task == "data_extraction":
            return {"claims": [{"text": "Webb has a 6.5 metre mirror and launched in 2021.", "source_ids": ["s1", "s2"]},
                               {"text": "Webb cost 99 billion dollars.", "source_ids": ["s1"]}]}
        return {"angles": ["How a folded mirror works"], "open_questions": ["What limits its lifetime?"]}
    b = brief_mod.build_brief("JWST", results, "en", ai=ai)
    assert b["claims"][0]["by"] == "ai" and b["claims"][0]["status"] == "verified"
    assert [r["text"] for r in b["rejected_claims"]] == ["Webb cost 99 billion dollars."]
    assert any(a.startswith("(AI suggestion)") for a in b["angles"])

    def down(task, prompt, schema):
        from nexus.intelligence.model_router import NoProviderAvailable
        raise NoProviderAvailable(task, ["nothing installed"])
    b2 = brief_mod.build_brief("JWST", results, "en", ai=down)
    assert "deterministic fallback (NoProviderAvailable" in b2["method"][0] and b2["claims"]


def test_lone_year_does_not_corroborate_across_languages():
    ar = {"id": "s2", "lang": "ar", "text": "أُطلق التلسكوب سنة 2021 وقطر مرآته 6.5 متر."}
    nasa = {"id": "s3", "lang": "en", "text": "Image released in 2021 of a galaxy."}
    assert brief_mod._supports("The telescope launched in 2021 with a 6.5 metre mirror.", "en", ar)
    assert not brief_mod._supports("Something happened in 2021 in the field.", "en", {**nasa, "lang": "ar"})
    assert not brief_mod._supports("The telescope launched in 2021 with a 6.5 metre mirror.", "en", nasa)


def test_conflicts_detected_between_sources():
    claims = [{"text": "The Great Pyramid was the tallest structure for 3800 years in history.", "source_ids": ["s1"], "lang": "en"},
              {"text": "The Great Pyramid was the tallest structure for 3871 years in history.", "source_ids": ["s2"], "lang": "en"}]
    srcs = [{"id": "s1", "url": "a"}, {"id": "s2", "url": "b"}]
    c = brief_mod.find_conflicts(claims, srcs)
    assert c and c[0]["about"] == "years" and {v["value"] for v in c[0]["values"]} == {"3800", "3871"}


def test_window_stats_do_not_impute():
    series = {(TODAY - dt.timedelta(days=i)).isoformat(): (200 if i <= 7 else 100) for i in range(1, 36)}
    series[(TODAY - dt.timedelta(days=2)).isoformat()] = None
    st = src.window_stats(series, TODAY)
    assert st["recent_days"] == 6 and st["base_days"] == 28 and st["ratio"] == pytest.approx(2.0)
    assert src.window_stats({}, TODAY)["ratio"] is None


def test_trend_monitor_flags_emerging():
    def pv(url, params):
        hot = "Hot" in params["titles"]
        return {"query": {"pages": {"1": {"title": params["titles"], "pageviews": {
            (TODAY - dt.timedelta(days=i)).isoformat(): (500 if hot and i <= 7 else 100) for i in range(1, 61)}}}}}
    ctx = replay_ctx(extra={"https://en.wikipedia.org/w/api.php": pv})
    tm = pipeline.trend_monitor(ctx, [{"topic": "Hot", "title": "Hot"}, {"topic": "Calm", "title": "Calm"},
                                      {"topic": "Gone", "title": "Gone", "lang": "fr"}])
    st = {r["topic"]: r["status"] for r in tm["topics"]}
    assert st == {"Hot": "emerging", "Calm": "stable", "Gone": "unknown"} and tm["emerging"] == ["Hot"]


def test_rest_pageviews_preferred_when_declared():
    ctx = replay_ctx(host_ok=lambda u: (True, "test"), extra={"https://wikimedia.org/": SHAPES["pageviews_rest"]})
    r = src.wiki_pageviews(ctx, "Test topic", "en")
    assert r["data"]["via"] == "pageviews-rest" and r["data"]["series"]["2026-11-11"] == 320
    assert "agent/user" not in r["items"][0]["url"] and "/user/Test_topic/daily/" in r["items"][0]["url"]


def test_gdelt_freshness_when_host_declared():
    ctx = replay_ctx(host_ok=lambda u: (True, "test"), extra={"https://api.gdeltproject.org/": SHAPES["gdelt_timelinevolraw"]})
    g = src.gdelt_timeline(ctx, "test topic")
    assert g["status"] == "ok" and g["data"]["series"]["2026-11-05"] == 9
    f = scoring.freshness(g, None, TODAY.isoformat())
    assert f["value"] > 0 and f["observed"]["articles_7d"] == 48


def test_rss_and_atom_parsing():
    ctx = replay_ctx(host_ok=lambda u: (True, "t"), extra={"https://feed.example/rss": SHAPES["rss"], "https://feed.example/atom": SHAPES["atom"]})
    r = src.rss_feeds(ctx, "test topic", ["https://feed.example/rss", "https://feed.example/atom"])
    assert r["status"] == "ok" and [i["url"] for i in r["items"]] == ["https://example.org/a", "https://example.org/c"]
    assert r["items"][0]["date"] == "2026-11-10" and r["items"][1]["date"] == "2026-11-11"
    assert src.rss_feeds(replay_ctx(), "x")["status"] == "skipped"


def test_youtube_needs_key_and_records_quota_without_leaking_key():
    assert src.youtube_competition(replay_ctx(), "x")["status"] == "disabled"
    resp = {"https://www.googleapis.com/youtube/v3/search": SHAPES["youtube_search"],
            "https://www.googleapis.com/youtube/v3/videos": SHAPES["youtube_videos"]}
    ctx = replay_ctx(host_ok=lambda u: (True, "t"), extra=resp, youtube_key=lambda: "SECRET123")
    r = src.youtube_competition(ctx, "test topic")
    assert r["status"] == "ok" and r["items"][0]["views"] == 12000 and r["data"]["quota"]["search"]["legacy_units"] == 100
    assert "SECRET123" not in json.dumps(ctx.calls) and "SECRET123" not in json.dumps(r)
    c = scoring.competition(r)
    assert c["value"] is not None and c["orientation"] == "higher_is_worse" and c["observed"]["recent_results"] == 1
    # undeclared host -> skipped, even with a key
    assert src.youtube_competition(replay_ctx(youtube_key=lambda: "k"), "x")["status"] == "skipped"


def test_google_trends_never_scraped():
    assert src.google_trends(replay_ctx(), "x")["status"] == "disabled"
    assert src.google_trends(replay_ctx(settings={"google_trends_alpha_access": True}), "x")["status"] == "disabled"


def test_niche_database_and_custom_niches():
    db = niches.load({})
    want = {"science", "technology", "education", "history", "documentary", "business", "lifestyle", "travel", "sports",
            "entertainment", "human_stories", "productivity", "consumer_awareness", "product_reviews", "arabic_content"}
    assert want <= set(db["niches"]) and all(db["niches"][n]["seeds"] for n in want)
    assert niches.get("arabic_content", {})["language"] == "ar"
    custom = niches.load({"custom_niches": {"astro_kids": {"seeds": ["Moon"], "preset": "educational"}}})
    assert custom["niches"]["astro_kids"]["custom"] is True
    with pytest.raises(ValueError):
        niches.load({"custom_niches": {"bad": {"monetisation": {"tier": "guaranteed"}}}})
    assert {"topic": "Moon", "niche": "astro_kids", "lang": "en"} in niches.watchlist(["astro_kids"], {"custom_niches": {"astro_kids": {"seeds": ["Moon"]}}})


def test_score_excludes_unknowns_and_lowers_confidence():
    none = {k: scoring.factor(None, [], 0, "missing") for k in scoring.WEIGHTS}
    assert scoring.score(none) == (0.0, 0.0)
    some = dict(none, demand=scoring.factor(0.8, [], 0.75, "x"))
    s, conf = scoring.score(some)
    assert s == 80.0 and conf == pytest.approx(0.2 * 0.75)
