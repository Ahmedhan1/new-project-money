"""Reference → original strategy: combine a research brief, an opportunity and one or more abstract style profiles into an
ORIGINAL script + storyboard brief, check originality, and hand it to Account 2's production engine.

Pipeline: ResearchBrief (facts with sources) + CreativeStyleProfile (abstract patterns, `do_not_copy`) →
ContentScript (router task "scriptwriting", schema-validated; deterministic outline when no AI provider is free) →
originality + grounding checks → StoryboardBrief → `production_job()` (params for Account 2's `create-from-idea` job)."""
from __future__ import annotations

import re

from . import contracts as C

PACE_TO_PRESET = [(2.5, "social-short"), (4.0, "commercial"), (6.0, "documentary"), (99.0, "cinematic")]
SCRIPT_SCHEMA = {"type": "object", "required": ["title", "hook", "scenes"], "properties": {
    "title": {"type": "string"}, "hook": {"type": "string"},
    "scenes": {"type": "array", "minItems": 3, "items": {"type": "object", "required": ["purpose", "narration", "visual"],
               "properties": {"purpose": {"type": "string"}, "narration": {"type": "string"}, "visual": {"type": "string"},
                              "claim_ids": {"type": "array", "items": {"type": "string"}}}}}}}


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", (s or "").lower())


def ngram_overlap(text: str, reference: str, n: int = 6) -> float:
    """Share of the text's n-grams that also occur in the reference (0 = fully original wording)."""
    a, b = _words(text), _words(reference)
    ga = {tuple(a[i:i + n]) for i in range(len(a) - n + 1)}
    gb = {tuple(b[i:i + n]) for i in range(len(b) - n + 1)}
    return round(len(ga & gb) / len(ga), 3) if ga else 0.0


def originality(script: dict, style: dict | None = None, reference_texts: list[str] | None = None) -> dict:
    text = " ".join([script.get("title", ""), script.get("hook", "")] + [s.get("narration", "") for s in script.get("scenes", [])])
    banned = [p for p in (style or {}).get("do_not_copy", []) if isinstance(p, str) and p.strip() and p.lower() in text.lower()]
    overlaps = [ngram_overlap(text, r) for r in (reference_texts or [])]
    worst = max(overlaps, default=0.0)
    return {"ok": not banned and worst <= 0.05, "banned_phrases_used": banned, "max_6gram_overlap": worst,
            "rule": "no do_not_copy phrase; at most 5 % of 6-word sequences shared with any reference transcript"}


def grounding(script: dict, brief: dict | None) -> dict:
    """Every number in the narration must appear in a claim of the research brief; claims cited must exist."""
    if not brief:
        return {"ok": False, "why": "no research brief - facts unverified", "unsupported_numbers": [], "unknown_claim_ids": []}
    claims = {c.get("id"): c for c in brief.get("claims", [])}
    corpus = " ".join(c.get("text", "") for c in claims.values())
    nums, bad_ids = [], []
    for s in script.get("scenes", []):
        for n in re.findall(r"\d[\d,.]*", s.get("narration", "")):
            if n.rstrip(".,").replace(",", "") not in corpus.replace(",", ""):
                nums.append(n)
        bad_ids += [i for i in s.get("claim_ids", []) if i not in claims]
    return {"ok": not nums and not bad_ids, "unsupported_numbers": nums, "unknown_claim_ids": bad_ids}


def claims_from_research(brief: dict, allow_single_source: bool = True) -> dict:
    """Market ResearchBrief -> the claim list the script writer may use (text + first supporting source URL).
    Claims in conflict are never passed on; single-source claims only when allowed (they stay marked)."""
    urls = {x["id"]: x.get("url") or x.get("title") for x in brief.get("sources", [])}
    out = []
    for i, c in enumerate(brief.get("claims", [])):
        st = c.get("status", "")
        if "conflict" in st or (st == "single_source" and not allow_single_source):
            continue
        sid = (c.get("supported_by") or c.get("source_ids") or [None])[0]
        out.append({"id": f"c{i + 1}", "text": c["text"], "source_url": urls.get(sid), "status": st})
    return {"id": brief.get("id", "brief"), "claims": out}


def _outline(topic: str, brief: dict | None, style: dict | None, n: int) -> dict:
    """Deterministic fallback: hook from the strongest claim, one scene per claim, takeaway."""
    claims = (brief or {}).get("claims", [])[: max(1, n - 1)]
    hook_pat = ((style or {}).get("hook_patterns") or ["question"])[0]
    first = claims[0]["text"] if claims else topic
    hook = f"What if everything you know about {topic} is only half the story?" if "question" in str(hook_pat) else first
    scenes = [{"purpose": "hook", "narration": hook, "visual": topic, "claim_ids": []}]
    scenes += [{"purpose": "explain", "narration": c["text"], "visual": c.get("visual", topic), "claim_ids": [c["id"]]} for c in claims]
    scenes.append({"purpose": "takeaway", "narration": f"That is why {topic} matters.", "visual": topic, "claim_ids": []})
    return {"title": topic[:60], "hook": hook, "scenes": scenes[: max(n, 3)], "source": "deterministic outline (no AI provider)"}


