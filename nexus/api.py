"""Local backend: REST API + the single-page UI. Heavy work runs on one background worker thread
(one heavy job at a time - the machine has 4 cores and 16 GB)."""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import assets, compute, config, costguard, db, edit, factory, jobs, project as prj, publish, remix, series, tools
from .media import analyze
from .timeline import ops
from .timeline.render import clip_duration, clip_starts, total_duration

app = FastAPI(title="NEXUS Video AI", version="0.1.0")
UI = Path(__file__).resolve().parent / "ui" / "index.html"
_q: "queue.Queue[tuple[str, callable]]" = queue.Queue()
_analyses: dict[str, dict] = {}


_API_WORKER = jobs.worker_id("api")


def _worker() -> None:
    while True:
        job_id, fn = _q.get()
        db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), job_id))
        try:
            with jobs.heavy_lock():
                result = fn()
            db.execute("UPDATE jobs SET status='done', progress=1, finished=?, result=? WHERE id=?",
                       (time.time(), json.dumps(result, default=str, ensure_ascii=False), job_id))
        except Exception as e:  # the job fails, the server keeps running
            db.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE id=?",
                       (time.time(), f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", job_id))


threading.Thread(target=_worker, daemon=True).start()
# Startup recovery: durable jobs whose worker died are re-queued from their last checkpoint; in-process jobs
# that died with a previous app run are marked 'interrupted' (never left 'running' forever).
jobs.recover(api_worker=_API_WORKER)
if os.environ.get("NEXUS_EMBEDDED_WORKER", "1") != "0":  # `nexus start` runs the worker as its own process instead
    threading.Thread(target=jobs.run_worker, kwargs={"role": "embedded"}, daemon=True).start()


def submit(kind: str, fn, project_id: str = "") -> dict:
    job_id = db.new_id("job")
    db.execute("INSERT INTO jobs(id,project_id,kind,status,worker,progress,created) VALUES(?,?,?,?,?,?,?)",
               (job_id, project_id, kind, "queued", _API_WORKER, 0.0, time.time()))
    _q.put((job_id, fn))
    return {"job": job_id, "status": "queued"}


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
            _analyses[m["path"]] = json.loads(f.read_text())
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
    return submit("evolution-check", lambda: tools.evolution_check())


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
    reports = [json.loads(f.read_text()) for f in sorted((p.dir / "reports").glob("*.qa.json"))]
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

    def run():
        an = analyze.analyze(m["path"])
        _analyses[m["path"]] = an
        (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(an, ensure_ascii=False))
        return {"issues": an["issues"], "scenes": len(an["scenes"]), "duration": an["probe"]["duration"]}

    return {"project": p.id, "media": m, **submit("analyze", run, p.id)}


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
        else:
            raise HTTPException(400, f"unknown op {r.op}")
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    if r.op in ("ripple_delete", "trim", "split", "move") and _analysis(p):
        edit.retime_subtitles(tl, _analysis(p))
    p.save_timeline(tl, f"{r.op} {r.clip or ''}".strip(), r.branch)
    return _tl_view(tl)


class RenderReq(BaseModel):
    branch: str = "main"
    name: str | None = None


@app.post("/api/projects/{pid}/render")
def api_render(pid: str, r: RenderReq):
    p = _project(pid)
    try:
        p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")

    return jobs.submit("render", {"project": pid, "branch": r.branch, "name": r.name or f"{r.branch}_{int(time.time()) % 100000}"}, pid)


class LocalizeReq(BaseModel):
    lang: str
    branch: str = "main"
    dub: bool = True


@app.post("/api/projects/{pid}/localize")
def api_localize(pid: str, r: LocalizeReq):
    p = _project(pid)
    try:
        p.timeline(r.branch)
    except KeyError:
        raise HTTPException(409, f"project has no timeline on branch '{r.branch}' yet")

    return jobs.submit("localize", {"project": pid, "lang": r.lang, "branch": r.branch, "dub": r.dub}, pid)


class PackageReq(BaseModel):
    platform: str
    title: str
    description: str = ""
    branch: str = "main"
    lang: str = "en"


@app.post("/api/projects/{pid}/package")
def api_package(pid: str, r: PackageReq):
    p = _project(pid)
    tl = p.timeline(r.branch)
    return submit(f"package-{r.platform}", lambda: publish.package(p, tl, r.platform, r.title, r.description, r.lang), pid)


# ---------------------------------------------------------------- create / remix / series
class CreateReq(BaseModel):
    topic: str
    scenes: int = 5
    lang: str = "en"
    notes: str | None = None
    vertical: bool = False


@app.post("/api/create")
def api_create(r: CreateReq):
    return jobs.submit("create-from-idea", {"topic": r.topic, "scenes": r.scenes, "lang": r.lang, "notes": r.notes,
                                            "size": [720, 1280] if r.vertical else [1280, 720]})


class RemixReq(BaseModel):
    project_id: str
    brief: str


@app.post("/api/remix")
def api_remix(r: RemixReq):
    p = _project(r.project_id)

    def run():
        lib = _analysis(p)
        if len(lib) < 2:
            raise ValueError("upload at least two analysed videos into this project first")
        versions = remix.make_versions(p, lib, r.brief)
        return {"plan": p.data["remix"]["plan"], "comparison": remix.compare(versions)}

    return submit("remix", run, p.id)


class SeriesReq(BaseModel):
    concept: str
    episodes: int = 3


@app.get("/api/series")
def api_series_list():
    return db.list_docs("series")


@app.post("/api/series")
def api_series(r: SeriesReq):
    return submit("series-bible", lambda: series.create_series(r.concept, r.episodes))


@app.post("/api/series/{sid}/next")
def api_series_next(sid: str):
    def run():
        b, ep, review = series.next_episode(sid)
        out = series.produce_episode(b, ep, review)
        return {"episode": ep, "project": out["project"], "status": out["report"]["status"], "issues": out["report"]["known_issues"]}

    return submit("series-episode", run)


class FilmReq(BaseModel):
    concept: str


@app.post("/api/film")
def api_film(r: FilmReq):
    return submit("film-plan", lambda: series.film_plan(r.concept))


class AnimReq(BaseModel):
    kind: str
    title: str = ""
    data: list = []
    project_id: str | None = None


@app.post("/api/animate")
def api_animate(r: AnimReq):
    from .animation import engines, mograph
    p = _project(r.project_id) if r.project_id else prj.Project.create(f"Animation - {r.title or r.kind}", "animation")
    out = p.dir / "renders" / f"{r.kind}_{int(time.time()) % 100000}.mp4"

    def run():
        if r.kind == "bar_chart":
            mograph.bar_chart(out, r.title, [(str(a), float(b)) for a, b in r.data])
        elif r.kind == "kinetic":
            mograph.kinetic_text(out, [str(x) for x in r.data])
        elif r.kind == "timeline":
            mograph.timeline_animation(out, r.title, [(str(a), str(b)) for a, b in r.data])
        elif r.kind == "explainer":
            engines.manim_explainer(out, r.title, [{"label": str(a), "detail": str(b)} for a, b in r.data])
        elif r.kind == "title3d":
            engines.blender_title(out, r.title or "NEXUS")
        else:
            raise ValueError(f"unknown animation kind {r.kind}")
        return {"project": p.id, "output": str(out.relative_to(p.dir))}

    return submit(f"animate-{r.kind}", run, p.id)


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


@app.get("/api/platforms")
def api_platforms():
    return {k: {**v, "auto_publish": publish.AUTO_PUBLISH[k]} for k, v in publish.PLATFORMS.items()}
