"""NEXUS REMOTE STUDIO: the browser control plane (sub-agent D).

GET /studio serves the single-file UI (public page; it shows a login when the API answers 401). Every data route under
/api/v1/studio requires the owner (`security.require_owner`). The studio reads the shared SQLite ledgers directly
(remote_jobs, cloud_connections, artifacts, transfers, notifications) and calls the compute/storage routers from the
browser, so it shows real state whether or not those services are installed in this build.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from .. import config, db
from . import scheduler, security

UI = Path(__file__).resolve().parents[1] / "ui" / "studio.html"
ROUTERS = ("routes_core", "routes_security", "routes_compute", "routes_storage", "routes_studio")


def _start_scheduler() -> None:
    if os.environ.get("NEXUS_SCHEDULER_THREAD", "1") != "0":
        scheduler.default().start_thread()


router = APIRouter(tags=["studio"], on_startup=[_start_scheduler])
owner = [Depends(security.require_owner)]
P = "/api/v1/studio"


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    return db.query(sql, args)


def _missing() -> dict[str, str]:
    api = sys.modules.get("nexus.api")
    return dict(getattr(api, "CLOUD_MISSING", {}) or {}) if api else {}


# ---------------------------------------------------------------- page + session
@router.get("/studio", response_class=HTMLResponse, include_in_schema=False)
def studio_page():
    return HTMLResponse(UI.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store",
                                                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


@router.get(P + "/auth")
def auth_state(request: Request):
    """Public: tells the page whether to show the login. Reveals nothing but the mode, and (unlike require_owner) does
    not count as a failed authentication, so simply opening the page never trips the rate limit."""
    import hmac
    tok = security.api_token()
    if tok is None:
        ok = security.is_direct_loopback(request)
    else:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        # the session cookie is a signed value (security.session_valid), never the raw token
        ok = (bool(bearer) and hmac.compare_digest(bearer.encode(), tok.encode())) or \
            security.session_valid(request.cookies.get(security.COOKIE, ""), tok)
    return {"token_mode": security.api_token() is not None, "authenticated": ok}


@router.post(P + "/logout")
def logout(response: Response):
    security.close_session(response)
    return {"ok": True}


# ---------------------------------------------------------------- summary
def _day_start() -> float:
    now = time.time()
    return now - now % 86400  # UTC midnight, same day boundary as the scheduler


def _connections() -> list[dict]:
    out = []
    for r in _rows("SELECT id, provider, label, status, identity, scopes, updated, last_success, last_error FROM cloud_connections ORDER BY id"):
        try:
            r["scopes"] = json.loads(r["scopes"]) if r["scopes"] else []
        except ValueError:
            r["scopes"] = [r["scopes"]]
        out.append(r)
    return out


@router.get(P + "/summary", dependencies=owner)
def summary():
    t0 = _day_start()
    counts = {r["status"]: r["n"] for r in _rows("SELECT status, COUNT(*) n FROM remote_jobs GROUP BY status")}
    today = {r["status"]: r["n"] for r in _rows("SELECT status, COUNT(*) n FROM remote_jobs WHERE COALESCE(finished, created) >= ? "
                                                "GROUP BY status", (t0,))}
    recent = _rows("SELECT id, task, status, provider, connection_id, attempts, priority, created, started, finished, error "
                   "FROM remote_jobs ORDER BY created DESC LIMIT 10")
    art = _rows("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM artifacts")[0]
    xfer = {r["status"]: r["n"] for r in _rows("SELECT status, COUNT(*) n FROM transfers GROUP BY status")}
    import shutil
    du = shutil.disk_usage(config.HOME)
    snap = scheduler.default().snapshot()
    snap.pop("slots", None)
    snap.pop("config", None)
    missing = _missing()
    active = sum(v for k, v in counts.items() if k not in scheduler.TERMINAL)
    return {"jobs": {"by_status": counts, "today": today, "active": active,
                     "running": sum(v for k, v in counts.items() if k in ("running", "dispatched")),
                     "queued": counts.get("queued", 0), "failed_today": sum(v for k, v in today.items() if k in scheduler.TERMINAL_FAIL),
                     "succeeded_today": sum(v for k, v in today.items() if k in scheduler.TERMINAL_OK), "recent": recent},
            "providers": _connections(),
            "storage": {"artifacts": art["n"], "artifact_bytes": art["b"], "transfers": xfer,
                        "disk": {"total": du.total, "used": du.used, "free": du.free, "path": "NEXUS_HOME"}},
            "notifications_unread": _rows("SELECT COUNT(*) n FROM notifications WHERE read = 0")[0]["n"],
            "scheduler": snap,
            "services": {"compute": "routes_compute" not in missing, "storage": "routes_storage" not in missing,
                         "security": "routes_security" not in missing}}


# ---------------------------------------------------------------- scheduler
@router.get(P + "/scheduler", dependencies=owner)
def get_scheduler():
    return scheduler.default().snapshot()


@router.put(P + "/scheduler", dependencies=owner)
def put_scheduler(patch: dict):
    try:
        scheduler.set_config(patch)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return scheduler.default().snapshot()


ACTIONS = {"start": "start", "pause": "pause", "resume": "resume", "stop-after-current": "stop_after_current", "stop": "stop",
           "tick": "tick"}


@router.post(P + "/scheduler/{action}", dependencies=owner)
def scheduler_action(action: str):
    if action not in ACTIONS:
        raise HTTPException(404, f"unknown action; use one of {sorted(ACTIONS)}")
    s = scheduler.default()
    getattr(s, ACTIONS[action])()
    if action in ("start", "resume"):
        s.tick()
        if os.environ.get("NEXUS_SCHEDULER_THREAD", "1") != "0":
            s.start_thread()
    return s.snapshot()


# ---------------------------------------------------------------- resources
@router.get(P + "/resources", dependencies=owner)
def resources():
    import psutil
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(config.HOME))
    proc = psutil.Process()
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None
    remote = []
    for r in _rows("SELECT id, task, status, provider, connection_id, started, finished, result FROM remote_jobs "
                   "WHERE result IS NOT NULL ORDER BY COALESCE(finished, created) DESC LIMIT 12"):
        try:
            res = json.loads(r.pop("result") or "{}")
        except ValueError:
            res = {}
        remote.append({**r, "accelerator": res.get("accelerator"), "duration_s": res.get("duration_s"), "metrics": res.get("metrics") or {}})
    return {"host": {"cpu_percent": psutil.cpu_percent(interval=0.2), "cpus": psutil.cpu_count(), "load_avg": load,
                     "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
                     "disk": {"total": du.total, "used": du.used, "free": du.free, "percent": du.percent},
                     "process_rss": proc.memory_info().rss, "uptime_s": round(time.time() - psutil.boot_time())},
            "remote_workers": remote, "measured_at": time.time()}


# ---------------------------------------------------------------- jobs / checkpoints / artifacts (shared ledgers, read-only)
@router.get(P + "/jobs", dependencies=owner)
def remote_jobs(limit: int = 50):
    return _rows("SELECT id, task, owner_account, status, priority, provider, connection_id, attempts, max_retries, error, created, "
                 "started, finished, heartbeat FROM remote_jobs ORDER BY created DESC LIMIT ?", (min(limit, 500),))


@router.get(P + "/checkpoints", dependencies=owner)
def checkpoints(job_id: str | None = None, limit: int = 100):
    q = "SELECT job_id, seq, step, data, created FROM remote_checkpoints" + (" WHERE job_id = ?" if job_id else "") + " ORDER BY created DESC LIMIT ?"
    rows = _rows(q, (job_id, min(limit, 500)) if job_id else (min(limit, 500),))
    for r in rows:
        try:
            r["data"] = json.loads(r["data"] or "{}")
        except ValueError:
            pass
    return rows


def _manifest(row: dict) -> dict:
    try:
        return json.loads(row.get("manifest") or "{}")
    except ValueError:
        return {}


DRIVE_PROVIDERS = {"gdrive", "google_drive", "drive"}


def _view(m: dict, art_id: str) -> dict:
    """How a browser may open this artifact: a local stream (Range) or the provider's own viewer."""
    for loc in m.get("locations", []):
        if loc.get("provider") == "local":
            return {"kind": "stream", "url": f"{P}/artifacts/{art_id}/stream"}
    for loc in m.get("locations", []):
        if loc.get("provider") in DRIVE_PROVIDERS or str(loc.get("connection_id", "")).startswith("drive_"):
            return {"kind": "drive", "url": f"https://drive.google.com/file/d/{loc.get('ref')}/view", "connection_id": loc.get("connection_id")}
    return {"kind": "none"}


@router.get(P + "/artifacts", dependencies=owner)
def list_artifacts(kind: str | None = None, limit: int = 100):
    q = "SELECT id, kind, name, sha256, size, project_id, job_id, manifest, created FROM artifacts" + (" WHERE kind = ?" if kind else "") + \
        " ORDER BY created DESC LIMIT ?"
    rows = _rows(q, (kind, min(limit, 500)) if kind else (min(limit, 500),))
    thumbs: dict[str, str] = {}
    for r in _rows("SELECT id, job_id, manifest FROM artifacts WHERE kind IN ('thumbnail','proxy') ORDER BY created DESC LIMIT 2000"):
        m = _manifest(r)
        if _view(m, r["id"])["kind"] != "stream":
            continue
        parent = (m.get("provenance") or {}).get("source_artifact") or (m.get("provenance") or {}).get("parent")
        if parent:
            thumbs.setdefault(f"a:{parent}", r["id"])
        if r["job_id"]:
            thumbs.setdefault(f"j:{r['job_id']}", r["id"])
    out = []
    for r in rows:
        m = _manifest(r)
        prev = r["id"] if r["kind"] in ("thumbnail", "proxy") else thumbs.get(f"a:{r['id']}") or thumbs.get(f"j:{r['job_id']}")
        prev_kind = None
        if prev:
            pm = r if prev == r["id"] else (_rows("SELECT manifest FROM artifacts WHERE id = ?", (prev,)) or [{}])[0]
            prev_kind = "image" if str(_manifest(pm).get("mime", "")).startswith("image/") else "video"
        out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "mime": m.get("mime"), "size": r["size"],
                    "project_id": r["project_id"], "job_id": r["job_id"], "created": r["created"],
                    "locations": [{k: loc.get(k) for k in ("provider", "connection_id", "path")} for loc in m.get("locations", [])],
                    "preview": {"id": prev, "url": f"{P}/artifacts/{prev}/stream", "type": prev_kind} if prev else None,
                    "open": _view(m, r["id"])})
    return out


