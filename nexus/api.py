"""Local backend: REST API + the single-page UI. Heavy work runs on one background worker thread
(one heavy job at a time - the machine has 4 cores and 16 GB)."""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import assets, compute, config, costguard, db, edit, factory, jobs, project as prj, publish, tools
from .timeline import ops
from .timeline.render import clip_duration, clip_starts, total_duration

app = FastAPI(title="NEXUS Video AI", version="0.1.0")
UI = Path(__file__).resolve().parent / "ui" / "index.html"
_q: "queue.Queue[tuple[str, callable]]" = queue.Queue()
_analyses: dict[str, dict] = {}


# Every job goes through the durable ledger (nexus/jobs.py): nothing is queued in this process's memory, so an
# app restart or crash never loses work. Startup recovery re-queues jobs whose worker died; anything left over
# from the old in-process queue of v0.1 is marked 'interrupted'.
jobs.recover(api_worker="")
if os.environ.get("NEXUS_EMBEDDED_WORKER", "1") != "0":  # `nexus start` runs the worker as its own process instead
    threading.Thread(target=jobs.run_worker, kwargs={"role": "embedded"}, daemon=True).start()


def _project(pid: str) -> prj.Project:
    try:
        return prj.Project(pid)
    except KeyError:
        raise HTTPException(404, "project not found")


def _analysis(p: prj.Project) -> dict[str, dict]:
    out = {}
    for m in p.data["media"]:
        f = p.dir / "reports" / f"analysis_{m['id']}.json"
        if m["path"] not in _analyses and f.exists():
            _analyses[m["path"]] = json.loads(f.read_text(encoding="utf-8"))
        if m["path"] in _analyses:
            out[m["path"]] = _analyses[m["path"]]
    return out


def _tl_view(tl: dict) -> dict:
    starts = clip_starts(tl)
    return {**tl, "duration": round(total_duration(tl), 2),
            "clips": [{**c, "_start": round(s, 2), "_duration": round(clip_duration(c), 2)} for c, s in zip(tl["clips"], starts)]}


# ---------------------------------------------------------------- UI + system
@app.get("/", response_class=HTMLResponse)
def index():
    return UI.read_text(encoding="utf-8")


@app.get("/api/dashboard")
def dashboard():
    return factory.dashboard()


@app.get("/api/tools")
def api_tools():
    return {"components": tools.health(), "models": tools.installed_models(), "voices": __import__("nexus.models", fromlist=["x"]).catalog()["voices"]}


@app.get("/api/catalog")
def api_catalog():
    return tools.catalog()


@app.post("/api/evolution/check")
def api_evolution():
    return jobs.submit("evolution-check", max_retries=0)


@app.get("/api/compute")
def api_compute():
    return {"workers": compute.Federation().registry(), "routes": {k: {"needs_gpu": v[0], "preferred": v[1]} for k, v in compute.ROUTES.items()}}


@app.get("/api/capacity")
def api_capacity(target: int = 5, languages: int = 2, platforms: int = 4, hours: float = 16, machine_factor: float = 1.25, gpu_hours_week: float = 0):
    return factory.capacity(target, languages, platforms, hours, machine_factor, gpu_hours_week)


class ScheduleReq(BaseModel):
    target: int = 5
    languages: list[str] = ["ar", "de"]
    platforms: list[str] = ["youtube", "shorts", "tiktok", "instagram-reels"]
    topics: list[str] = []


@app.post("/api/schedule")
def api_schedule(r: ScheduleReq):
    return factory.schedule(r.target, r.languages, r.platforms, r.topics)


@app.get("/api/health")
def api_health():
    return {"ok": True, "version": config.VERSION, "db_schema": db.schema_version(), "jobs": jobs.summary()["counts"]}


@app.get("/api/jobs")
def api_jobs():
    rows = db.query("SELECT id,project_id,kind,status,worker,progress,created,started,finished,error,result,stage,stages,retries,recovery,durable "
                    "FROM jobs ORDER BY created DESC LIMIT 60")
    for r in rows:
        r["result"] = json.loads(r["result"]) if r["result"] else None
        st = json.loads(r.pop("stages") or "{}")
        r["stages"] = [{"name": k, "status": v["status"], "seconds": v.get("seconds"), "runs": v.get("runs", 1)} for k, v in st.items()]
    return rows


