"""Durable job ledger: production jobs survive app restarts, worker crashes and agent sessions ending.

A job is a row in SQLite, not a closure in memory. A handler is a named function that runs its work
as a sequence of *stages*; each finished stage stores its (JSON) output in the row, so when a worker
dies the next worker re-runs the handler and every completed stage is answered from its checkpoint
instead of being recomputed.

    @jobs.handler("create-from-idea")
    def run(job):
        res = job.stage("research", lambda: research(job.params["topic"]))
        ...

Workers are plain processes (`python -m nexus worker`). They claim jobs atomically, heartbeat while
working, and `recover()` re-queues anything whose worker stopped heartbeating.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Any, Callable

from . import config, db

HANDLERS: dict[str, Callable[["Job"], Any]] = {}
HEARTBEAT_S = 5.0
STALE_S = 30.0
ACTIVE = ("queued", "running")


LIGHT: set[str] = set()  # network-bound kinds (uploads, API polling) that must not wait for the CPU-heavy lock


def handler(kind: str, heavy: bool = True):
    def deco(fn):
        HANDLERS[kind] = fn
        (LIGHT.discard if heavy else LIGHT.add)(kind)
        return fn
    return deco


def worker_id(role: str = "worker") -> str:
    return f"{role}-{socket.gethostname()}-{os.getpid()}"


_held = threading.local()


@contextmanager
def heavy_lock():
    """One heavy job at a time on this machine, across processes (API thread and worker process).

    Re-entrant within a thread: a job handler already runs inside the lock, and library code that also guards its own
    heavy calls (e.g. the Account 3 research brief calling the local LLM) must not wait on itself."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    config.ensure_dirs()
    f = open(config.RUN / "heavy.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        _held.depth = 1
        yield
    finally:
        _held.depth = 0
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


class Job:
    def __init__(self, row: dict):
        self.id: str = row["id"]
        self.kind: str = row["kind"]
        self.project_id: str = row.get("project_id") or ""
        self.params: dict = json.loads(row["params"]) if row.get("params") else {}
        self._stages: dict[str, dict] = json.loads(row["stages"]) if row.get("stages") else {}
        self.log_path = config.LOGS / "jobs" / f"{self.id}.log"

    def log(self, msg: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

    def progress(self, p: float) -> None:
        db.execute("UPDATE jobs SET progress=? WHERE id=?", (max(0.0, min(1.0, p)), self.id))

    def set_project(self, pid: str) -> None:
        self.project_id = pid
        db.execute("UPDATE jobs SET project_id=? WHERE id=?", (pid, self.id))

    def _save(self, stage: str) -> None:
        db.execute("UPDATE jobs SET stage=?, stages=?, checkpoint=?, heartbeat=? WHERE id=?",
                   (stage, json.dumps(self._stages, ensure_ascii=False, default=str),
                    json.dumps({"last_done": [k for k, v in self._stages.items() if v["status"] == "done"][-1:], "ts": time.time()}),
                    time.time(), self.id))

    def stage(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` once. Its return value must be JSON-serialisable; a finished stage is never re-run."""
        st = self._stages.get(name)
        if st and st["status"] == "done":
            self.log(f"stage {name}: restored from checkpoint (not re-run)")
            return st["output"]
        runs = (st or {}).get("runs", 0) + 1
        self._stages[name] = {"status": "running", "started": time.time(), "runs": runs}
        self._save(name)
        self.log(f"stage {name}: start (run {runs})")
        t0 = time.time()
        out = fn()
        out = json.loads(json.dumps(out, ensure_ascii=False, default=str))  # what a resumed run will see
        el = round(time.time() - t0, 3)
        self._stages[name] = {"status": "done", "started": t0, "finished": time.time(), "seconds": el, "runs": runs, "output": out}
        self._save(name)
        db.metric(f"stage_seconds.{self.kind}.{name}", el, job=self.id)
        self.log(f"stage {name}: done in {el}s")
        return out


def submit(kind: str, params: dict | None = None, project_id: str = "", workflow: str | None = None, max_retries: int = 2) -> dict:
    job_id = db.new_id("job")
    db.execute("INSERT INTO jobs(id,project_id,kind,status,worker,progress,created,params,workflow,stages,retries,max_retries,durable,log)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
               (job_id, project_id, kind, "queued", None, 0.0, time.time(), json.dumps(params or {}, ensure_ascii=False),
                workflow or kind, "{}", 0, max_retries, str(config.LOGS / "jobs" / f"{job_id}.log")))
    return {"job": job_id, "status": "queued"}


def get(job_id: str) -> dict | None:
    rows = db.query("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        return None
    r = rows[0]
    for k in ("params", "stages", "result", "checkpoint"):
        r[k] = json.loads(r[k]) if r.get(k) else None
    return r


def cancel(job_id: str) -> bool:
    return db.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'", (time.time(), job_id)).rowcount == 1


def retry(job_id: str) -> bool:
    """Re-queue a failed/interrupted durable job; finished stages are kept."""
    return db.execute("UPDATE jobs SET status='queued', error=NULL, finished=NULL, retries=0, recovery='re-queued by user' "
                      "WHERE id=? AND durable=1 AND status IN ('failed','interrupted','cancelled')", (job_id,)).rowcount == 1


def claim(wid: str) -> Job | None:
    now = time.time()
    with db._lock:  # RETURNING rows must be read before the commit
        c = db.conn()
        rows = c.execute("UPDATE jobs SET status='running', worker=?, started=COALESCE(started,?), heartbeat=? WHERE id=("
                         "SELECT id FROM jobs WHERE status='queued' AND durable=1 ORDER BY created LIMIT 1) AND status='queued' RETURNING id",
                         (wid, now, now)).fetchall()
        c.commit()
    if not rows:
        return None
    return Job(db.query("SELECT * FROM jobs WHERE id=?", (rows[0][0],))[0])


def _local_worker_dead(wid: str | None) -> bool:
    """True when `wid` names a worker process on this machine that no longer exists.

    A fast restart (app killed and started again within STALE_S) leaves a fresh-looking heartbeat behind; checking the
    worker's process id lets recovery act at once instead of waiting for the heartbeat to age.
    """
    if not wid or "-" not in wid:
        return False
    head, _, pid = wid.rpartition("-")
    if not pid.isdigit() or not head.endswith("-" + socket.gethostname()):
        return False
    if int(pid) == os.getpid():
        return False
    try:
        import psutil
        try:  # a killed process can linger briefly as an unreaped zombie: that is still a dead worker
            return psutil.Process(int(pid)).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
    except Exception:  # noqa: BLE001 - without psutil fall back to the heartbeat rule
        return False


def recover(stale_after: float = STALE_S, api_worker: str | None = None) -> dict:
    """Re-queue durable jobs whose worker stopped heartbeating (or whose local process is gone); flag lost in-process
    jobs. Safe to call any time."""
    now = time.time()
    out = {"requeued": [], "failed": [], "interrupted": []}
    for r in db.query("SELECT id,stage,retries,max_retries,heartbeat,worker FROM jobs WHERE status='running' AND durable=1"):
        if r["heartbeat"] and now - r["heartbeat"] < stale_after and not _local_worker_dead(r["worker"]):
            continue
        n = (r["retries"] or 0) + 1
        note = f"worker {r['worker']} lost at stage '{r['stage'] or 'start'}'"
        if n > (r["max_retries"] or 0):
            db.execute("UPDATE jobs SET status='failed', finished=?, retries=?, error=?, recovery=? WHERE id=? AND status='running'",
                       (now, n, note + "; retry limit reached", "inspect the job log, then `nexus jobs retry <id>`", r["id"]))
            out["failed"].append(r["id"])
        else:
            db.execute("UPDATE jobs SET status='queued', retries=?, recovery=? WHERE id=? AND status='running'",
                       (n, note + "; resumed from last checkpoint", r["id"]))
            out["requeued"].append(r["id"])
    # In-process (closure) jobs cannot outlive the API process that created them: say so instead of showing 'running' forever.
    if api_worker is not None:
        for r in db.query("SELECT id FROM jobs WHERE status IN ('queued','running') AND COALESCE(durable,0)=0 AND COALESCE(worker,'')<>?", (api_worker,)):
            db.execute("UPDATE jobs SET status='interrupted', finished=?, error=?, recovery=? WHERE id=?",
                       (now, "the app stopped while this in-process job was pending", "run the action again from the app", r["id"]))
            out["interrupted"].append(r["id"])
    return out


def run_job(job: Job) -> str:
    """Execute one claimed job. Returns its final status."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_S):
            db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND status='running'", (time.time(), job.id))

    threading.Thread(target=beat, daemon=True).start()
    try:
        fn = HANDLERS.get(job.kind)
        if fn is None:
            raise LookupError(f"no handler registered for job kind '{job.kind}'")
        if job.kind in LIGHT:
            result = fn(job)
        else:
            with heavy_lock():
                result = fn(job)
        db.execute("UPDATE jobs SET status='done', progress=1, finished=?, result=? WHERE id=?",
                   (time.time(), json.dumps(result, default=str, ensure_ascii=False), job.id))
        job.log("job done")
        return "done"
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        job.log(f"job error: {err}")
        row = db.query("SELECT retries,max_retries FROM jobs WHERE id=?", (job.id,))[0]
        n = (row["retries"] or 0) + 1
        if isinstance(e, LookupError) or n > (row["max_retries"] or 0):
            db.execute("UPDATE jobs SET status='failed', finished=?, retries=?, error=?, recovery=? WHERE id=?",
                       (time.time(), n, err, "fix the cause, then `nexus jobs retry <id>` (finished stages are kept)", job.id))
            return "failed"
        db.execute("UPDATE jobs SET status='queued', retries=?, error=?, recovery=? WHERE id=?",
                   (n, err, f"attempt {n} failed; retrying from last checkpoint", job.id))
        return "queued"
    finally:
        stop.set()


def schedule_backup(now: float | None = None) -> str | None:
    """Queue a backup when the last one is older than settings.backup_every_hours (default 24; 0 disables).
    Only when there is something to protect, and never while another backup job is pending."""
    import json as _json
    hours = float(config.settings().get("backup_every_hours", 24))
    if hours <= 0 or not db.query("SELECT 1 FROM projects LIMIT 1"):
        return None
    now = now or time.time()
    f = config.HOME / "last_backup.json"
    try:
        last = _json.loads(f.read_text(encoding="utf-8"))["ts"] if f.exists() else 0
    except (ValueError, KeyError, OSError):
        last = 0
    if now - last < hours * 3600:
        return None
    recent = db.query("SELECT status, created FROM jobs WHERE kind='backup' ORDER BY created DESC LIMIT 1")
    if recent and (recent[0]["status"] in ACTIVE or now - recent[0]["created"] < 3600):  # pending, or failed less than an hour ago
        return None
    return submit("backup", {"drill": False, "scheduled": True}, max_retries=0)["job"]


def schedule_archive_recovery() -> str | None:
    """Archive uploads interrupted by a crash or waiting for a retry are resumed by an 'archive-recover' job (one at a
    time). Nothing happens when no record needs it."""
    try:
        from . import archive
        now = time.time()
        due = [r for r in archive.records() if r["state"] in ("queued", "uploading", "uploaded")
               or (r["state"] == "failed" and r.get("next_retry", 0) <= now and r.get("attempts", 0) < archive.settings()["max_attempts"])]
    except Exception:  # noqa: BLE001
        return None
    if not due or db.query("SELECT id FROM jobs WHERE kind IN ('archive-recover','archive') AND status IN ('queued','running')"):
        return None
    return submit("archive-recover", {})["job"]


def run_worker(stop: threading.Event | None = None, poll: float = 1.0, once: bool = False, role: str = "worker") -> None:
    from . import workflows  # noqa: F401  (registers the production handlers)
    wid = worker_id(role)
    recover()
    schedule_backup()
    schedule_archive_recovery()
    # Recovery runs on its own thread: a worker busy with a long render must not delay re-queuing a job whose worker
    # died (found in the crash-recovery demo, where a killed render stayed 'running' behind a 10-minute dub job).
    halt = stop or threading.Event()

    def watchdog():
        while not halt.wait(STALE_S):
            try:
                recover()
                schedule_backup()
                schedule_archive_recovery()
            except Exception:  # noqa: BLE001 - a locked database must not kill the watchdog
                pass

    threading.Thread(target=watchdog, daemon=True, name=f"nexus-recover-{role}").start()
    while not halt.is_set():
        job = claim(wid)
        if job is None:
            if once:
                return
            time.sleep(poll)
            continue
        run_job(job)


def summary() -> dict:
    rows = db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
    active = db.query("SELECT id,kind,status,stage,worker,retries,recovery FROM jobs WHERE status IN ('queued','running','interrupted') ORDER BY created")
    return {"counts": {r["status"]: r["n"] for r in rows}, "active": active}
