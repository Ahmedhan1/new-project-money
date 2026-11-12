"""/api/v1 core: contract schemas, events, notifications, owner session (lead)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import contracts
from . import events, security

router = APIRouter(prefix="/api/v1", tags=["core"])


@router.get("/contracts")
def list_contracts():
    """Versioned integration contracts (JSON Schema) for Accounts 2 and 3. Public: contains no data."""
    return {"version": contracts.VERSION, "schemas": {m.__name__: m.model_json_schema() for m in contracts.ALL}}


class SessionReq(BaseModel):
    token: str


@router.post("/session")
def open_session(r: SessionReq, response: Response):
    """Exchange the owner token for an HttpOnly, SameSite=Strict cookie so the browser UI never stores the token in JS."""
    tok = security.api_token()
    if tok is None:
        return {"mode": "loopback", "detail": "no NEXUS_API_TOKEN configured: only local clients are accepted"}
    import hmac
    if not hmac.compare_digest(r.token.encode(), tok.encode()):
        raise HTTPException(401, "wrong token")
    response.set_cookie("nexus_session", tok, httponly=True, secure=True, samesite="strict", max_age=12 * 3600)
    return {"mode": "token", "ok": True}


@router.get("/events", dependencies=[Depends(security.require_owner)])
def list_events(job_id: str | None = None, since: float = 0.0, limit: int = 200):
    return events.events(job_id, since, min(limit, 1000))


@router.get("/notifications", dependencies=[Depends(security.require_owner)])
def list_notifications(unread: bool = False):
    return events.notifications(unread)


class ReadReq(BaseModel):
    ids: list[str] | None = None


@router.post("/notifications/read", dependencies=[Depends(security.require_owner)])
def read_notifications(r: ReadReq):
    return {"marked": events.mark_read(r.ids)}
