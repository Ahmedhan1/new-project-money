"""/api/v1/storage: storage connections (local + Google Drive A/B/C), OAuth flows, transfers, ingestion, artifacts.

Every route requires the owner (security.require_owner) EXCEPT GET /oauth/callback: Google redirects the owner's
browser there cross-site, so the SameSite=Strict session cookie is not sent. That request is authorised instead by the
`state` value, which only an owner-authenticated call to /connections/{id}/oauth/start can create, which is random
(256 bit), single-use, bound to one connection id and expires after 10 minutes. Tokens are never returned by any route;
identity e-mails are returned only on owner-only routes.
"""
from __future__ import annotations

import os
import uuid
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..contracts import MediaIngestionJob, MediaSource
from ..pathsafe import is_within
from . import ingest, security
from .storage import artifacts, connections, get_provider, oauth, provider_infos, transfers
from .storage.base import NotConfigured, QuotaExceeded, ReauthRequired, StorageError

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])
owner = [Depends(security.require_owner)]
MAX_UPLOAD = int(os.environ.get("NEXUS_MAX_UPLOAD_BYTES", str(4 * 2 ** 30)))


def _err(e: Exception) -> HTTPException:
    from ..vault import redact
    msg = redact(str(e))
    if isinstance(e, KeyError):
        return HTTPException(404, msg.strip("'\""))
    if isinstance(e, oauth.OAuthError):
        return HTTPException(400, {"error": e.code, "detail": msg})
    if isinstance(e, NotConfigured):
        return HTTPException(409, {"error": "not_configured", "detail": msg})
    if isinstance(e, ReauthRequired):
        return HTTPException(409, {"error": "reauth_required", "detail": msg})
    if isinstance(e, artifacts.StorageBlocked):
        return HTTPException(403, {"error": "blocked", "detail": msg})
    if isinstance(e, QuotaExceeded):
        return HTTPException(507, {"error": "quota_exceeded", "detail": msg})
    return HTTPException(502, {"error": "storage_error", "detail": msg})


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except HTTPException:
        raise
    except (KeyError, StorageError, oauth.OAuthError) as e:
        raise _err(e) from None
    except Exception as e:  # costguard and others: never leak internals beyond the redacted message
        raise _err(StorageError(f"{type(e).__name__}: {e}")) from None


def _cid(cid: str) -> str:
    try:
        connections.provider_of(cid)
    except KeyError as e:
        raise _err(e) from None
    return cid


def _drive(cid: str) -> str:
    if cid not in connections.DRIVE_IDS:
        raise HTTPException(400, f"{cid} is not a Google Drive connection (drive_a, drive_b, drive_c)")
    return cid


# ---------------------------------------------------------------- connections
@router.get("/providers", dependencies=owner)
def list_providers():
    return [i.model_dump() for i in provider_infos()]


@router.get("/connections", dependencies=owner)
def list_connections():
    return [connections.public(c) for c in connections.all_storage()]


@router.post("/connections/{cid}/device/start", dependencies=owner)
def device_start(cid: str):
    r = _guard(oauth.device_start, _drive(cid))
    return {**r, "verification_uri": r["verification_url"]}


@router.post("/connections/{cid}/device/poll", dependencies=owner)
def device_poll(cid: str):
    return _guard(oauth.device_poll, _drive(cid))


def _redirect_uri(request: Request) -> str:
    base = os.environ.get("NEXUS_PUBLIC_URL") or str(request.base_url)
    return base.rstrip("/") + "/api/v1/storage/oauth/callback"


@router.get("/connections/{cid}/oauth/start", dependencies=owner)
def oauth_start(cid: str, request: Request, redirect: bool = False):
    r = _guard(oauth.web_start, _drive(cid), _redirect_uri(request))
    return RedirectResponse(r["auth_url"], 302) if redirect else r


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(state: str = "", code: str | None = None, error: str | None = None):
    try:
        c = oauth.web_finish(state, code, error)
        body = f"<h1>Connected</h1><p>{escape(c['id'])} is connected as {escape(c['identity'] or 'unknown')}. You can close this tab.</p>"
        status = 200
    except (oauth.OAuthError, StorageError) as e:
        body = f"<h1>Not connected</h1><p>{escape(str(e))}</p>"
        status = 400
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>NEXUS storage</title>{body}", status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/connections/{cid}/disconnect", dependencies=owner)
def disconnect(cid: str):
    return _guard(oauth.disconnect, _drive(cid))


