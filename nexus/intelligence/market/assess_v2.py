"""Market research V2: an evidence-quality layer over a V1 report (opportunity + research brief).

Adds what a reviewer needs before trusting a score:
  credibility        publisher tier of every source (primary/official, reference, established media, unknown) -> mean
  cross_source       share of claims confirmed by >= 2 independent publishers; conflicts listed
  recency            age of the newest and median source; stale evidence lowers confidence for news-driven niches
  geography          countries the topic and its claims are about (ISO codes, from the place resolver)
  language_demand    readers per language edition (from the localisation factor), ranked, with share
  competition        YouTube supply when measured; otherwise unknown (never guessed)
  saturation         supply relative to demand when both are measured; otherwise unknown
  evidence_coverage  share of score weight backed by measured evidence
  uncertainty        the score's range if every unknown factor were worst / best (an honest interval, not a forecast)
Output is a `nexus.MarketAssessment/2` document with a verdict that says which decisions it can support."""
from __future__ import annotations

import datetime as dt
import statistics
from urllib.parse import urlparse

from . import scoring

PRIMARY = ("nasa.gov", "esa.int", "who.int", "un.org", "unesco.org", "europa.eu", "worldbank.org", "imf.org", "noaa.gov",
           "nih.gov", "cdc.gov", "gov.uk", "gov.eg", "bund.de", "census.gov", "data.gov")
REFERENCE = ("wikipedia.org", "wikidata.org", "britannica.com", "nature.com", "science.org", "arxiv.org", "doi.org", "jstor.org")
MEDIA = ("bbc.co.uk", "bbc.com", "reuters.com", "apnews.com", "aljazeera.net", "aljazeera.com", "dw.com", "lemonde.fr",
         "nytimes.com", "theguardian.com", "spiegel.de", "ahram.org.eg", "france24.com")
TIER = {"primary": 1.0, "reference": 0.8, "established_media": 0.7, "academic": 0.9, "unknown": 0.4}
NEWS_NICHES = {"news", "tech", "finance", "sports"}


def tier(url: str | None, source: str = "") -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if source.startswith("nasa") or any(host == d or host.endswith("." + d) for d in PRIMARY) or host.endswith(".gov") or ".gov." in host:
        return "primary"
    if host.endswith(".edu") or host.endswith(".ac.uk") or ".edu." in host:
        return "academic"
    if any(host == d or host.endswith("." + d) for d in REFERENCE) or source.startswith("wikipedia"):
        return "reference"
    if any(host == d or host.endswith("." + d) for d in MEDIA):
        return "established_media"
    return "unknown"