def _local_path(art_id: str) -> tuple[Path, str]:
    rows = _rows("SELECT manifest FROM artifacts WHERE id = ?", (art_id,))
    if not rows:
        raise HTTPException(404, "artifact not found")
    m = _manifest(rows[0])
    for loc in m.get("locations", []):
        if loc.get("provider") != "local":
            continue
        root = config.HOME.resolve()
        p = Path(loc.get("ref") or "")
        p = (p if p.is_absolute() else root / p).resolve()
        if root != p and root not in p.parents:
            raise HTTPException(403, "artifact path is outside NEXUS_HOME")
        if p.is_file():
            return p, m.get("mime") or "application/octet-stream"
        raise HTTPException(410, "local copy is missing on this host")
    raise HTTPException(409, "artifact has no local copy; open it in its storage provider's viewer")


@router.get(P + "/artifacts/{art_id}/stream", dependencies=owner)
def stream_artifact(art_id: str):
    """Streams a LOCAL artifact inline with HTTP Range support (video seeking) - only on an explicit click in the UI."""
    p, mime = _local_path(art_id)
    return FileResponse(p, media_type=mime, content_disposition_type="inline", filename=p.name,
                        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- health / security / config / github
@router.get(P + "/health", dependencies=owner)
def health():
    missing = _missing()
    provider_jobs = {}
    for r in _rows("SELECT provider, connection_id, MAX(CASE WHEN status IN ('succeeded','done') THEN finished END) last_success, "
                   "MAX(CASE WHEN status IN ('failed','lost') THEN finished END) last_failure, COUNT(*) jobs "
                   "FROM remote_jobs WHERE provider IS NOT NULL GROUP BY provider, connection_id"):
        provider_jobs[f"{r['provider']}/{r['connection_id']}"] = r
    s = scheduler.default()
    return {"routers": {n: ("missing: " + missing[n]) if n in missing else "mounted" for n in ROUTERS},
            "connections": _connections(), "provider_jobs": list(provider_jobs.values()),
            "scheduler": {"thread_alive": bool(s._thread and s._thread.is_alive()), "status": scheduler.get_state()["status"],
                          "last_tick": scheduler.get_state()["last_tick"]},
            "db_schema": db.schema_version(), "checked_at": time.time()}


@router.get(P + "/security", dependencies=owner)
def security_info(request: Request):
    from .. import vault
    how = "loopback"
    if security.api_token():
        how = "bearer" if request.headers.get("authorization", "").lower().startswith("bearer ") else "cookie"
    try:
        names = vault.names()
    except Exception:  # noqa: BLE001 - vault not initialised yet
        names = []
    return {"mode": "token" if security.api_token() else "loopback", "authenticated_by": how,
            "cookie": {"name": "nexus_session", "httponly": True, "samesite": "strict", "secure": True, "max_age_h": 12},
            "secrets": [{"name": n} for n in names],
            "notes": ["Secret values are never returned by any API; the vault is encrypted at rest.",
                      "Disconnect a storage account from Connected accounts; that revokes and deletes its stored token."]}


@router.get(P + "/config", dependencies=owner)
def system_config():
    st = config.settings()
    return {"version": config.VERSION, "home": str(config.HOME), "db_schema": db.schema_version(), "port": st.get("port", config.DEFAULT_PORT),
            "embedded_worker": os.environ.get("NEXUS_EMBEDDED_WORKER", "1") != "0",
            "scheduler_thread": os.environ.get("NEXUS_SCHEDULER_THREAD", "1") != "0",
            "token_mode": security.api_token() is not None, "routers_missing": sorted(_missing()),
            "python": sys.version.split()[0], "platform": sys.platform, "scheduler": scheduler.get_config()}


_gh: dict = {}


def _sanitize(url: str) -> str:
    if "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]:
        scheme, rest = url.split("://", 1)
        return scheme + "://" + rest.split("@", 1)[1]
    return url


@router.get(P + "/github", dependencies=owner)
def github_status(refresh: bool = False):
    """Read-only: is the project's Git remote reachable? Never returns credentials."""
    if _gh and not refresh and time.time() - _gh["checked_at"] < 300:
        return _gh
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    out = {"checked_at": time.time(), "remote": None, "reachable": False, "branch": None, "detail": ""}
    try:
        r = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5, env=env)
        out["remote"] = _sanitize(r.stdout.strip()) or None
        b = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5, env=env)
        out["branch"] = b.stdout.strip() or None
        if out["remote"]:
            ls = subprocess.run(["git", "-C", str(root), "ls-remote", "--exit-code", "origin", "HEAD"], capture_output=True, text=True,
                                timeout=12, env=env)
            out["reachable"] = ls.returncode == 0
            from ..vault import redact
            err = ls.stderr.strip().splitlines()[-1] if ls.stderr.strip() else f"git exit code {ls.returncode}"
            out["detail"] = "reachable" if ls.returncode == 0 else redact(_sanitize(err))[:300]
        else:
            out["detail"] = "no 'origin' remote configured"
    except (OSError, subprocess.TimeoutExpired) as e:
        out["detail"] = f"git check failed: {type(e).__name__}"
    _gh.clear()
    _gh.update(out)
    return out