def write_script(topic: str, brief: dict | None = None, style: dict | None = None, *, scenes: int = 5, language: str = "en",
                 audience: str = "curious general audience", privacy: str = "internal", reference_texts: list[str] | None = None) -> dict:
    from .model_router import NoProviderAvailable, complete
    claims = "\n".join(f"[{c['id']}] {c['text']}" for c in (brief or {}).get("claims", [])[:30]) or "(no verified claims)"
    st = style or {}
    prompt = (f"Write an ORIGINAL video script.\nTopic: {topic}\nLanguage: {language}\nAudience: {audience}\n"
              f"Structure to follow (abstract, from analysed references): {st.get('structure') or ['hook', 'setup', 'escalation', 'payoff']}\n"
              f"Hook patterns that work for this format: {st.get('hook_patterns') or ['question', 'surprising fact']}\n"
              f"Pacing: {st.get('pacing') or {}}\nNever use these (belong to other creators): {st.get('do_not_copy') or []}\n"
              f"Use ONLY these verified claims for facts and cite their ids in claim_ids:\n{claims}\n"
              f"Write {scenes} scenes. Each: purpose, 2-3 sentence narration, concrete visual. Return JSON "
              '{"title": ..., "hook": ..., "scenes": [{"purpose","narration","visual","claim_ids"}]}')
    try:
        r = complete("scriptwriting", prompt, schema=SCRIPT_SCHEMA, privacy=privacy, max_tokens=1600, temperature=0.6)
        body, how = r.data, f"{r.provider}/{r.model}"
    except NoProviderAvailable as e:
        body, how = _outline(topic, brief, style, scenes), f"fallback: {e.reasons[:2]}"
    sources = [c.get("source_url") or c.get("source") for c in (brief or {}).get("claims", []) if c.get("source_url") or c.get("source")]
    orig = originality(body, style, reference_texts)
    doc = C.make("ContentScript", id=C.new_id("scr"), title=str(body["title"])[:90], language=language,
                 scenes=[{k: s.get(k) for k in ("purpose", "narration", "visual", "claim_ids")} for s in body["scenes"]],
                 brief_id=(brief or {}).get("id"), style_id=st.get("id"), sources=sorted(set(map(str, sources))),
                 originality={**orig, "grounding": grounding(body, brief), "written_by": how, "hook": body.get("hook", "")})
    return doc


def storyboard(script: dict, *, format: str = "explainer", aspect: str = "16:9", style: dict | None = None) -> dict:
    pace = ((style or {}).get("pacing") or {}).get("mean_shot_s") or 5.0
    shots = []
    for i, s in enumerate(script["scenes"]):
        words = len(_words(s.get("narration", "")))
        dur = max(3.0, words / 2.6)
        for k in range(max(1, round(dur / pace))):
            shots.append({"scene": i + 1, "shot": k + 1, "purpose": s.get("purpose"), "visual": s.get("visual"), "seconds": round(dur / max(1, round(dur / pace)), 2)})
    return C.make("StoryboardBrief", id=C.new_id("sb"), script_id=script["id"], format=format, aspect=aspect,
                  target_s=round(sum(x["seconds"] for x in shots), 1), shots=shots, style={"mean_shot_s": pace, "style_id": (style or {}).get("id")})


def preset_for(style: dict | None) -> str:
    pace = ((style or {}).get("pacing") or {}).get("mean_shot_s") or 5.0
    return next(p for lim, p in PACE_TO_PRESET if pace <= lim)


def production_job(script: dict, board: dict, brief: dict | None = None, style: dict | None = None) -> dict:
    """Params for Account 2's durable `create-from-idea` job. Notes carry the verified claims (with sources) and the
    approved creative direction; Account 2 writes the final narration from them and runs its own QA.
    (Integration request to Account 2: accept `script` directly so the approved ContentScript is used verbatim.)"""
    claims = "\n".join(f"- {c['text']} (source: {c.get('source_url') or c.get('source')})" for c in (brief or {}).get("claims", []))
    direction = "\n".join(f"Scene {i + 1} ({s.get('purpose')}): {s.get('narration')} | visual: {s.get('visual')}" for i, s in enumerate(script["scenes"]))
    vertical = board["aspect"] == "9:16"
    return {"topic": script["title"], "scenes": len(script["scenes"]), "lang": script["language"], "preset": preset_for(style),
            "size": [720, 1280] if vertical else [1280, 720], "name": f"A3 {script['title'][:50]}",
            "notes": f"APPROVED CREATIVE DIRECTION (original; follow the order and the hook):\nHook: {script['originality'].get('hook', '')}\n{direction}\n\n"
                     f"VERIFIED FACTS (use only these):\n{claims}", "a3": {"script_id": script["id"], "storyboard_id": board["id"]}}


def submit(script: dict, board: dict, brief: dict | None = None, style: dict | None = None, require_checks: bool = True) -> dict:
    o = script["originality"]
    if require_checks and not (o["ok"] and o["grounding"]["ok"]):
        raise ValueError(f"script not approved for production: originality={o['ok']} grounding={o['grounding']}")
    from .. import jobs
    params = production_job(script, board, brief, style)
    return {**jobs.submit("create-from-idea", params), "params": params}
