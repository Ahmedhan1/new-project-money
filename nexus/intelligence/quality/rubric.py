"""Scored reviewer rubric (QLT-1): hook, story arc, scene relevance, repeated visuals, audio balance.

Deterministic and explainable: every score comes from the script, the scene visual plan (`project.data["visual_plan"]`,
written by nexus/create.py) and Account 2's QualityReport checks - never from a model's opinion. A dimension whose
evidence is missing is reported as `None` with the reason and is left out of the overall score; it is never guessed.

    rubric.score(script, visual_plan=None, report=None) -> {"dimensions": {...}, "overall", "verdict", "unknown"}
    rubric.gate(result) -> {"ok", "blockers"}   # for an approval step: verdict "pass" required

The thresholds are fixed constants (below); a release must not lower them to pass.
"""
from __future__ import annotations

import re

WEIGHTS = {"hook": 0.2, "story_arc": 0.2, "scene_relevance": 0.25, "repeated_visuals": 0.15, "audio_balance": 0.2}
PASS_OVERALL = 0.7      # weighted mean of the known dimensions
FLOOR = 0.4             # any single known dimension below this -> needs_review
MIN_KNOWN = 3           # fewer known dimensions than this -> insufficient_evidence, never "pass"

_STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they have has had not "
            "can will its their our your into than then about over when which who how what why".split())
_HOOK_CUES = re.compile(r"\?|\b\d[\d,.]*\b|\b(you|your|imagine|why|how|what if|never|secret|first|only|but|until)\b", re.I)
_CLOSURE = re.compile(r"\b(to be continued|finally|in the end|today|now|that is why|so|which means|remains|will|until)\b", re.I)
AUDIO_CHECKS = {"loudness": 1.0, "clipping / true peak": 1.0, "dead air": 0.75, "narration overlap": 0.75}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]{4,}", (text or "").lower()) if w not in _STOP]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?؟。])\s+", (text or "").strip()) if s]


def _spoken(script: dict) -> list[dict]:
    return [s for s in script.get("scenes") or [] if (s.get("narration") or "").strip()]


def hook(script: dict) -> dict:
    sc = _spoken(script)
    if not sc:
        return {"score": None, "why": "no narrated scene"}
    first = sc[0]
    if first.get("on_screen_text") == "Previously" and len(sc) > 1:   # series recap is not the hook
        first = sc[1]
    s1 = (_sentences(first["narration"]) or [""])[0]
    n = len(s1.split())
    length = 1.0 if n <= 18 else max(0.0, 1 - (n - 18) / 17)          # 18 words -> 1.0, 35+ -> 0.0
    cue = 1.0 if _HOOK_CUES.search(" ".join(s1.split()[:12])) else 0.4   # the cue has to come early to work as a hook
    text = 1.0 if (first.get("on_screen_text") or "").strip() else 0.6
    sc_ = round(0.5 * length + 0.3 * cue + 0.2 * text, 3)
    return {"score": sc_, "why": f"first sentence {n} words, {'has' if cue == 1 else 'no'} hook cue (question/number/address), "
                                 f"{'with' if text == 1 else 'without'} on-screen text"}


def story_arc(script: dict) -> dict:
    sc = _spoken(script)
    if len(sc) < 2:
        return {"score": None, "why": "fewer than 2 narrated scenes"}
    depth = sum(len(s["narration"].split()) >= 12 for s in sc) / len(sc)
    links = []
    for a, b in zip(sc, sc[1:]):
        wa, wb = set(_words(a["narration"])), set(_words(b["narration"]))
        links.append(1.0 if wa & wb else 0.0)
    continuity = sum(links) / len(links)
    closure = 1.0 if _CLOSURE.search(sc[-1]["narration"]) or sc[-1]["narration"].rstrip().endswith(("?", "!")) else 0.5
    enough = min(1.0, len(sc) / 4)
    sc_ = round(0.35 * depth + 0.3 * continuity + 0.2 * closure + 0.15 * enough, 3)
    return {"score": sc_, "why": f"{len(sc)} scenes, {depth:.0%} with >= 12 words, {continuity:.0%} adjacent scenes share a "
                                 f"content word, {'closing' if closure == 1 else 'no clear closing'} line"}


