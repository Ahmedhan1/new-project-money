"""End-to-end: gather sources -> ResearchBrief -> ContentOpportunity; trend monitor; hand-off to Account 2's create."""
from __future__ import annotations

from . import brief as brief_mod, niches, scoring, sources as src

PRESET_FALLBACK = "educational"


def gather(ctx: src.Ctx, topic: str, lang: str = "en", loc_targets=src.LOCALISATION_LANGS) -> dict:
    """Run every adapter once for a topic. Each result says ok / skipped (host not declared) / disabled / error."""
    sig: dict = {"today": ctx.today.isoformat(), "loc_targets": [t for t in loc_targets if t != lang]}
    sig["wiki"] = wiki = src.wiki_search(ctx, topic, lang)
    title = (wiki["data"].get("titles") or [None])[0]
    sig["main_title"] = title
    sig["extra_facts"] = []
    if title:
        sig["pageviews"] = pv = src.wiki_pageviews(ctx, title, lang)
        sig["demand_stats"] = src.window_stats(pv["data"].get("series", {}), ctx.today) if pv["status"] == "ok" else {}
        sig["langlinks"] = ll = src.wiki_langlinks(ctx, title, lang, targets=tuple(sig["loc_targets"]) + (("en",) if lang != "en" else ()))
        links = ll["data"].get("links", {}) if ll["status"] == "ok" else {}
        loc, skipped = {}, {}
        for tl in sig["loc_targets"]:
            if tl not in links:
                skipped[tl] = "no article in this language" if ll["status"] == "ok" else f"langlinks {ll['status']}"
                continue
            r = src.wiki_pageviews(ctx, links[tl], tl)
            if r["status"] != "ok":
                skipped[tl] = f"{r['status']}: {r['reason']}"
                continue
            st = src.window_stats(r["data"]["series"], ctx.today)
            loc[tl] = {**st, "title": links[tl], "url": src.wiki_url(tl, links[tl])}
        sig["localisation"], sig["loc_skipped"] = loc, skipped
        sig["missing_langs"] = [t for t in sig["loc_targets"] if t not in links] if ll["status"] == "ok" else []
        # a second-language edition of the same article = a second (non-independent) source for cross-checking numbers
        second = "en" if lang != "en" else next((t for t in ("ar", "de") if t in links), None)
        if second and links.get(second):
            sig["extra_facts"].append(src.wiki_extract(ctx, links[second], second))
    query = topic
    sig["gdelt"] = src.gdelt_timeline(ctx, query)
    sig["rss"] = src.rss_feeds(ctx, query)
    sig["youtube"] = src.youtube_competition(ctx, query, lang)
    sig["google_trends"] = src.google_trends(ctx, query)
    vq = title if lang == "en" and title else (sig.get("langlinks", {}).get("data", {}).get("links", {}).get("en") or topic)
    sig["openverse"] = src.openverse_count(ctx, vq)
    sig["nasa"] = src.nasa_count(ctx, vq)
    return sig


def analyse(ctx: src.Ctx, topic: str, niche_id: str, lang: str = "en", ai=brief_mod._router_ai, settings: dict | None = None) -> dict:
    niche = niches.get(niche_id, settings if settings is not None else ctx.settings)
    sig = gather(ctx, topic, lang)
    text_results = [sig["wiki"], *sig["extra_facts"], sig["nasa"], sig["rss"]]
    signals = {"localisation": sig.get("localisation"), "missing_langs": sig.get("missing_langs"), "demand": sig.get("demand_stats")}
    br = brief_mod.build_brief(topic, text_results, lang, signals, ai=ai)
    opp = scoring.build_opportunity(topic, niche, lang, br, sig)
    adapters = {k: {"status": v["status"], "reason": v["reason"], "items": v["items"], "data": v["data"]}
                for k, v in sig.items() if isinstance(v, dict) and "adapter" in v}
    adapters["extra_facts"] = [{"status": r["status"], "reason": r["reason"], "items": r["items"]} for r in sig["extra_facts"]]
    return {"topic": topic, "niche": niche_id, "language": lang, "date": ctx.today.isoformat(), "adapters": adapters,
            "opportunity": opp, "brief": br, "production_brief": to_production_brief(opp, br, niche),
            "http_calls": ctx.calls}


