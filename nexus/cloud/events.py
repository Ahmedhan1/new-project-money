"""ProductionEvent / NotificationEvent bus (contracts v1), persisted in SQLite so the Remote Studio, the scheduler and
other accounts read one history. Warnings and errors also become notifications."""
from __future__ import annotations

import json

from .. import db
from ..contracts import NotificationEvent, ProductionEvent

NOTIFY = {"job.failed": "error", "worker.lost": "warning", "quota.exhausted": "warning", "storage.failed": "error",
          "ingest.rejected": "warning", "job.succeeded": "success", "scheduler.stopped": "warning"}


def emit(type: str, message: str = "", job_id: str | None = None, **data) -> ProductionEvent:
    from ..vault import redact
    ev = ProductionEvent(type=type, message=redact(message)[:2000], job_id=job_id, data=data)
    db.execute("INSERT INTO cloud_events(id,job_id,type,message,data,at) VALUES(?,?,?,?,?,?)",
               (ev.event_id, ev.job_id, ev.type, ev.message, json.dumps(ev.data, default=str), ev.at))
    lvl = NOTIFY.get(type)
    if lvl:
        notify(lvl, f"{type.replace('.', ' ')}" + (f" ({job_id})" if job_id else ""), ev.message, ev.event_id)
    return ev


def notify(level: str, title: str, body: str = "", source_event: str | None = None) -> NotificationEvent:
    n = NotificationEvent(level=level, title=title[:200], body=body[:2000], source_event=source_event)
    db.execute("INSERT INTO notifications(id,level,title,body,source_event,read,at) VALUES(?,?,?,?,?,0,?)",
               (n.notification_id, n.level, n.title, n.body, n.source_event, n.at))
    return n


def events(job_id: str | None = None, since: float = 0.0, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM cloud_events WHERE at > ?" + (" AND job_id = ?" if job_id else "") + " ORDER BY at DESC LIMIT ?"
    rows = db.query(q, (since, job_id, limit) if job_id else (since, limit))
    for r in rows:
        r["data"] = json.loads(r["data"] or "{}")
    return rows


def notifications(unread_only: bool = False, limit: int = 100) -> list[dict]:
    return db.query("SELECT * FROM notifications" + (" WHERE read = 0" if unread_only else "") + " ORDER BY at DESC LIMIT ?", (limit,))


def mark_read(ids: list[str] | None = None) -> int:
    if ids:
        return sum(db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (i,)).rowcount for i in ids)
    return db.execute("UPDATE notifications SET read = 1 WHERE read = 0").rowcount