def scene_relevance(visual_plan: list[dict] | None) -> dict:
    plan = [p for p in visual_plan or [] if p.get("query")]           # an empty query is a title card on purpose
    if not plan:
        return {"score": None, "why": "no scene visual plan (render not done or no stock visuals requested)"}
    pts, kinds = [], {}
    for p in plan:
        k = p.get("kind", "unknown")
        kinds[k] = kinds.get(k, 0) + 1
        if k == "licensed_photo":
            pts.append(1.0)
        elif k == "fact_graphic":
            pts.append(0.8)
        elif k == "title_card_fallback":
            pts.append(0.0)
        else:
            pts.append(0.5)
    return {"score": round(sum(pts) / len(pts), 3), "why": f"{len(plan)} scenes: {kinds}"}


def repeated_visuals(visual_plan: list[dict] | None) -> dict:
    plan = [p for p in visual_plan or [] if p.get("kind") == "licensed_photo" and p.get("source")]
    if len(plan) < 2:
        return {"score": None, "why": "fewer than 2 licensed images"}
    seen: dict[str, str | None] = {}
    dups = 0
    for p in plan:
        src, key = p["source"], p.get("reuse_key") or p.get("reused_for")
        if src in seen and not (key and seen[src] == key):              # same image for the same location = intended
            dups += 1
        seen.setdefault(src, key)
    return {"score": round(1 - dups / (len(plan) - 1), 3), "why": f"{dups} unintended repeat(s) among {len(plan)} images"}


def audio_balance(report: dict | None) -> dict:
    checks = [c for st in (report or {}).get("stages") or [] for c in st.get("checks") or [] if c.get("check") in AUDIO_CHECKS]
    if not checks:
        return {"score": None, "why": "no QualityReport audio checks"}
    tot = sum(AUDIO_CHECKS[c["check"]] for c in checks)
    got = sum(AUDIO_CHECKS[c["check"]] * (1.0 if c.get("ok") else 0.5 if c.get("severity") == "minor" else 0.0) for c in checks)
    bad = [c["check"] for c in checks if not c.get("ok")]
    return {"score": round(got / tot, 3), "why": f"{len(checks)} audio checks, failing: {bad or 'none'}"}


def score(script: dict, visual_plan: list[dict] | None = None, report: dict | None = None) -> dict:
    dims = {"hook": hook(script), "story_arc": story_arc(script), "scene_relevance": scene_relevance(visual_plan),
            "repeated_visuals": repeated_visuals(visual_plan), "audio_balance": audio_balance(report)}
    known = {k: v["score"] for k, v in dims.items() if v["score"] is not None}
    unknown = sorted(set(dims) - set(known))
    overall = round(sum(WEIGHTS[k] * v for k, v in known.items()) / sum(WEIGHTS[k] for k in known), 3) if known else None
    if len(known) < MIN_KNOWN:
        verdict = "insufficient_evidence"
    elif overall >= PASS_OVERALL and min(known.values()) >= FLOOR:
        verdict = "pass"
    else:
        verdict = "needs_review"
    return {"schema": "nexus.ReviewRubric/1", "dimensions": dims, "overall": overall, "verdict": verdict, "unknown": unknown,
            "thresholds": {"pass_overall": PASS_OVERALL, "floor": FLOOR, "min_known": MIN_KNOWN}}


def gate(result: dict) -> dict:
    """For an approval step: only `pass` is approvable; the low dimensions are named."""
    low = [k for k, v in result["dimensions"].items() if v["score"] is not None and v["score"] < FLOOR]
    blockers = [] if result["verdict"] == "pass" else [f"rubric {result['verdict']}: overall {result['overall']}"
                                                       + (f"; low: {low}" if low else "") + (f"; unknown: {result['unknown']}" if result["unknown"] else "")]
    return {"ok": not blockers, "blockers": blockers}