@app.post("/api/jobs/{job_id}/retry")
def api_job_retry(job_id: str):
    if not jobs.retry(job_id):
        raise HTTPException(409, "only failed, interrupted or cancelled durable jobs can be retried")
    return {"job": job_id, "status": "queued"}


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    if not jobs.cancel(job_id):
        raise HTTPException(409, "only queued jobs can be cancelled")
    return {"job": job_id, "status": "cancelled"}


@app.get("/api/storage")
def api_storage():
    from . import state, storage
    rt = state._runtime()
    return {"destinations": storage.report(), "backup": rt["backup"],
            "providers": [{"id": k, "name": v["name"], "free_gb": v["free_gb"], "eligible": storage.eligibility(k)[0], "why": storage.eligibility(k)[1]}
                          for k, v in storage.providers().items()]}


class BackupReq(BaseModel):
    drill: bool = True
    to: str | None = None


@app.post("/api/backup")
def api_backup(r: BackupReq):
    return jobs.submit("backup", {"drill": r.drill, "to": r.to}, max_retries=0)


@app.get("/api/assets")
def api_assets():
    return assets.library()


@app.get("/api/spend")
def api_spend():
    return costguard.spend_report()


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def api_projects():
    return prj.Project.list()


@app.get("/api/projects/{pid}")
def api_project(pid: str, branch: str = "main"):
    p = _project(pid)
    try:
        tl = _tl_view(p.timeline(branch))
    except KeyError:
        tl = None
    reports = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((p.dir / "reports").glob("*.qa.json"))]
    renders = [str(f.relative_to(p.dir)) for f in sorted(p.dir.rglob("*.mp4")) if "work" not in f.parts and "media" not in f.parts]
    return {"id": p.id, "name": p.name, "kind": p.kind, "media": p.data["media"], "history": p.history(), "timeline": tl,
            "reports": reports, "renders": renders, "proposal": p.data.get("proposal"), "script": p.data.get("script"),
            "credits": p.data.get("credits"), "analysis": {m: {k: a[k] for k in ("issues", "scenes", "silences", "repetitions", "not_available")} |
                                                           {"text": (a.get("transcript") or {}).get("text"), "duration": a["probe"]["duration"],
                                                            "loudness": a.get("loudness")} for m, a in _analysis(p).items()}}


@app.get("/api/projects/{pid}/file")
def api_file(pid: str, path: str):
    p = _project(pid)
    f = (p.dir / path).resolve()
    if not str(f).startswith(str(p.dir.resolve())) or not f.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(f)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), project_id: str | None = None):
    """Upload media the user owns or is authorised to use. Analysis starts immediately."""
    p = _project(project_id) if project_id else prj.Project.create(Path(file.filename).stem, "edit")
    tmp = config.CACHE / "uploads" / Path(file.filename).name
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    m = p.import_media(tmp)
    tmp.unlink()

    return {"project": p.id, "media": m, **jobs.submit("analyze", {"project": p.id, "media": m["id"]}, p.id)}


class ProposeReq(BaseModel):
    goal: str = "tighten"
    target_duration: float | None = None
    grade: str = "cinematic"
    title: str | None = None


@app.post("/api/projects/{pid}/propose")
def api_propose(pid: str, r: ProposeReq):
    p = _project(pid)
    an = _analysis(p)
    if not an:
        raise HTTPException(409, "analysis not finished yet")
    path, a = next(iter(an.items()))
    prop = edit.propose(a, r.goal, r.target_duration, {"grade": r.grade})
    p.data["proposal"] = {**prop, "media": path, "title": r.title}
    p.save()
    return prop


@app.post("/api/projects/{pid}/accept")
def api_accept(pid: str):
    p = _project(pid)
    prop = p.data.get("proposal")
    if not prop:
        raise HTTPException(409, "no proposal to accept")
    tl = edit.build_timeline(prop["media"], _analysis(p)[prop["media"]], prop, title=prop.get("title"))
    p.save_timeline(tl, "accepted editing proposal")
    return _tl_view(tl)


class OpReq(BaseModel):
    op: str
    clip: str | None = None
    args: dict = {}
    branch: str = "main"


