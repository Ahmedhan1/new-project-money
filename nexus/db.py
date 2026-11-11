"""SQLite persistence (public-domain engine, no server, zero cost)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, kind TEXT, created REAL, data TEXT);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, status TEXT, worker TEXT,
  progress REAL, created REAL, started REAL, finished REAL, params TEXT, result TEXT, error TEXT, checkpoint TEXT);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, title TEXT, url TEXT, creator TEXT, source TEXT,
  license TEXT, license_url TEXT, attribution TEXT, commercial INTEGER, restrictions TEXT, downloaded REAL,
  path TEXT, sha256 TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS asset_usage(asset_id TEXT, project_id TEXT, used REAL);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, component TEXT, operation TEXT,
  decision TEXT, reason TEXT, cost_usd REAL);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, value REAL, meta TEXT);
CREATE TABLE IF NOT EXISTS docs(kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY(kind,id));
"""


# Schema migrations. Append only; each entry runs once and bumps PRAGMA user_version.
MIGRATIONS: list[list[str]] = [
    [  # 1: durable job ledger (stage checkpoints, retries, heartbeat, recovery notes)
        "ALTER TABLE jobs ADD COLUMN workflow TEXT",
        "ALTER TABLE jobs ADD COLUMN stage TEXT",
        "ALTER TABLE jobs ADD COLUMN stages TEXT",
        "ALTER TABLE jobs ADD COLUMN retries INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN max_retries INTEGER DEFAULT 2",
        "ALTER TABLE jobs ADD COLUMN heartbeat REAL",
        "ALTER TABLE jobs ADD COLUMN durable INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN log TEXT",
        "ALTER TABLE jobs ADD COLUMN recovery TEXT",
        "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created)",
    ],
]


def _migrate(c: sqlite3.Connection) -> None:
    have = c.execute("PRAGMA user_version").fetchone()[0]
    for n, steps in enumerate(MIGRATIONS, start=1):
        if n <= have:
            continue
        for sql in steps:
            try:
                c.execute(sql)
            except sqlite3.OperationalError as e:  # another process migrated at the same moment
                if "duplicate column" not in str(e):
                    raise
        c.execute(f"PRAGMA user_version={n}")
        c.commit()


def schema_version() -> int:
    with _lock:
        return conn().execute("PRAGMA user_version").fetchone()[0]


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            # WAL + busy timeout: the API and the worker are separate processes sharing this file.
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=30000")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# Generic JSON documents (series bibles, channels, calendars, revenue entries...)
def put_doc(kind: str, data: dict, doc_id: str | None = None) -> str:
    doc_id = doc_id or data.get("id") or new_id(kind)
    data["id"] = doc_id
    execute("INSERT OR REPLACE INTO docs(kind,id,created,data) VALUES(?,?,?,?)",
            (kind, doc_id, time.time(), json.dumps(data, ensure_ascii=False)))
    return doc_id


def get_doc(kind: str, doc_id: str) -> dict | None:
    rows = query("SELECT data FROM docs WHERE kind=? AND id=?", (kind, doc_id))
    return json.loads(rows[0]["data"]) if rows else None


def list_docs(kind: str) -> list[dict]:
    return [json.loads(r["data"]) for r in query("SELECT data FROM docs WHERE kind=? ORDER BY created", (kind,))]


def metric(name: str, value: float, **meta: Any) -> None:
    execute("INSERT INTO metrics(ts,name,value,meta) VALUES(?,?,?,?)", (time.time(), name, value, json.dumps(meta)))


def metric_avg(name: str, default: float | None = None) -> float | None:
    rows = query("SELECT AVG(value) v, COUNT(*) n FROM metrics WHERE name=?", (name,))
    return rows[0]["v"] if rows and rows[0]["n"] else default
