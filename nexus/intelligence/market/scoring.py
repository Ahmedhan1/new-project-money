"""ContentOpportunity scoring. Every factor = {value 0-1 or None, orientation, evidence, confidence, method}.

* Missing data -> value None and confidence 0. It is never imputed; the overall confidence drops instead.
* `orientation`: "higher_is_better" or "higher_is_worse" (competition, production_cost). The score uses 1 - value for
  the latter.
* No view / revenue predictions anywhere: demand is an observed Wikipedia readership level, not a forecast.
"""
from __future__ import annotations

import math

from .. import contracts

WEIGHTS = {"demand": 0.2, "freshness": 0.1, "competition": 0.12, "differentiation": 0.08, "evidence": 0.14,
           "production_cost": 0.1, "monetisation": 0.08, "platform_fit": 0.06, "localisation": 0.07, "creative_potential": 0.05}
WORSE = {"competition", "production_cost"}
TIER = {"high": 0.85, "medium": 0.6, "low": 0.35, "restricted": 0.1}


def factor(value, evidence: list, confidence: float, method: str, orientation: str = "higher_is_better", **extra) -> dict:
    if value is None:
        confidence = 0.0
    return {"value": None if value is None else round(max(0.0, min(1.0, value)), 3), "orientation": orientation,
            "evidence": evidence, "confidence": round(confidence, 2), "method": method, **extra}


def _ev(r: dict, kind: str) -> list:
    return [contracts.evidence(it["source"], it.get("url"), kind=kind) for it in r.get("items", [])[:3]] if r else []


def _level(mean: float | None) -> float | None:
    """Daily views -> 0-1 on a log scale: 10/day ~0.2, 1k/day ~0.6, 100k/day = 1."""
    return None if mean is None else min(1.0, math.log10(mean + 1) / 5)


def demand(pv: dict, stats: dict) -> dict:
    if not pv or pv.get("status") != "ok" or stats.get("recent_mean") is None:
        return factor(None, [], 0, "Wikipedia pageviews unavailable: " + (pv or {}).get("reason", "no data"))
    lvl = _level(stats["base_mean"] if stats["base_mean"] is not None else stats["recent_mean"])
    trend = None if stats["ratio"] is None else max(0.0, min(1.0, (stats["ratio"] - 0.5) / 1.5))
    v = lvl if trend is None else 0.7 * lvl + 0.3 * trend
    conf = 0.75 * stats["coverage"] * (1 if trend is not None else 0.7)
    return factor(v, _ev(pv, "pageviews"), conf, "0.7*log-level of mean daily pageviews (prev 28 d) + 0.3*trend (last 7 d / prev 28 d); "
                  "readers of one Wikipedia edition are a proxy for interest, not for video views", observed=stats,
                  via=pv["data"].get("via"))


def freshness(gdelt: dict, rss: dict, today_iso: str) -> dict:
    if gdelt and gdelt.get("status") == "ok" and gdelt["data"].get("series"):
        s = list(gdelt["data"]["series"].values())
        recent, base = sum(s[-7:]), sum(s[-35:-7])
        ratio = (recent / 7) / (base / 28) if base else None
        v = 0.0 if not recent else min(1.0, 0.4 * math.log10(recent + 1) / 3 + (0.6 * min(1.0, (ratio or 1) / 3)))
        return factor(v, _ev(gdelt, "news-volume"), 0.6, "GDELT raw article counts, last 7 d vs prev 28 d",
                      observed={"articles_7d": recent, "articles_prev_28d": base, "ratio": ratio})
    if rss and rss.get("status") == "ok":
        n = len(rss["items"])
        return factor(min(1.0, n / 5), _ev(rss, "headline"), 0.3, f"{n} matching headlines in configured RSS feeds (small sample)",
                      observed={"matching_headlines": n})
    reasons = "; ".join(f"{r['adapter']}: {r['reason']}" for r in (gdelt, rss) if r)
    return factor(None, [], 0, "no news source available - " + reasons)