@app.post("/api/projects/{pid}/op")
def api_op(pid: str, r: OpReq):
    """Every automated decision stays editable: trim, split, move, delete, per-scene and global properties, reframe."""
    p = _project(pid)
    tl = p.timeline(r.branch)
    try:
        if r.op in ("undo", "redo"):
            return _tl_view(getattr(p, r.op)(r.branch))
        if r.op == "fork":
            return _tl_view(p.fork(r.args["name"], r.branch))
        fn = {"ripple_delete": ops.ripple_delete, "trim": ops.trim, "split": ops.split, "move": ops.move, "set_props": ops.set_props}.get(r.op)
        if fn:
            tl = fn(tl, r.clip, **r.args)
        elif r.op == "set_global":
            tl = ops.set_global(tl, **r.args)
        elif r.op == "reframe":
            tl = ops.reframe(tl, **r.args)
        elif r.op == "insert_video":  # from the scene/B-roll browser: a source range as a new clip
            if r.args.get("src") not in {m["path"] for m in p.data["media"]}:
                raise HTTPException(400, "only media imported into this project can be inserted")
            clip = ops.video_clip(r.args["src"], float(r.args["in"]), float(r.args["out"]))
            tl = ops.insert(tl, clip, r.args.get("index"))
        elif r.op == "restore":  # bring an earlier version back as the newest one (history is kept)
            tl = p.timeline(r.branch, vid=r.args["version"])
        else:
            raise HTTPException(400, f"unknown op {r.op}")
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    if r.op in ("ripple_delete", "trim", "split", "move", "insert_video") and _analysis(p):
        edit.retime_subtitles(tl, _analysis(p))
    p.save_timeline(tl, f"{r.op} {r.clip or ''}".strip(), r.branch)
    return _tl_view(tl)


class RenderReq(BaseModel):
    branch: str = "main"
    name: str | None = None
    draft: bool = False
    platform: str | None = None


@app.post("/api/projects/{pid}/render")
def api_render(pid: str, r: RenderReq):
    p = _project(pid)
    try:
        p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")

    name = r.name or f"{r.branch}{'_draft' if r.draft else ''}_{int(time.time()) % 100000}"
    return jobs.submit("render", {"project": pid, "branch": r.branch, "name": name, "draft": r.draft, "platform": r.platform}, pid)


class LocalizeReq(BaseModel):
    lang: str
    branch: str = "main"
    dub: bool = True
    mode: str = "replace"          # replace | voiceover (original speaker stays audible underneath)
    out_branch: str | None = None


@app.post("/api/projects/{pid}/localize")
def api_localize(pid: str, r: LocalizeReq):
    p = _project(pid)
    try:
        p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")

    if r.mode not in ("replace", "voiceover"):
        raise HTTPException(400, "mode must be replace or voiceover")
    return jobs.submit("localize", {"project": pid, "lang": r.lang, "branch": r.branch, "dub": r.dub, "mode": r.mode,
                                    "out_branch": r.out_branch}, pid)


class PackageReq(BaseModel):
    platform: str
    title: str
    description: str = ""
    branch: str = "main"
    lang: str = "en"


@app.post("/api/projects/{pid}/package")
def api_package(pid: str, r: PackageReq):
    p = _project(pid)
    try:
        p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")
    return jobs.submit("package", {"project": pid, "branch": r.branch, "platform": r.platform, "title": r.title,
                                   "description": r.description, "lang": r.lang}, pid)


# ---------------------------------------------------------------- create / remix / series
class CreateReq(BaseModel):
    topic: str
    scenes: int = 5
    lang: str = "en"
    notes: str | None = None
    vertical: bool = False
    preset: str | None = None


@app.post("/api/create")
def api_create(r: CreateReq):
    return jobs.submit("create-from-idea", {"topic": r.topic, "scenes": r.scenes, "lang": r.lang, "notes": r.notes,
                                            "size": [720, 1280] if r.vertical else [1280, 720], "preset": r.preset})


@app.get("/api/presets")
def api_presets():
    from . import presets
    return presets.names()


