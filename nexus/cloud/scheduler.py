"""Autonomous production scheduler (sub-agent D).

Turns the owner's daily targets into ProductionJobManifest/v1 submissions for the compute layer and watches them to
completion. Everything the scheduler knows is persisted in the `scheduler_state` table (rows "config" and "state"), so
it survives restarts; `tick(now)` is idempotent: calling it twice with nothing new changes nothing, and every
submission carries a deterministic idempotency key so a crash between "submitted" and "saved" cannot duplicate a job.

Talks to the compute layer ONLY through `Adapter` (submit / cancel / status / quota / storage). The defaults use
`nexus.cloud.compute` (`submit`, `cancel`, `quota_status`) when that package is installed, otherwise the documented HTTP
endpoints under /api/v1/compute; tests inject fakes.

Day boundaries are UTC. Limits are never dodged: quota or storage exhaustion pauses the scheduler with a notification
and waits for the owner; it never switches to another account.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import db
from ..contracts import ProductionJobManifest
from . import events

TERMINAL_OK = {"succeeded", "done", "completed"}
TERMINAL_FAIL = {"failed", "lost", "error"}
TERMINAL = TERMINAL_OK | TERMINAL_FAIL | {"cancelled", "quota_exhausted"}
KINDS = ("master", "short")

DEFAULT_CONFIG: dict[str, Any] = {
    "daily_masters": 1,               # masters per day (per language)
    "shorts_per_master": 3,
    "languages": ["en"],
    "platforms": ["youtube", "shorts"],
    "resolution": "1920x1080",
    "quality_preset": "standard",
    "max_active_jobs": 1,
    "priority": {"master": 4, "short": 5},   # 0 = most urgent (ProductionJobManifest.priority)
    "job_types": ["master", "short"],        # which kinds are produced
    # kind -> ordered task names; each task waits for the previous one. render_* are implemented by Account 2 later;
    # until then the owner can plan the registered remote tasks "probe" / "ffmpeg_transcode".
    "plan": {"master": ["render_master"], "short": ["render_short"]},
    "task_params": {},                       # task -> extra params merged into the manifest
    "preferred_providers": [],
    "max_retries": 2,                        # ProductionJobManifest.max_retries for each submission
    "max_runtime_s": 3600,
    "retry_backoff_s": 120,                  # first retry delay; doubles each attempt
    "max_backoff_s": 3600,
    "max_gpu_seconds_day": None,             # None = no own limit (provider quotas still apply)
    "max_storage_bytes": None,               # None = no own limit (connection quotas still apply)
    "min_free_disk_bytes": 1 * 2 ** 30,      # control-plane disk floor
    "stop": {"max_items": None, "until": None},  # stop conditions: items completed since start / UNIX time
    "tick_interval_s": 20,
}

DEFAULT_STATE: dict[str, Any] = {
    "status": "stopped",          # stopped | running | paused | stopping
    "pause_reason": None,
    "started_at": None,
    "updated_at": None,
    "last_tick": None,
    "last_error": None,
    "blocked": None,              # why nothing was submitted on the last tick (informational)
    "items_done": 0,              # slots completed since start (stop condition)
    "slots": {},                  # slot key -> slot dict
}


class ComputeUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- adapter to the compute / storage layer
def _api_base() -> str:
    from .. import config
    return os.environ.get("NEXUS_API_URL") or f"http://127.0.0.1:{config.settings().get('port', config.DEFAULT_PORT)}"


def _http(method: str, path: str, body: dict | None = None, timeout: float = 15) -> Any:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_api_base() + path, data=data, method=method, headers={"Content-Type": "application/json"})
    tok = os.environ.get("NEXUS_API_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - own API, fixed scheme
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ComputeUnavailable("compute service not installed in this build") from e
        raise ComputeUnavailable(f"compute API returned HTTP {e.code}") from e
    except OSError as e:
        raise ComputeUnavailable(f"compute API unreachable: {e}") from e


def _compute_module():
    try:
        import importlib
        return importlib.import_module("nexus.cloud.compute")
    except ImportError:
        return None


def _jobid(res: Any, fallback: str) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        return res.get("job_id") or res.get("id") or fallback
    return getattr(res, "job_id", None) or fallback


def _compute_router_missing() -> bool:
    import sys
    api = sys.modules.get("nexus.api")
    return bool(api and "routes_compute" in getattr(api, "CLOUD_MISSING", {}))


def default_submit(m: ProductionJobManifest) -> str:
    mod = _compute_module()
    if mod is None and _compute_router_missing():
        raise ComputeUnavailable("compute service not installed in this build")
    if mod is not None and hasattr(mod, "submit"):
        return _jobid(mod.submit(m), m.job_id)
    return _jobid(_http("POST", "/api/v1/compute/jobs", m.model_dump(mode="json")), m.job_id)


def default_cancel(job_id: str) -> bool:
    mod = _compute_module()
    if mod is not None and hasattr(mod, "cancel"):
        return bool(mod.cancel(job_id))
    _http("POST", f"/api/v1/compute/jobs/{job_id}/cancel", {})
    return True


def _as_dict(x: Any) -> dict:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else dict(x)


def default_quota() -> list[dict]:
    """QuotaStatus/v1 dicts. A missing compute service reports no quotas (nothing to exhaust)."""
    mod = _compute_module()
    if mod is not None and hasattr(mod, "quota_status"):
        res = mod.quota_status()
    elif _compute_router_missing():
        return []
    else:
        try:
            res = _http("GET", "/api/v1/compute/quota", timeout=8)
        except ComputeUnavailable:
            return []
    if isinstance(res, dict):
        res = res.get("quotas", [res])
    return [_as_dict(q) for q in (res or [])]


def default_status(job_id: str) -> dict:
    """Job status from the shared `remote_jobs` ledger (the dispatcher's source of truth)."""
    rows = db.query("SELECT status, started, finished, error, attempts, result FROM remote_jobs WHERE id = ?", (job_id,))
    if not rows:
        return {"status": "unknown"}
    r = rows[0]
    return {"status": r["status"], "started": r["started"], "finished": r["finished"], "error": r["error"]}


def default_storage() -> dict:
    """Bytes the platform has stored (artifacts ledger) and control-plane free disk."""
    import shutil

    from .. import config
    used = db.query("SELECT COALESCE(SUM(size),0) s FROM artifacts")[0]["s"]
    try:
        free = shutil.disk_usage(config.HOME).free
    except OSError:
        free = None
    return {"used_bytes": int(used or 0), "free_disk_bytes": free, "exhausted_connections": []}


@dataclass
class Adapter:
    submit_fn: Callable[[ProductionJobManifest], str] = default_submit
    cancel_fn: Callable[[str], bool] = default_cancel
    quota_fn: Callable[[], list[dict]] = default_quota
    status_fn: Callable[[str], dict] = default_status
    storage_fn: Callable[[], dict] = default_storage


# ---------------------------------------------------------------- persistence
def _load(key: str, default: dict) -> dict:
    rows = db.query("SELECT data FROM scheduler_state WHERE id = ?", (key,))
    data = copy.deepcopy(default)
    if rows:
        try:
            data.update(json.loads(rows[0]["data"]))
        except ValueError:
            pass
    return data


def _save(key: str, data: dict) -> None:
    db.execute("INSERT OR REPLACE INTO scheduler_state(id, data, updated) VALUES(?,?,?)",
               (key, json.dumps(data, default=str), time.time()))


def get_config() -> dict:
    c = _load("config", DEFAULT_CONFIG)
    c["stop"] = {**DEFAULT_CONFIG["stop"], **(c.get("stop") or {})}
    c["priority"] = {**DEFAULT_CONFIG["priority"], **(c.get("priority") or {})}
    return c


def validate_config(patch: dict) -> dict:
    """Merge `patch` into the current config and validate; raises ValueError with a readable message."""
    unknown = set(patch) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown scheduler setting(s): {', '.join(sorted(unknown))}")
    c = get_config()
    for k, v in patch.items():
        c[k] = {**c[k], **v} if isinstance(DEFAULT_CONFIG[k], dict) and isinstance(v, dict) and k != "plan" and k != "task_params" else v

    def _int(name, lo, hi):
        v = c[name]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{name} must be an integer between {lo} and {hi}")

    _int("daily_masters", 0, 100)
    _int("shorts_per_master", 0, 50)
    _int("max_active_jobs", 1, 32)
    _int("max_retries", 0, 10)
    _int("tick_interval_s", 5, 3600)
    for name in ("retry_backoff_s", "max_backoff_s", "max_runtime_s"):
        if not isinstance(c[name], (int, float)) or c[name] <= 0:
            raise ValueError(f"{name} must be a positive number")
    for name in ("max_gpu_seconds_day", "max_storage_bytes", "min_free_disk_bytes"):
        if c[name] is not None and (not isinstance(c[name], (int, float)) or c[name] < 0):
            raise ValueError(f"{name} must be empty or a non-negative number")
    for name in ("languages", "platforms", "job_types", "preferred_providers"):
        if not isinstance(c[name], list) or not all(isinstance(x, str) and x for x in c[name]):
            raise ValueError(f"{name} must be a list of names")
    if not c["languages"]:
        raise ValueError("at least one language is required")
    if set(c["job_types"]) - set(KINDS):
        raise ValueError(f"job_types must be among {list(KINDS)}")
    for k, v in c["priority"].items():
        if k not in KINDS or not isinstance(v, int) or not 0 <= v <= 9:
            raise ValueError("priority must map master/short to 0..9")
    if not isinstance(c["plan"], dict):
        raise ValueError("plan must map kind -> list of task names")
    for kind in KINDS:
        tasks = c["plan"].get(kind)
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"plan.{kind} must be a non-empty list of task names")
        for t in tasks:
            ProductionJobManifest(task=t)  # same task-name rule as the contract
    if not isinstance(c["task_params"], dict):
        raise ValueError("task_params must map task -> params")
    s = c["stop"]
    if s.get("max_items") is not None and (not isinstance(s["max_items"], int) or s["max_items"] < 1):
        raise ValueError("stop.max_items must be empty or >= 1")
    if s.get("until") is not None and not isinstance(s["until"], (int, float)):
        raise ValueError("stop.until must be empty or a UNIX time")
    if not isinstance(c["resolution"], str) or "x" not in c["resolution"]:
        raise ValueError("resolution must look like 1920x1080")
    return c


