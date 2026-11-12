"""ResearchBrief: claims with citations, cross-checked across >= 2 sources, conflicts, open questions, angles.

AI (model router, tasks "data_extraction" + "deep_research", privacy "public") proposes claims and angles; every AI
claim is then re-checked deterministically against the cited source texts - a claim whose numbers are not in its
sources is rejected. Without an AI provider the deterministic extractor runs alone (and the brief says so).
"""
from __future__ import annotations

import re
import urllib.parse

from .. import contracts

STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they have has had "
           "not can will its their our your more most very also into than then about over when which who how what why there these "
           "those such other only after before during between under while where some many much".split())
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
SENT = re.compile(r"(?<=[.!?؟。])\s+")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

CLAIM_SCHEMA = {"type": "object", "required": ["claims"], "properties": {"claims": {"type": "array", "maxItems": 12, "items": {
    "type": "object", "required": ["text", "source_ids"], "properties": {
        "text": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}}}}}}}
ANGLE_SCHEMA = {"type": "object", "required": ["angles", "open_questions"], "properties": {
    "angles": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    "open_questions": {"type": "array", "maxItems": 6, "items": {"type": "string"}}}}


def numbers(text: str) -> set[str]:
    """Significant numbers (>= 2 digits or decimals), normalised; Arabic-Indic digits mapped to ASCII."""
    out = set()
    for n in NUM.findall(text.translate(AR_DIGITS)):
        n = n.replace(",", "").rstrip(".")
        if len(n.replace(".", "")) >= 2:
            out.add(n)
    return out


def words(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[^\W\d_]{4,}", text.lower()) if w not in STOP}


def _publisher(src: dict) -> str:
    return (src.get("source") or "").split(":")[0] or (urllib.parse.urlparse(src.get("url") or "").hostname or "?")


def collect_sources(results: list[dict]) -> list[dict]:
    """Text-bearing items from adapter results -> numbered brief sources (s1, s2, ...)."""
    out = []
    for r in results:
        for it in r.get("items", []):
            if it.get("text") and len(it["text"]) > 60:
                out.append({"id": f"s{len(out) + 1}", **{k: it.get(k) for k in ("source", "url", "date", "licence", "title", "kind")},
                            "lang": it.get("lang", "en"), "text": it["text"]})
    return out


YEAR = re.compile(r"^(1[5-9]|20)\d\d$")


def _supports(claim: str, claim_lang: str, src: dict) -> bool:
    """Deterministic support test (numbers + words, not semantics):
    same language: all significant numbers present and >= 30 % word overlap (>= 60 % when the claim has no numbers);
    other language: only numbers can match, and a lone year is too common to count - needs >= 2 numbers or a non-year."""
    nums, text = numbers(claim), src["text"]
    w = words(claim)
    overlap = len(w & words(text)) / len(w) if w else 0.0
    if src.get("lang", "en") == claim_lang:
        return (nums <= numbers(text) and overlap >= 0.3) if nums else overlap >= 0.6
    if not nums or not nums <= numbers(text):
        return False
    return len(nums) >= 2 or not YEAR.match(next(iter(nums)))


def cross_check(claim: dict, sources: list[dict]) -> dict:
    """Which sources support the claim; 'verified' needs >= 2 supporting sources (publisher independence noted)."""
    lang = claim.get("lang", "en")
    sup = [s["id"] for s in sources if s["id"] in claim["source_ids"] or _supports(claim["text"], lang, s)]
    sup = [i for i in sup if _supports(claim["text"], lang, next(s for s in sources if s["id"] == i))]
    pubs = {_publisher(next(s for s in sources if s["id"] == i)) for i in sup}
    status = "verified" if len(sup) >= 2 else "single_source" if sup else "unsupported"
    return {**claim, "supported_by": sup, "status": status,
            "independence": "independent publishers" if len(pubs) >= 2 else ("same publisher (e.g. two Wikipedia editions)" if len(sup) >= 2 else "n/a")}


def extract_claims(sources: list[dict], per_source: int = 6) -> list[dict]:
    """Deterministic: first two sentences + sentences with numbers, from fact sources."""
    claims = []
    for s in sources:
        if s.get("kind") not in ("facts", "visual"):
            continue
        sents = [x.strip() for x in SENT.split(s["text"]) if 40 <= len(x.strip()) <= 320]
        pick = sents[:2] + [x for x in sents[2:] if numbers(x)]
        for x in pick[:per_source if s.get("kind") == "facts" else 2]:
            claims.append({"text": x, "source_ids": [s["id"]], "lang": s.get("lang", "en"), "by": "deterministic"})
    return claims


UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|[^\W\d_]+)")