# ---------------------------------------------------------------- growth: shorts, retention, hooks
class ShortsReq(BaseModel):
    n: int | str = 3                       # a number, or "all" (every moment whose context check passes)
    min_s: float = 18                      # legacy fields (used when no v2 field is set)
    max_s: float = 58
    pick: list[int] | None = None          # choose specific ranks from a previous selection
    time_range: list[float] | None = None  # [start, end] seconds in the source
    topic: str | None = None
    speaker: str | None = None             # only with speaker labels in the analysis
    durations: list[float] | None = None   # e.g. [30, 45, 60]
    variants: int = 1                      # hook variants per short (natural / cold open / text hooks)
    platforms: list[str] | None = None
    intensity: str | None = None           # light | medium | heavy
    captions: str | None = None            # classic | boxed | pop | karaoke
    music: bool | None = None
    aspect: str | None = None              # 9:16 | 1:1 | 4:5 | 16:9
    brand: dict | None = None
    render: bool = False
    render_limit: int = 12


@app.post("/api/projects/{pid}/shorts")
def api_shorts(pid: str, r: ShortsReq):
    _project(pid)
    v2 = any(x not in (None, False, 1) for x in (r.pick, r.time_range, r.topic, r.speaker, r.durations, r.platforms, r.intensity,
                                                 r.captions, r.music, r.aspect, r.brand, r.render)) or r.variants != 1 or r.n == "all"
    if r.n != "all" and (not isinstance(r.n, int) or r.n < 1):
        raise HTTPException(400, "n must be a positive number or 'all'")
    params = {"project": pid, "n": r.n, "min_s": r.min_s, "max_s": r.max_s}
    if v2:
        params.update({"v2": True, **r.model_dump(exclude={"min_s", "max_s"})})
    return jobs.submit("shorts", params, project_id=pid)


@app.get("/api/projects/{pid}/retention")
def api_retention(pid: str, branch: str = "main", platform: str = "shorts"):
    """Scored attention rubric for a timeline. Values are editorial rules of thumb, not platform data."""
    from . import growth
    p = _project(pid)
    rep = growth.retention_report(p.timeline(branch), _analysis(p) or None, platform)
    return {k: rep[k] for k in ("platform", "score", "checks", "auto_fixes", "manual_fixes", "metrics", "note") if k in rep}


class RetentionReq(BaseModel):
    branch: str = "main"
    platform: str = "shorts"


@app.post("/api/projects/{pid}/retention/apply")
def api_retention_apply(pid: str, r: RetentionReq):
    from . import growth
    p = _project(pid)
    an = _analysis(p) or None
    tl = p.timeline(r.branch)
    before = growth.retention_report(tl, an, r.platform)
    new = growth.apply_retention_pass(tl, before, an)
    after = growth.retention_report(new, an, r.platform)
    p.save_timeline(new, f"retention pass ({r.platform}): {before['score']} -> {after['score']}", r.branch)
    return {"score_before": before["score"], "score_after": after["score"], "applied": new.get("retention_pass", {}).get("applied", []),
            "manual": after.get("manual_fixes", []), "timeline": _tl_view(new)}


class HooksReq(BaseModel):
    text: str
    n: int = 6


@app.post("/api/hooks")
def api_hooks(r: HooksReq):
    from . import growth
    return growth.hook_variants(r.text, r.n)


class RemixReq(BaseModel):
    project_id: str
    brief: str


@app.post("/api/remix")
def api_remix(r: RemixReq):
    p = _project(r.project_id)

    if len(_analysis(p)) < 2:
        raise HTTPException(409, "upload at least two analysed videos into this project first")
    return jobs.submit("remix", {"project": p.id, "brief": r.brief}, p.id)


class SeriesReq(BaseModel):
    concept: str
    episodes: int = 3


@app.get("/api/series")
def api_series_list():
    return db.list_docs("series")


@app.post("/api/series")
def api_series(r: SeriesReq):
    return jobs.submit("series-bible", {"concept": r.concept, "episodes": r.episodes})


@app.post("/api/series/{sid}/next")
def api_series_next(sid: str):
    if db.get_doc("series", sid) is None:
        raise HTTPException(404, "series not found")
    return jobs.submit("series-episode", {"series": sid})


class FilmReq(BaseModel):
    concept: str


@app.post("/api/film")
def api_film(r: FilmReq):
    return jobs.submit("film-plan", {"concept": r.concept})


class AnimReq(BaseModel):
    kind: str
    title: str = ""
    data: list = []
    project_id: str | None = None


