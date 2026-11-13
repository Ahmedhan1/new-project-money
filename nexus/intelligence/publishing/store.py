"""SQLite tables of the publishing package (created on demand in the shared nexus DB; no secrets are stored)."""
from __future__ import annotations

import json
import time

from ... import db

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pub_accounts(id TEXT PRIMARY KEY, platform TEXT, label TEXT, external_id TEXT,"
    " secret_ref TEXT, created REAL, data TEXT)",
    "CREATE TABLE IF NOT EXISTS pub_manifests(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, platform TEXT,"
    " project_id TEXT, status TEXT, due REAL, attempts INTEGER DEFAULT 0, updated REAL, data TEXT)",
    "CREATE TABLE IF NOT EXISTS pub_analytics(platform TEXT, video_id TEXT, date TEXT, source TEXT, project_id TEXT,"
    " imported REAL, data TEXT, PRIMARY KEY(platform, video_id, date, source))",
    "CREATE TABLE IF NOT EXISTS pub_quota(day TEXT, platform TEXT, bucket TEXT, used INTEGER, PRIMARY KEY(day, platform, bucket))",
)


def ensure() -> None:
    for s in SCHEMA:
        db.execute(s)


def save_manifest(m: dict) -> dict:
    ensure()
    tr = m.get("tracking") or {}
    due = tr.get("due")
    db.execute("INSERT INTO pub_manifests(id,idempotency_key,platform,project_id,status,due,attempts,updated,data) VALUES(?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET status=excluded.status, due=excluded.due, attempts=excluded.attempts,"
               " updated=excluded.updated, data=excluded.data",
               (m["id"], m["idempotency_key"], m["platform"], m["project_id"], m["status"], due,
                int(tr.get("attempts", 0)), time.time(), json.dumps(m, ensure_ascii=False)))
    return m


def get_manifest(mid: str) -> dict | None:
    ensure()
    r = db.query("SELECT data FROM pub_manifests WHERE id=?", (mid,))
    return json.loads(r[0]["data"]) if r else None


def by_key(key: str) -> dict | None:
    ensure()
    r = db.query("SELECT data FROM pub_manifests WHERE idempotency_key=?", (key,))
    return json.loads(r[0]["data"]) if r else None


def list_manifests(status: str | None = None, project_id: str | None = None) -> list[dict]:
    ensure()
    sql, args = "SELECT data FROM pub_manifests WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    return [json.loads(r["data"]) for r in db.query(sql + " ORDER BY updated", tuple(args))]


def due(now: float) -> list[dict]:
    ensure()
    return [json.loads(r["data"]) for r in db.query(
        "SELECT data FROM pub_manifests WHERE status='queued' AND (due IS NULL OR due<=?) ORDER BY due, updated", (now,))]


def quota_use(day: str, platform: str, bucket: str, n: int) -> int:
    ensure()
    db.execute("INSERT INTO pub_quota(day,platform,bucket,used) VALUES(?,?,?,?) ON CONFLICT(day,platform,bucket) "
               "DO UPDATE SET used=used+excluded.used", (day, platform, bucket, n))
    return quota_used(day, platform, bucket)


def quota_used(day: str, platform: str, bucket: str) -> int:
    ensure()
    r = db.query("SELECT used FROM pub_quota WHERE day=? AND platform=? AND bucket=?", (day, platform, bucket))
    return int(r[0]["used"]) if r else 0