def find_conflicts(claims: list[dict], sources: list[dict]) -> list[dict]:
    """Same-language claims from different sources that talk about the same thing (word overlap) and give a different
    number for the same unit word. Automatic and approximate - every conflict needs a human check."""
    src = {s["id"]: s for s in sources}
    out = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if a.get("lang") != b.get("lang") or set(a["source_ids"]) & set(b["source_ids"]):
                continue
            wa, wb = words(a["text"]), words(b["text"])
            if not wa or len(wa & wb) / len(wa | wb) < 0.3:
                continue
            ua = {u.lower(): n.replace(",", "") for n, u in UNIT.findall(a["text"].translate(AR_DIGITS))}
            ub = {u.lower(): n.replace(",", "") for n, u in UNIT.findall(b["text"].translate(AR_DIGITS))}
            for unit in set(ua) & set(ub):
                if ua[unit] != ub[unit] and unit not in STOP:
                    out.append({"about": unit, "values": [{"value": ua[unit], "claim": a["text"], "source": src[a["source_ids"][0]]["url"]},
                                                          {"value": ub[unit], "claim": b["text"], "source": src[b["source_ids"][0]]["url"]}],
                                "status": "needs human check"})
    return out


def default_angles(topic: str, signals: dict) -> list[str]:
    """Deterministic angle suggestions from the measured signals (no invented facts)."""
    out = []
    loc = signals.get("localisation") or {}
    for lang, v in sorted(loc.items(), key=lambda kv: -(kv[1].get("recent_mean") or 0)):
        if v.get("recent_mean"):
            out.append(f"{lang}-language version: the {lang} Wikipedia article '{v['title']}' averages "
                       f"{v['recent_mean']:.0f} views/day (last 7 days) - original narration in {lang}, not a translation of a competitor")
    if signals.get("missing_langs"):
        out.append(f"Gap: no Wikipedia article in {', '.join(signals['missing_langs'])} - an explainer in those languages has less reference material to compete with (and less to research from)")
    d = signals.get("demand") or {}
    if d.get("ratio") and d["ratio"] >= 1.3:
        out.append(f"Why now: reader interest in the last 7 days is {d['ratio']:.1f}x the previous 28-day average")
    if signals.get("conflicts"):
        out.append("What the sources disagree on: present the competing figures and how each was measured")
    if signals.get("numbers"):
        out.append(f"By the numbers: build the story around the verified figures ({', '.join(sorted(signals['numbers'])[:4])})")
    out.append(f"Misconceptions about {topic}: pick claims that are verified in >= 2 sources and contrast them with common assumptions (human picks the assumptions)")
    return out


def _router_ai(task: str, prompt: str, schema: dict):
    from ... import jobs, llm
    from ..model_router import complete
    with jobs.heavy_lock():
        try:
            return complete(task, prompt, schema=schema, privacy="public",
                            max_tokens=700 if task == "data_extraction" else 450, temperature=0.2).data
        finally:
            llm.unload()


