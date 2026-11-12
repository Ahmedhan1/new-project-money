"""Provider abstraction for remote compute (contracts v1: RemoteWorkerCapability, ProviderHealth, WorkerExecutionResult).

A provider runs one job *attempt* and hands back a directory holding the worker's result.json, checkpoints/ and
artifacts/ (layout: nexus/cloud/worker_kit/nexus_worker.py). Two modes:

    push  the dispatcher starts the run itself (local subprocess, `kaggle kernels push`)
    pull  the attempt waits until an authorised worker the owner runs (user_gpu PC, Colab notebook) claims it

Per-connection state that is not secret (measured capability, pause flag, identity) lives in the cloud_connections
table; credentials live only in nexus.vault.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ... import config, db
from ...contracts import ProviderHealth, RemoteWorkerCapability


class ProviderError(RuntimeError):
    """The provider could not start / poll / fetch an attempt (message is already redacted)."""


class QuotaExhausted(ProviderError):
    """The provider reported a quota or usage limit. The dispatcher pauses it; it never works around the limit."""


@dataclass
class RemoteStatus:
    state: str          # queued | running | complete | error | cancelled | lost | unknown
    detail: str = ""


def compute_dir(*parts: str) -> Path:
    d = config.HOME.joinpath("compute", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def conn_state(cid: str) -> dict:
    rows = db.query("SELECT * FROM cloud_connections WHERE id=?", (cid,))
    if not rows:
        return {}
    r = rows[0]
    data = json.loads(r.get("data") or "{}")
    return {**data, "_identity": r.get("identity"), "_status": r.get("status"), "_last_success": r.get("last_success"),
            "_last_error": r.get("last_error")}


def save_conn(cid: str, provider: str, *, status: str | None = None, identity: str | None = None,
              last_success: float | None = None, last_error: str | None = None, **data) -> None:
    now = time.time()
    cur = db.query("SELECT data FROM cloud_connections WHERE id=?", (cid,))
    if not cur:
        db.execute("INSERT INTO cloud_connections(id,provider,label,status,identity,scopes,created,updated,last_success,last_error,data)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, provider, cid, status or "unknown", identity, "[]", now, now, last_success,
                                                     last_error, json.dumps(data, default=str)))
        return
    merged = {**json.loads(cur[0]["data"] or "{}"), **data}
    sets, args = ["updated=?", "data=?"], [now, json.dumps(merged, default=str)]
    for col, val in (("status", status), ("identity", identity), ("last_success", last_success), ("last_error", last_error)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    db.execute(f"UPDATE cloud_connections SET {', '.join(sets)} WHERE id=?", (*args, cid))


class Provider:
    provider: str = "other"     # RemoteWorkerCapability.provider value
    mode: str = "push"
    gpu_billing: bool = False   # never True for an eligible provider (zero spend)

    def __init__(self, connection_id: str):
        self.connection_id = connection_id

    # ---- description
    def capability(self) -> RemoteWorkerCapability:
        st = conn_state(self.connection_id).get("capability")
        if st:
            return RemoteWorkerCapability(**st)
        return self.declared_capability()

    def declared_capability(self) -> RemoteWorkerCapability:
        return RemoteWorkerCapability(provider=self.provider, connection_id=self.connection_id)

    def store_measured_capability(self, cap: RemoteWorkerCapability) -> None:
        save_conn(self.connection_id, self.provider, capability=cap.model_dump())

    def health(self) -> ProviderHealth:
        raise NotImplementedError

    def paused(self) -> tuple[bool, str]:
        st = conn_state(self.connection_id)
        until = st.get("paused_until") or 0
        if until and until > time.time():
            return True, st.get("paused_reason") or "paused"
        return False, ""

    def pause(self, reason: str, seconds: float) -> None:
        save_conn(self.connection_id, self.provider, status="quota_exhausted", paused_until=time.time() + seconds, paused_reason=reason[:500])

    def resume(self) -> None:
        save_conn(self.connection_id, self.provider, status="ok", paused_until=0, paused_reason="")

    def available(self) -> tuple[bool, str]:
        h = self.health()
        return h.status in ("ok", "degraded"), f"[{h.status}] {h.detail}".strip()

    # ---- one attempt
    def submit(self, job_id: str, attempt: int, bundle: dict, gpu: bool, timeout_s: float) -> str:
        raise NotImplementedError

    def poll(self, ref: str) -> RemoteStatus:
        raise NotImplementedError

    def fetch(self, ref: str, dest: Path) -> Path:
        raise NotImplementedError

    def cancel(self, ref: str) -> str:
        """Best effort; returns what actually happened (honest text shown to the owner)."""
        return "no cancel operation for this provider: polling stopped, no further steps are started"

    def log_tail(self, ref: str) -> str:
        return ""
