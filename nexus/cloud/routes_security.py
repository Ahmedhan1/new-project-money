"""/api/v1/health and /api/v1/security/* (sub-agent A). No endpoint here ever returns a secret value."""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from .. import config, db
from . import audit, security

router = APIRouter(prefix="/api/v1", tags=["security"])
SAFE_META = {"kind", "provider", "connection_id", "identity", "scopes", "label", "worker_id", "issued_at", "stored_at",
             "created", "expires_at", "rotated_at", "account"}


@router.get("/health/live")
def live():
    """Unauthenticated liveness probe for uptime monitors: reveals nothing but 'ok'."""
    return {"ok": True}


def vault_status() -> dict:
    """Is the vault usable? Counts only - never names' values. `undecryptable` > 0 means the key changed or was lost."""
    from .. import vault
    out = {"ok": False, "key_source": "env" if os.environ.get("NEXUS_VAULT_KEY") else "file", "secrets": 0,
           "undecryptable": 0, "error": None}
    try:
        from cryptography.fernet import Fernet
        Fernet(vault._key())
        names = vault.names()
        out["secrets"] = len(names)
        out["undecryptable"] = sum(1 for n in names if vault.get(n) is None)
        out["ok"] = out["undecryptable"] == 0
    except Exception as e:  # noqa: BLE001 - a broken key must show as unhealthy, never crash or leak
        out["error"] = type(e).__name__
    return out


@router.get("/health", dependencies=[Depends(security.require_owner)])
def health():
    api = sys.modules.get("nexus.api")
    wanted = list(getattr(api, "CLOUD_ROUTERS", ()))
    missing = dict(getattr(api, "CLOUD_MISSING", {}))
    try:
        db.query("SELECT 1")
        db_ok, schema = True, db.schema_version()
    except Exception:  # noqa: BLE001
        db_ok, schema = False, None
    try:
        du = shutil.disk_usage(config.HOME)
        disk = {"path": "NEXUS_HOME", "free_bytes": du.free, "total_bytes": du.total, "low": du.free < 5 * 2 ** 30}
    except OSError:
        disk = {"path": "NEXUS_HOME", "free_bytes": None, "total_bytes": None, "low": None}
    v = vault_status()
    return {"ok": db_ok and v["ok"] and not missing, "version": config.VERSION, "api": "v1", "time": time.time(),
            "routers": {"mounted": [r for r in wanted if r not in missing], "missing": sorted(missing)},
            "db": {"ok": db_ok, "schema": schema}, "vault": v, "disk": disk, "auth": security.auth_mode()}


@router.get("/security/secrets", dependencies=[Depends(security.require_owner)])
def secrets_list():
    """Names and non-secret metadata of stored secrets. Values are never returned."""
    from .. import vault
    out = []
    for n in vault.names():
        m = vault.meta(n) or {}
        safe = {k: v for k, v in m.items() if k in SAFE_META and not (isinstance(v, str) and vault.TOKEN_RE.search(v))}
        out.append({"name": n, "meta": safe, "readable": vault.get(n) is not None})
    return {"secrets": out}


@router.get("/security/audit", dependencies=[Depends(security.require_owner)])
def audit_list(limit: int = 200, since: float = 0.0, event: str | None = None):
    return {"entries": audit.entries(min(max(limit, 1), 1000), since, event)}


class ScanReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    history: bool = False


def _kind(match: str) -> str:
    for rx, name in ((r"^KGAT_", "kaggle_api_token"), (r"^ya29\.", "google_access_token"), (r"^1//0", "google_refresh_token"),
                     (r"^GOCSPX-", "google_client_secret"), (r"^gh[p_]|^github_pat_", "github_token"),
                     (r"^AKIA", "aws_access_key"), (r"^sk-", "api_key"), (r"^-----BEGIN", "private_key"),
                     (r"^xox", "slack_token")):
        if re.match(rx, match):
            return name
    return "token"


@router.post("/security/scan", dependencies=[Depends(security.require_owner)])
def scan(r: ScanReq, request: Request):
    """Token-shaped strings in the repository (tracked files; with history=true every committed blob). Hits name the
    file and the kind of token - never any part of the value."""
    from .. import vault
    hits = []
    for h in vault.scan_repo(config.ROOT, history=r.history):
        # scan_repo keeps an 8-char prefix; classify it and drop it
        hits.append({"file": h["file"], "where": h["where"], "kind": _kind(h["match"].removesuffix("..."))})
    audit.record("scan.run", ok=not hits, client=request.client.host if request.client else None,
                 history=r.history, hits=len(hits))
    return {"clean": not hits, "hits": hits, "history": r.history}