@app.post("/api/animate")
def api_animate(r: AnimReq):
    if r.kind not in ("bar_chart", "kinetic", "timeline", "explainer", "title3d"):
        raise HTTPException(422, f"unknown animation kind {r.kind}")
    p = _project(r.project_id) if r.project_id else prj.Project.create(f"Animation - {r.title or r.kind}", "animation")
    return jobs.submit("animate", {"project": p.id, "kind": r.kind, "title": r.title, "data": r.data,
                                   "output": f"renders/{r.kind}_{int(time.time()) % 100000}.mp4"}, p.id)


# ---------------------------------------------------------------- business
class ChannelReq(BaseModel):
    name: str
    niche: str
    audience: str = ""
    languages: list[str] = ["en"]
    platforms: list[str] = ["youtube"]
    pillars: list[str] = []


@app.get("/api/channels")
def api_channels():
    return [{**c, "business": publish.business_summary(c["id"])} for c in db.list_docs("channel")]


@app.post("/api/channels")
def api_channel(r: ChannelReq):
    return publish.create_channel(r.name, r.niche, r.audience, r.languages, r.platforms, r.pillars)


# ---------------------------------------------------------------- cinematic workspace (Account 2)
@app.get("/api/fx/catalog")
def api_fx_catalog():
    """Everything the editor's effect/transition/colour/caption panels may offer - read from the renderer itself,
    so the UI never shows a control the renderer cannot render."""
    from .fx import captions, effects, lut, post, transitions
    luts = []
    try:
        luts = lut.list_luts()
    except Exception:  # noqa: BLE001 - a broken LUT folder must not hide the rest of the catalogue
        pass
    return {"transitions": sorted(transitions.NAMES), "aliases": getattr(transitions, "ALIASES", {}),
            "effects": list(effects.KNOWN), "grades": sorted(post.GRADES), "luts": luts, "caption_styles": list(captions.STYLES),
            "keyframe_props": list(getattr(effects, "KEYFRAME_PROPS", ("x", "y", "scale", "volume"))),
            "fits": ["cover", "contain", "blur"]}


@app.get("/api/effects")
def api_effects():
    """Media-to-media processors (enhancement + VFX) with availability on this machine and model download sizes."""
    from . import enhance, vfx
    return {"enhance": enhance.effect_manifest(), "vfx": vfx.effect_manifest()}


@app.get("/api/generation/models")
def api_gen_models(task: str | None = None):
    from .generate import catalog
    return catalog.table(task)


@app.get("/api/generation/hardware")
def api_gen_hardware():
    from .generate import hardware
    return hardware.detect()


@app.get("/api/models/{model_id}/plan")
def api_model_plan(model_id: str):
    from .generate import downloads
    try:
        return downloads.plan(model_id)
    except KeyError:
        raise HTTPException(404, f"unknown model {model_id}")


class DownloadReq(BaseModel):
    accepted_gb: float
    licence_accepted: bool = False


@app.post("/api/models/{model_id}/download")
def api_model_download(model_id: str, r: DownloadReq):
    from .generate import downloads
    p = downloads.plan(model_id)
    if not r.licence_accepted or r.accepted_gb < p["total_gb"]:
        raise HTTPException(400, f"accept the licence and at least {p['total_gb']} GB to download {p['name']}")
    return jobs.submit("model-download", {"model": model_id, "accepted_gb": r.accepted_gb, "licence_accepted": True})


@app.delete("/api/models/{model_id}")
def api_model_remove(model_id: str):
    from .generate import downloads
    return {"removed": downloads.remove(model_id)}


class ProcessReq(BaseModel):
    op: str
    params: dict = {}
    branch: str = "main"


@app.post("/api/projects/{pid}/clips/{clip_id}/process")
def api_clip_process(pid: str, clip_id: str, r: ProcessReq):
    from . import enhance
    p = _project(pid)
    if not any(c["id"] == clip_id for c in p.timeline(r.branch)["clips"]):
        raise HTTPException(404, "clip not found on this branch")
    known = {e["type"].split(".", 1)[-1] for e in enhance.effect_manifest()["effects"]} | {"replace_background", "tracked_mask"}
    if r.op not in known:
        raise HTTPException(400, f"unknown operation {r.op}")
    return jobs.submit("clip-process", {"project": pid, "clip": clip_id, "op": r.op, "params": r.params, "branch": r.branch}, pid)


