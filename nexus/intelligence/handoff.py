"""Strategy Handoff: Account 3's approved strategy in the form Account 2 consumes.

One document (`nexus.StrategyHandoff/1`) carries
  editorial_plan  Account 2's `nexus.EditorialPlan/1` - one "insert" decision per scene, each with its reason, the claim
                  ids that ground it and the requested visual (so the director's plan is explainable and editable);
  render_job      Account 2's `nexus.RenderJobManifest/1` - job kind create-from-idea, settings, expected outputs and
                  resource requirements (what Account 1's scheduler needs to place the job);
  create_params   the exact params of Account 2's durable `create-from-idea` job, including `script`, which
                  `create.create_video(script=...)` uses VERBATIM (Demo 4 defect: the approved script was paraphrased);
  checks          originality, grounding and framing results - `approved` is false unless all three pass.
Both Account 2 documents are validated with Account 2's own validator when it is installed (nexus.contracts.media, the
shared contracts package); otherwise with the structural subset below, so this module works on every branch."""
from __future__ import annotations

import re

from . import contracts as C
from . import strategy

_STOP = {"a", "an", "the", "of", "on", "in", "at", "with", "and", "showing", "view", "shot", "close", "up", "wide", "aerial",
         "footage", "image", "photo", "slow", "pan", "over", "from", "to", "its", "their", "under", "across"}


def visual_query(visual: str, topic: str) -> str:
    """A 2-4 word photo search phrase from a storyboard visual: named places/things first, then concrete nouns."""
    from .quality import visual_context
    names = [n for n in visual_context.candidates(visual or "") if len(n) > 3]
    if names:
        return max(names, key=len)[:60]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]+", visual or "") if w.lower() not in _STOP]
    return " ".join(words[:3])[:60] or topic[:60]


def a2_script(script: dict) -> dict:
    """Account 3 ContentScript -> the script dict Account 2's renderer reads (narration unchanged)."""
    from .quality.scene_plan import key_fact
    scenes = []
    for s in script["scenes"]:
        kf = key_fact(s.get("narration", ""))
        scenes.append({"narration": s["narration"], "on_screen_text": (kf[0] if kf else "")[:48],
                       "visual_query": visual_query(s.get("visual", ""), script["title"]), "visual": s.get("visual", ""),
                       "claim_ids": s.get("claim_ids") or []})
    return {"title": script["title"], "scenes": scenes, "language": script["language"], "script_id": script["id"]}


def _a2_validate(doc: dict) -> list[str]:
    try:
        from ..contracts import media  # shared contracts package (integration branch / main after merge)
        return media.validate(doc)
    except ImportError:
        need = {"nexus.EditorialPlan/1": ("brief", "profile", "decisions"),
                "nexus.RenderJobManifest/1": ("job_id", "kind", "inputs", "settings", "outputs", "requirements")}[doc["schema"]]
        return [f"missing {k}" for k in need if k not in doc]


def build(script: dict, board: dict, brief: dict | None = None, style: dict | None = None) -> dict:
    o = script["originality"]
    checks = {"originality": bool(o.get("ok")), "grounding": bool(o.get("grounding", {}).get("ok")),
              "framing": bool(o.get("framing", {"ok": True}).get("ok"))}
    params = strategy.production_job(script, board, brief, style)
    params["script"] = a2_script(script)
    claims = {c["id"]: c for c in (brief or {}).get("claims", []) if c.get("id")}
    decisions = []
    t = 0.0
    secs = {}
    for sh in board.get("shots", []):
        secs[sh["scene"]] = secs.get(sh["scene"], 0) + sh["seconds"]
    for i, s in enumerate(params["script"]["scenes"]):
        d = secs.get(i + 1, 5.0)
        src = [claims[c].get("source_url") or claims[c].get("source") for c in s["claim_ids"] if c in claims]
        decisions.append({"start": round(t, 2), "end": round(t + d, 2), "action": "insert", "kind": "scene", "scene": i + 1,
                          "text": s["narration"], "visual": s["visual"], "visual_query": s["visual_query"],
                          "reason": f"{script['scenes'][i].get('purpose') or 'scene'}; grounded in {s['claim_ids'] or 'no claim'}",
                          "claim_ids": s["claim_ids"], "sources": [x for x in src if x]})
        t += d
    plan = {"schema": "nexus.EditorialPlan/1", "brief": f"{script['title']} - original script {script['id']} (Account 3)",
            "profile": params["preset"], "decisions": decisions, "target_s": board.get("target_s"),
            "chapters": [{"start": d["start"], "title": (script["scenes"][d["scene"] - 1].get("purpose") or f"Scene {d['scene']}")[:60]}
                         for d in decisions],
            "deliverables": [{"aspect": board["aspect"], "lang": script["language"], "kind": "master"}]}
    job = {"schema": "nexus.RenderJobManifest/1", "job_id": C.new_id("a3job"), "kind": "create-from-idea", "inputs": [],
           "settings": {k: v for k, v in params.items() if k not in ("notes",)}, "outputs": [{"uri": f"nexus://project/{params['name']}/master",
                                                                                            "role": "master"}],
           "requirements": {"gpu": False, "ram_gb": 6, "est_s": round(60 + 20 * len(decisions)), "models": ["tts", "llm-local"]}}
    errs = _a2_validate(plan) + _a2_validate(job)
    if errs:
        raise C.ContractError("; ".join(errs))
    return C.make("StrategyHandoff", id=C.new_id("hand"), script_id=script["id"], storyboard_id=board["id"], approved=all(checks.values()),
                  editorial_plan=plan, render_job=job, create_params=params, checks=checks,
                  claims=[{"id": c["id"], "text": c["text"], "source": c.get("source_url") or c.get("source")} for c in claims.values()])


def submit(handoff: dict) -> dict:
    """Queue Account 2's create-from-idea job from an approved handoff (refuses an unapproved one)."""
    if not handoff["approved"]:
        raise ValueError(f"handoff not approved: {handoff['checks']}")
    from .. import jobs
    return jobs.submit("create-from-idea", handoff["create_params"])