def build_brief(topic: str, results: list[dict], language: str = "en", signals: dict | None = None, ai=_router_ai) -> dict:
    """`results` are adapter SourceResults. `ai(task, prompt, schema) -> dict` or None for deterministic only."""
    sources = collect_sources(results)
    notes, claims, rejected = [], extract_claims(sources), []
    if ai is not None and sources:
        listing = "\n\n".join(f"[{s['id']}] ({s['source']}, {s['lang']}) {s['text'][:1200]}" for s in sources[:6])
        try:
            data = ai("data_extraction", f"Topic: {topic}\nExtract up to 10 factual claims about the topic that are stated in the "
                      f"sources below. Copy numbers exactly. Cite the source ids that state each claim.\n\n{listing}\n\n"
                      'Return JSON: {"claims": [{"text": "...", "source_ids": ["s1"]}]}', CLAIM_SCHEMA) or {}
            for c in data.get("claims", []):
                c = {"text": str(c["text"])[:320], "source_ids": [i for i in c.get("source_ids", []) if any(s["id"] == i for s in sources)],
                     "lang": language, "by": "ai"}
                if c["source_ids"] and all(_supports(c["text"], next(s for s in sources if s["id"] == c["source_ids"][0]).get("lang", language),
                                                     next(s for s in sources if s["id"] == i)) for i in c["source_ids"]):
                    claims.insert(0, c)
                else:
                    rejected.append({**c, "reason": "cited sources do not contain the claim's numbers/words"})
            notes.append("claims: AI (data_extraction) + deterministic, all re-checked against source text")
        except Exception as e:  # NoProviderAvailable or provider failure -> deterministic fallback, said explicitly
            notes.append(f"claims: deterministic fallback ({type(e).__name__}: {str(e)[:160]})")
    else:
        notes.append("claims: deterministic only" + ("" if sources else " (no text sources)"))
    seen, checked = set(), []
    for c in claims:
        k = re.sub(r"\W+", " ", c["text"].lower()).strip()
        if k not in seen:
            seen.add(k)
            checked.append(cross_check(c, sources))
    conflicts = find_conflicts(checked, sources)
    signals = dict(signals or {})
    signals.setdefault("conflicts", conflicts)
    signals.setdefault("numbers", set().union(*[numbers(c["text"]) for c in checked if c["status"] == "verified"]) if checked else set())
    angles, questions = default_angles(topic, signals), []
    questions += [f"Verify with a second source: {c['text']}" for c in checked if c["status"] == "single_source" and numbers(c["text"])][:5]
    questions += [f"Sources disagree on '{x['about']}': " + " vs ".join(v["value"] for v in x["values"]) for x in conflicts]
    if ai is not None and sources:
        try:
            data = ai("deep_research", f"Topic: {topic}\nVerified claims:\n" + "\n".join(f"- {c['text']}" for c in checked if c["status"] == "verified")[:3000]
                      + "\nSuggest original video angles (not copies of existing videos) and open research questions that the sources do "
                      'not answer. Do not state new facts. Return JSON: {"angles": ["..."], "open_questions": ["..."]}', ANGLE_SCHEMA) or {}
            angles += [f"(AI suggestion) {a}" for a in data.get("angles", [])[:4]]
            questions += [f"(AI suggestion) {q}" for q in data.get("open_questions", [])[:4]]
            notes.append("angles: deterministic + AI (deep_research)")
        except Exception as e:
            notes.append(f"angles: deterministic only ({type(e).__name__})")
    ver = sum(c["status"] == "verified" for c in checked)
    conf = round(min(1.0, 0.2 + 0.6 * (ver / max(len(checked), 1)) + 0.05 * min(len(sources), 4)), 2) if checked else 0.0
    doc = contracts.make("ResearchBrief", id=contracts.new_id("brief"), topic=topic, language=language,
                         sources=[{k: v for k, v in s.items() if k != "text"} for s in sources], claims=checked,
                         conflicts=conflicts, open_questions=questions, angles=angles, confidence=conf)
    doc["method"] = notes
    doc["rejected_claims"] = rejected
    return doc
