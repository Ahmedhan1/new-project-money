"""Owner-authorised pull workers: "user_gpu" (the owner's own GPU PC) and "colab" (a notebook the owner opens).

Protocol (HTTPS, per-worker token; the owner token never leaves the server):
    owner  POST /api/v1/compute/workers/register {kind, label}  -> {worker_id, token (shown ONCE)}
    worker GET  /api/v1/compute/workers/next     (Authorization: Bearer <worker_id>.<token>)
           -> 204 nothing to do | 200 {job_id, ref, bundle, script}
    worker runs `python script --bundle bundle.json --out out/`, then
           POST /api/v1/compute/workers/{worker_id}/result  multipart: result=<result.json text>, files=<artifacts/*, checkpoints/*>
The dispatcher then verifies every SHA-256 exactly as for Kaggle. Only a SHA-256 of each token is stored (in the vault,
secret name "worker-<id>"), compared in constant time. Revoking = deleting that secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import time
from pathlib import Path

from ... import db, vault
from ...contracts import ProviderHealth, RemoteWorkerCapability
from .. import worker_kit
from .base import Provider, RemoteStatus, compute_dir, conn_state, save_conn

ONLINE_S = 180          # a worker that polled within this window counts as online
KINDS = ("user_gpu", "colab")
SAFE = re.compile(r"^(artifacts|checkpoints)/[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_worker(kind: str, label: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    wid = f"wrk-{secrets.token_hex(6)}"
    token = secrets.token_urlsafe(32)
    vault.put(f"worker-{wid}", _hash(token), {"kind": kind, "label": label[:80], "created": time.time()})
    save_conn(wid, kind, status="registered", label=label[:80], kind=kind)
    return {"worker_id": wid, "kind": kind, "token": f"{wid}.{token}",
            "note": "shown once; the server keeps only its SHA-256. Give it to the worker as NEXUS_WORKER_TOKEN."}


def workers() -> list[dict]:
    out = []
    for n in vault.names():
        if n.startswith("worker-"):
            wid = n[len("worker-"):]
            m = vault.meta(n) or {}
            st = conn_state(wid)
            out.append({"worker_id": wid, "kind": m.get("kind"), "label": m.get("label"), "created": m.get("created"),
                        "last_seen": st.get("last_seen"), "online": bool(st.get("last_seen") and time.time() - st["last_seen"] < ONLINE_S),
                        "hardware": st.get("hardware")})
    return out


def authenticate(bearer: str) -> dict | None:
    """`<worker_id>.<token>` -> worker record, or None. Constant-time comparison of SHA-256 digests."""
    if not bearer or "." not in bearer:
        return None
    wid, tok = bearer.split(".", 1)
    if not re.fullmatch(r"wrk-[0-9a-f]{12}", wid):
        return None
    stored = vault.get(f"worker-{wid}")
    if not stored or not hmac.compare_digest(stored.encode(), _hash(tok).encode()):
        return None
    m = vault.meta(f"worker-{wid}") or {}
    return {"worker_id": wid, "kind": m.get("kind")}


def revoke(wid: str) -> bool:
    return vault.delete(f"worker-{wid}")


def touch(wid: str, kind: str, hardware: dict | None = None) -> None:
    data = {"last_seen": time.time()}
    if hardware:
        data["hardware"] = hardware
    save_conn(wid, kind, status="online", **data)


def claim_next(worker: dict) -> dict | None:
    """Give the oldest highest-priority attempt waiting for this worker kind to this worker (atomic)."""
    kind = worker["kind"]
    for r in db.query("SELECT id, remote_ref FROM remote_jobs WHERE provider=? AND status='running' ORDER BY priority, created", (kind,)):
        d = compute_dir("pull", r["remote_ref"])
        claim = d / "claimed.json"
        try:
            with open(claim, "x", encoding="utf-8") as f:
                json.dump({"worker_id": worker["worker_id"], "at": time.time()}, f)
        except FileExistsError:
            continue
        bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
        return {"job_id": r["id"], "ref": r["remote_ref"], "bundle": bundle, "script": worker_kit.build_script(None),
                "worker_version": worker_kit.worker_version()}
    return None


def accept_result(worker: dict, ref: str, result_text: str, files: list[tuple[str, object]]) -> dict:
    """Store an uploaded attempt (files: (relative name, binary file-like)). Verification happens in the dispatcher."""
    d = compute_dir("pull", ref)
    claim = d / "claimed.json"
    if not claim.exists() or json.loads(claim.read_text(encoding="utf-8")).get("worker_id") != worker["worker_id"]:
        raise PermissionError("this attempt is not claimed by this worker")
    res = json.loads(result_text)
    out = d / "out"
    out.mkdir(exist_ok=True)
    for name, fh in files:
        if not SAFE.match(name):
            raise ValueError(f"refused upload name {name!r}")
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            shutil.copyfileobj(fh, f, 1 << 20)
    (out / "result.json").write_text(json.dumps(res), encoding="utf-8")
    return {"stored": len(files), "status": res.get("status")}


class PullProvider(Provider):
    mode = "pull"

    def __init__(self, kind: str):
        if kind not in KINDS:
            raise ValueError(kind)
        self.provider = kind
        super().__init__(kind)

    def online(self) -> list[dict]:
        return [w for w in workers() if w["kind"] == self.provider and w["online"]]

    def declared_capability(self) -> RemoteWorkerCapability:
        on = self.online()
        hw = (on[0].get("hardware") if on else None) or {}
        g = (hw.get("gpus") or [None])[0]
        return RemoteWorkerCapability(provider=self.provider, connection_id=self.connection_id, accelerator=g["name"] if g else None,
                                      vram_gb=g["vram_gb"] if g else None, ram_gb=hw.get("ram_total_gb"), cpus=hw.get("cpus"),
                                      frameworks=[x for x in ("torch", "ffmpeg") if hw.get(x)], internet=True, measured=bool(hw),
                                      measured_at=on[0]["last_seen"] if on and hw else None)

    def capability(self) -> RemoteWorkerCapability:
        return self.declared_capability()

    def health(self) -> ProviderHealth:
        regs = [w for w in workers() if w["kind"] == self.provider]
        on = [w for w in regs if w["online"]]
        if not regs:
            return ProviderHealth(provider=self.provider, connection_id=self.connection_id, status="not_configured",
                                  detail="no worker registered (POST /api/v1/compute/workers/register)")
        if not on:
            return ProviderHealth(provider=self.provider, connection_id=self.connection_id, status="unavailable",
                                  detail=f"{len(regs)} registered worker(s), none polled in the last {ONLINE_S}s")
        return ProviderHealth(provider=self.provider, connection_id=self.connection_id, status="ok", detail=f"{len(on)} worker(s) online",
                              last_success=max(w["last_seen"] for w in on))

    def submit(self, job_id: str, attempt: int, bundle: dict, gpu: bool, timeout_s: float) -> str:
        ref = f"{job_id}-a{attempt}"
        d = compute_dir("pull", ref)
        (d / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        (d / "submitted.json").write_text(json.dumps({"at": time.time(), "timeout_s": timeout_s}), encoding="utf-8")
        return ref

    def poll(self, ref: str) -> RemoteStatus:
        d = compute_dir("pull", ref)
        if (d / "out" / "result.json").exists():
            return RemoteStatus("complete")
        if (d / "claimed.json").exists():
            c = json.loads((d / "claimed.json").read_text(encoding="utf-8"))
            sub = json.loads((d / "submitted.json").read_text(encoding="utf-8"))
            if time.time() - c["at"] > sub["timeout_s"] + 600:
                return RemoteStatus("lost", f"worker {c['worker_id']} claimed the job but sent no result in time")
            return RemoteStatus("running", f"claimed by {c['worker_id']}")
        return RemoteStatus("queued", "waiting for an owner worker to poll")

    def fetch(self, ref: str, dest: Path) -> Path:
        src = compute_dir("pull", ref) / "out"
        if src.exists():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        return dest

    def cancel(self, ref: str) -> str:
        d = compute_dir("pull", ref)
        if not (d / "claimed.json").exists():
            (d / "claimed.json").write_text(json.dumps({"worker_id": "cancelled", "at": time.time()}), encoding="utf-8")
            return "not yet claimed: withdrawn, no worker will receive it"
        return "already claimed by a worker: its upload will be ignored; no further steps are started"


def colab_notebook(api_base: str, worker_id: str) -> dict:
    """A Colab notebook (nbformat 4) running the pull loop. It contains no token: the owner enters it at run time
    (Colab Secrets 'NEXUS_WORKER_TOKEN' or a getpass prompt)."""
    src = worker_kit.PULL_WORKER.read_text(encoding="utf-8")
    cells = [
        ("markdown", "# NEXUS Colab worker\nRuns NEXUS jobs on this Colab runtime (Runtime > Change runtime type > GPU). "
                     "It polls your NEXUS server over HTTPS with a per-worker token, runs the job, uploads the results. "
                     "Stop it any time (Runtime > Interrupt)."),
        ("code", "import os, getpass\ntry:\n    from google.colab import userdata\n    os.environ['NEXUS_WORKER_TOKEN'] = userdata.get('NEXUS_WORKER_TOKEN')\n"
                 "except Exception:\n    os.environ['NEXUS_WORKER_TOKEN'] = getpass.getpass('NEXUS worker token: ')"),
        ("code", "PULL_WORKER = " + json.dumps(src) + "\nopen('pull_worker.py', 'w').write(PULL_WORKER)"),
        ("code", f"!python pull_worker.py --api {json.dumps(api_base)} --kind colab"),
    ]
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {"accelerator": "GPU", "kernelspec": {"name": "python3", "display_name": "Python 3"},
                                                             "nexus_worker_id": worker_id},
            "cells": [{"cell_type": t, "metadata": {}, "source": s, **({"outputs": [], "execution_count": None} if t == "code" else {})}
                      for t, s in cells]}
