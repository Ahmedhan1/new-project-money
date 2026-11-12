"""Operational alerts for the owner (Account 1, Part 8). `check()` is idempotent: each condition notifies once per
`REPEAT_S` while it lasts, so a long condition never floods the notification list.

Conditions: storage use >= WARN/CRIT of a connection's provider-reported quota; a connection that needs
reauthorisation; failed transfers; repeated failures of one task; lost workers; Kaggle GPU quota use >= QUOTA_WARN of
the allowance. Quota alerts say whether the figure is provider-reported or NEXUS's own measurement - an estimate is
never shown as an exact provider quota.
"""
from __future__ import annotations

import json
import threading
import time

from .. import db
from . import events

STORAGE_WARN, STORAGE_CRIT, QUOTA_WARN = 0.80, 0.90, 0.80
REPEAT_S = 6 * 3600
FAILS_IN_DAY = 3


def _once(key: str, level: str, title: str, body: str, now: float) -> dict | None:
    db.execute("CREATE TABLE IF NOT EXISTS alert_state(key TEXT PRIMARY KEY, last REAL, title TEXT)")
    r = db.query("SELECT last FROM alert_state WHERE key=?", (key,))
    if r and now - (r[0]["last"] or 0) < REPEAT_S:
        return None
    db.execute("INSERT OR REPLACE INTO alert_state(key,last,title) VALUES(?,?,?)", (key, now, title))
    events.notify(level, title, body)
    return {"key": key, "level": level, "title": title, "body": body}


def _storage(now: float) -> list[dict]:
    out = []
    try:
        from .storage import artifacts, connections
        rows = [connections.public(c) for c in connections.all_storage()]
    except Exception:  # noqa: BLE001 - storage package not installed in this build
        return out
    for c in rows:
        cid = c.get("id")
        if c.get("reauth_needed") or c.get("status") == "reauth_required":
            out.append(("reauth:" + cid, "warning", f"{cid} needs reauthorisation",
                        "Google refused the stored sign-in; reconnect it in Drive & storage (device code)."))
        if c.get("status") != "connected" and cid != "local":
            continue
        try:  # fresh figure from the provider itself (Drive: about.storageQuota; local: real disk space)
            q = artifacts.provider(cid).quota().model_dump()
        except Exception:  # noqa: BLE001
            continue
        used, limit = q.get("used"), q.get("limit")
        if used is None or not limit:
            continue
        share = used / limit
        if share >= STORAGE_WARN:
            lvl = "error" if share >= STORAGE_CRIT else "warning"
            out.append((f"storage:{cid}:{lvl}", lvl, f"{cid} storage {share:.0%} full",
                        f"{used / 2**30:.2f} of {limit / 2**30:.2f} GiB used ({q.get('source') or 'provider'} figure). New uploads go to the "
                        "next destination with room in your storage rules; nothing is deleted automatically."))
    return [_once(k, lvl, t, b, now) for k, lvl, t, b in out]


def _transfers(now: float) -> list[dict]:
    try:
        rows = db.query("SELECT id, connection_id, error FROM transfers WHERE status='failed' AND updated > ?", (now - 86400,))
    except Exception:  # noqa: BLE001
        return []
    return [_once(f"transfer:{r['id']}", "error", f"transfer to {r['connection_id']} failed",
                  f"{(r['error'] or '')[:300]} - resume it from Drive & storage -> Transfers.", now) for r in rows]


def _jobs(now: float) -> list[dict]:
    out = []
    for r in db.query("SELECT task, COUNT(*) n FROM remote_jobs WHERE status='failed' AND finished > ? GROUP BY task", (now - 86400,)):
        if r["n"] >= FAILS_IN_DAY:
            out.append(_once(f"fails:{r['task']}", "error", f"{r['n']} failed '{r['task']}' jobs in 24 h",
                             "Repeated failures of the same task: check the job log in Job queue before scheduling more.", now))
    lost = db.query("SELECT COUNT(*) n FROM cloud_events WHERE type='worker.lost' AND at > ?", (now - 3600,))[0]["n"]
    if lost:
        out.append(_once(f"lost:{int(now // 3600)}", "warning", f"{lost} worker loss(es) in the last hour",
                         "Jobs resume from their last checkpoint automatically; repeated losses point to the provider.", now))
    return out


def _quota(now: float) -> list[dict]:
    try:
        from .compute import dispatcher
        qs = dispatcher.quota_status()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for q in qs:
        q = q.model_dump() if hasattr(q, "model_dump") else q
        if q.get("limit") and q.get("used") is not None and q["used"] / q["limit"] >= QUOTA_WARN:
            kind = "provider-reported" if q.get("source") == "provider_api" else "NEXUS's own measurement against the documented allowance (an estimate)"
            out.append(_once(f"quota:{q['provider']}:{q['resource']}", "warning",
                             f"{q['provider']} {q['resource']} at {q['used'] / q['limit']:.0%}",
                             f"{q['used']:.0f} of {q['limit']:.0f} ({kind}). GPU jobs pause when it runs out.", now))
    return out


def check(now: float | None = None) -> list[dict]:
    now = now or time.time()
    fired = []
    for fn in (_storage, _transfers, _jobs, _quota):
        try:
            fired += [a for a in fn(now) if a]
        except Exception:  # noqa: BLE001 - one broken check must not stop the others
            continue
    return fired


def active(now: float | None = None) -> list[dict]:
    """Conditions currently true (for the dashboard), without notifying."""
    now = now or time.time()
    db.execute("CREATE TABLE IF NOT EXISTS alert_state(key TEXT PRIMARY KEY, last REAL, title TEXT)")
    return db.query("SELECT key, title, last FROM alert_state WHERE last > ? ORDER BY last DESC", (now - REPEAT_S,))


_thread: threading.Thread | None = None


def start(interval: float = 300.0) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        while True:
            check()
            time.sleep(interval)
    _thread = threading.Thread(target=loop, daemon=True, name="nexus-alerts")
    _thread.start()


def _dump(x) -> str:
    return json.dumps(x, default=str)
