"""/api/v1/compute - remote compute (sub-agent B).

Owner routes use security.require_owner. The two pull-worker routes (GET /workers/next, POST /workers/{id}/result) are
called by the owner's own GPU PC or Colab notebook and authenticate with that worker's own token instead (a worker never
holds the owner token); failed worker authentications count toward the same per-client rate limit.
"""
from __future__ import annotations

import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from .. import vault
from ..contracts import ProductionJobManifest
from . import events, security
from .compute import dispatcher as dsp
from .compute import pull

router = APIRouter(prefix="/api/v1/compute", tags=["compute"])
owner = [Depends(security.require_owner)]


def _d() -> dsp.Dispatcher:
    return dsp.get()


def _public_row(r: dict, detail: bool = False) -> dict:
    out = {k: r[k] for k in ("id", "task", "owner_account", "project_id", "status", "priority", "provider", "connection_id",
                             "attempts", "max_retries", "created", "started", "finished", "idempotency_key", "cancel_requested")}
    out["error"] = vault.redact(r["error"]) if r.get("error") else None
    if r["provider"] == "kaggle" and r.get("remote_ref"):
        out["remote_ref"] = "<owner>/" + r["remote_ref"].split("/", 1)[-1]
    else:
        out["remote_ref"] = r.get("remote_ref")
    if detail:
        out["manifest"] = json.loads(r["manifest"])
        res = json.loads(r["result"]) if r.get("result") else {}
        out["result"] = json.loads(vault.redact(json.dumps(res, default=str)))
        out["log_tail"] = vault.redact(((res.get("final") or {}).get("log_tail")) or next(
            (a.get("log_tail", "") for a in reversed(res.get("attempts", [])) if a.get("log_tail")), ""))[-4000:]
        out["checkpoints"] = dsp.checkpoints(r["id"])
        out["artifacts"] = dsp.job_artifacts(r["id"])
        out["events"] = events.events(job_id=r["id"], limit=200)
    return out


@router.get("/providers", dependencies=owner)
def providers():
    out = []
    for name, p in _d().providers.items():
        try:
            h, cap = p.health(), p.capability()
            out.append({"name": name, "mode": p.mode, "health": json.loads(vault.redact(h.model_dump_json())), "capability": cap.model_dump()})
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "mode": p.mode, "error": vault.redact(str(e))[:300]})
    return out


@router.post("/providers/kaggle/verify", dependencies=owner)
def verify_kaggle():
    """Refresh identity and queue a GPU probe job on Kaggle; its measured capability is stored when it succeeds."""
    p = _d().providers.get("kaggle")
    if p is None:
        raise HTTPException(404, "kaggle provider not configured")
    ok, why = p.configured()
    if not ok:
        raise HTTPException(409, why)
    try:
        p.username(refresh=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, vault.redact(str(e))[:300]) from None
    m = ProductionJobManifest(task="probe", requires={"gpu": True, "providers": ["kaggle"]}, priority=1, max_retries=0,
                              max_runtime_s=900, idempotency_key=f"kaggle-verify-{int(time.time() // 600)}")
    row, created = _d().submit(m)
    return {"job": _public_row(row), "created": created, "detail": "probe queued; GET /api/v1/compute/jobs/{id} for the result"}


@router.post("/providers/kaggle/resume", dependencies=owner)
def resume_kaggle():
    """Owner lifts a quota pause (e.g. after the weekly reset shown on the Kaggle settings page)."""
    p = _d().providers.get("kaggle")
    if p is None:
        raise HTTPException(404, "kaggle provider not configured")
    p.resume()
    events.emit("scheduler.resumed", "Kaggle dispatch resumed by the owner", provider="kaggle")
    return {"ok": True}


@router.post("/jobs", dependencies=owner)
def submit_job(m: ProductionJobManifest, response: Response):
    try:
        row, created = _d().submit(m)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    response.status_code = 201 if created else 200
    return {"job": _public_row(row), "created": created}


@router.get("/jobs", dependencies=owner)
def list_jobs(status: str | None = None, limit: int = 100):
    from .. import db
    q = "SELECT * FROM remote_jobs" + (" WHERE status=?" if status else "") + " ORDER BY created DESC LIMIT ?"
    rows = db.query(q, (status, min(limit, 500)) if status else (min(limit, 500),))
    return [_public_row(r) for r in rows]


