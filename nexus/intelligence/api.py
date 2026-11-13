"""Account 3 HTTP API, mounted under /api/a3 by nexus/api.py (one include line), plus the workspace UI script
(/ui/intelligence.js, loaded by index.html). Each section degrades to a clear 503 when its work stream is not
installed, instead of failing the whole app."""
from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/a3", tags=["account3"])
UI_JS = Path(__file__).resolve().parent.parent / "ui" / "intelligence.js"


def _mod(name: str):
    try:
        return importlib.import_module(f"nexus.intelligence.{name}")
    except ImportError as e:
        raise HTTPException(503, f"Account 3 module '{name}' is not installed in this build ({e})")


def ui_js() -> Response:
    return Response(UI_JS.read_text(encoding="utf-8"), media_type="application/javascript")


# ----------------------------------------------------------------- AI models / provider router
@router.get("/ai/providers")
def ai_providers():
    from .capability_registry import catalog
    from .model_router import router as r
    out = []
    for p in catalog()["providers"]:
        for m in p.get("models") or [{"model": "(discovered at runtime)"}]:
            e = {**{k: v for k, v in p.items() if k != "models"}, "provider": p["id"], **m}
            ok, why = r.eligibility(e, "public") if m.get("model") != "(discovered at runtime)" else (False, "configure base_url + models")
            out.append({"provider": p["id"], "name": p.get("name"), "model": m.get("model"), "hosting": p["hosting"], "privacy": p["privacy"],
                        "licence": m.get("licence", p.get("licence_runtime")), "commercial_use": p.get("commercial_use"),
                        "quotas": p.get("quotas", {}), "verified": p.get("verified"), "quality": m.get("quality", {}),
                        "available": ok, "why": why})
    return {"models": out, "health": r.health()}


@router.get("/ai/routes")
def ai_routes(privacy: str = "public"):
    from .model_router import router as r
    if privacy not in r.PRIVACY_RANK:
        raise HTTPException(422, "privacy must be public, internal or private")
    return {"privacy": privacy, "tasks": r.status(privacy)}


class AskReq(BaseModel):
    task: str = "general"
    prompt: str
    privacy: str = "internal"


@router.post("/ai/try")
def ai_try(q: AskReq):
    """Run one prompt through the router (UI 'try it'). Never cached, never sent to a provider the privacy class forbids."""
    from .model_router import router as r
    try:
        res = r.complete(q.task, q.prompt, privacy=q.privacy, max_tokens=300, use_cache=False)
    except r.NoProviderAvailable as e:
        raise HTTPException(503, str(e))
    except KeyError as e:
        raise HTTPException(422, str(e))
    return {"text": res.text, "provider": res.provider, "model": res.model, "latency_s": res.latency_s, "fallbacks": res.fallbacks,
            "below_bar": res.below_bar}


# ----------------------------------------------------------------- research / opportunities
def _jobs():
    from .. import jobs, workflows  # noqa: F401  (registers Account 3 job kinds)
    return jobs


class ResearchReq(BaseModel):
    topic: str
    niche: str = "science"
    lang: str = "en"


@router.post("/research")
def research_start(r: ResearchReq):
    if not r.topic.strip():
        raise HTTPException(422, "topic is required")
    return _jobs().submit("a3-market-research", r.model_dump(), max_retries=1)


@router.get("/research")
def research_list():
    from .. import db
    out = []
    for d in reversed(db.list_docs("a3_report")):
        o = d.get("opportunity", {})
        out.append({"id": d["id"], "topic": d["topic"], "niche": d["niche"], "language": d["language"], "date": d["date"],
                    "score": o.get("score"), "confidence": o.get("confidence"), "status": o.get("status")})
    return out


@router.get("/research/{rid}")
def research_get(rid: str):
    from .. import db
    d = db.get_doc("a3_report", rid)
    if not d:
        raise HTTPException(404, "report not found")
    return d


@router.get("/research/{rid}/assessment")
def research_assessment(rid: str):
    """Market research V2 evidence-quality layer (credibility, cross-source, recency, geography, language demand,
    competition/saturation, evidence coverage, uncertainty interval)."""
    from .market import assess_v2
    return assess_v2.assess(research_get(rid))


