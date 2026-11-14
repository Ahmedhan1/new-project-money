"""Local backend: REST API + the single-page UI. Heavy work runs on one background worker thread
(one heavy job at a time - the machine has 4 cores and 16 GB)."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import assets, compute, config, costguard, db, edit, factory, jobs, project as prj, publish, tools
from .timeline import ops
from .timeline.render import clip_duration, clip_starts, total_duration

from .cloud import production as _production  # noqa: E402

# Production profile (NEXUS_ENV=production): refuse to start unless every required gate passes, hold the single-instance
# lock, and do not publish the interactive API docs. Development (the default) keeps docs, guarded by the owner check.
_PROD = _production.is_production()
if _PROD:
    _production.enforce(os.environ.get("NEXUS_BIND_HOST"))
    from .cloud import instance as _instance
    if not _instance.acquire("api"):
        raise _production.ProductionConfigError("another NEXUS API process already serves this NEXUS_HOME (single instance only)")


def _lifespan_factory():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        from .cloud import lifecycle
        lifecycle.started()
        try:
            yield
        finally:
            lifecycle.stopping()
    return lifespan


app = FastAPI(title="NEXUS Video AI", version="0.1.0", lifespan=_lifespan_factory(),
              docs_url=None if _PROD else "/docs", redoc_url=None if _PROD else "/redoc",
              openapi_url=None if _PROD else "/openapi.json")

# Account 1 security: response headers (CSP, nosniff, frame-ancestors none, no-store for API) and an owner guard on the
# legacy /api/* routes and the main page, which previously relied only on binding to 127.0.0.1.
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402

from .cloud import security as _security  # noqa: E402

_security.install(app)


DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def _owner_guard(request, call_next):
    p = request.url.path
    # the legacy UI/API and the interactive docs (route inventory) are owner-only; /api/v1 routers carry their own check
    if ((p == "/" or p.startswith("/api/")) and not p.startswith("/api/v1")) or p in DOC_PATHS or p.startswith("/docs/"):
        try:
            _security.check(request)
        except HTTPException as e:
            return _JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)
from .cloud import limits as _limits  # noqa: E402

app.add_middleware(_limits.BodyLimit)

# Account 3 (intelligence, research, publishing, growth): own router + UI script, mounted here only.
from .intelligence import api as _a3  # noqa: E402

app.include_router(_a3.router)
app.add_api_route("/ui/intelligence.js", _a3.ui_js, include_in_schema=False)
UI = Path(__file__).resolve().parent / "ui" / "index.html"
_q: "queue.Queue[tuple[str, callable]]" = queue.Queue()
_analyses: dict[str, dict] = {}


# Every job goes through the durable ledger (nexus/jobs.py): nothing is queued in this process's memory, so an
# app restart or crash never loses work. Startup recovery re-queues jobs whose worker died; anything left over
# from the old in-process queue of v0.1 is marked 'interrupted'.
jobs.recover(api_worker="")
if os.environ.get("NEXUS_EMBEDDED_WORKER", "1") != "0":  # `nexus start` runs the worker as its own process instead
    threading.Thread(target=jobs.run_worker, kwargs={"role": "embedded"}, daemon=True).start()
    # remote compute (Account 1): poll/dispatch Kaggle and pull-worker jobs while any are active
    from .cloud.compute import poller as _remote_poller
    _remote_poller.start()
    # storage: uploads left "running" by a crash become "interrupted" and can be resumed from the provider's offset
    from .cloud import alerts as _alerts
    _alerts.start()
    try:
        from .cloud.storage import transfers as _transfers
        _transfers.recover_after_restart()
    except Exception:  # noqa: BLE001 - never block startup on transfer bookkeeping
        pass


# Account 1 cloud platform routers (/api/v1). Each owner module exposes `router`; a module that is not present in
# this build is reported by /api/v1/health instead of failing the whole app.
CLOUD_ROUTERS = ("routes_core", "routes_security", "routes_compute", "routes_storage", "routes_studio", "routes_system")
CLOUD_MISSING: dict[str, str] = {}
for _name in CLOUD_ROUTERS:
    try:
        import importlib as _il
        app.include_router(_il.import_module(f"nexus.cloud.{_name}").router)
    except ModuleNotFoundError as _e:
        CLOUD_MISSING[_name] = str(_e)


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


# ---------------------------------------------------------------- simple mode (autopilot): /simple
SIMPLE_UI = Path(__file__).resolve().parent / "ui" / "simple.html"


@app.get("/simple", response_class=HTMLResponse)
def simple_page():
    """The one-screen UI (edit my video / video from an idea or script / let NEXUS pick). Data comes from owner-only /api."""
    return SIMPLE_UI.read_text(encoding="utf-8")


@app.post("/api/autopilot")
def api_autopilot(body: dict):
    from . import autopilot
    try:
        return autopilot.submit(body)
    except autopilot.AutopilotError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/autopilot/options")
def api_autopilot_options():
    from . import autopilot, platforms
    from .intelligence.market import load_niches
    plats = {}
    for k in autopilot.VERTICAL + autopilot.LANDSCAPE:
        try:
            plats[k] = platforms.get(k)["display"]
        except Exception:  # noqa: BLE001
            pass
    raw = load_niches()
    raw = raw.get("niches", raw) if isinstance(raw, dict) else raw
    nich = [{"id": k, "name": (v or {}).get("label") or k} for k, v in raw.items()] if isinstance(raw, dict) else []
    return {"platforms": plats, "vertical": list(autopilot.VERTICAL), "styles": list(autopilot.PROFILES), "niches": nich}


@app.get("/api/autopilot/library")
def api_autopilot_library(limit: int = 60):
    """Videos already in NEXUS (uploads and links), newest first, to edit again without uploading: name, length, cover."""
    from .media import ff
    out = []
    for row in prj.Project.list()[: max(1, min(200, limit))]:
        try:
            p = prj.Project(row["id"])
        except KeyError:
            continue
        for m in p.data.get("media") or []:
            src = Path(m["path"])
            if not src.exists() or src.suffix.lower() not in _UPLOAD_EXT:
                continue
            f = p.dir / "reports" / f"analysis_{m['id']}.json"
            dur = None
            if f.exists():
                try:
                    dur = json.loads(f.read_text(encoding="utf-8"))["probe"]["duration"]
                except (ValueError, KeyError):
                    dur = None
            thumb = p.dir / "work" / "thumbs" / f"cover_{m['id']}.jpg"
            if not thumb.exists():
                thumb.parent.mkdir(parents=True, exist_ok=True)
                try:
                    ff.run(["-ss", f"{(dur or 6) / 3:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(thumb)],
                           op="thumbnail", timeout=60)
                except Exception:  # noqa: BLE001 - audio files and broken media simply have no cover
                    pass
            out.append({"project": p.id, "media": m["id"], "name": m.get("name") or p.name, "duration": dur, "analysed": dur is not None,
                        "created": row["created"], "bytes": src.stat().st_size,
                        "cover": str(thumb.relative_to(p.dir)) if thumb.exists() else None})
            break  # one entry per project: its first video
    return out


@app.get("/api/autopilot/runs")
def api_autopilot_runs(limit: int = 30):
    rows = db.query("SELECT id,project_id,status,progress,created,started,finished,error,result,stage,stages,params "
                    "FROM jobs WHERE kind='autopilot' ORDER BY created DESC LIMIT ?", (min(100, limit),))
    for r in rows:
        r["result"] = json.loads(r["result"]) if r["result"] else None
        r["params"] = json.loads(r["params"]) if r["params"] else {}
        st = json.loads(r.pop("stages") or "{}")
        r["stages"] = [{"name": k, "status": v["status"], "seconds": v.get("seconds")} for k, v in st.items()]
    return rows


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
        raise HTTPException(409, "only queued or running jobs can be cancelled")
    st = (jobs.get(job_id) or {}).get("status")
    return {"job": job_id, "status": st if st == "cancelled" else "cancelling"}


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
            "credits": p.data.get("credits"), "analysis": {m: {k: a.get(k) or [] for k in ("issues", "scenes", "silences", "repetitions", "not_available")} |
                                                           {"text": (a.get("transcript") or {}).get("text"), "duration": a["probe"]["duration"],
                                                            "loudness": a.get("loudness")} for m, a in _analysis(p).items()}}


@app.get("/api/projects/{pid}/file")
def api_file(pid: str, path: str):
    p = _project(pid)
    from .pathsafe import PathEscape, contained
    try:
        f = contained(p.dir, path, must_be_file=True)
    except PathEscape:
        raise HTTPException(404, "file not found") from None
    return FileResponse(f)


_UPLOAD_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".wmv", ".flv",
               ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
               ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}


UPLOAD_MARGIN_BYTES = 256 * 2 ** 20  # never fill the disk to the last byte: the database and logs need room too


def _upload_size(file: UploadFile) -> int:
    if getattr(file, "size", None) is not None:
        return int(file.size)
    f = file.file
    pos = f.tell()
    f.seek(0, 2)
    n = f.tell()
    f.seek(pos)
    return n


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), project_id: str | None = None):
    """Upload media the user owns or is authorised to use. Analysis starts immediately.

    QA-D4: free space is checked before anything is written (HTTP 507 when it does not fit), the file is staged under a
    temporary name in NEXUS_HOME (same filesystem as the projects, so it is moved, not copied a second time), and a
    project created for this upload is removed again if the import fails - no truncated media, no orphan project."""
    import errno
    import uuid
    name = Path((file.filename or "").replace("\\", "/")).name.strip()
    if not name or name in (".", "..") or Path(name).suffix.lower() not in _UPLOAD_EXT:
        raise HTTPException(415, "unsupported file type: upload a video, audio or image file")
    existing = _project(project_id) if project_id else None
    size = _upload_size(file)
    stage_root = config.HOME / "tmp" / "uploads"
    stage_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(stage_root).free
    if size + UPLOAD_MARGIN_BYTES > free:
        raise HTTPException(507, f"not enough free disk space for this upload: it needs {(size + UPLOAD_MARGIN_BYTES) / 1e6:.0f} MB, "
                                 f"{free / 1e6:.0f} MB free in {config.HOME}. Free some space (Storage page -> cleanup) and try again.")
    tmp = stage_root / uuid.uuid4().hex / name   # one folder per upload: concurrent same-name uploads never collide
    tmp.parent.mkdir(parents=True, exist_ok=True)
    created = None
    try:
        try:
            with tmp.open("wb") as f:
                shutil.copyfileobj(file.file, f, 1 << 20)
            p = existing or prj.Project.create(Path(name).stem, "edit")
            created = None if existing else p
            m = p.import_media(tmp, move=True)
        except BaseException as e:
            if created is not None:
                created.discard()
            if isinstance(e, OSError) and e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                raise HTTPException(507, "the disk filled up during the upload; nothing was kept. Free some space and try again.") from None
            raise
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return {"project": p.id, "media": m, **jobs.submit("analyze", {"project": p.id, "media": m["id"]}, p.id)}


# ---------------------------------------------------------------- big uploads: chunked + automatic size reduction
CHUNK_MAX = 64 * 2 ** 20          # one request (Cloudflare's gateway accepts up to 100 MB per request)
CHUNKED_MAX = 20 * 2 ** 30        # a whole upload
_UID = re.compile(r"^[a-f0-9]{16,40}$")


def _chunk_dir(uid: str) -> Path:
    if not _UID.match(uid or ""):
        raise HTTPException(400, "bad upload id")
    return config.HOME / "tmp" / "chunked" / uid


@app.put("/api/upload/chunk")
async def api_upload_chunk(request: Request, upload_id: str, index: int, total: int, size: int):
    """One piece of a big upload (the browser splits the file). Idempotent: a piece sent twice just replaces itself."""
    d = _chunk_dir(upload_id)
    if not (0 <= index < total <= 4000) or not (0 < size <= CHUNKED_MAX):
        raise HTTPException(400, "bad chunk numbers")
    if index == 0 and not d.exists():
        for old in d.parent.glob("*") if d.parent.exists() else []:  # abandoned uploads older than a day
            if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)
        free = shutil.disk_usage(config.HOME).free
        if size * 2 + UPLOAD_MARGIN_BYTES > free:  # the pieces + the assembled file
            raise HTTPException(507, f"not enough free disk space: the upload needs {size * 2 / 1e9:.1f} GB, {free / 1e9:.1f} GB free")
    d.mkdir(parents=True, exist_ok=True)
    part, n = d / f"{index:05d}.part", 0
    tmp = part.with_suffix(".tmp")
    with tmp.open("wb") as f:
        async for block in request.stream():
            n += len(block)
            if n > CHUNK_MAX:
                f.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, f"a piece may be at most {CHUNK_MAX // 2 ** 20} MB")
            f.write(block)
    tmp.replace(part)
    return {"upload_id": upload_id, "index": index, "bytes": n, "received": len(list(d.glob("*.part")))}


class ChunkFinishReq(BaseModel):
    upload_id: str
    name: str
    total: int
    size: int
    project_id: str | None = None
    optimize: bool = True


@app.post("/api/upload/finish")
def api_upload_finish(r: ChunkFinishReq):
    """Join the pieces, then a durable job reduces the size when worthwhile, imports and analyses the video."""
    import uuid
    d = _chunk_dir(r.upload_id)
    name = Path((r.name or "").replace("\\", "/")).name.strip()
    if not name or Path(name).suffix.lower() not in _UPLOAD_EXT:
        raise HTTPException(415, "unsupported file type: upload a video, audio or image file")
    parts = [d / f"{i:05d}.part" for i in range(r.total)]
    missing = [i for i, x in enumerate(parts) if not x.exists()]
    if missing:
        raise HTTPException(409, f"{len(missing)} piece(s) missing (first: {missing[0]}): send them again")
    if sum(x.stat().st_size for x in parts) != r.size:
        raise HTTPException(409, "the pieces do not add up to the file size: send the upload again")
    out = config.HOME / "tmp" / "uploads" / uuid.uuid4().hex / name
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        for x in parts:
            with x.open("rb") as g:
                shutil.copyfileobj(g, f, 4 << 20)
            x.unlink()
    shutil.rmtree(d, ignore_errors=True)
    p = _project(r.project_id) if r.project_id else prj.Project.create(Path(name).stem, "edit")
    job = jobs.submit("media-prepare", {"project": p.id, "file": str(out), "optimize": r.optimize}, p.id)
    return {"project": p.id, **job}


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
        elif r.op in ("add_track", "remove_track", "set_track", "add_item", "remove_item", "move_item", "set_item", "set_keyframes"):
            # multi-track layers (nexus/timeline/tracks.py); `clip` carries the track/item id where the op needs one
            if r.op == "add_item" and r.args.get("item", {}).get("src") and r.args["item"]["src"] not in {m["path"] for m in p.data["media"]}:
                raise HTTPException(400, "only media imported into this project can be placed on a layer")
            fn2 = getattr(ops, r.op)
            tl = fn2(tl, r.clip, **r.args) if r.op not in ("add_track",) else fn2(tl, **r.args)
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
    return {"transitions": sorted(transitions.ALL), "aliases": getattr(transitions, "ALIASES", {}),
            "effects": list(effects.KNOWN), "grades": sorted(post.GRADES), "luts": luts, "caption_styles": list(captions.STYLES),
            "keyframe_props": list(getattr(effects, "KEYFRAME_PROPS", ("x", "y", "scale", "volume"))),
            "fits": ["cover", "contain", "blur"], "mask_keyframe_props": list(getattr(effects, "MASK_KEYFRAME_PROPS", ())),
            "split_layouts": list(getattr(post, "SPLIT_LAYOUTS", ())), "slowmo": list(getattr(post, "SLOWMO_PROFILES", ())),
            "pipelines": ["pieces", "legacy"]}


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
    keep = ("label", "description", "shot_len", "shot_len_median", "hook_window_s", "pattern_interrupt_s", "cutaways_per_min",
            "pacing", "pause_tolerance", "filler_removal", "opening", "ending", "end_structure", "title_style", "grade", "music",
            "captions", "alias_of")
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
    broll_subject: list[str] = []  # the film's named subject (e.g. ["Mars"]): library B-roll must name it in its metadata
    overrides: dict = {}          # {"keep": [decision ids], "drop": [...], "opening": id, "no_lift": bool, "reject_cutaways": [...]}
    profile_overrides: dict = {}  # editorial parameters on top of the profile (e.g. {"shot_len_median": 5})
    end_text: str | None = None   # call-to-action / end card text (only ever from the user)
    reference_profile_id: str | None = None  # a ReferenceStyleProfile made by POST /api/projects/{pid}/reference


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
              "languages": r.languages or None, "shorts": r.shorts or None, "broll": r.broll, "broll_share": r.broll_share,
              "broll_subject": r.broll_subject or None, "overrides": r.overrides or None, "profile_overrides": r.profile_overrides or None, "end_text": r.end_text or None,
              "reference_profile_id": r.reference_profile_id or None}
    if r.reference_profile_id and not (_project(pid).dir / "reports" / "references" / f"{r.reference_profile_id}.json").exists():
        raise HTTPException(404, f"no reference profile '{r.reference_profile_id}' in this project")
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
            "chapters": plan.get("chapters"), "decisions": [{k: d.get(k) for k in ("id", "sentence_id", "src", "start", "end", "action", "kind", "reason",
                                                                                  "score", "confidence", "text")}
                                                            for d in dec[:limit]], "decisions_total": len(dec)}


class ReferenceReq(BaseModel):
    media_id: str                 # a video already imported into this project
    rights_basis: str             # owned | licensed | public-domain | commentary (refused otherwise)
    name: str | None = None


@app.post("/api/projects/{pid}/reference")
def api_reference(pid: str, r: ReferenceReq):
    """Measure a lawfully supplied reference video's STRUCTURE (pacing, hook, density, captions, music density, colour) as
    a ReferenceStyleProfile. Nothing of the reference (frames, words, sound) is ever used in an output."""
    p = _project(pid)
    m = next((x for x in p.data["media"] if x["id"] == r.media_id), None)
    if m is None:
        raise HTTPException(404, "media not in this project")
    from . import reference
    if r.rights_basis not in reference.RIGHTS_OK:
        raise HTTPException(422, f"rights basis must be one of {sorted(reference.RIGHTS_OK)}")
    return jobs.submit("reference", {"project": pid, "media_id": r.media_id, "rights_basis": r.rights_basis, "name": r.name}, pid)


@app.get("/api/projects/{pid}/references")
def api_references(pid: str):
    d = _project(pid).dir / "reports" / "references"
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        doc = json.loads(f.read_text(encoding="utf-8"))
        out.append({"id": f.stem, "name": doc.get("name"), "metrics": doc.get("metrics"), "rights": doc.get("rights")})
    return {"references": out}


@app.get("/api/visual/info")
def api_visual_info():
    from .media import visual
    return visual.info()


class VisualInstallReq(BaseModel):
    licence_accepted: bool = False
    accepted_mb: float = 0


@app.post("/api/visual/install")
def api_visual_install(r: VisualInstallReq):
    """Opt-in download of the visual-understanding model (Apache-2.0, ~213 MB) through the Cost Guard."""
    from .media import visual
    if visual.available():
        return {"status": "installed", **visual.info()}
    if not r.licence_accepted or r.accepted_mb < visual.DOWNLOAD_MB:
        raise HTTPException(409, f"consent required: licence {visual.LICENCE}; download {visual.DOWNLOAD_MB} MB")
    return jobs.submit("visual-install", {"accepted_mb": r.accepted_mb})


@app.get("/api/projects/{pid}/edl")
def api_edl(pid: str, branch: str = "main", fmt: str = "cmx3600"):
    from fastapi.responses import PlainTextResponse
    from . import director
    p = _project(pid)
    if fmt not in ("cmx3600", "json", "otio"):
        raise HTTPException(400, "fmt must be cmx3600, json or otio")
    if fmt == "otio":  # OpenTimelineIO JSON (main storyline), readable by Resolve/Premiere/Kdenlive importers
        from .timeline import edl as edlmod
        return PlainTextResponse(json.dumps(edlmod.to_otio(p.timeline(branch), name=p.name[:60]), indent=1), media_type="application/json")
    return PlainTextResponse(director.edl(p.timeline(branch), fmt, title=p.name[:60]))


@app.get("/api/projects/{pid}/tracks/validate")
def api_tracks_validate(pid: str, branch: str = "main"):
    from .timeline import tracks
    return {"warnings": tracks.validate(_project(pid).timeline(branch))}


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


# ---------------------------------------------------------------- asset library (nexus/assetlib)
@app.get("/api/library")
def api_library(category: str | None = None, q: str | None = None, only: str | None = None):
    from . import assetlib
    return {"assets": assetlib.catalog(category, q, only), "summary": assetlib.summary(),
            "categories": sorted(assetlib.CATEGORY_DIRS), "optional_future": assetlib.optional_future()}


class LibInstallReq(BaseModel):
    asset_id: str


@app.post("/api/library/install")
def api_library_install(r: LibInstallReq):
    """Generated assets are made right away; downloads run as a job (progress in the job log)."""
    from . import assetlib
    try:
        e = assetlib.entry(r.asset_id)
    except KeyError as ex:
        raise HTTPException(404, str(ex))
    if e["kind"] in ("download", "pack"):
        return jobs.submit("asset-install", {"asset_id": r.asset_id})
    try:
        return {"installed": assetlib.install(r.asset_id)}
    except assetlib.AssetError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/library/uninstall")
def api_library_uninstall(r: LibInstallReq):
    from . import assetlib
    try:
        return assetlib.uninstall(r.asset_id)
    except (assetlib.AssetError, KeyError) as ex:
        raise HTTPException(409, str(ex))


class LibFavReq(BaseModel):
    asset_id: str
    on: bool = True


@app.post("/api/library/favorite")
def api_library_favorite(r: LibFavReq):
    from . import assetlib
    try:
        return {"favorites": assetlib.set_favorite(r.asset_id, r.on)}
    except KeyError as ex:
        raise HTTPException(404, str(ex))


_PREVIEW_LIMIT = None


@app.get("/api/library/preview/{asset_id}")
async def api_library_preview(asset_id: str):
    """Previews render on first request (FFmpeg). They run on their own 2-thread limiter so a page full of new previews
    never occupies the shared worker threads that every other API call needs."""
    import anyio

    from . import assetlib
    global _PREVIEW_LIMIT
    if _PREVIEW_LIMIT is None:
        _PREVIEW_LIMIT = anyio.CapacityLimiter(2)
    try:
        path = await anyio.to_thread.run_sync(assetlib.preview, asset_id, limiter=_PREVIEW_LIMIT)
        return FileResponse(path)
    except FileNotFoundError as ex:
        raise HTTPException(404, str(ex))
    except KeyError as ex:
        raise HTTPException(404, str(ex))
    except RuntimeError as ex:
        raise HTTPException(500, str(ex))


@app.get("/api/library/file/{asset_id}")
def api_library_file(asset_id: str):
    """The installed file (play a sound, download a LUT)."""
    from . import assetlib
    rec = assetlib.installed().get(asset_id)
    if not rec or not Path(rec["files"][0]).is_file():
        raise HTTPException(404, "not installed")
    return FileResponse(rec["files"][0])


@app.post("/api/library/verify")
def api_library_verify():
    from . import assetlib
    return assetlib.verify_all() | {"missing": assetlib.missing()}


@app.post("/api/library/repair")
def api_library_repair():
    from . import assetlib
    return assetlib.repair_missing()


class LibApplyReq(BaseModel):
    asset_id: str
    target: str = "clip"
    clip: str | None = None
    branch: str = "main"
    at: float | None = None
    member: str | None = None


@app.post("/api/projects/{pid}/assets/apply")
def api_library_apply(pid: str, r: LibApplyReq):
    from . import assetlib
    _project(pid)
    try:
        return assetlib.apply(pid, r.asset_id, r.target, r.clip, r.branch, r.at, r.member)
    except (assetlib.AssetError, KeyError, ValueError) as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/library/import")
async def api_library_import(file: UploadFile = File(...), category: str = "overlays", name: str = "", license: str = "",
                             author: str = "", source_url: str = "", commercial_use: bool = False, attribution: str = ""):
    """Import a file you own or are licensed to use. Licence fields are required and checked."""
    from . import assetlib
    tmp = config.ASSETS / "tmp" / f"upload_{int(time.time() * 1000)}_{Path(file.filename or 'x').name}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(await file.read())
    try:
        return assetlib.import_file(tmp, category=category, name=name or Path(file.filename or "asset").stem, license=license,
                                    author=author, source_url=source_url, commercial_use=commercial_use, attribution=attribution)
    except assetlib.AssetError as ex:
        raise HTTPException(400, str(ex))
    finally:
        tmp.unlink(missing_ok=True)


class LibExportReq(BaseModel):
    asset_ids: list[str]


@app.post("/api/library/export")
def api_library_export(r: LibExportReq):
    from . import assetlib
    dest = config.ASSETS / "tmp" / f"nexus_assets_{int(time.time())}.zip"
    try:
        out = assetlib.export_pack(r.asset_ids, dest)
    except (assetlib.AssetError, KeyError) as ex:
        raise HTTPException(400, str(ex))
    return FileResponse(out["path"], filename=Path(out["path"]).name)


# ---------------------------------------------------------------- disk management + daily archive
@app.get("/api/disk")
def api_disk():
    from . import diskman
    return diskman.report()


class DiskSettingsReq(BaseModel):
    settings: dict


@app.post("/api/disk/settings")
def api_disk_settings(r: DiskSettingsReq):
    from . import diskman
    try:
        return diskman.save_settings(r.settings)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


class CleanupReq(BaseModel):
    rules: list[str] | None = None
    plan_id: str | None = None


@app.post("/api/disk/cleanup")
def api_disk_cleanup(r: CleanupReq):
    """Without plan_id: a dry run (what would be deleted and why). With the plan_id you were shown: delete exactly that."""
    from . import diskman
    try:
        if not r.plan_id:
            p = diskman.plan(r.rules)
            return {**p, "items": p["items"][:500], "dry_run": True}
        return diskman.execute(r.plan_id, r.rules)
    except ValueError as ex:
        raise HTTPException(409, str(ex))


@app.get("/api/archive")
def api_archive(project: str | None = None):
    from . import archive
    return {"deliverables": archive.browse(project), "summary": archive.summary()}


class ArchiveReq(BaseModel):
    render: str
    kind: str = "master"
    release_local: bool | None = None


@app.post("/api/projects/{pid}/archive")
def api_archive_output(pid: str, r: ArchiveReq):
    _project(pid)
    return jobs.submit("archive", {"project": pid, "render": r.render, "kind": r.kind, "release_local": r.release_local}, pid)


class ArchiveRecReq(BaseModel):
    record: str


@app.post("/api/archive/retrieve")
def api_archive_retrieve(r: ArchiveRecReq):
    from . import archive
    rec = archive.get(r.record)
    if not rec:
        raise HTTPException(404, "unknown archive record")
    return jobs.submit("archive-retrieve", {"record": r.record}, rec.get("project_id") or "")


@app.post("/api/archive/retry")
def api_archive_retry():
    return jobs.submit("archive-recover", {})


@app.get("/api/archive/thumb/{record}")
def api_archive_thumb(record: str):
    from . import archive
    rows = [x for x in archive.browse() if x["id"] == record]
    if not rows or not rows[0]["thumbnail"]:
        raise HTTPException(404, "no thumbnail")
    return FileResponse(rows[0]["thumbnail"])
