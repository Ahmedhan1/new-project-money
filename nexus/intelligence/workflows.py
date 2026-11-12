"""Account 3 durable job kinds (registered with nexus.jobs by importing this module from nexus/workflows.py).

    a3-market-research   topic -> sources, demand, research brief, ContentOpportunity, production brief (stored)
    a3-reference         lawfully held reference file -> CreatorReferenceProfile (stored)
    a3-strategy          approved opportunity (+ style) -> original script + storyboard -> Account 2 create job
Results are stored as documents (db.put_doc kinds a3_report / a3_reference / a3_strategy) so the UI can list them."""
from __future__ import annotations

from .. import db, jobs


@jobs.handler("a3-market-research")
def market_research(job: jobs.Job) -> dict:
    from .market import Ctx, analyse
    p = job.params
    rep = job.stage("research", lambda: analyse(Ctx(), p["topic"], p.get("niche", "science"), p.get("lang", "en")))
    rid = db.put_doc("a3_report", {**rep, "id": job.id})
    o = rep["opportunity"]
    return {"report": rid, "status": "ok", "score": o.get("score"), "confidence": o.get("confidence"), "issues": []}


@jobs.handler("a3-reference")
def reference(job: jobs.Job) -> dict:
    from .creators import analyze_reference
    p = job.params
    prof = job.stage("analyse", lambda: analyze_reference(p["path"], rights=p["rights"], meta=p.get("meta") or {},
                                                          licence_url=p.get("licence_url"), use_router=p.get("use_router", False)))
    db.put_doc("a3_reference", prof, prof["id"])
    return {"reference": prof["id"], "status": "ok", "confidence": prof.get("confidence"), "issues": []}


@jobs.handler("a3-strategy")
def strategy_job(job: jobs.Job) -> dict:
    from . import strategy
    p = job.params
    rep = db.get_doc("a3_report", p["report"])
    style = db.get_doc("a3_style", p["style"]) if p.get("style") else None
    br = strategy.claims_from_research(rep["brief"], allow_single_source=p.get("allow_single_source", True))

    def write():
        s = strategy.write_script(rep["topic"], br, style, scenes=p.get("scenes", 5), language=rep.get("language", "en"), privacy="internal")
        sb = strategy.storyboard(s, aspect=p.get("aspect", "16:9"), style=style)
        return {"script": s, "storyboard": sb}

    w = job.stage("write", write)
    doc = {"id": job.id, "report": p["report"], **w, "submitted": None}
    o = w["script"]["originality"]
    if p.get("submit") and o["ok"] and o["grounding"]["ok"]:
        doc["submitted"] = job.stage("submit", lambda: strategy.submit(w["script"], w["storyboard"], br, style))
    db.put_doc("a3_strategy", doc, job.id)
    issues = ([] if o["ok"] else ["originality check failed"]) + ([] if o["grounding"]["ok"] else ["grounding: " + str(o["grounding"])[:160]])
    return {"strategy": job.id, "status": "ok" if not issues else "NEEDS_REVIEW", "issues": issues,
            "production_job": (doc["submitted"] or {}).get("job")}