def set_config(patch: dict) -> dict:
    c = validate_config(patch)
    _save("config", c)
    return c


def get_state() -> dict:
    return _load("state", DEFAULT_STATE)


# ---------------------------------------------------------------- history / ETA
def task_history(task: str, limit: int = 50) -> list[float]:
    rows = db.query("SELECT started, finished FROM remote_jobs WHERE task = ? AND status IN ('succeeded','done','completed') "
                    "AND started IS NOT NULL AND finished IS NOT NULL ORDER BY finished DESC LIMIT ?", (task, limit))
    return [r["finished"] - r["started"] for r in rows if r["finished"] >= r["started"]]


def median_duration(task: str) -> float | None:
    h = task_history(task)
    return statistics.median(h) if h else None


def _day(now: float) -> str:
    return _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")


def _day_start(now: float) -> float:
    d = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------------------------------------------------------------- the scheduler
class Scheduler:
    def __init__(self, adapter: Adapter | None = None, clock: Callable[[], float] = time.time):
        self.adapter = adapter or Adapter()
        self.clock = clock
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ---- plan
    def desired_slots(self, cfg: dict, day: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(cfg["daily_masters"]):
            for lang in cfg["languages"]:
                mkey = f"{day}/master/{i}/{lang}"
                if "master" in cfg["job_types"]:
                    out[mkey] = {"kind": "master", "idx": str(i), "lang": lang, "depends": None}
                if "short" in cfg["job_types"]:
                    for j in range(cfg["shorts_per_master"]):
                        out[f"{day}/short/{i}.{j}/{lang}"] = {"kind": "short", "idx": f"{i}.{j}", "lang": lang,
                                                              "depends": mkey if "master" in cfg["job_types"] else None}
        return out

    def _plan_day(self, st: dict, cfg: dict, now: float) -> None:
        day = _day(now)
        want = self.desired_slots(cfg, day)
        for key, s in want.items():
            if key not in st["slots"]:
                st["slots"][key] = {**s, "day": day, "tasks": list(cfg["plan"][s["kind"]]), "step": 0, "status": "pending",
                                    "job_id": None, "attempt": 0, "retry_at": None, "history": []}
        for key, s in st["slots"].items():  # target lowered today: drop work that has not started
            if s["day"] == day and key not in want and s["status"] == "pending" and s["step"] == 0:
                s["status"] = "skipped"

    # ---- helpers
    def _active(self, st: dict) -> list[dict]:
        return [s for s in st["slots"].values() if s["status"] == "active"]

    def _manifest(self, key: str, s: dict, cfg: dict) -> ProductionJobManifest:
        task = s["tasks"][s["step"]]
        params = {"kind": s["kind"], "language": s["lang"], "platforms": cfg["platforms"], "resolution": cfg["resolution"],
                  "quality_preset": cfg["quality_preset"], "slot": key, "day": s["day"], "step": s["step"],
                  **(cfg["task_params"].get(task) or {})}
        if s["kind"] == "short":
            params["shorts_index"] = s["idx"]
            dep = s.get("depends")
            if dep:
                params["master_slot"] = dep
        return ProductionJobManifest(
            task=task, owner_account="account1", params=params, priority=cfg["priority"][s["kind"]],
            max_retries=cfg["max_retries"], max_runtime_s=cfg["max_runtime_s"], preferred_providers=cfg["preferred_providers"],
            idempotency_key=f"sched:{key}:s{s['step']}:h{len(s['history'])}")

    def _gpu_seconds_today(self, st: dict, now: float) -> float:
        start = _day_start(now)
        total = 0.0
        for s in st["slots"].values():
            for h in s.get("history", []):
                if h.get("finished") and h["finished"] >= start and h.get("started"):
                    total += max(0.0, h["finished"] - h["started"])
        return total

    def _pause(self, st: dict, reason: str, message: str, event: str = "scheduler.paused") -> None:
        if st["status"] == "paused" and st["pause_reason"] == reason:
            return
        st["status"], st["pause_reason"] = "paused", reason
        events.emit(event, message, reason=reason)
        if event == "scheduler.paused":
            events.notify("warning", "Scheduler paused", message)

    # ---- one idempotent step
    def tick(self, now: float | None = None) -> dict:
        with self._lock:
            now = self.clock() if now is None else now
            cfg, st = get_config(), get_state()
            st["last_tick"], st["blocked"] = now, None
            self._refresh(st, cfg, now)
            if st["status"] in ("running", "stopping"):
                self._check_limits(st, cfg, now)
            if st["status"] == "running":
                self._plan_day(st, cfg, now)
                self._submit(st, cfg, now)
            if st["status"] == "stopping" and not self._active(st):
                st["status"] = "stopped"
                events.emit("scheduler.stopped", "stopped after the current job(s) finished", reason="stop_after_current")
            self._prune(st, now)
            _save("state", st)
            return st

    def _refresh(self, st: dict, cfg: dict, now: float) -> None:
        for key, s in st["slots"].items():
            if s["status"] != "active":
                continue
            try:
                info = self.adapter.status_fn(s["job_id"])
            except Exception as e:  # noqa: BLE001 - a status probe must never crash the loop
                st["last_error"] = f"status of {s['job_id']}: {e}"
                continue
            stat = (info or {}).get("status", "unknown")
            if stat not in TERMINAL:
                continue
            s["history"].append({"job_id": s["job_id"], "task": s["tasks"][s["step"]], "status": stat,
                                 "started": info.get("started"), "finished": info.get("finished") or now,
                                 "error": info.get("error")})
            if stat in TERMINAL_OK:
                s["step"] += 1
                s["attempt"], s["job_id"] = 0, None
                if s["step"] >= len(s["tasks"]):
                    s["status"] = "done"
                    st["items_done"] += 1
                else:
                    s["status"] = "pending"
            elif stat == "quota_exhausted":
                # The work did not fail: it goes back to pending (same attempt) and the scheduler waits for the owner.
                s["status"], s["job_id"] = "pending", None
                self._pause(st, "quota", f"provider quota exhausted while running {key}; waiting for the owner "
                            "(accounts are never rotated to get around a limit)")
                events.emit("quota.exhausted", f"job {info.get('job_id') or s['history'][-1]['job_id']} hit a provider quota")
            elif stat == "cancelled":
                s["status"], s["job_id"] = "cancelled", None
            else:
                if s["attempt"] < cfg["max_retries"]:
                    delay = min(cfg["retry_backoff_s"] * (2 ** s["attempt"]), cfg["max_backoff_s"])
                    s["attempt"] += 1
                    s["status"], s["retry_at"], s["job_id"] = "retry_wait", now + delay, None
                    events.emit("job.retrying", f"{key}: attempt {s['attempt'] + 1} in {int(delay)} s "
                                f"({info.get('error') or stat})", job_id=s["history"][-1]["job_id"], slot=key, delay_s=delay)
                else:
                    s["status"], s["job_id"] = "failed", None
                    events.notify("error", f"Scheduled {s['kind']} failed", f"{key} failed after {s['attempt'] + 1} attempt(s): "
                                  f"{info.get('error') or stat}")
                    for o in st["slots"].values():  # shorts cut from a failed master cannot run
                        if o.get("depends") == key and o["status"] in ("pending", "retry_wait"):
                            o["status"] = "skipped"

    def _check_limits(self, st: dict, cfg: dict, now: float) -> None:
        stop = cfg["stop"]
        if stop.get("until") and now >= stop["until"] and st["status"] == "running":
            st["status"] = "stopping"
            events.emit("scheduler.stopped", "stop date reached; finishing current jobs", reason="date")
        if stop.get("max_items") and st["items_done"] >= stop["max_items"] and st["status"] == "running":
            st["status"] = "stopping"
            events.emit("scheduler.stopped", f"target of {stop['max_items']} item(s) reached; finishing current jobs", reason="count")
        if st["status"] != "running":
            return
        try:
            quotas = self.adapter.quota_fn() or []
        except Exception as e:  # noqa: BLE001
            quotas = []
            st["last_error"] = f"quota probe: {e}"
        ex = [q for q in quotas if q.get("exhausted")]
        if ex:
            q = ex[0]
            self._pause(st, "quota", f"{q.get('provider')}/{q.get('connection_id')} {q.get('resource')} quota exhausted "
                        f"(used {q.get('used')} of {q.get('limit')}); scheduler paused until the owner resumes")
            events.emit("quota.exhausted", f"{q.get('provider')} {q.get('resource')} exhausted", connection_id=q.get("connection_id"))
            return
        try:
            sto = self.adapter.storage_fn() or {}
        except Exception as e:  # noqa: BLE001
            sto = {}
            st["last_error"] = f"storage probe: {e}"
        why = None
        if cfg["max_storage_bytes"] is not None and sto.get("used_bytes", 0) >= cfg["max_storage_bytes"]:
            why = f"stored {sto.get('used_bytes')} bytes >= limit {cfg['max_storage_bytes']}"
        elif sto.get("free_disk_bytes") is not None and cfg["min_free_disk_bytes"] and sto["free_disk_bytes"] < cfg["min_free_disk_bytes"]:
            why = f"control-plane free disk {sto['free_disk_bytes']} bytes below {cfg['min_free_disk_bytes']}"
        elif sto.get("exhausted_connections"):
            why = f"storage full on {', '.join(sto['exhausted_connections'])}"
        if why:
            self._pause(st, "storage", f"storage exhausted: {why}; scheduler paused until the owner frees space and resumes")

    def _submit(self, st: dict, cfg: dict, now: float) -> None:
        if cfg["max_gpu_seconds_day"] is not None and self._gpu_seconds_today(st, now) >= cfg["max_gpu_seconds_day"]:
            st["blocked"] = "daily GPU-seconds budget reached; new work starts tomorrow (UTC)"
            return
        free = cfg["max_active_jobs"] - len(self._active(st))
        if free <= 0:
            st["blocked"] = "max active jobs reached"
            return
        ready = []
        for key, s in st["slots"].items():
            if s["status"] == "retry_wait" and s["retry_at"] is not None and now >= s["retry_at"]:
                s["status"] = "pending"
            if s["status"] != "pending":
                continue
            dep = st["slots"].get(s.get("depends") or "")
            if dep is not None and dep["status"] != "done":
                continue
            ready.append((cfg["priority"][s["kind"]], s["day"], KINDS.index(s["kind"]), key))
        for *_, key in sorted(ready)[:free]:
            s = st["slots"][key]
            m = self._manifest(key, s, cfg)
            try:
                jid = self.adapter.submit_fn(m)
            except ComputeUnavailable as e:
                st["blocked"] = st["last_error"] = str(e)
                return
            except Exception as e:  # noqa: BLE001
                st["last_error"] = f"submit {key}: {e}"
                return
            s["status"], s["job_id"], s["submitted_at"] = "active", jid, now
            events.emit("job.submitted", f"scheduler submitted {m.task} for {key}", job_id=jid, slot=key, task=m.task)

    def _prune(self, st: dict, now: float) -> None:
        keep_from = _day(now - 3 * 86400)
        st["slots"] = {k: s for k, s in st["slots"].items() if s["day"] >= keep_from or s["status"] in ("active", "retry_wait")}

    # ---- owner controls
    def _set(self, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            st = get_state()
            fn(st)
            st["updated_at"] = self.clock()
            _save("state", st)
            return st

    def start(self) -> dict:
        def f(st):
            if st["status"] != "running":
                st.update(status="running", pause_reason=None, started_at=self.clock(), items_done=0, last_error=None)
                day = _day(self.clock())
                for sl in st["slots"].values():  # today's work cancelled by an earlier stop is planned again
                    if sl["day"] == day and sl["status"] == "cancelled":
                        sl["status"], sl["attempt"], sl["retry_at"] = "pending", 0, None
                events.emit("scheduler.resumed", "scheduler started")
        return self._set(f)

    def pause(self, reason: str = "owner") -> dict:
        def f(st):
            if st["status"] in ("running", "stopping"):
                st.update(status="paused", pause_reason=reason)
                events.emit("scheduler.paused", f"paused by {reason}", reason=reason)
        return self._set(f)

    def resume(self) -> dict:
        def f(st):
            if st["status"] == "paused":
                st.update(status="running", pause_reason=None)
                events.emit("scheduler.resumed", "resumed by owner")
        return self._set(f)

    def stop_after_current(self) -> dict:
        def f(st):
            if st["status"] in ("running", "paused"):
                st.update(status="stopping", pause_reason=None)
                events.emit("scheduler.paused", "stop requested after the current job(s)", reason="stop_after_current")
        return self._set(f)

    def stop(self) -> dict:
        """Safe cancel: ask the compute layer to cancel (it checkpoints) every active scheduler job, then stop."""
        def f(st):
            for s in st["slots"].values():
                if s["status"] == "active" and s["job_id"]:
                    try:
                        self.adapter.cancel_fn(s["job_id"])
                    except Exception as e:  # noqa: BLE001
                        st["last_error"] = f"cancel {s['job_id']}: {e}"
                    s["history"].append({"job_id": s["job_id"], "task": s["tasks"][s["step"]], "status": "cancel_requested",
                                         "finished": self.clock()})
                    s["status"], s["job_id"] = "cancelled", None
                elif s["status"] in ("pending", "retry_wait"):
                    s["status"] = "cancelled"
            if st["status"] != "stopped":
                st.update(status="stopped", pause_reason=None)
                events.emit("scheduler.stopped", "stopped by owner; active jobs asked to cancel safely", reason="owner")
        return self._set(f)

    # ---- reporting
    def snapshot(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        cfg, st = get_config(), get_state()
        day = _day(now)
        today = [s for s in st["slots"].values() if s["day"] == day]
        planned = self.desired_slots(cfg, day)
        counts = {k: {"target": sum(1 for p in planned.values() if p["kind"] == k),
                      "done": sum(1 for s in today if s["kind"] == k and s["status"] == "done")} for k in KINDS}
        by = {}
        for s in today:
            by[s["status"]] = by.get(s["status"], 0) + 1
        remaining_tasks: dict[str, int] = {}
        for key, p in planned.items():
            s = st["slots"].get(key)
            if s and s["status"] in ("done", "failed", "skipped", "cancelled"):
                continue
            tasks = cfg["plan"][p["kind"]]
            for t in tasks[(s or {}).get("step", 0):]:
                remaining_tasks[t] = remaining_tasks.get(t, 0) + 1
        eta = {"tasks": {}, "remaining_s": None, "basis": "median duration of completed remote jobs of the same task"}
        total, known = 0.0, True
        for t, n in remaining_tasks.items():
            med = median_duration(t)
            eta["tasks"][t] = {"remaining": n, "median_s": med, "samples": len(task_history(t)),
                               "note": None if med is not None else "no history yet"}
            if med is None:
                known = False
            else:
                total += med * n
        if remaining_tasks and known:
            eta["remaining_s"] = total / max(1, cfg["max_active_jobs"])
        elif not remaining_tasks:
            eta["remaining_s"] = 0.0
        else:
            eta["note"] = "no history yet for " + ", ".join(t for t, v in eta["tasks"].items() if v["median_s"] is None)
        recent = sorted(sorted(st["slots"].items()), key=lambda kv: kv[1]["day"], reverse=True)[:60]  # newest day, then by key
        return {"status": st["status"], "pause_reason": st["pause_reason"], "started_at": st["started_at"],
                "last_tick": st["last_tick"], "last_error": st["last_error"], "blocked": st["blocked"],
                "items_done": st["items_done"], "day": day, "today": counts, "by_status": by,
                "gpu_seconds_today": round(self._gpu_seconds_today(st, now), 1), "eta": eta,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "slots": [{"key": k, **{f: s.get(f) for f in ("kind", "lang", "status", "step", "tasks", "job_id", "attempt", "retry_at")}}
                          for k, s in recent],
                "config": cfg}

    # ---- background loop
    def run_forever(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep the loop alive; the error is visible in the studio
                try:
                    st = get_state()
                    st["last_error"] = f"tick: {e}"
                    _save("state", st)
                except Exception:  # noqa: BLE001
                    pass
            try:
                interval = get_config()["tick_interval_s"]
            except Exception:  # noqa: BLE001
                interval = DEFAULT_CONFIG["tick_interval_s"]
            self._stop_evt.wait(interval)

    def start_thread(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self.run_forever, name="nexus-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop_thread(self) -> None:
        self._stop_evt.set()


_default: Scheduler | None = None


def default() -> Scheduler:
    global _default
    if _default is None:
        _default = Scheduler()
    return _default


__all__ = ["Adapter", "ComputeUnavailable", "DEFAULT_CONFIG", "Scheduler", "default", "get_config", "set_config",
           "get_state", "median_duration"]