@router.get("/connections/{cid}/health", dependencies=owner)
def health(cid: str):
    return _guard(lambda: get_provider(_cid(cid)).health().model_dump())


@router.get("/connections/{cid}/quota", dependencies=owner)
def quota(cid: str):
    return _guard(lambda: get_provider(_cid(cid)).quota().model_dump())


@router.post("/connections/{cid}/folders", dependencies=owner)
def folders(cid: str):
    return {"connection_id": cid, "folders": _guard(artifacts.create_structure, _cid(cid))}


def _web_view(cid: str, f) -> str | None:
    if connections.provider_of(cid) == "gdrive" and not f.is_folder:
        return f"https://drive.google.com/file/d/{f.ref}/view"
    if connections.provider_of(cid) == "gdrive":
        return f"https://drive.google.com/drive/folders/{f.ref}"
    return None


@router.get("/connections/{cid}/files", dependencies=owner)
def files(cid: str, path: str = "NEXUS"):
    items = _guard(lambda: get_provider(_cid(cid)).list(path))
    return [{"name": f.name, "ref": f.ref, "path": f.path, "mime": f.mime or ("folder" if f.is_folder else ""),
             "size": f.size, "modified": f.modified, "is_folder": f.is_folder, "nexus_created": f.app_created,
             "sha256": f.sha256, "web_view": _web_view(cid, f)} for f in items]


# ---------------------------------------------------------------- rules
class Rules(BaseModel):
    authorized_remote: list[str] | None = None
    order: list[str] | None = None
    kinds: dict[str, list[str]] | None = None
    reserve_bytes: int | None = None
    allow_unknown_quota: bool | None = None


@router.get("/rules", dependencies=owner)
def get_rules():
    return artifacts.rules()


@router.put("/rules", dependencies=owner)
def put_rules(r: Rules):
    return _guard(artifacts.save_rules, r.model_dump(exclude_none=True))


# ---------------------------------------------------------------- uploads / transfers
async def _receive(file: UploadFile) -> str:
    name = os.path.basename(file.filename or "upload.bin").replace("__", "_") or "upload.bin"
    dest = ingest.upload_dir() / f"{uuid.uuid4().hex[:12]}__{name}"
    n = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            n += len(chunk)
            if n > MAX_UPLOAD:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"upload exceeds {MAX_UPLOAD} bytes")
            out.write(chunk)
    return str(dest)


@router.post("/uploads", dependencies=owner)
async def upload(file: Annotated[UploadFile | None, File()] = None, connection_id: Annotated[str, Form()] = "local",
                 folder: Annotated[str, Form()] = "", artifact_id: Annotated[str | None, Form()] = None,
                 kind: Annotated[str, Form()] = "other", project_id: Annotated[str | None, Form()] = None,
                 source_artifact: Annotated[str | None, Form()] = None):
    """Multipart: a new file (file + connection_id + folder), or copy an existing artifact (artifact_id) to
    connection_id. Uploads are resumable: on interruption the transfer id can be resumed with /transfers/{id}/resume."""
    _cid(connection_id)
    if artifact_id:
        m = _guard(artifacts.copy_to, artifact_id, connection_id)
        return {"artifact": m.model_dump()}
    if file is None:
        raise HTTPException(400, "send a file or an artifact_id")
    staged = await _receive(file)
    name = os.path.basename(staged).split("__", 1)[1]
    tid = None
    try:
        from .storage.base import safe_rel
        dest = safe_rel(f"{folder}/{name}") if folder else None
        prov = {"uploaded_by": "owner", **({"source_artifact": source_artifact} if source_artifact else {})}
        tid = "xfer_" + uuid.uuid4().hex[:10]
        m = _guard(artifacts.store_file, staged, kind=kind, name=name, connection_id=connection_id,
                   mime=file.content_type or "application/octet-stream", project_id=project_id, dest_path=dest,
                   provenance=prov, transfer_id=tid)
        os.unlink(staged)
        return {"artifact": m.model_dump(), "transfer_id": tid}
    except HTTPException:
        t = transfers.get(tid) if tid else None
        if t is None or t["status"] not in ("interrupted", "running"):
            if os.path.exists(staged):
                os.unlink(staged)
        raise


