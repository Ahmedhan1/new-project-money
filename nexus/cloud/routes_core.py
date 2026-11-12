"""/api/v1 core: contract schemas, events, notifications, owner session (lead)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
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
def open_session(r: SessionReq, request: Request, response: Response):
    """Exchange the owner token for a signed, HttpOnly, SameSite=Strict session cookie (the token never reaches the
    browser; rate limited and audited in security.open_session)."""
    return security.open_session(request, response, r.token)


@router.get("/events", dependencies=[Depends(security.require_owner)])
def list_events(job_id: str | None = None, since: float = 0.0, limit: int = 200):
    return events.events(job_id, since, min(limit, 1000))


@router.get("/alerts", dependencies=[Depends(security.require_owner)])
def list_alerts(check: bool = False):
    """Operational alerts (storage thresholds, reauth, failed transfers, repeated failures, lost workers, quota).
    check=true runs the checks now; otherwise the alerts raised in the last few hours are returned."""
    from . import alerts
    fired = alerts.check() if check else []
    return {"fired_now": fired, "active": alerts.active()}


@router.get("/notifications", dependencies=[Depends(security.require_owner)])
def list_notifications(unread: bool = False):
    return events.notifications(unread)


class ReadReq(BaseModel):
    ids: list[str] | None = None


@router.post("/notifications/read", dependencies=[Depends(security.require_owner)])
def read_notifications(r: ReadReq):
    return {"marked": events.mark_read(r.ids)}
