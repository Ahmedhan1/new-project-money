"""Background polling for remote jobs.

Two integrations (pick by deployment):
* `start()` - a daemon thread in the API process that calls Dispatcher.tick() every few seconds while remote jobs are
  active (per-job polling uses exponential backoff, so Kaggle is asked at most every 5-60 s per job). The API should call
  `nexus.cloud.compute.poller.start()` on startup.
* the durable job handler "remote-compute-tick" (registered when this module is imported, e.g. from nexus/workflows.py)
  runs one bounded series of ticks inside `python -m nexus worker`, for deployments without a long-lived API process.
  It is short on purpose: nexus.jobs runs handlers under the machine-wide heavy lock, and waiting on Kaggle must never
  block a local render.
"""
from __future__ import annotations

import threading
import time

from ... import jobs

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop(interval: float) -> None:
    from . import dispatcher
    while not _stop.wait(interval):
        try:
            d = dispatcher.get()
            if d.active():
                d.tick()
        except Exception:  # noqa: BLE001 - the poller must survive a locked DB or a provider hiccup
            time.sleep(interval)


def start(interval: float = 3.0) -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), daemon=True, name="nexus-remote-compute")
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


@jobs.handler("remote-compute-tick")
def tick_handler(job) -> dict:
    """Poll/dispatch remote jobs for up to params.seconds (default 20)."""
    from . import dispatcher
    d = dispatcher.get()
    end = time.time() + float(job.params.get("seconds", 20))
    last = {}
    while time.time() < end:
        last = d.tick()
        if not d.active():
            break
        time.sleep(2)
    return {"active": d.active(), "last_tick": last}
