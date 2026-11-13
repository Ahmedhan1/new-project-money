"""One-command start/stop: preflight checks -> recover jobs -> worker + API as background processes ->
health check -> open the browser. Used by NEXUS_START.bat / NEXUS_STOP.bat and `python -m nexus start|stop`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import psutil

from . import config

SERVICES = ("api", "worker")


def _pidfile(name: str) -> Path:
    return config.RUN / f"{name}.json"


def _alive(name: str) -> dict | None:
    f = _pidfile(name)
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        p = psutil.Process(info["pid"])
        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE and abs(p.create_time() - info["started"]) < 5:
            return info
    except (psutil.Error, ValueError, KeyError, OSError):
        pass
    f.unlink(missing_ok=True)
    return None


def preflight() -> list[dict]:
    """Checks that must pass before services start. Each: {check, ok, detail, fix}."""
    s = config.settings()
    out = []

    def add(check, ok, detail, fix=""):
        out.append({"check": check, "ok": bool(ok), "detail": detail, "fix": fix})

    add("python", sys.version_info >= (3, 10), sys.version.split()[0], "install Python 3.10+ and run NEXUS_SETUP")
    missing = []
    for mod in ("fastapi", "uvicorn", "yaml", "PIL", "numpy", "psutil"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    add("python packages", not missing, "all core packages importable" if not missing else f"missing: {', '.join(missing)}", "run NEXUS_SETUP")
    add("ffmpeg", shutil.which("ffmpeg") and shutil.which("ffprobe"), shutil.which("ffmpeg") or "not found on PATH",
        "run NEXUS_SETUP (installs FFmpeg) or install it and reopen the terminal")
    try:
        config.ensure_dirs()
        free = shutil.disk_usage(config.HOME).free / 1e9
        add("disk space", free >= s["min_free_disk_gb"], f"{free:.1f} GB free in {config.HOME}", f"free at least {s['min_free_disk_gb']} GB")
    except OSError as e:
        add("data folder", False, f"{config.HOME}: {e}", "check NEXUS_HOME / folder permissions")
    try:
        from . import db
        add("database", True, f"{config.DB_PATH.name} schema v{db.schema_version()}")
    except db.DatabaseDamaged as e:
        add("database", False, str(e), "python -m nexus restore <new folder>  (NEXUS_RESTORE.bat)")
    except Exception as e:
        add("database", False, f"{type(e).__name__}: {e}", "restore the database from a backup: NEXUS_RESTORE")
    return out


LOG_MAX_BYTES, LOG_KEEP = 20 << 20, 5


def rotate_log(log: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> bool:
    """Size-based rotation at service start: name.log -> name.log.1 -> ... -> name.log.<keep> (oldest dropped).
    Service logs never grow without bound and the last `keep` generations stay available for diagnosis."""
    try:
        if not log.exists() or log.stat().st_size < max_bytes:
            return False
        oldest = log.with_name(f"{log.name}.{keep}")
        oldest.unlink(missing_ok=True)
        for i in range(keep - 1, 0, -1):
            src = log.with_name(f"{log.name}.{i}")
            if src.exists():
                os.replace(src, log.with_name(f"{log.name}.{i + 1}"))
        os.replace(log, log.with_name(f"{log.name}.1"))
        return True
    except OSError:
        return False


def _spawn(name: str, args: list[str], env: dict) -> dict:
    log = config.LOGS / f"{name}.log"
    rotate_log(log)
    f = open(log, "ab")
    kw: dict = {"creationflags": 0x00000008 | 0x00000200 | 0x08000000} if os.name == "nt" else {"start_new_session": True}  # detached, no console
    p = subprocess.Popen([sys.executable, "-m", "nexus", *args], cwd=str(config.ROOT), stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=env, **kw)
    info = {"pid": p.pid, "started": psutil.Process(p.pid).create_time(), "log": str(log), "args": args}
    _pidfile(name).write_text(json.dumps(info))
    return info


def _health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _tail(path: str, n: int = 15) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def start(port: int | None = None, open_browser: bool = True, wait: float = 90.0, say=print) -> int:
    s = config.settings()
    port = port or s["port"]
    url = f"http://{s['host']}:{port}"
    api = _alive("api")
    if api and _health(f"http://{s['host']}:{api.get('port', port)}"):
        url = f"http://{s['host']}:{api.get('port', port)}"
        say(f"NEXUS is already running at {url}")
        if not _alive("worker"):
            _spawn("worker", ["worker"], {**os.environ, "NEXUS_HOME": str(config.HOME)})
            say("worker was down - restarted")
        if open_browser:
            webbrowser.open(url)
        return 0
    checks = preflight()
    for c in checks:
        say(f"[{'OK ' if c['ok'] else 'FAIL'}] {c['check']:<16} {c['detail']}")
    bad = [c for c in checks if not c["ok"]]
    if bad:
        say("\nNEXUS cannot start:")
        for c in bad:
            say(f"  - {c['check']}: {c['detail']}\n    fix: {c['fix']}")
        return 2
    if any(c.laddr.port == port for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN):
        say(f"\nNEXUS cannot start: port {port} is already used by another program.\n"
            f"  fix: close it, or choose another port: set NEXUS_PORT=8770 (or \"port\" in {config.HOME / 'settings.json'})")
        return 3
    from . import jobs
    # no worker alive -> every 'running' durable job is an orphan; a live worker keeps its job (heartbeat decides)
    rec = jobs.recover(stale_after=jobs.STALE_S if _alive("worker") else 0, api_worker="")
    if any(rec.values()):
        say(f"recovered jobs: {len(rec['requeued'])} resumed from checkpoint, {len(rec['interrupted'])} need re-running, {len(rec['failed'])} failed")
    env = {**os.environ, "NEXUS_HOME": str(config.HOME), "NEXUS_EMBEDDED_WORKER": "0", "PYTHONUNBUFFERED": "1"}
    if not _alive("worker"):
        _spawn("worker", ["worker"], env)
    info = _spawn("api", ["serve", "--port", str(port), "--host", s["host"]], env)
    info["port"] = port
    _pidfile("api").write_text(json.dumps(info))
    t0 = time.time()
    while time.time() - t0 < wait:
        h = _health(url)
        if h:
            say(f"\nNEXUS {h['version']} is running at {url}   (stop with NEXUS_STOP)")
            if open_browser:
                webbrowser.open(url)
            return 0
        if not _alive("api"):
            break
        time.sleep(0.5)
    say(f"\nNEXUS failed to start (API did not become healthy). Last log lines from {info['log']}:\n{_tail(info['log'])}")
    stop(say=lambda *_: None)
    return 4


def stop(say=print) -> int:
    """Stop services. Running durable jobs are not lost: they resume from their last checkpoint on next start."""
    n = 0
    for name in SERVICES:
        info = _alive(name)
        if not info:
            continue
        try:
            p = psutil.Process(info["pid"])
            procs = [p] + p.children(recursive=True)
            for q in procs:
                q.terminate()
            _, alive = psutil.wait_procs(procs, timeout=10)
            for q in alive:
                q.kill()
            n += 1
        except psutil.Error:
            pass
        _pidfile(name).unlink(missing_ok=True)
    say(f"stopped {n} service(s)" if n else "NEXUS was not running")
    return 0


def restart(port: int | None = None, open_browser: bool = False, say=print) -> int:
    """Stop then start (jobs resume from their checkpoints). Used by the System page and `nexus restart`."""
    stop(say=say)
    time.sleep(1.0)
    return start(port=port, open_browser=open_browser, say=say)


def service_status() -> dict:
    s = config.settings()
    api, worker = _alive("api"), _alive("worker")
    url = f"http://{s['host']}:{(api or {}).get('port', s['port'])}"
    return {"api": bool(api), "worker": bool(worker), "url": url, "healthy": bool(api and _health(url)), "home": str(config.HOME)}