@router.get("/niches")
def niches_list():
    m = _mod("market")
    return m.load_niches()


class TrendReq(BaseModel):
    niches: list[str] | None = None


@router.post("/trends")
def trends(r: TrendReq):
    m = _mod("market")
    return m.trend_monitor(m.Ctx(), m.watchlist(r.niches))


class StrategyReq(BaseModel):
    report: str
    style: str | None = None
    scenes: int = 5
    aspect: str = "16:9"
    submit: bool = False
    allow_single_source: bool = True


@router.post("/strategy")
def strategy_start(r: StrategyReq):
    from .. import db
    if not db.get_doc("a3_report", r.report):
        raise HTTPException(404, "research report not found")
    if r.style and not db.get_doc("a3_style", r.style):
        raise HTTPException(404, "style not found")
    return _jobs().submit("a3-strategy", r.model_dump(), max_retries=1)


@router.get("/strategy")
def strategy_list():
    from .. import db
    return [{"id": d["id"], "report": d["report"], "title": d["script"]["title"], "language": d["script"]["language"],
             "originality": d["script"]["originality"], "scenes": d["script"]["scenes"], "shots": len(d["storyboard"]["shots"]),
             "target_s": d["storyboard"]["target_s"], "submitted": d.get("submitted")} for d in reversed(db.list_docs("a3_strategy"))]


# ----------------------------------------------------------------- creator references
class RefReq(BaseModel):
    path: str
    rights: str
    creator: str = "unknown"
    category: str = "unknown"
    format: str = "unknown"
    title: str | None = None
    licence_url: str | None = None


@router.post("/references")
def reference_start(r: RefReq):
    m = _mod("creators")
    if r.rights not in m.RIGHTS_OK:
        raise HTTPException(422, f"rights must be one of {sorted(m.RIGHTS_OK)} - NEXUS only analyses media you may use")
    meta = {k: v for k, v in {"creator": r.creator, "category": r.category, "format": r.format, "title": r.title}.items() if v}
    return _jobs().submit("a3-reference", {"path": r.path, "rights": r.rights, "meta": meta, "licence_url": r.licence_url}, max_retries=0)


@router.get("/references")
def reference_list():
    from .. import db
    return [{"id": d["id"], "creator": d.get("creator"), "category": d.get("category"), "format": d.get("format"),
             "duration_s": d.get("duration_s"), "hook": (d.get("hook") or {}).get("type"), "confidence": d.get("confidence"),
             "source": d.get("source")} for d in reversed(db.list_docs("a3_reference"))]


@router.get("/references/library")
def reference_library():
    return _mod("creators").library.archetypes()


class IdsReq(BaseModel):
    ids: list[str]
    name: str = "reference style"


def _profiles(ids: list[str]) -> list[dict]:
    from .. import db
    ps = [db.get_doc("a3_reference", i) for i in ids]
    if not ids or any(p is None for p in ps):
        raise HTTPException(404, "unknown reference id")
    return ps


@router.post("/references/compare")
def reference_compare(r: IdsReq):
    return _mod("creators").compare(_profiles(r.ids))


class BlendReq(BaseModel):
    ids: list[str]
    weights: list[float] | None = None
    name: str = "blended reference style"
    frames: bool = False  # measure on-screen text / colour / motion from the local file (slower)


@router.post("/references/blend")
def reference_blend(r: BlendReq):
    """Creator intelligence V2: 14 dimensions per reference and a weighted blend with conflicts surfaced."""
    from .creators import style_v2
    profs = _profiles(r.ids)
    dims = [style_v2.dimensions(p, (p.get("source") or {}).get("local_path") if r.frames else None) for p in profs]
    try:
        return {"references": dims, "blend": style_v2.combine(dims, r.weights, r.name)}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/styles")
def style_create(r: IdsReq):
    from .. import db
    st = _mod("creators").style_from(_profiles(r.ids), name=r.name)
    db.put_doc("a3_style", st, st["id"])
    return st


@router.get("/styles")
def style_list():
    from .. import db
    return list(reversed(db.list_docs("a3_style")))


# ----------------------------------------------------------------- publishing
@router.get("/platforms")
def platforms():
    return _mod("publishing.capabilities").matrix()


class PackReq(BaseModel):
    platform: str
    transcript: str
    title: str | None = None
    language: str = "en"
    duration_s: float | None = None


@router.post("/package")
def package(r: PackReq):
    try:
        return _mod("publishing.packaging").package(r.platform, r.transcript, r.title, r.language, r.duration_s)
    except KeyError as e:
        raise HTTPException(404, f"unknown platform {e}")


class PubReq(BaseModel):
    project_id: str
    file: str
    platform: str
    metadata: dict
    visibility: str | None = None
    schedule: str | None = None
    connection_id: str | None = None
    release: dict | None = None   # quality_report / provenance / disclosure_decision / language (release_gate.py)


class ReleaseReq(BaseModel):
    quality_report: dict | None = None
    provenance: dict | None = None
    disclosure_decision: dict | None = None
    language: dict | None = None


@router.post("/publish")
def publish_create(r: PubReq):
    e = _mod("publishing.engine")
    rg = _mod("publishing.release_gate")
    art = {"uri": r.file}
    p = rg._local(r.file)
    if p is not None and p.is_file():
        art.update(sha256=rg.sha256_file(p), bytes=p.stat().st_size)
    try:
        m = e.create(r.project_id, art, r.platform, r.metadata, r.connection_id, r.visibility, r.schedule, release=r.release)
    except Exception as ex:  # PublishError and contract violations -> the reason, verbatim
        raise HTTPException(422, str(ex))
    return e.public_view(m)


@router.get("/publish")
def publish_list():
    e = _mod("publishing.engine")
    store = _mod("publishing.store")
    return [e.public_view(m) for m in store.list_manifests()]


@router.post("/publish/{mid}/approve")
def publish_approve(mid: str):
    """The owner's explicit approval (required for unlisted/public). Only reachable from the owner's own UI session."""
    e = _mod("publishing.engine")
    try:
        return e.public_view(e.approve(mid))
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.post("/publish/{mid}/queue")
def publish_queue(mid: str):
    jh = _mod("publishing.jobs_handler")
    try:
        return jh.submit(mid)
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.post("/publish/{mid}/release")
def publish_release(mid: str, r: ReleaseReq):
    """Attach release evidence (QualityReport after human review, disclosure decision, language review record)."""
    e = _mod("publishing.engine")
    try:
        return e.public_view(e.attach_release(mid, **{k: v for k, v in r.model_dump().items() if v is not None}))
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.get("/publish/{mid}/release")
def publish_release_check(mid: str):
    try:
        return _mod("publishing.engine").release_check(mid)
    except Exception as ex:
        raise HTTPException(404, str(ex))


@router.get("/publish/{mid}/history")
def publish_history(mid: str):
    return _mod("publishing.engine").history(mid)


# ----------------------------------------------------------------- analytics + growth
class CsvReq(BaseModel):
    kind: str
    text: str
    period: str | None = None
    project_id: str | None = None


@router.post("/analytics/import")
def analytics_import(r: CsvReq):
    try:
        return _mod("publishing.analytics").import_csv(r.text, r.kind, r.period, r.project_id)
    except Exception as ex:
        raise HTTPException(422, str(ex))


@router.get("/analytics")
def analytics_list(platform: str | None = None):
    return _mod("publishing.analytics").records(platform)


class ExpReq(BaseModel):
    hypothesis: str
    variable: str
    variants: list[dict]
    platform: str
    metric: str
    baseline: float
    mde: float


@router.post("/experiments")
def experiment_create(r: ExpReq):
    from .. import db
    ex = _mod("growth.experiments")
    try:
        e = ex.create(r.hypothesis, r.variable, r.variants, r.platform, r.metric, baseline=r.baseline, mde=r.mde)
    except Exception as err:
        raise HTTPException(422, str(err))
    db.put_doc("a3_experiment", e, e["id"])
    return e


@router.get("/experiments")
def experiment_list():
    from .. import db
    return list(reversed(db.list_docs("a3_experiment")))


@router.post("/experiments/{eid}/evaluate")
def experiment_evaluate(eid: str):
    from .. import db
    e = db.get_doc("a3_experiment", eid)
    if not e:
        raise HTTPException(404, "experiment not found")
    res = _mod("growth.experiments").evaluate(e, _mod("publishing.analytics").records(e["platform"]))
    db.put_doc("a3_experiment", res, eid)
    return res


