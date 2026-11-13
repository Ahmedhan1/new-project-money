"""Remote job dispatcher: ProductionJobManifest -> provider attempt -> verified artifacts (contracts v1).

Lifecycle of a row in `remote_jobs` (status column):
    queued --dispatch--> dispatched --poll--> running --complete/error--> (retrieve + verify) --> succeeded
       ^                                                                          |  lost / failed (retries left)
       +-------------------------- re-queued with the last JobCheckpoint ---------+
    any state --cancel--> cancelled          failed when retries are exhausted

* Idempotency: a manifest whose idempotency_key already exists returns the existing job (unique index in the DB).
* Ordering: priority ascending (0 = most urgent), then age.
* Provider choice: `requires.providers` (exclusive allow-list) > `preferred_providers` (order) > task default order;
  a provider is skipped when not configured / unhealthy / paused for quota / lacking the GPU or VRAM the job needs.
  Kaggle paused -> the job fails over to a *different provider type* (local CPU if the task allows CPU, or an owner
  worker) or waits; there is exactly one Kaggle account and no rotation.
* Verification: every artifact listed in result.json must exist with the same size and SHA-256 before anything is
  recorded; one mismatch fails the attempt and nothing from it is recorded.
* Resume: checkpoints/*.json from every attempt are stored in remote_checkpoints; a re-dispatch embeds the last one so
  the worker skips finished steps. Artifacts from a lost attempt that verified are kept.
* Kaggle GPU seconds are accounted (dispatch -> completion observed, an upper bound) in metrics 'kaggle_gpu_seconds'.
"""
from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from pydantic import ValidationError

from ... import config, db, vault
from ...contracts import ArtifactLocation, ArtifactManifest, JobCheckpoint, ProductionJobManifest, RemoteWorkerCapability, WorkerExecutionResult
from .. import worker_kit
from ..events import emit
from . import tasks as task_registry
from .base import Provider, ProviderError, QuotaExhausted, compute_dir

ACTIVE = ("running",)            # persisted statuses: queued | running | succeeded | failed | cancelled | quota_exhausted
WAITING = ("queued", "quota_exhausted")
TERMINAL = ("succeeded", "failed", "cancelled")
QUOTA_PAUSE_S = 6 * 3600         # after a quota message Kaggle is not tried again for 6 h (or until the owner resumes it)
MAX_ACTIVE = {"kaggle": 1, "local": 1}
LOST_GRACE_S = 900               # beyond max_runtime_s before an attempt that never finishes is declared lost
MAX_UNKNOWN_POLLS = 20


def default_providers() -> dict[str, Provider]:
    from .kaggle import KaggleProvider
    from .local import LocalProvider
    from .pull import PullProvider
    return {"kaggle": KaggleProvider(), "local": LocalProvider(), "user_gpu": PullProvider("user_gpu"), "colab": PullProvider("colab")}


def _row(job_id: str) -> dict | None:
    rows = db.query("SELECT * FROM remote_jobs WHERE id=?", (job_id,))
    return rows[0] if rows else None


def _set(job_id: str, **kv) -> None:
    cols = ", ".join(f"{k}=?" for k in kv)
    db.execute(f"UPDATE remote_jobs SET {cols} WHERE id=?", (*kv.values(), job_id))


def last_checkpoint(job_id: str) -> JobCheckpoint | None:
    rows = db.query("SELECT data FROM remote_checkpoints WHERE job_id=? ORDER BY seq DESC LIMIT 1", (job_id,))
    return JobCheckpoint(**json.loads(rows[0]["data"])) if rows else None


def checkpoints(job_id: str) -> list[dict]:
    return [json.loads(r["data"]) for r in db.query("SELECT data FROM remote_checkpoints WHERE job_id=? ORDER BY seq", (job_id,))]


def job_artifacts(job_id: str) -> list[dict]:
    return [json.loads(r["manifest"]) for r in db.query("SELECT manifest FROM artifacts WHERE job_id=? ORDER BY created", (job_id,))]


def artifact_path(a: dict | ArtifactManifest) -> Path | None:
    a = a if isinstance(a, ArtifactManifest) else ArtifactManifest(**a)
    for loc in a.locations:
        if loc.provider == "local":
            p = config.HOME / loc.ref
            if p.exists():
                return p
    return None


def register_local_file(path, kind: str = "source", project_id: str | None = None, **provenance) -> ArtifactManifest:
    """Copy a local file into NEXUS storage and record it as an artifact (e.g. an input for a remote job)."""
    src = Path(path)
    h = _sha(src)
    d = compute_dir("artifacts", "imported")
    dst = d / f"{h[:12]}_{src.name}"
    shutil.copyfile(src, dst)
    mime = "video/mp4" if src.suffix.lower() == ".mp4" else "application/octet-stream"
    a = ArtifactManifest(kind=kind, name=src.name, mime=mime, size=dst.stat().st_size, sha256=h, project_id=project_id,
                         locations=[ArtifactLocation(provider="local", connection_id="local", ref=str(dst.relative_to(config.HOME)),
                                                     path=str(dst.relative_to(config.HOME)), verified_at=time.time())],
                         provenance=provenance)
    db.execute("INSERT INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
               (a.artifact_id, a.kind, a.name, a.sha256, a.size, project_id, None, a.model_dump_json(), a.created_at))
    return a