@router.get("/transfers", dependencies=owner)
def list_transfers(status: str | None = None):
    return [transfers.public(t) for t in transfers.list_all(status)]


@router.post("/transfers/{tid}/resume", dependencies=owner)
def resume_transfer(tid: str):
    t = transfers.get(tid)
    if t is None:
        raise HTTPException(404, "unknown transfer")
    m = _guard(artifacts.resume_transfer, tid)
    staged = t["local_path"]
    if is_within(staged, ingest.upload_dir()) and os.path.isfile(staged):
        os.unlink(staged)
    return {"transfer": transfers.public(transfers.get(tid)), "artifact": m.model_dump()}


# ---------------------------------------------------------------- ingestion + artifacts
class IngestReq(BaseModel):
    source: MediaSource
    destination: str | None = None
    project_id: str | None = None
    max_bytes: int | None = None


@router.post("/ingest", dependencies=owner)
def ingest_url(r: IngestReq):
    if r.destination:
        _cid(r.destination)
    if r.source.kind == "upload":
        raise HTTPException(400, "use POST /api/v1/storage/ingest/upload (multipart) for files")
    kw = {"max_bytes": r.max_bytes} if r.max_bytes else {}
    job = MediaIngestionJob(source=r.source, destination=r.destination, project_id=r.project_id, **kw)
    return ingest.run(job).model_dump()


@router.post("/ingest/upload", dependencies=owner)
async def ingest_upload(file: Annotated[UploadFile, File()], rights: Annotated[str, Form()],
                        licence: Annotated[str | None, Form()] = None, attribution: Annotated[str | None, Form()] = None,
                        destination: Annotated[str | None, Form()] = None, project_id: Annotated[str | None, Form()] = None):
    if destination:
        _cid(destination)
    staged = await _receive(file)
    try:
        src = MediaSource(kind="upload", ref=staged, rights=rights, licence=licence, attribution=attribution)
    except ValueError as e:
        os.unlink(staged)
        raise HTTPException(422, str(e)) from None
    job = ingest.run(MediaIngestionJob(source=src, destination=destination, project_id=project_id))
    if os.path.exists(staged):
        os.unlink(staged)
    return job.model_dump()


@router.get("/ingest/{iid}", dependencies=owner)
def ingest_status(iid: str):
    j = ingest.get(iid)
    if j is None:
        raise HTTPException(404, "unknown ingestion id")
    return j.model_dump()


@router.get("/artifacts", dependencies=owner)
def list_artifacts(project_id: str | None = None, kind: str | None = None, limit: int = 200):
    return [m.model_dump() for m in artifacts.list_artifacts(project_id, kind, min(limit, 1000))]


@router.get("/artifacts/{aid}", dependencies=owner)
def get_artifact(aid: str):
    m = artifacts.get(aid)
    if m is None:
        raise HTTPException(404, "unknown artifact")
    return m.model_dump()


class MoveReq(BaseModel):
    to: str
    delete_source: str | None = None


@router.post("/artifacts/{aid}/copy", dependencies=owner)
def copy_artifact(aid: str, r: MoveReq):
    return _guard(artifacts.copy_to, aid, _cid(r.to), delete_source=r.delete_source).model_dump()


class CleanupReq(BaseModel):
    dry_run: bool = True
    plan_id: str | None = None


@router.post("/cleanup", dependencies=owner)
def cleanup(r: CleanupReq):
    return _guard(artifacts.cleanup, r.dry_run, r.plan_id)
