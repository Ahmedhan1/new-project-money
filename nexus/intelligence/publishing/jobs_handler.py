"""Durable job handler `publish-upload` (nexus.jobs ledger).

Wiring (lead): add `from .intelligence.publishing import jobs_handler  # noqa: F401` to nexus/workflows.py so workers
know the kind. Submit with `submit(manifest_id)`. A transient failure re-raises so the ledger re-queues the job
(`max_retries`); the manifest keeps its resumable upload session, so the next attempt resumes instead of restarting.
Owner-action failures (credentials, unapproved app, quota) finish the job with the manifest `failed` + `blocked`.
"""
from __future__ import annotations

import time

from ... import jobs
from . import engine

KIND = "publish-upload"
MAX_WAIT_S = 300.0


def submit(manifest_id: str, max_retries: int = engine.MAX_ATTEMPTS) -> dict:
    m = engine.get(manifest_id)
    if m["status"] not in ("queued",):
        m = engine.enqueue(manifest_id)
    return jobs.submit(KIND, {"manifest_id": manifest_id}, project_id=m["project_id"], max_retries=max_retries)


@jobs.handler(KIND)
def run(job) -> dict:
    mid = job.params["manifest_id"]
    m = engine.get(mid)
    due = (m.get("tracking") or {}).get("due")
    if m["status"] == "queued" and due and due > time.time():  # honour the backoff / schedule, bounded
        time.sleep(min(MAX_WAIT_S, due - time.time()))
    m = engine.run_one(mid, now=max(time.time(), due or 0))
    job.log(f"manifest {mid}: {m['status']}")
    if m["status"] == "queued":  # transient failure: let the ledger retry from here
        raise RuntimeError(f"publish {mid}: transient failure, retry scheduled ({m['tracking'].get('last_error')})")
    return engine.public_view(m)