@app.get("/api/export/profiles")
def api_export_profiles():
    from . import export
    return export.profiles()


class ExportReq(BaseModel):
    render: str
    codec: str = "h264"
    quality: str = "balanced"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    max_mb: float | None = None
    audio: str | None = "aac"


@app.post("/api/projects/{pid}/export")
def api_export(pid: str, r: ExportReq):
    _project(pid)
    q = r.model_dump()
    q["max_bytes"] = int(q.pop("max_mb") * 1e6) if r.max_mb else None
    return jobs.submit("export", {"project": pid, **q}, pid)


@app.get("/api/projects/{pid}/scenes")
def api_scenes(pid: str):
    """Scene browser: every detected shot of every source with a cached thumbnail at its middle."""
    from .media import ff
    p = _project(pid)
    out = []
    for m in p.data["media"]:
        an = _analysis(p).get(m["path"])
        if not an:
            continue
        tdir = p.dir / "work" / "thumbs" / m["id"]
        tdir.mkdir(parents=True, exist_ok=True)
        for i, (a, b) in enumerate(an.get("scenes") or []):
            t = (a + b) / 2
            f = tdir / f"s{i:04d}.jpg"
            if not f.exists():
                try:
                    ff.run(["-ss", f"{t:.3f}", "-i", m["path"], "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "5", str(f)], op="thumbnail")
                except Exception:  # noqa: BLE001 - a missing thumbnail must not break the browser
                    continue
            out.append({"media": m["id"], "src": m["path"], "index": i, "in": round(a, 3), "out": round(b, 3),
                        "thumb": str(f.relative_to(p.dir))})
    return out


@app.get("/api/platforms")
def api_platforms():
    return {k: {**v, "auto_publish": publish.AUTO_PUBLISH[k]} for k, v in publish.PLATFORMS.items()}


# ---------------------------------------------------------------- rights (repurpose gate)
class RightsReq(BaseModel):
    media: str
    basis: str  # owned | licensed | public-domain | commentary
    evidence: str = ""
    holder: str = ""


@app.post("/api/projects/{pid}/rights")
def api_rights_declare(pid: str, r: RightsReq):
    """The user's own declaration of the right to use an uploaded source. Exports of reworked footage need one."""
    from . import repurpose
    try:
        return repurpose.declare_rights(_project(pid), r.media, r.basis, r.evidence, r.holder)
    except (ValueError, KeyError, LookupError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/projects/{pid}/rights")
def api_rights_review(pid: str, branch: str = "main"):
    from . import repurpose
    p = _project(pid)
    try:
        tl = p.timeline(branch)
    except KeyError:
        return {"passed": False, "checks": [], "media": [{"id": m["id"], "name": m["name"], "rights": m.get("rights")} for m in p.data["media"]]}
    rv = repurpose.rights_review(p, tl, _analysis(p) or None)
    return {"passed": rv["passed"], "checks": rv["checks"], "transformation": rv["report"].get("text"),
            "media": [{"id": m["id"], "name": m["name"], "rights": m.get("rights")} for m in p.data["media"]]}


# ---------------------------------------------------------------- director (Edit my video) + quality
@app.get("/api/director/profiles")
def api_director_profiles():
    """Creative-direction profiles with the parameters each one sets (editorial profiles, not claims about any studio)."""
    from . import director
    keep = ("description", "shot_length", "pause_tolerance", "filler_removal", "opening", "ending", "grade", "music", "captions")
    return {k: {x: v.get(x) for x in keep if x in v} for k, v in director.profiles().items()}


class DirectReq(BaseModel):
    profile: str = "documentary"
    target_seconds: float | None = None
    instructions: str = ""
    title: str | None = None
    size: str = "1920x1080"
    branch: str = "main"
    platforms: list[str] = []
    languages: list[str] = []
    shorts: int = 0
    broll: bool = False          # add licensed B-roll cutaways (NASA library, Openverse) over the speaker's audio
    broll_share: float = 0.35


@app.post("/api/projects/{pid}/direct")
def api_direct(pid: str, r: DirectReq):
    from . import director, repurpose
    _project(pid)
    if r.profile not in director.profiles():
        raise HTTPException(400, f"unknown profile '{r.profile}'")
    try:
        repurpose.check_request(r.instructions)
    except repurpose.RepurposeRefused as e:
        raise HTTPException(422, str(e))
    try:
        w, h = (int(x) for x in r.size.lower().split("x"))
    except ValueError:
        raise HTTPException(400, "size must look like 1920x1080")
    params = {"project": pid, "profile": r.profile, "target_seconds": r.target_seconds, "instructions": r.instructions or None,
              "title": r.title or None, "size": [w, h], "branch": r.branch, "platforms": r.platforms or None,
              "languages": r.languages or None, "shorts": r.shorts or None, "broll": r.broll, "broll_share": r.broll_share}
    return jobs.submit("direct", params, pid)


@app.get("/api/projects/{pid}/plan")
def api_plan(pid: str, limit: int = 400):
    """The director's plan: every kept and dropped span with its reason and score (explainable edit)."""
    p = _project(pid)
    f = p.dir / "reports" / "director_plan.json"
    if not f.exists():
        raise HTTPException(404, "no director plan yet")
    plan = json.loads(f.read_text(encoding="utf-8"))
    dec = plan.get("decisions", [])
    return {"profile": plan.get("profile"), "summary": plan.get("summary"), "notes": plan.get("notes"),
            "chapters": plan.get("chapters"), "decisions": [{k: d.get(k) for k in ("start", "end", "action", "kind", "reason", "score", "text")}
                                                            for d in dec[:limit]], "decisions_total": len(dec)}


@app.get("/api/projects/{pid}/edl")
def api_edl(pid: str, branch: str = "main", fmt: str = "cmx3600"):
    from fastapi.responses import PlainTextResponse
    from . import director
    p = _project(pid)
    if fmt not in ("cmx3600", "json"):
        raise HTTPException(400, "fmt must be cmx3600 or json")
    return PlainTextResponse(director.edl(p.timeline(branch), fmt, title=p.name[:60]))


@app.get("/api/projects/{pid}/quality")
def api_quality(pid: str):
    """Weighted quality reports of this project's finished renders (newest first)."""
    from . import quality
    p = _project(pid)
    out = []
    for f in sorted((p.dir / "reports").glob("*.quality.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        rep = json.loads(f.read_text(encoding="utf-8"))
        out.append({"render": f.name.replace(".quality.json", ""), **quality.summary(rep),
                    "checks": [{k: c.get(k) for k in ("id", "category", "ok", "level", "severity", "detail")} for c in rep.get("checks", [])]})
    return out


# ---------------------------------------------------------------- destination selector
@app.get("/api/platforms/profiles")
def api_platform_profiles():
    """Every destination with its key values and how each is known (documented / observed / experimental / unverified)."""
    from . import platforms
    keys = ("aspect_recommended", "resolution_recommended", "duration_max_s", "max_file_bytes", "safe_zone", "loudness_target_lufs",
            "title_max_chars", "hook_window_s")
    out = {}
    for n in platforms.names():
        prof = platforms.get(n)
        out[n] = {"display": prof["display"], "reviewed_on": prof.get("reviewed_on"),
                  "fields": {k: {x: prof["fields"].get(k, {}).get(x) for x in ("value", "kind", "source", "read_on")} for k in keys}}
    return {"profiles": out, "stale": platforms.stale()}


class DeliverReq(BaseModel):
    platforms: list[str]
    branch: str = "main"
    title: str | None = None
    description: str = ""
    lang: str | None = None


@app.post("/api/projects/{pid}/deliver")
def api_deliver(pid: str, r: DeliverReq):
    from . import platforms
    p = _project(pid)
    if not r.platforms:
        raise HTTPException(400, "choose at least one destination")
    try:
        names = [platforms.get(n)["name"] for n in r.platforms]
    except KeyError as e:
        raise HTTPException(400, str(e))
    try:
        tl = p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")
    plan = platforms.deliverables(names, duration=total_duration(tl), fps=int(tl.get("fps", 30)))
    job = jobs.submit("deliver", {"project": pid, "platforms": names, "branch": r.branch, "title": r.title or p.name,
                                  "description": r.description, "lang": r.lang}, pid)
    return {**job, "renders_planned": len(plan), "groups": plan}


# ---------------------------------------------------------------- review (REV-1): speakers, translated lines, cutaways
class SpeakerEditReq(BaseModel):
    media: str
    op: str                      # rename | name | merge | reassign
    speaker: str
    value: str | None = None


@app.get("/api/projects/{pid}/review/speakers")
def api_review_speakers(pid: str):
    from . import review
    return review.speakers(_project(pid))


@app.post("/api/projects/{pid}/review/speakers")
def api_review_speaker_edit(pid: str, r: SpeakerEditReq):
    from . import review
    p = _project(pid)
    try:
        out = review.edit_speaker(p, r.media, r.op, r.speaker, r.value)
    except (KeyError, ValueError, IndexError) as e:
        raise HTTPException(400, str(e))
    _analyses.clear()  # analysis files changed on disk
    return out


class NamesReq(BaseModel):
    branch: str = "main"


@app.post("/api/projects/{pid}/review/names")
def api_review_names(pid: str, r: NamesReq):
    """Lower thirds from the names a reviewer set (roles are never shown on screen)."""
    from . import review
    p = _project(pid)
    tl, added = review.apply_names(p.timeline(r.branch), _analysis(p))
    if added:
        p.save_timeline(tl, f"lower thirds: {', '.join(added)}", r.branch)
    return {"added": added}


@app.get("/api/projects/{pid}/review/lines")
def api_review_lines(pid: str, branch: str):
    from . import review
    try:
        return review.lines(_project(pid).timeline(branch))
    except KeyError:
        raise HTTPException(404, f"no branch '{branch}'")


class LineReq(BaseModel):
    branch: str
    index: int
    text: str | None = None
    approved: bool | None = None
    render_name: str | None = None   # e.g. "main_de": rewrite that render's SRT/VTT with the reviewed text


@app.post("/api/projects/{pid}/review/lines")
def api_review_line_edit(pid: str, r: LineReq):
    from . import review
    p = _project(pid)
    try:
        tl = review.edit_line(p.timeline(r.branch), r.index, r.text, r.approved)
    except (KeyError, IndexError) as e:
        raise HTTPException(400, str(e))
    p.save_timeline(tl, f"review line {r.index}", r.branch)
    side = review.write_sidecars(p, tl, r.render_name) if r.render_name else []
    st = review.lines(tl)
    return {"approved": st["approved"], "total": st["total"], "native_review_done": st["native_review_done"], "sidecars": side}


@app.get("/api/projects/{pid}/review/cutaways")
def api_review_cutaways(pid: str, branch: str = "main"):
    from . import review
    return review.cutaways(_project(pid).timeline(branch))


class CutawayReq(BaseModel):
    branch: str = "main"
    clip: str
    why: str = ""


@app.post("/api/projects/{pid}/review/cutaways/reject")
def api_review_cutaway_reject(pid: str, r: CutawayReq):
    from . import review
    p = _project(pid)
    try:
        tl = review.reject_cutaway(p.timeline(r.branch), r.clip, r.why)
    except KeyError as e:
        raise HTTPException(400, str(e))
    p.save_timeline(tl, f"cutaway rejected on {r.clip}", r.branch)
    return {"rejected": r.clip, "remaining": len(review.cutaways(tl))}


@app.get("/api/projects/{pid}/review/shorts")
def api_review_shorts(pid: str):
    from . import review
    return review.shorts_titles(_project(pid))


class ShortTitleReq(BaseModel):
    branch: str
    title: str


@app.post("/api/projects/{pid}/review/shorts")
def api_review_short_title(pid: str, r: ShortTitleReq):
    from . import review
    p = _project(pid)
    try:
        tl = review.set_short_title(p.timeline(r.branch), r.title)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    p.save_timeline(tl, "title edited in review", r.branch)
    return {"branch": r.branch, "title": r.title}


@app.post("/api/projects/{pid}/subtitle-package")
def api_subtitle_package(pid: str, branch: str = "main"):
    """All localised subtitle files of a branch + a readiness manifest (subtitle vs dub readiness, native review)."""
    r = publish.subtitle_package(_project(pid), branch)
    if not r["languages"]:
        raise HTTPException(409, "no localised renders on this branch yet: run Languages first")
    return {k: r[k] for k in ("folder", "zip", "languages", "subtitle_ready")} | {"manifest": r["manifest"]}
