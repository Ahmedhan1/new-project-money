"""Service lifecycle (Account 1, production profile): start/stop are audited, an unclean previous exit is detected and
raised to the owner, and a graceful shutdown stops the background loops and releases the single-instance lock.

Graceful shutdown (SIGTERM/SIGINT -> uvicorn -> FastAPI lifespan exit):
  1. stop accepting new remote polls (poller), alert checks and scheduler ticks (no new dispatches);
  2. in-flight durable jobs are NOT killed mid-write: they stay 'running' in the SQLite ledger and the next start
     re-queues them from their last checkpoint (jobs.recover); remote Kaggle jobs keep running on Kaggle and are
     adopted again by job id (no second push);
  3. the marker NEXUS_HOME/run/api.running is removed and the lock released.
A start that finds the marker still present means the previous process died without step 3 (crash, kill -9, power
loss): it is recorded as `service.unclean_restart` and the owner gets a notification.
"""
from __future__ import annotations

import json
import os
import time

from .. import config

MARKER = "api.running"


def _marker():
    config.RUN.mkdir(parents=True, exist_ok=True)
    return config.RUN / MARKER


def _audit(event: str, ok: bool = True, **detail) -> None:
    try:
        from . import audit
        audit.record(event, ok=ok, actor="system", **detail)
    except Exception:  # noqa: BLE001 - lifecycle bookkeeping never blocks start or stop
        pass


def started() -> dict:
    from . import production
    m = _marker()
    unclean = None
    if m.exists():
        try:
            unclean = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unclean = {}
        _audit("service.unclean_restart", False, previous_pid=unclean.get("pid"), previous_start=unclean.get("since"))
        try:
            from . import events
            events.notify("warning", "NEXUS restarted after an unclean stop",
                          "The previous API process ended without a clean shutdown (crash, kill or power loss). Durable jobs "
                          "resume from their last checkpoint; check Job queue and the service log.")
        except Exception:  # noqa: BLE001
            pass
    info = {"pid": os.getpid(), "since": time.time(), "mode": production.mode()}
    try:
        m.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass
    _audit("service.start", True, mode=info["mode"], pid=info["pid"], after_unclean_stop=unclean is not None)
    return {"unclean_previous_stop": unclean is not None, **info}


def stopping() -> None:
    for stop in _stoppers():
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass
    _audit("service.stop", True, pid=os.getpid())
    try:
        _marker().unlink()
    except OSError:
        pass
    try:
        from . import instance
        instance.release("api")
    except Exception:  # noqa: BLE001
        pass


def _stoppers():
    out = []
    try:
        from .compute import poller
        out.append(poller.stop)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import alerts
        out.append(alerts.stop)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import scheduler
        if scheduler._default is not None:
            out.append(scheduler._default.stop_thread)
    except Exception:  # noqa: BLE001
        pass
    return out