@router.get("/recommendations")
def recommendations():
    """Evidence-backed recommendations from the owner's imported analytics. Empty until real records exist."""
    recs_mod, learn = _mod("growth.recommend"), _mod("growth.learning")
    rows = _mod("publishing.analytics").records()
    if not rows:
        return {"records": 0, "recommendations": [], "note": "import your analytics first (CSV export or YouTube Analytics)"}
    out = recs_mod.from_weak_openings(learn.weak_openings(rows))
    return {"records": len(rows), "recommendations": out}


# ----------------------------------------------------------------- languages
@router.get("/languages")
def languages():
    return _mod("language.matrix").summary()


class LangCheckReq(BaseModel):
    source: list[str]
    target: list[str]
    lang: str
    glossary: dict | None = None
    protected: list[str] | None = None
    back_translate: bool = False
    privacy: str = "private"


@router.post("/language/check")
def language_check(r: LangCheckReq):
    qa = _mod("language.translation_qa")
    back, how = (qa.back_translate(r.target, r.lang, r.privacy) if r.back_translate else (None, "not requested"))
    memory = _mod("language.terminology").glossary(r.lang, " ".join(r.source))  # reviewer-approved terms always apply
    gl = {**memory, **(r.glossary or {})}
    return {**qa.check_lines(r.source, r.target, r.lang, gl or None, back, r.protected), "back_translation": how,
            "terminology_applied": sorted(memory)}


@router.get("/language/readiness")
def language_readiness(lang: str | None = None, ocr: bool = True):
    rd = _mod("language.readiness")
    return rd.assess(lang, ocr) if lang else {"weights": rd.WEIGHTS, "languages": rd.report(ocr)}


class ReviewSubmitReq(BaseModel):
    lang: str
    lines: list[dict]
    project: str = ""
    variety_note: str = ""


class ReviewRecordReq(BaseModel):
    verdict: str
    reviewer: str
    correction: str = ""
    notes: str = ""
    terms: dict[str, str] | None = None


@router.post("/language/review")
def language_review_submit(r: ReviewSubmitReq):
    return {"ids": _mod("language.review").submit(r.lang, r.lines, r.project, r.variety_note)}


@router.get("/language/review")
def language_review_queue(lang: str | None = None, status: str | None = "pending"):
    rv = _mod("language.review")
    return {"items": rv.queue(lang, status), "stats": rv.stats()}


@router.post("/language/review/{item_id}")
def language_review_record(item_id: str, r: ReviewRecordReq):
    try:
        return _mod("language.review").record(item_id, r.verdict, r.reviewer, correction=r.correction, notes=r.notes, terms=r.terms)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


class TermReq(BaseModel):
    term: str
    lang: str
    target: str
    origin: str = "owner"
    by: str = ""


@router.get("/language/terms")
def language_terms(lang: str):
    return {"lang": lang, "glossary": _mod("language.terminology").glossary(lang)}


@router.post("/language/terms")
def language_terms_add(r: TermReq):
    try:
        return _mod("language.terminology").add(r.term, r.lang, r.target, origin=r.origin, by=r.by)
    except ValueError as e:
        raise HTTPException(400, str(e))


class DialectReq(BaseModel):
    text: str
    adapt_to: str | None = None   # "ar-EG" | "ar-Gulf"
    use_router: bool = False


@router.post("/language/dialect")
def language_dialect(r: DialectReq):
    d = _mod("language.dialect")
    out = {"classification": d.classify(r.text)}
    if r.adapt_to:
        res = d.adapt_lines([r.text], r.adapt_to, privacy="private", use_router=r.use_router)[0]
        out["adapted"] = res
        out["adapted_classification"] = d.classify(res.get("text", r.text))
    return out


@router.get("/language/voice")
def language_voice(lang: str, commercial: bool = True):
    dub = _mod("language.dubbing")
    try:
        return dub.select_voice(lang, commercial=commercial)
    except Exception as e:  # VoiceRefused: the reasons are the answer
        raise HTTPException(409, str(e))