def trend_monitor(ctx: src.Ctx, watchlist: list[dict], ratio_min: float = 1.5, min_daily: float = 20) -> dict:
    """Pageviews last 7 days vs previous 28 days per watchlist topic. 'emerging' = ratio >= ratio_min and recent mean >=
    min_daily. Topics without data are listed as unknown, never ranked."""
    rows = []
    for w in watchlist:
        lang = w.get("lang", "en")
        title = w.get("title")
        if not title:
            s = src.wiki_search(ctx, w["topic"], lang, max_pages=1)
            title = (s["data"].get("titles") or [None])[0]
            if not title:
                rows.append({**w, "status": "unknown", "reason": s["reason"] or "no Wikipedia article"})
                continue
        pv = src.wiki_pageviews(ctx, title, lang)
        if pv["status"] != "ok":
            rows.append({**w, "title": title, "status": "unknown", "reason": pv["reason"]})
            continue
        st = src.window_stats(pv["data"]["series"], ctx.today)
        status = "unknown" if st["ratio"] is None else (
            "emerging" if st["ratio"] >= ratio_min and (st["recent_mean"] or 0) >= min_daily else
            "rising" if st["ratio"] >= 1.15 else "falling" if st["ratio"] <= 0.85 else "stable")
        rows.append({**w, "title": title, "status": status, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in st.items()},
                     "url": pv["items"][0]["url"], "via": pv["data"]["via"]})
    emerging = sorted([r for r in rows if r["status"] == "emerging"], key=lambda r: -r["ratio"])
    return {"date": ctx.today.isoformat(), "rule": f"emerging = last-7-day mean >= {ratio_min}x previous-28-day mean and >= {min_daily}/day",
            "topics": rows, "emerging": [r["topic"] for r in emerging]}


def to_production_brief(opportunity: dict, brief: dict, niche: dict | None = None, scenes: int | None = None) -> dict:
    """A request body for Account 2's create pipeline (`POST /api/create` = CreateReq{topic, scenes, lang, notes, vertical,
    preset}; `nexus.create.create_video(topic, notes=, lang=, preset=, scenes=)`). Unknown keys are ignored by both.

    notes = only claims verified in >= 2 sources, each with [n] citations, then single-source claims marked as such,
    then disagreements marked DO NOT STATE; ends with the numbered source list. create.research() puts notes first
    in the script-writer's corpus, so the script is grounded in checked facts."""
    src_ids = {}
    for s in brief["sources"]:
        src_ids[s["id"]] = len(src_ids) + 1
    def cite(c):
        return "".join(f"[{src_ids[i]}]" for i in c["supported_by"] if i in src_ids)
    ver = [c for c in brief["claims"] if c["status"] == "verified"]
    single = [c for c in brief["claims"] if c["status"] == "single_source"]
    lines = [f"Verified research for: {opportunity['topic']} (each fact confirmed by >= 2 sources)"]
    lines += [f"- {c['text']} {cite(c)}" for c in ver]
    if single:
        lines += ["", "Single-source facts (use only with attribution, e.g. 'according to ...'):"]
        lines += [f"- {c['text']} {cite(c)}" for c in single[:6]]
    if brief.get("conflicts"):
        lines += ["", "Sources disagree - DO NOT STATE these figures as fact:"]
        lines += [f"- {x['about']}: " + " vs ".join(v["value"] for v in x["values"]) for x in brief["conflicts"]]
    lines += ["", "Sources:"] + [f"[{n}] {s['title']} - {s.get('url') or s['source']} ({s.get('licence')})"
                                for s, n in ((s, src_ids[s["id"]]) for s in brief["sources"])]
    pf = (niche or {}).get("platform_fit") or (opportunity["factors"].get("platform_fit") or {}).get("per_platform") or {}
    vertical = bool(pf) and max(pf, key=pf.get) in ("youtube_shorts", "tiktok")
    n_scenes = scenes or max(3, min(8, len(ver) + 1))
    return {"topic": opportunity["topic"], "scenes": n_scenes, "lang": opportunity["language"], "notes": "\n".join(lines),
            "vertical": vertical, "preset": (niche or {}).get("preset") or PRESET_FALLBACK,
            "market": {"opportunity_id": opportunity["id"], "brief_id": brief["id"], "score": opportunity["score"],
                       "confidence": opportunity["confidence"], "verified_claims": len(ver), "angles": brief.get("angles", [])[:3]}}