def competition(yt: dict) -> dict:
    if not yt or yt.get("status") != "ok":
        return factor(None, [], 0, "unknown - YouTube Data API not available (" + (yt or {}).get("reason", "not run") + "); never guessed",
                      "higher_is_worse")
    items = yt["items"]
    views = sorted(i["views"] for i in items if i.get("views") is not None)
    med = views[len(views) // 2] if views else None
    recent = sum(1 for i in items if i.get("date") and i["date"] >= yt["data"].get("recent_cutoff", "")) if yt["data"].get("recent_cutoff") else None
    v = 0.6 * (_level(med) or 0) + 0.4 * (min(1.0, len(items) / 25))
    return factor(v, _ev(yt, "competitor"), 0.6, "median views of top results (log) + result count; public metadata only",
                  "higher_is_worse", observed={"results": len(items), "median_views_top": med, "recent_results": recent,
                                               "quota": yt["data"].get("quota")})


def differentiation(brief: dict, yt: dict, loc: dict) -> dict:
    ev, parts = [], []
    if yt and yt.get("status") == "ok":
        titles = [i["title"].lower() for i in yt["items"]]
        topic = brief["topic"].lower()
        same = sum(topic in t for t in titles) / max(len(titles), 1)
        parts.append(1 - same)
        ev += _ev(yt, "competitor")
    gaps = [lang for lang, v in loc.items() if v.get("recent_mean") and v["recent_mean"] >= 50]
    parts.append(min(1.0, 0.3 + 0.15 * len(brief.get("conflicts") or []) + 0.1 * len(gaps)))
    conf = 0.5 if len(parts) == 2 else 0.25
    return factor(sum(parts) / len(parts), ev, conf, "heuristic: share of competitor titles not repeating the topic phrase (if YouTube "
                  "available) + source disagreements + languages with readership", gaps=gaps)


def evidence_factor(brief: dict) -> dict:
    claims = brief["claims"]
    if not brief["sources"]:
        return factor(None, [], 0, "no text sources")
    ver = sum(c["status"] == "verified" for c in claims)
    indep = sum(c["status"] == "verified" and c.get("independence") == "independent publishers" for c in claims)
    v = 0.4 * min(1.0, len(brief["sources"]) / 4) + 0.4 * (ver / max(len(claims), 1)) + 0.2 * min(1.0, indep / 3)
    return factor(v, [contracts.evidence(s["source"], s.get("url"), kind="source") for s in brief["sources"][:5]], 0.8,
                  "sources count + share of claims verified in >= 2 sources + claims from independent publishers",
                  observed={"sources": len(brief["sources"]), "claims": len(claims), "verified": ver, "independent": indep})


def production_cost(ov: dict, nasa: dict, brief: dict) -> dict:
    counts = {}
    if ov and ov.get("status") == "ok":
        counts["openverse"] = ov["data"].get("result_count")
    if nasa and nasa.get("status") == "ok":
        counts["nasa"] = nasa["data"].get("total_hits")
    if not counts:
        return factor(None, [], 0, "no visual-material source available", "higher_is_worse")
    total = sum(c or 0 for c in counts.values())
    visual_ease = min(1.0, math.log10(total + 1) / 3)  # ~1000 licensable items = easy
    depth = min(1.0, sum(len(s.get("title") or "") > 0 for s in brief["sources"]) / 4)
    cost = 1 - (0.7 * visual_ease + 0.3 * depth)
    return factor(cost, _ev(ov, "visual") + _ev(nasa, "visual"), 0.6, "1 - (0.7*log(count of commercially usable CC/NASA visuals) + "
                  "0.3*research depth); counts are search hits, not checked assets", "higher_is_worse", observed=counts)


def monetisation(niche: dict, topic: str, brief: dict) -> dict:
    text = (topic + " " + " ".join(c["text"] for c in brief["claims"][:8])).lower()
    hits = [w for w in niche.get("sensitive", []) if w in text]
    v = TIER[niche["monetisation"]["tier"]] - 0.15 * len(hits)
    return factor(v, [{"source": "niches.yaml rules", "kind": "rule"}], 0.35, "editorial rules (niche tier minus sensitive-keyword "
                  "penalty); NOT platform-verified, no revenue estimate", sensitive_hits=hits, notes=niche["monetisation"].get("notes"))


def platform_fit(niche: dict, brief: dict) -> dict:
    pf = niche["platform_fit"]
    best = max(pf, key=pf.get)
    ver = sum(c["status"] == "verified" for c in brief["claims"])
    v = pf.get("youtube_long", 0.5) * (1 if ver >= 4 else 0.85)
    return factor(v, [{"source": "niches.yaml rules", "kind": "rule"}], 0.35, "niche prior; long-form needs >= 4 verified claims",
                  per_platform=pf, best=best)


def localisation(loc: dict, targets: list[str], skipped: dict) -> dict:
    measured = {k: v for k, v in loc.items() if v.get("recent_mean") is not None}
    if not measured:
        return factor(None, [], 0, "no other-language pageviews measured: " + "; ".join(f"{k}: {v}" for k, v in skipped.items()),
                      skipped=skipped)
    total = sum(v["recent_mean"] for v in measured.values())
    v = min(1.0, math.log10(total + 1) / 4)
    ev = [contracts.evidence(f"pageviews:{k}", v.get("url"), kind="pageviews") for k, v in measured.items()]
    return factor(v, ev, round(0.7 * len(measured) / max(len(targets), 1) + 0.1, 2),
                  "log of summed daily readership of the same article in ar/de/fr/hi/zh/ru Wikipedias (only declared hosts measured)",
                  observed={k: {"recent_mean": round(v["recent_mean"], 1), "title": v["title"]} for k, v in measured.items()},
                  skipped=skipped)


def creative_potential(brief: dict) -> dict:
    ver = [c for c in brief["claims"] if c["status"] == "verified"]
    nums = sum(1 for c in ver if any(ch.isdigit() for ch in c["text"]))
    v = min(1.0, 0.2 + 0.1 * len(brief.get("angles") or []) + 0.05 * nums + 0.1 * len(brief.get("conflicts") or []))
    return factor(v, [], 0.3, "heuristic: angles + verified numeric facts + source disagreements (a human judges creativity)")


def score(factors: dict) -> tuple[float, float]:
    num = den = wsum = 0.0
    for k, w in WEIGHTS.items():
        f = factors[k]
        wsum += w
        if f["value"] is None:
            continue
        v = 1 - f["value"] if k in WORSE else f["value"]
        num += w * f["confidence"] * v
        den += w * f["confidence"]
    s = 100 * num / den if den else 0.0
    conf = den / wsum
    return round(s, 1), round(conf, 2)


def build_opportunity(topic: str, niche: dict, language: str, brief: dict, sig: dict) -> dict:
    """`sig`: adapter results + derived stats from pipeline.gather()."""
    f = {
        "demand": demand(sig.get("pageviews"), sig.get("demand_stats") or {}),
        "freshness": freshness(sig.get("gdelt"), sig.get("rss"), sig.get("today", "")),
        "competition": competition(sig.get("youtube")),
        "differentiation": differentiation(brief, sig.get("youtube"), sig.get("localisation") or {}),
        "evidence": evidence_factor(brief),
        "production_cost": production_cost(sig.get("openverse"), sig.get("nasa"), brief),
        "monetisation": monetisation(niche, topic, brief),
        "platform_fit": platform_fit(niche, brief),
        "localisation": localisation(sig.get("localisation") or {}, sig.get("loc_targets") or [], sig.get("loc_skipped") or {}),
        "creative_potential": creative_potential(brief),
    }
    s, conf = score(f)
    ev = [e for x in f.values() for e in x["evidence"]][:20]
    doc = contracts.make("ContentOpportunity", id=contracts.new_id("opp"), topic=topic, niche=niche["id"], language=language,
                         score=s, factors=f, evidence=ev, confidence=conf, brief_id=brief["id"], status="proposed")
    doc["score_note"] = ("0-100 confidence-weighted mean of known factors; factors with no data are excluded and lower 'confidence'. "
                         "Not a prediction of views or revenue.")
    doc["unknown_factors"] = [k for k, x in f.items() if x["value"] is None]
    return doc
