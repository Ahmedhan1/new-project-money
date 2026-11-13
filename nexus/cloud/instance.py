"""Single-instance lock for the API process (Account 1, production profile).

The control plane is a single-process service by design: the brute-force counters (security._fail), the remote-job
poller and the alert loop live in this process's memory, and SQLite is the only state store. Two API processes on one
NEXUS_HOME would each keep their own rate-limit window (an attacker would get N x the attempts) and would poll the same
remote jobs twice. Production therefore refuses to start a second API process on the same NEXUS_HOME; uvicorn is
always run with one worker (`python -m nexus serve`). Horizontal scaling is not supported (see
docs/ACCOUNT1_PRODUCTION_DEPLOYMENT.md, "Single instance").

The lock is an OS advisory lock on NEXUS_HOME/run/<name>.lock (fcntl on Linux/macOS, msvcrt on Windows), so it is
released by the kernel when the process dies: a crash never leaves a stale lock behind.
"""
from __future__ import annotations

import os
import time

from .. import config

_held: dict[str, object] = {}


def _path(name: str):
    config.RUN.mkdir(parents=True, exist_ok=True)
    return config.RUN / f"{name}.lock"


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire(name: str = "api") -> bool:
    """Take the lock for this process. True if taken (or already held by this process), False if another process
    holds it."""
    if name in _held:
        return True
    fh = open(_path(name), "a+b")
    if os.name == "nt":  # msvcrt locks a byte range: make sure byte 0 exists
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
    if not _try_lock(fh):
        fh.close()
        return False
    _held[name] = fh
    try:
        (config.RUN / f"{name}.lock.info").write_text(f"pid={os.getpid()} since={int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass
    return True


def release(name: str = "api") -> None:
    fh = _held.pop(name, None)
    if fh is None:
        return
    _unlock(fh)
    fh.close()
    try:
        (config.RUN / f"{name}.lock.info").unlink()
    except OSError:
        pass


def held_here(name: str = "api") -> bool:
    return name in _held


def available(name: str = "api") -> bool:
    """True if this process holds the lock or nobody does (probe-and-release; never keeps it)."""
    if name in _held:
        return True
    fh = open(_path(name), "a+b")
    try:
        if os.name == "nt":
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
        ok = _try_lock(fh)
        if ok:
            _unlock(fh)
        return ok
    finally:
        fh.close()