def _sha(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


DELIVER_KINDS = {"master", "short", "localized", "proxy", "thumbnail", "audio", "subtitle", "analysis", "project_file", "other"}


def deliver_outputs(job_id: str, dest: str, artifacts: list) -> dict:
    """Copy a succeeded job's verified outputs to the storage connection named in ProductionJobManifest.output_destination
    (download -> upload -> SHA-256 re-check, nexus.cloud.storage.artifacts.copy_to). Logs and checkpoints stay local.
    A delivery failure is reported, never hidden, and does not undo the compute result."""
    from ..storage import artifacts as store
    out = {"destination": dest, "delivered": [], "failed": []}
    for a in artifacts:
        aid = a.get("artifact_id") if isinstance(a, dict) else getattr(a, "artifact_id", None)
        kind = a.get("kind") if isinstance(a, dict) else getattr(a, "kind", None)
        if not aid or kind not in DELIVER_KINDS:
            continue
        try:
            m = store.copy_to(aid, dest)
            loc = next(lo for lo in m.locations if lo.connection_id == dest)
            out["delivered"].append({"artifact_id": aid, "name": m.name, "size": m.size, "sha256": m.sha256, "path": loc.path,
                                     "ref": loc.ref})
            emit("storage.uploaded", f"{m.name} -> {dest}:{loc.path} (sha256 verified)", job_id, artifact_id=aid, destination=dest)
        except Exception as e:  # noqa: BLE001 - reported per artifact; the owner can retry with POST /storage/artifacts/{id}/copy
            out["failed"].append({"artifact_id": aid, "error": str(e)[:300]})
            emit("storage.failed", f"delivery of {aid} to {dest} failed: {e}"[:500], job_id, artifact_id=aid, destination=dest)
    return out


class Dispatcher:
    def __init__(self, providers: dict[str, Provider] | None = None, poll_min: float = 5.0, poll_max: float = 60.0):
        self.providers = providers if providers is not None else default_providers()
        self.poll_min, self.poll_max = poll_min, poll_max
        self._next_poll: dict[str, tuple[float, float]] = {}
        self._unknown: dict[str, int] = {}
        self._waiting: dict[str, str] = {}
        self._seen: dict[str, str] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ submission
    def submit(self, manifest: ProductionJobManifest) -> tuple[dict, bool]:
        """Store a job. Returns (row, created). An existing idempotency_key returns the existing job, created=False."""
        if manifest.idempotency_key:
            ex = db.query("SELECT * FROM remote_jobs WHERE idempotency_key=?", (manifest.idempotency_key,))
            if ex:
                return ex[0], False
        if task_registry.spec(manifest.task) is None:
            raise ValueError(f"unknown task '{manifest.task}' (registered: {sorted(task_registry.TASKS)})")
        # params and inputs travel to remote workers (a Kaggle kernel, a pull worker): a credential must never be among
        # them. Token-shaped strings and any value stored in the vault are refused; the message never echoes the value.
        from ... import vault
        blob = manifest.model_dump_json()
        if vault.redact(blob) != blob:
            raise ValueError("the job manifest contains a credential (token-shaped or a stored secret value); secrets never "
                             "travel in a job: workers get scoped access from the control plane instead")
        now = time.time()
        try:
            db.execute("INSERT INTO remote_jobs(id,task,owner_account,project_id,status,priority,provider,connection_id,remote_ref,attempts,"
                       "max_retries,manifest,result,error,created,started,finished,heartbeat,idempotency_key,cancel_requested)"
                       " VALUES(?,?,?,?,'queued',?,NULL,NULL,NULL,0,?,?,NULL,NULL,?,NULL,NULL,NULL,?,0)",
                       (manifest.job_id, manifest.task, manifest.owner_account, manifest.project_id, manifest.priority, manifest.max_retries,
                        manifest.model_dump_json(), now, manifest.idempotency_key))
        except sqlite3.IntegrityError:
            ex = db.query("SELECT * FROM remote_jobs WHERE idempotency_key=? OR id=?", (manifest.idempotency_key, manifest.job_id))
            if ex:
                return ex[0], False
            raise
        emit("job.submitted", f"{manifest.task} submitted (priority {manifest.priority})", manifest.job_id, task=manifest.task,
             owner_account=manifest.owner_account)
        return _row(manifest.job_id), True

    # ------------------------------------------------------------ provider choice
    def candidates(self, m: ProductionJobManifest) -> tuple[list[Provider], list[str]]:
        spec = task_registry.spec(m.task) or task_registry.TaskSpec()
        need_gpu = m.requires.get("gpu") is True or spec.gpu == "required"
        if spec.gpu == "required" or need_gpu:
            order = ["kaggle", "user_gpu", "colab", "local"]
        elif spec.gpu == "preferred":
            order = ["kaggle", "user_gpu", "colab", "local"]
        else:
            order = ["local", "user_gpu", "colab", "kaggle"]
        for p in reversed(m.preferred_providers):
            if p in order:
                order.remove(p)
            order.insert(0, p)
        allow = m.requires.get("providers")
        if allow:
            order = [p for p in order if p in allow]
        if spec.providers:
            order = [p for p in order if p in spec.providers]
        out, why = [], []
        for name in order:
            p = self.providers.get(name)
            if p is None:
                why.append(f"{name}: not available in this deployment")
                continue
            ok, detail = p.available()
            if not ok and not (p.mode == "pull" and name in m.preferred_providers and p.health().status == "unavailable"):
                why.append(f"{name}: {detail}")
                continue
            cap = p.capability()
            if need_gpu and name == "local" and cap.accelerator is None:
                why.append("local: no GPU on this machine")
                continue
            vram = m.requires.get("vram_gb")
            if vram and cap.vram_gb is not None and cap.vram_gb < float(vram):
                why.append(f"{name}: {cap.vram_gb} GB VRAM < {vram} GB required")
                continue
            out.append(p)
        return out, why

    def _busy(self, p: Provider) -> bool:
        lim = MAX_ACTIVE.get(p.provider)
        if lim is None:
            return False
        n = db.query("SELECT COUNT(*) n FROM remote_jobs WHERE provider=? AND status='running'", (p.provider,))[0]["n"]
        return n >= lim

    def _bundle(self, row: dict, m: ProductionJobManifest, p: Provider, attempt: int) -> dict:
        inputs = []
        for aid in m.inputs:
            r = db.query("SELECT manifest FROM artifacts WHERE id=?", (aid,))
            if not r:
                raise ValueError(f"input artifact {aid} not found")
            a = ArtifactManifest(**json.loads(r[0]["manifest"]))
            path = artifact_path(a)
            if path is None:
                # the input lives only in cloud storage (e.g. Drive): stream it to the control plane's cache (server side,
                # never the owner's PC), verifying its SHA-256, then stage it for the worker
                from ..storage import artifacts as store
                path = config.CACHE / "remote_inputs" / a.sha256[:16] / Path(a.name).name
                if not (path.exists() and path.stat().st_size == a.size):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    store.fetch_local(a, path)
                emit("job.progress", f"input {a.name} fetched from cloud storage to the control plane ({a.size} bytes, sha256 verified)",
                     m.job_id, artifact_id=aid)
            if a.size > worker_kit.INLINE_MAX:
                if not getattr(p, "can_stage_inputs", False):
                    raise ValueError(f"input {a.name} is {a.size} bytes (> {worker_kit.INLINE_MAX} inline limit) and provider "
                                     f"{p.provider} cannot stage files: pass an https params.input_url")
                staged = f"{a.sha256[:16]}_{Path(a.name).name}"
                inputs.append({"name": a.name, "sha256": a.sha256, "size": a.size, "dataset_file": staged,
                               "_stage_path": str(path)})
                continue
            inputs.append({"name": a.name, "sha256": a.sha256, "size": a.size, "b64": base64.b64encode(path.read_bytes()).decode()})
        cp = last_checkpoint(m.job_id)
        return {"job_id": m.job_id, "task": m.task, "params": m.params, "attempt": attempt, "provider": p.provider,
                "connection_id": p.connection_id, "max_runtime_s": m.max_runtime_s, "worker_version": worker_kit.worker_version(),
                "inputs": inputs, "checkpoint": cp.model_dump() if cp else None}

    def _gpu_flag(self, m: ProductionJobManifest) -> bool:
        spec = task_registry.spec(m.task) or task_registry.TaskSpec()
        if "gpu" in m.requires:
            return bool(m.requires["gpu"])
        return spec.gpu in ("required", "preferred")

    def _dispatch(self, row: dict) -> str:
        m = ProductionJobManifest(**json.loads(row["manifest"]))
        cands, why = self.candidates(m)
        errors = []
        for p in cands:
            if self._busy(p):
                self._wait(row, f"waiting: {p.provider} is busy (max {MAX_ACTIVE[p.provider]} active)")
                return "waiting"
            attempt = (row["attempts"] or 0) + 1
            try:
                bundle = self._bundle(row, m, p, attempt)
            except ValueError as e:
                return self._finish_failed(row, f"bad inputs: {e}")
            try:
                ref = p.submit(m.job_id, attempt, bundle, self._gpu_flag(m), m.max_runtime_s)
            except QuotaExhausted as e:
                self._quota(p, str(e), m.job_id)
                continue
            except ProviderError as e:
                errors.append(f"{p.provider}: {vault.redact(str(e))[:300]}")
                emit("provider.health", f"{p.provider} dispatch failed: {vault.redact(str(e))[:300]}", m.job_id, provider=p.provider)
                continue
            now = time.time()
            _set(m.job_id, status="running", provider=p.provider, connection_id=p.connection_id, remote_ref=ref, attempts=attempt,
                 started=row["started"] or now, heartbeat=now, error=None)
            self._next_poll[m.job_id] = (now + self.poll_min, self.poll_min)
            self._waiting.pop(m.job_id, None)
            emit("job.dispatched", f"attempt {attempt} on {p.provider}" + (" (resuming from checkpoint)" if bundle["checkpoint"] else ""),
                 m.job_id, provider=p.provider, connection_id=p.connection_id, ref=ref, attempt=attempt,
                 resume_from=bundle["checkpoint"]["seq"] if bundle["checkpoint"] else None)
            return "dispatched"
        if errors and len(errors) == len(cands):
            n = (row["attempts"] or 0) + 1
            _set(m.job_id, attempts=n)
            row = {**row, "attempts": n}
            return self._retry_or_fail(row, "every provider failed to start the job: " + "; ".join(errors))
        quota_only = not errors and any("[quota_exhausted]" in w for w in why)
        self._wait(row, "waiting for a provider: " + ("; ".join(why + errors) or "none eligible"), "quota_exhausted" if quota_only else "queued")
        return "waiting"

    def _wait(self, row: dict, reason: str, status: str = "queued") -> None:
        if row["status"] != status:
            _set(row["id"], status=status)
        if self._waiting.get(row["id"]) != reason:
            self._waiting[row["id"]] = reason
            _set(row["id"], error=reason[:1000])
            emit("job.progress", reason, row["id"], state="waiting")

    def _quota(self, p: Provider, msg: str, job_id: str | None) -> None:
        p.pause(vault.redact(msg), QUOTA_PAUSE_S)
        emit("quota.exhausted", f"{p.provider} reported a quota/limit; paused for {QUOTA_PAUSE_S // 3600} h (no other account is used): "
             f"{vault.redact(msg)[:300]}", job_id, provider=p.provider, connection_id=p.connection_id)

    # ------------------------------------------------------------ polling / retrieval
    def _poll(self, row: dict) -> str:
        jid = row["id"]
        p = self.providers.get(row["provider"])
        if p is None:
            return self._lost(row, f"provider {row['provider']} is not configured in this process")
        if row["cancel_requested"]:
            what = p.cancel(row["remote_ref"])
            _set(jid, status="cancelled", finished=time.time(), error=f"cancelled by owner; {what}")
            emit("job.cancelled", what, jid, provider=p.provider)
            return "cancelled"
        nxt, back = self._next_poll.get(jid, (0.0, self.poll_min))
        if time.time() < nxt:
            return "backoff"
        try:
            st = p.poll(row["remote_ref"])
        except QuotaExhausted as e:
            self._quota(p, str(e), jid)
            self._next_poll[jid] = (time.time() + self.poll_max, self.poll_max)
            return "backoff"
        if st.state in ("queued", "running"):
            if self._seen.get(jid) != st.state:
                self._seen[jid] = st.state
                emit("job.progress", f"{st.state} on {p.provider}", jid, state=st.state, provider=p.provider)
            if time.time() - (row["heartbeat"] or row["started"] or time.time()) > json.loads(row["manifest"]).get("max_runtime_s", 3600) + LOST_GRACE_S:
                return self._lost(row, "attempt exceeded max_runtime_s + grace without finishing")
            back = min(self.poll_max, max(self.poll_min, back * 1.6))
            self._next_poll[jid] = (time.time() + back, back)
            self._unknown.pop(jid, None)
            return st.state
        if st.state == "unknown":
            n = self._unknown.get(jid, 0) + 1
            self._unknown[jid] = n
            if n >= MAX_UNKNOWN_POLLS:
                return self._lost(row, f"status unknown {n} times: {st.detail}")
            back = min(self.poll_max, max(self.poll_min, back * 1.6))
            self._next_poll[jid] = (time.time() + back, back)
            return "unknown"
        return self._retrieve(row, p, st.state, st.detail)

    def _account(self, row: dict, p: Provider) -> float:
        secs = max(0.0, time.time() - (row["heartbeat"] or time.time()))
        m = json.loads(row["manifest"])
        if p.provider == "kaggle" and self._gpu_flag(ProductionJobManifest(**m)):
            db.metric("kaggle_gpu_seconds", secs, job=row["id"], attempt=row["attempts"])
        return secs

    def _retrieve(self, row: dict, p: Provider, state: str, detail: str) -> str:
        jid, attempt = row["id"], row["attempts"]
        wall = self._account(row, p)
        dest = compute_dir("jobs", jid, f"attempt_{attempt}")
        fetch_err = None
        if state != "lost":
            try:
                p.fetch(row["remote_ref"], dest)
            except QuotaExhausted as e:
                self._quota(p, str(e), jid)
                fetch_err = str(e)
            except ProviderError as e:
                fetch_err = vault.redact(str(e))
        log_tail = self._log_tail(dest) or vault.redact(p.log_tail(row["remote_ref"]))
        res_files = sorted(dest.rglob("result.json"))
        hist = {"attempt": attempt, "provider": p.provider, "ref": row["remote_ref"], "remote_state": state, "wall_s": round(wall, 1)}
        if not res_files:
            reason = f"no result.json from attempt {attempt} ({state}{': ' + detail if detail else ''}{'; ' + fetch_err if fetch_err else ''})"
            self._store_checkpoints(jid, dest)
            self._history(row, {**hist, "outcome": "lost", "error": reason, "log_tail": log_tail[-1500:]})
            return self._lost(row, reason)
        try:
            res = WorkerExecutionResult(**json.loads(res_files[0].read_text(encoding="utf-8")))
        except (ValidationError, ValueError) as e:
            self._history(row, {**hist, "outcome": "failed", "error": f"invalid result.json: {e}"[:500]})
            return self._retry_or_fail(row, f"invalid result.json: {str(e)[:300]}")
        if res.job_id != jid:
            self._history(row, {**hist, "outcome": "failed", "error": "result.json belongs to another job"})
            return self._retry_or_fail(row, "result.json job_id mismatch")
        new_cps = self._store_checkpoints(jid, dest)
        bad = self._verify(res, dest)
        if bad:
            msg = "artifact verification failed: " + "; ".join(bad)
            self._history(row, {**hist, "outcome": "failed", "error": msg[:800], "worker_status": res.status})
            emit("job.failed" if row["attempts"] > row["max_retries"] else "job.retrying", msg[:500], jid, provider=p.provider, verification="sha256")
            return self._retry_or_fail(row, msg, emit_event=False)
        stored = self._record_artifacts(jid, row, res, dest)
        res = res.model_copy(update={"artifacts": stored, "log_tail": vault.redact(res.log_tail)[-4000:],
                                     "error": vault.redact(res.error) if res.error else None})
        self._history(row, {**hist, "outcome": res.status, "duration_s": res.duration_s, "accelerator": res.accelerator,
                            "error": res.error, "artifacts": [a.name for a in stored], "checkpoints_new": new_cps,
                            "metrics": _brief(res.metrics)})
        if res.status == "succeeded":
            if row["task"] == "probe":
                self._store_capability(p, res)
            return self._succeed(row, res)
        if res.status == "quota_exhausted":
            self._quota(p, res.error or "worker reported quota exhausted", jid)
            _set(jid, status="queued", error=res.error)
            return "queued"
        if res.status == "cancelled":
            _set(jid, status="cancelled", finished=time.time(), error=res.error)
            emit("job.cancelled", res.error or "cancelled on the worker", jid)
            return "cancelled"
        if res.status == "lost":
            return self._lost(row, res.error or "worker lost")
        return self._retry_or_fail(row, res.error or "worker reported failure")

    @staticmethod
    def _log_tail(dest: Path) -> str:
        out = []
        for lg in sorted(dest.rglob("*.log")):
            raw = lg.read_text(encoding="utf-8", errors="replace")
            try:
                raw = "".join(x.get("data", "") for x in json.loads(raw))
            except (ValueError, AttributeError):
                pass
            out.append(raw)
        return vault.redact("\n".join(out))[-4000:]

    def _store_checkpoints(self, jid: str, dest: Path) -> list[int]:
        new = []
        for f in sorted(dest.rglob("ckpt_*.json")):
            try:
                cp = JobCheckpoint(**json.loads(f.read_text(encoding="utf-8")))
            except (ValidationError, ValueError):
                continue
            if cp.job_id != jid:
                continue
            cur = db.execute("INSERT OR IGNORE INTO remote_checkpoints(job_id,seq,step,data,created) VALUES(?,?,?,?,?)",
                             (jid, cp.seq, cp.step, cp.model_dump_json(), cp.created_at))
            if cur.rowcount:
                new.append(cp.seq)
                emit("job.checkpoint", f"checkpoint {cp.seq}: {cp.step}", jid, seq=cp.seq, step=cp.step)
        return new

    @staticmethod
    def _find(dest: Path, name: str) -> Path | None:
        p = dest / "artifacts" / name
        if p.is_file():
            return p
        hits = [h for h in dest.rglob(name) if h.is_file()]
        return hits[0] if hits else None

    def _verify(self, res: WorkerExecutionResult, dest: Path) -> list[str]:
        bad = []
        for a in res.artifacts:
            f = self._find(dest, a.name)
            if f is None:
                bad.append(f"{a.name}: missing")
                continue
            if f.stat().st_size != a.size:
                bad.append(f"{a.name}: size {f.stat().st_size} != {a.size}")
                continue
            got = _sha(f)
            if got != a.sha256:
                bad.append(f"{a.name}: sha256 {got[:12]}.. != {a.sha256[:12]}..")
        return bad

    def _record_artifacts(self, jid: str, row: dict, res: WorkerExecutionResult, dest: Path) -> list[ArtifactManifest]:
        d = compute_dir("artifacts", jid)
        out = []
        for a in res.artifacts:
            src = self._find(dest, a.name)
            dst = d / a.name
            shutil.copyfile(src, dst)
            a2 = a.model_copy(update={"produced_by_job": jid, "project_id": row["project_id"],
                                      "locations": [*a.locations, ArtifactLocation(provider="local", connection_id="local",
                                                                                   ref=str(dst.relative_to(config.HOME)),
                                                                                   path=str(dst.relative_to(config.HOME)), verified_at=time.time())],
                                      "provenance": {**a.provenance, "provider": res.provider, "connection_id": res.connection_id}})
            db.execute("INSERT OR REPLACE INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
                       (a2.artifact_id, a2.kind, a2.name, a2.sha256, a2.size, row["project_id"], jid, a2.model_dump_json(), a2.created_at))
            out.append(a2)
        return out

    def _store_capability(self, p: Provider, res: WorkerExecutionResult) -> None:
        hw = res.metrics.get("hardware") or {}
        g = hw.get("gpus") or []
        fw = [k for k in ("torch", "ffmpeg") if hw.get(k)]
        cap = RemoteWorkerCapability(provider=p.provider if p.provider in ("local", "kaggle", "colab", "user_gpu") else "other",
                                     connection_id=p.connection_id, accelerator=res.accelerator,
                                     vram_gb=g[0]["vram_gb"] if g else None, ram_gb=hw.get("ram_total_gb"), cpus=hw.get("cpus"),
                                     max_runtime_s=p.declared_capability().max_runtime_s, frameworks=fw + (["nvenc"] if hw.get("nvenc") else []),
                                     internet=bool(hw.get("internet")), measured=True, measured_at=res.finished_at or time.time())
        p.store_measured_capability(cap)
        emit("provider.health", f"{p.provider} capability measured: {cap.accelerator or 'CPU'}", res.job_id, provider=p.provider,
             capability=cap.model_dump())

    def _history(self, row: dict, entry: dict) -> None:
        cur = _row(row["id"]) or row
        data = json.loads(cur["result"]) if cur.get("result") else {}
        data.setdefault("attempts", []).append(entry)
        _set(row["id"], result=json.dumps(data, default=str))

    def _succeed(self, row: dict, res: WorkerExecutionResult) -> str:
        jid = row["id"]
        extra = postprocess(row, res)
        cur = _row(jid)
        data = json.loads(cur["result"]) if cur.get("result") else {}
        data["final"] = json.loads(res.model_dump_json())
        if extra:
            data["postprocess"] = extra
        data["artifacts"] = job_artifacts(jid)
        data.update(accelerator=res.accelerator, duration_s=res.duration_s, metrics=res.metrics, provider=res.provider)
        dest = (json.loads(row["manifest"]) if row.get("manifest") else {}).get("output_destination")
        if dest:
            data["delivery"] = deliver_outputs(jid, dest, data["artifacts"])
        _set(jid, status="succeeded", finished=time.time(), result=json.dumps(data, default=str), error=None)
        emit("job.succeeded", f"{row['task']} succeeded on {res.provider} in {res.duration_s}s ({len(res.artifacts)} verified artifact(s))", jid,
             provider=res.provider, accelerator=res.accelerator, duration_s=res.duration_s, artifacts=len(data["artifacts"]))
        return "succeeded"

    def _lost(self, row: dict, reason: str) -> str:
        emit("worker.lost", reason[:500], row["id"], provider=row["provider"], attempt=row["attempts"])
        return self._retry_or_fail(row, f"worker lost: {reason}")

    def _retry_or_fail(self, row: dict, reason: str, emit_event: bool = True) -> str:
        jid = row["id"]
        self._next_poll.pop(jid, None)
        self._unknown.pop(jid, None)
        cur = _row(jid) or row
        if cur["cancel_requested"]:
            _set(jid, status="cancelled", finished=time.time(), error=reason[:2000])
            emit("job.cancelled", "cancel requested: not re-dispatched", jid)
            return "cancelled"
        if (cur["attempts"] or 0) <= (cur["max_retries"] or 0):
            cp = last_checkpoint(jid)
            _set(jid, status="queued", error=vault.redact(reason)[:2000])
            if emit_event:
                emit("job.retrying", f"{reason[:300]} - retry {cur['attempts']}/{cur['max_retries']}" + (f", resuming from checkpoint {cp.seq} ({cp.step})" if cp else ""),
                     jid, attempt=cur["attempts"], resume_from=cp.seq if cp else None)
            return "queued"
        return self._finish_failed(cur, reason, emit_event)

    def _finish_failed(self, row: dict, reason: str, emit_event: bool = True) -> str:
        _set(row["id"], status="failed", finished=time.time(), error=vault.redact(reason)[:2000])
        if emit_event:
            emit("job.failed", reason[:500], row["id"])
        return "failed"

    # ------------------------------------------------------------ owner actions
    def cancel(self, job_id: str) -> dict | None:
        row = _row(job_id)
        if row is None:
            return None
        if row["status"] in TERMINAL:
            return row
        _set(job_id, cancel_requested=1)
        if row["status"] in WAITING:
            _set(job_id, status="cancelled", finished=time.time(), error="cancelled by owner before dispatch")
            emit("job.cancelled", "cancelled before dispatch", job_id)
        else:
            with self._lock:
                self._poll(_row(job_id))
        return _row(job_id)

    def retry(self, job_id: str) -> dict | None:
        row = _row(job_id)
        if row is None or row["status"] not in ("failed", "cancelled"):
            return row
        m = json.loads(row["manifest"])
        _set(job_id, status="queued", cancel_requested=0, error=None, finished=None,
             max_retries=(row["attempts"] or 0) + max(1, int(m.get("max_retries", 0))))
        emit("job.retrying", "re-queued by owner (finished steps are kept as checkpoints)", job_id, by="owner")
        return _row(job_id)

    # ------------------------------------------------------------ loop
    def tick(self) -> dict:
        out = {"polled": {}, "dispatched": {}}
        with self._lock:
            for row in db.query("SELECT * FROM remote_jobs WHERE status='running' ORDER BY priority, created"):
                try:
                    out["polled"][row["id"]] = self._poll(row)
                except Exception as e:  # noqa: BLE001 - one bad job must not stop the loop
                    out["polled"][row["id"]] = f"error: {vault.redact(str(e))[:200]}"
            for row in db.query("SELECT * FROM remote_jobs WHERE status IN ('queued','quota_exhausted') ORDER BY priority, created"):
                if row["cancel_requested"]:
                    _set(row["id"], status="cancelled", finished=time.time())
                    continue
                try:
                    out["dispatched"][row["id"]] = self._dispatch(row)
                except Exception as e:  # noqa: BLE001
                    out["dispatched"][row["id"]] = f"error: {vault.redact(str(e))[:200]}"
        return out

    def active(self) -> int:
        return db.query("SELECT COUNT(*) n FROM remote_jobs WHERE status IN ('queued','quota_exhausted','running')")[0]["n"]

    def run_until_done(self, job_id: str, timeout: float = 1800, interval: float = 2.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            r = _row(job_id)
            if r and r["status"] in TERMINAL:
                return r
            time.sleep(interval)
        return _row(job_id)


def _brief(m: dict) -> dict:
    keep = ("encoder", "gpu_encode", "encode_s", "realtime_factor", "chunks_run_this_attempt", "chunks_skipped", "gpu_util_mean", "gpu_util_max",
            "gpu_mem_used_max_mb", "peak_vram_allocated_mb", "cpu_percent_mean", "input_bytes", "output_bytes", "task_s", "nvenc_available",
            "parts_run_this_attempt", "parts_skipped", "items_ok", "items_error", "completion_tokens_per_s", "model_load_s")
    return {k: m[k] for k in keep if k in m}


def _record_local(row: dict, out: Path, kind: str, mime: str, provenance: dict) -> ArtifactManifest:
    a = ArtifactManifest(kind=kind, name=out.name, mime=mime, size=out.stat().st_size, sha256=_sha(out), project_id=row["project_id"],
                         produced_by_job=row["id"], locations=[ArtifactLocation(provider="local", connection_id="local", ref=str(out.relative_to(config.HOME)),
                                                                                path=str(out.relative_to(config.HOME)), verified_at=time.time())],
                         provenance=provenance)
    db.execute("INSERT OR REPLACE INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
               (a.artifact_id, a.kind, a.name, a.sha256, a.size, row["project_id"], row["id"], a.model_dump_json(), a.created_at))
    return a


def _join_batch_llm(row: dict, res: WorkerExecutionResult) -> dict:
    """batch_llm: join the verified part files of ALL attempts (as listed by the final checkpoint) in part order."""
    cp = last_checkpoint(row["id"])
    st = (cp.state if cp else {}) or {}
    files, total = st.get("part_files") or {}, int(st.get("parts_total") or 0)
    if not total or sorted(int(k) for k in files) != list(range(total)):
        return {"join": "skipped", "reason": f"have parts {sorted(files)}, expected {total}"}
    by_sha = {a["sha256"]: a for a in job_artifacts(row["id"]) if a["name"].startswith("batch_llm_part_")}
    rows = []
    for i in range(total):
        f = files[str(i)]
        a = by_sha.get(f["sha256"])
        p = artifact_path(a) if a else None
        if p is None or _sha(p) != f["sha256"]:
            return {"join": "failed", "reason": f"part {i} ({f['name']}) missing or checksum mismatch"}
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    fmt = (json.loads(row["manifest"]).get("params") or {}).get("output_format", "jsonl") if row.get("manifest") else "jsonl"
    d = compute_dir("artifacts", row["id"])
    out = d / f"batch_llm_results.{fmt}"
    if fmt == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    a = _record_local(row, out, "analysis", "application/json" if fmt == "json" else "application/x-ndjson",
                      {"step": "local join of verified batch_llm parts", "parts": [files[str(i)]["sha256"] for i in range(total)],
                       "simulated": bool((res.metrics.get("batch_llm") or {}).get("simulated"))})
    errs = sum(1 for r in rows if r.get("error"))
    return {"join": "ok", "artifact": a.artifact_id, "sha256": a.sha256, "items": len(rows), "items_error": errs, "parts": total}


def postprocess(row: dict, res: WorkerExecutionResult) -> dict | None:
    """Local steps after a verified success. chunked_transcode: concatenate the verified chunks of ALL attempts;
    batch_llm: join the verified result parts of ALL attempts."""
    if row["task"] == "batch_llm":
        return _join_batch_llm(row, res)
    if row["task"] != "chunked_transcode" or not shutil.which("ffmpeg"):
        return None
    chunks = sorted((a for a in job_artifacts(row["id"]) if a["name"].startswith("chunk_")), key=lambda a: a["name"])
    n = int(res.metrics.get("chunks_total") or 0)
    if not chunks or (n and len(chunks) != n):
        return {"concat": "skipped", "reason": f"have {len(chunks)} chunk(s), expected {n}"}
    d = compute_dir("artifacts", row["id"])
    lst = d / "concat.txt"
    lst.write_text("".join(f"file '{artifact_path(a)}'\n" for a in chunks), encoding="utf-8")
    out = d / "joined.mp4"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode != 0:
        return {"concat": "failed", "error": r.stderr[-300:]}
    a = ArtifactManifest(kind="master", name="joined.mp4", mime="video/mp4", size=out.stat().st_size, sha256=_sha(out), project_id=row["project_id"],
                         produced_by_job=row["id"], locations=[ArtifactLocation(provider="local", connection_id="local", ref=str(out.relative_to(config.HOME)),
                                                                                path=str(out.relative_to(config.HOME)), verified_at=time.time())],
                         provenance={"step": "local concat (-c copy) of verified chunks", "chunks": [c["sha256"] for c in chunks]})
    db.execute("INSERT OR REPLACE INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
               (a.artifact_id, a.kind, a.name, a.sha256, a.size, row["project_id"], row["id"], a.model_dump_json(), a.created_at))
    return {"concat": "ok", "artifact": a.artifact_id, "sha256": a.sha256, "chunks": len(chunks)}


# ---------------------------------------------------------------- module-level interface (scheduler, Accounts 2/3)
def submit(manifest: ProductionJobManifest) -> dict:
    """Queue a job (idempotency_key honoured). Returns {"job_id", "status", "created"}."""
    row, created = get().submit(manifest)
    return {"job_id": row["id"], "status": row["status"], "created": created}


def cancel(job_id: str) -> bool:
    """True when the job is (now) cancelled."""
    r = get().cancel(job_id)
    return bool(r) and r["status"] == "cancelled"


def status(job_id: str) -> dict | None:
    return _row(job_id)


def quota_status() -> list:
    """QuotaStatus/v1 for every provider that has a quota (today: Kaggle weekly GPU seconds, measured)."""
    out = []
    for p in get().providers.values():
        q = getattr(p, "quota", None)
        if callable(q):
            out.append(q())
    return out


_DISPATCHER: Dispatcher | None = None
_DLOCK = threading.Lock()


def get() -> Dispatcher:
    global _DISPATCHER
    with _DLOCK:
        if _DISPATCHER is None:
            _DISPATCHER = Dispatcher()
        return _DISPATCHER


def set_dispatcher(d: Dispatcher | None) -> None:
    global _DISPATCHER
    with _DLOCK:
        _DISPATCHER = d