@router.get("/jobs/{job_id}", dependencies=owner)
def get_job(job_id: str):
    r = dsp._row(job_id)
    if r is None:
        raise HTTPException(404, "job not found")
    return _public_row(r, detail=True)


@router.post("/jobs/{job_id}/cancel", dependencies=owner)
def cancel_job(job_id: str):
    r = _d().cancel(job_id)
    if r is None:
        raise HTTPException(404, "job not found")
    return _public_row(r)


@router.post("/jobs/{job_id}/retry", dependencies=owner)
def retry_job(job_id: str):
    r = _d().retry(job_id)
    if r is None:
        raise HTTPException(404, "job not found")
    if r["status"] != "queued":
        raise HTTPException(409, f"only failed or cancelled jobs can be retried (status: {r['status']})")
    return _public_row(r)


@router.get("/quota", dependencies=owner)
def quota():
    """List of QuotaStatus/v1 objects - exactly the published contract (extra fields are forbidden by it)."""
    p = _d().providers.get("kaggle")
    return [p.quota().model_dump()] if p is not None else []


@router.get("/quota/details", dependencies=owner)
def quota_details():
    """Quota plus where the allowance figure comes from and the provider's pause state (not part of the contract)."""
    p = _d().providers.get("kaggle")
    if p is None:
        return []
    from .compute.kaggle import ALLOWANCE_SOURCE
    return [{"quota": p.quota().model_dump(), "allowance_source": ALLOWANCE_SOURCE, "paused": p.paused()[0],
             "paused_reason": p.paused()[1]}]


# ---------------------------------------------------------------- pull workers (user_gpu / colab)
class RegisterReq(BaseModel):
    kind: str
    label: str = ""


@router.post("/workers/register", dependencies=owner)
def register_worker(r: RegisterReq):
    try:
        return pull.register_worker(r.kind, r.label)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/workers", dependencies=owner)
def list_workers():
    return pull.workers()


@router.delete("/workers/{worker_id}", dependencies=owner)
def revoke_worker(worker_id: str):
    return {"revoked": pull.revoke(worker_id)}


@router.get("/workers/{worker_id}/notebook", dependencies=owner)
def colab_notebook(worker_id: str, api_base: str):
    """Colab notebook running the pull loop for this worker. Contains no token."""
    if not api_base.startswith("https://"):
        raise HTTPException(422, "api_base must be the https:// address of this NEXUS server")
    return pull.colab_notebook(api_base, worker_id)


async def require_worker(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    w = pull.authenticate(auth[7:].strip() if auth.lower().startswith("bearer ") else "")
    who = security._client(request)
    q = security._fail[who]
    while q and q[0] < time.time() - security.WINDOW_S:
        q.popleft()
    if len(q) >= security.MAX_FAILS:
        raise HTTPException(429, "too many failed authentications; wait a few minutes")
    if w is None:
        q.append(time.time())
        raise HTTPException(401, "worker authentication required")
    return w


Worker = Annotated[dict, Depends(require_worker)]


@router.get("/workers/next")
def worker_next(w: Worker, hardware: str | None = None):
    hw = None
    if hardware:
        try:
            hw = json.loads(hardware)
        except ValueError:
            hw = None
    pull.touch(w["worker_id"], w["kind"], hw if isinstance(hw, dict) else None)
    job = pull.claim_next(w)
    if job is None:
        return Response(status_code=204)
    events.emit("job.progress", f"claimed by {w['kind']} worker {w['worker_id']}", job["job_id"], state="running", worker=w["worker_id"])
    return job


@router.post("/workers/{worker_id}/result")
def worker_result(worker_id: str, w: Worker, result: Annotated[str, Form()], ref: Annotated[str, Form()],
                  files: Annotated[list[UploadFile] | None, File()] = None):
    if worker_id != w["worker_id"]:
        raise HTTPException(403, "token does not belong to this worker")
    pull.touch(w["worker_id"], w["kind"])
    try:
        out = pull.accept_result(w, ref, result, [(f.filename or "", f.file) for f in (files or [])])
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return out
