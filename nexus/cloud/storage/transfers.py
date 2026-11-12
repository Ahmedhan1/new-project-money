"""Persistent transfer ledger (table `transfers`, db migration 2) so an interrupted upload/download survives a crash.

Columns used: id, connection_id, direction, local_path, remote_ref (destination path while running), session_uri,
size, sent (last byte count the PROVIDER acknowledged), sha256, status, error, created, updated. Extra non-secret
fields (mime, chunk size, app properties, the finished file's provider ref) live in the `docs` table under kind
"transfer_meta" because the shared schema has no column for them.

The session URI of a Drive resumable upload is a short-lived capability for that one upload; it is stored here
server-side and never returned by the API (`public()` replaces it with `has_session`).
"""
from __future__ import annotations

import time

from ... import db

STATUSES = ("pending", "running", "interrupted", "done", "failed", "cancelled")


def create(connection_id: str, direction: str, local_path: str, remote_ref: str, size: int, sha256: str,
           transfer_id: str | None = None, meta: dict | None = None) -> dict:
    tid = transfer_id or db.new_id("xfer")
    now = time.time()
    db.execute("INSERT INTO transfers(id,connection_id,direction,local_path,remote_ref,session_uri,size,sent,sha256,status,"
               "error,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (tid, connection_id, direction, local_path, remote_ref, None, size, 0, sha256, "pending", None, now, now))
    db.put_doc("transfer_meta", {"meta": meta or {}, "result_ref": None}, tid)
    return get(tid)


def get(tid: str) -> dict | None:
    rows = db.query("SELECT * FROM transfers WHERE id=?", (tid,))
    if not rows:
        return None
    t = rows[0]
    extra = db.get_doc("transfer_meta", tid) or {}
    t["meta"] = extra.get("meta") or {}
    t["result_ref"] = extra.get("result_ref")
    return t


def update(tid: str, **kv) -> None:
    result_ref = kv.pop("result_ref", None)
    if kv:
        cols = ", ".join(f"{k}=?" for k in kv)
        db.execute(f"UPDATE transfers SET {cols}, updated=? WHERE id=?", (*kv.values(), time.time(), tid))
    if result_ref is not None:
        extra = db.get_doc("transfer_meta", tid) or {"meta": {}}
        extra["result_ref"] = result_ref
        db.put_doc("transfer_meta", extra, tid)


def list_all(status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        rows = db.query("SELECT id FROM transfers WHERE status=? ORDER BY created DESC LIMIT ?", (status, limit))
    else:
        rows = db.query("SELECT id FROM transfers ORDER BY created DESC LIMIT ?", (limit,))
    return [get(r["id"]) for r in rows]


def public(t: dict) -> dict:
    """API view: no session URI (a capability), no local absolute path beyond the file name."""
    return {"id": t["id"], "connection_id": t["connection_id"], "direction": t["direction"],
            "file": t["local_path"].replace("\\", "/").rsplit("/", 1)[-1], "destination": t["remote_ref"],
            "size": t["size"], "sent": t["sent"], "progress": round((t["sent"] or 0) / t["size"], 4) if t["size"] else None,
            "sha256": t["sha256"], "status": t["status"], "error": t["error"], "has_session": bool(t.get("session_uri")),
            "result_ref": t.get("result_ref"), "created": t["created"], "updated": t["updated"]}


def resumable() -> list[dict]:
    return [t for t in list_all() if t["status"] in ("running", "interrupted", "pending") and t["direction"] == "upload"]
