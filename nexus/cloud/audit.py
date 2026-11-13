"""Security audit log (sub-agent A): login ok/fail, lockouts, sessions, worker tokens, connection changes, scans.

Stored in SQLite (`security_audit`, created lazily with CREATE TABLE IF NOT EXISTS - no db migration needed). Rows never
hold a secret: detail keys that look secret are dropped, every string goes through vault.redact, and values are
length-capped. Old rows are pruned (RETENTION_S / MAX_ROWS) so an attacker cannot fill the disk with failures.

    record(event, ok=True, client=None, actor="owner", **detail)
    entries(limit=200, since=0.0, event=None) -> list[dict]
"""
from __future__ import annotations

import json
import re
import time

from .. import db

TABLE = """CREATE TABLE IF NOT EXISTS security_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, event TEXT,
  ok INTEGER, actor TEXT, client TEXT, detail TEXT)"""
INDEX = "CREATE INDEX IF NOT EXISTS security_audit_at ON security_audit(at)"
EVENTS = {"auth.fail", "auth.locked", "session.open", "session.close", "worker_token.issued", "worker_token.revoked",
          "worker.auth_fail", "connection.connect", "connection.disconnect", "oauth.start", "oauth.callback",
          "secret.put", "secret.delete", "scan.run", "dispatch.remote", "service.start", "service.stop",
          "service.unclean_restart", "backup.run", "restore.run", "vault_key.export", "other"}
SECRET_KEY_RE = re.compile(r"token|secret|password|passwd|refresh|authorization|cookie|api_?key|credential|code_verifier",
                           re.I)
RETENTION_S = 90 * 86400
MAX_ROWS = 20_000
_ready_conn = None  # the connection the table was ensured on (held, so a new connection never reuses its id)
_writes = 0


def _ensure() -> None:
    global _ready_conn
    c = db.conn()
    if c is _ready_conn:
        return
    with db._lock:
        c.execute(TABLE)
        c.execute(INDEX)
        c.commit()
    _ready_conn = c


def _clean(v, depth: int = 0):
    from ..vault import redact
    if depth > 3:
        return "[nested]"
    if isinstance(v, dict):
        return {str(k)[:64]: ("[removed]" if SECRET_KEY_RE.search(str(k)) else _clean(x, depth + 1)) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in list(v)[:30]]
    if isinstance(v, str):
        return redact(v)[:500]
    if v is None or isinstance(v, (int, float, bool)):
        return v
    return redact(str(v))[:500]


def record(event: str, ok: bool = True, client: str | None = None, actor: str = "owner", **detail) -> None:
    global _writes
    if event not in EVENTS:
        detail = {"event": event, **detail}
        event = "other"
    _ensure()
    db.execute("INSERT INTO security_audit(at,event,ok,actor,client,detail) VALUES(?,?,?,?,?,?)",
               (time.time(), event, 1 if ok else 0, (actor or "")[:64], (client or "")[:64],
                json.dumps(_clean(detail), default=str)))
    _writes += 1
    if _writes % 200 == 0:
        prune()


def prune(now: float | None = None) -> int:
    _ensure()
    now = now or time.time()
    n = db.execute("DELETE FROM security_audit WHERE at < ?", (now - RETENTION_S,)).rowcount
    n += db.execute("DELETE FROM security_audit WHERE id <= (SELECT MAX(id) FROM security_audit) - ?", (MAX_ROWS,)).rowcount
    return n


def entries(limit: int = 200, since: float = 0.0, event: str | None = None) -> list[dict]:
    _ensure()
    q = "SELECT * FROM security_audit WHERE at > ?" + (" AND event = ?" if event else "") + " ORDER BY id DESC LIMIT ?"
    rows = db.query(q, (since, event, limit) if event else (since, limit))
    for r in rows:
        r["ok"] = bool(r["ok"])
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows
