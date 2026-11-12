"""Storage connections (table `cloud_connections`): one row per authorised account. Never holds a credential -
tokens live in the vault under `gdrive_<connection_id>`.

Google Drive: at most three connections, ids drive_a, drive_b, drive_c, each its own Google identity. The same Google
account cannot be connected twice (that would double-count its 15 GB and is the kind of account rotation the
providers forbid).
"""
from __future__ import annotations

import json
import time

from ... import db

DRIVE_IDS = ("drive_a", "drive_b", "drive_c")
LOCAL_ID = "local"
STATUSES = ("not_configured", "pending", "connected", "reauth_required", "error", "disconnected")


def provider_of(cid: str) -> str:
    if cid == LOCAL_ID:
        return "local"
    if cid in DRIVE_IDS:
        return "gdrive"
    raise KeyError(f"unknown storage connection {cid!r}; valid: {[LOCAL_ID, *DRIVE_IDS]}")


def ensure(cid: str) -> dict:
    prov = provider_of(cid)
    if not db.query("SELECT id FROM cloud_connections WHERE id=?", (cid,)):
        now = time.time()
        status = "connected" if prov == "local" else "not_configured"
        label = "Local storage (NEXUS_HOME)" if prov == "local" else f"Google Drive {cid[-1].upper()}"
        db.execute("INSERT OR IGNORE INTO cloud_connections(id,provider,label,status,identity,scopes,created,updated,"
                   "last_success,last_error,data) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, prov, label, status, None, "[]", now, now, None, None, "{}"))
    return get(cid)


def get(cid: str) -> dict:
    provider_of(cid)
    rows = db.query("SELECT * FROM cloud_connections WHERE id=?", (cid,))
    if not rows:
        return ensure(cid)
    r = rows[0]
    r["scopes"] = json.loads(r["scopes"] or "[]")
    r["data"] = json.loads(r["data"] or "{}")
    return r


def all_storage() -> list[dict]:
    return [get(c) for c in (LOCAL_ID, *DRIVE_IDS)]


def update(cid: str, **kv) -> dict:
    ensure(cid)
    if "scopes" in kv:
        kv["scopes"] = json.dumps(kv["scopes"])
    if "data" in kv:
        kv["data"] = json.dumps(kv["data"])
    cols = ", ".join(f"{k}=?" for k in kv)
    db.execute(f"UPDATE cloud_connections SET {cols}, updated=? WHERE id=?", (*kv.values(), time.time(), cid))
    return get(cid)


def merge_data(cid: str, **kv) -> dict:
    d = get(cid)["data"]
    d.update(kv)
    return update(cid, data=d)


def success(cid: str) -> None:
    update(cid, last_success=time.time(), last_error=None)


def failure(cid: str, err: str, status: str | None = None) -> None:
    from ...vault import redact
    kv = {"last_error": redact(err)[:500]}
    if status:
        kv["status"] = status
    update(cid, **kv)


def identity_in_use(identity: str, except_cid: str) -> str | None:
    for c in DRIVE_IDS:
        if c == except_cid:
            continue
        r = get(c)
        if r["identity"] and r["identity"].lower() == identity.lower() and r["status"] in ("connected", "reauth_required"):
            return c
    return None


def public(r: dict) -> dict:
    """Owner view: identity e-mail included (routes are owner-only), no secrets, no internal folder ids."""
    return {"id": r["id"], "provider": r["provider"], "label": r["label"], "status": r["status"],
            "identity": r["identity"], "scopes": r["scopes"], "last_success": r["last_success"],
            "last_error": r["last_error"], "updated": r["updated"],
            "authorized_by_owner_at": r["data"].get("authorized_by_owner_at")}