def _publisher(src: dict) -> str:
    host = (urlparse(src.get("url") or "").hostname or src.get("source") or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def credibility(sources: list[dict]) -> dict:
    rows = [{"id": s.get("id"), "url": s.get("url"), "tier": tier(s.get("url"), s.get("source", ""))} for s in sources]
    if not rows:
        return {"value": None, "sources": [], "method": "no sources"}
    by = {}
    for r in rows:
        by[r["tier"]] = by.get(r["tier"], 0) + 1
    return {"value": round(statistics.fmean(TIER[r["tier"]] for r in rows), 3), "by_tier": by, "sources": rows,
            "method": "mean publisher tier (primary 1.0, academic 0.9, reference 0.8, established media 0.7, unknown 0.4)"}


def cross_source(brief: dict) -> dict:
    srcs = {s["id"]: s for s in brief.get("sources", [])}
    claims = brief.get("claims", [])
    if not claims:
        return {"value": None, "method": "no claims"}
    multi = []
    for c in claims:
        pubs = {_publisher(srcs[i]) for i in (c.get("supported_by") or c.get("source_ids") or []) if i in srcs}
        if len(pubs) >= 2:
            multi.append(c["text"][:80])
    return {"value": round(len(multi) / len(claims), 3), "confirmed": len(multi), "claims": len(claims),
            "single_source": len(claims) - len(multi), "conflicts": brief.get("conflicts", []),
            "method": "claims supported by >= 2 different publishers (same-site pages count once)"}


RETRIEVAL_ONLY = ("wikipedia", "pageviews", "wikidata")  # their `date` is the retrieval date, not a publication date


def recency(sources: list[dict], today: str) -> dict:
    t = dt.date.fromisoformat(today)
    ages = []
    for s in sources:
        if str(s.get("source", "")).startswith(RETRIEVAL_ONLY) and not s.get("published"):
            continue
        try:
            ages.append((t - dt.date.fromisoformat(str(s.get("published") or s.get("date"))[:10])).days)
        except ValueError:
            continue
    if not ages:
        return {"value": None, "dated_publications": 0, "method": "no source with a publication date (retrieval dates are not used)"}
    return {"newest_days": min(ages), "median_days": statistics.median(ages), "dated_publications": len(ages),
            "value": round(1 / (1 + statistics.median(ages) / 365), 3),
            "method": "1/(1+median age in years) over sources with a publication date (Wikipedia retrieval dates excluded)"}


def geography(report: dict) -> dict:
    from ..quality import visual_context
    text = " ".join([report.get("topic", "")] + [c["text"] for c in (report.get("brief") or {}).get("claims", [])])
    pl = visual_context.places(text)
    cc = {}
    for codes in pl.values():
        for c in codes:
            cc[c] = cc.get(c, 0) + 1
    return {"countries": dict(sorted(cc.items(), key=lambda x: -x[1])), "places": pl,
            "method": "place names in topic + claims resolved to ISO 3166 (pycountry + exonyms)"}


def language_demand(opp: dict) -> dict:
    loc = (opp.get("factors") or {}).get("localisation") or {}
    obs = loc.get("observed") or {}
    rows = {lang: (v or {}).get("recent_mean") for lang, v in obs.items() if isinstance(v, dict) and (v or {}).get("recent_mean") is not None}
    dem = ((opp.get("factors") or {}).get("demand") or {}).get("observed") or {}
    if dem.get("recent_mean") is not None:
        rows[opp.get("language", "en")] = dem["recent_mean"]
    tot = sum(rows.values())
    ranked = sorted(rows.items(), key=lambda x: -x[1])
    return {"by_language": [{"lang": k, "daily_readers": round(v, 1), "share": round(v / tot, 3) if tot else None} for k, v in ranked],
            "method": "mean daily Wikipedia readers of the same article per language edition - a proxy for interest, not video views",
            "not_measured": [x for x in ("ar", "de", "fr", "hi", "zh", "ru", "es") if x not in rows]}


def uncertainty(opp: dict) -> dict:
    f = opp["factors"]
    lo = {k: dict(v) for k, v in f.items()}
    hi = {k: dict(v) for k, v in f.items()}
    unknown = [k for k, v in f.items() if v.get("value") is None]
    for k in unknown:
        worst, best = (1.0, 0.0) if k in scoring.WORSE else (0.0, 1.0)
        lo[k].update(value=worst, confidence=0.5)
        hi[k].update(value=best, confidence=0.5)
    s_lo, _ = scoring.score(lo)
    s_hi, _ = scoring.score(hi)
    s, _ = scoring.score(f)
    return {"score": s, "low": min(s_lo, s), "high": max(s_hi, s), "width": round(abs(s_hi - s_lo), 1), "unknown_factors": unknown,
            "method": "score recomputed with each unknown factor at its worst and best value (confidence 0.5)"}


def evidence_coverage(opp: dict) -> dict:
    f = opp["factors"]
    w = scoring.WEIGHTS
    measured = sum(w[k] for k, v in f.items() if v.get("value") is not None and v.get("evidence") and v.get("confidence", 0) > 0)
    return {"value": round(measured / sum(w.values()), 3), "method": "share of score weight backed by measured evidence"}


def assess(report: dict, today: str | None = None) -> dict:
    opp, brief = report["opportunity"], report.get("brief") or {}
    today = today or report.get("date") or dt.date.today().isoformat()
    comp = opp["factors"].get("competition") or {}
    dem = opp["factors"].get("demand") or {}
    sat = None
    if comp.get("value") is not None and dem.get("value") is not None:
        sat = {"value": round(comp["value"] / max(dem["value"], 0.05), 3), "method": "competition / demand (both measured)"}
    out = {"schema": "nexus.MarketAssessment/2", "topic": report.get("topic"), "language": opp.get("language"), "date": today,
           "credibility": credibility(brief.get("sources", [])), "cross_source": cross_source(brief),
           "recency": recency(brief.get("sources", []), today), "geography": geography(report),
           "language_demand": language_demand(opp),
           "competition": comp if comp.get("value") is not None else {"value": None, "method": comp.get("method", "unknown")},
           "saturation": sat or {"value": None, "method": "unknown until competition (YouTube supply) is measured"},
           "evidence_coverage": evidence_coverage(opp), "uncertainty": uncertainty(opp)}
    cs, cov, unc = out["cross_source"].get("value") or 0, out["evidence_coverage"]["value"], out["uncertainty"]["width"]
    news = report.get("niche") in NEWS_NICHES
    if cov >= 0.75 and cs >= 0.5 and unc <= 15:
        verdict = "decision-grade: the score can rank topics"
    elif cov >= 0.5:
        verdict = (f"indicative only: score range {out['uncertainty']['low']}-{out['uncertainty']['high']}; "
                   f"{out['cross_source']['single_source']} single-source claim(s) need a second source before scripting")
    else:
        verdict = "insufficient evidence: gather more sources before deciding"
    if news and out["recency"].get("value") is None:
        verdict += "; news niche without dated publications - recency unknown"
    out["verdict"] = verdict
    return out
