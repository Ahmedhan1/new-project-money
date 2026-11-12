"""Owner authentication for the cloud API (sub-agent A hardens and extends; contract: `require_owner` dependency).

Model: a single owner. On a hosted deployment NEXUS_API_TOKEN must be set (the platform's secret store injects it);
requests then need `Authorization: Bearer <token>` or the `nexus_session` cookie issued by POST /api/v1/session.
Without NEXUS_API_TOKEN only loopback clients (127.0.0.1 / ::1) are accepted - the local/Codespaces development mode.
Tokens are compared in constant time; failures are counted per client for rate limiting.
"""
from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_fail: dict[str, deque] = defaultdict(deque)
MAX_FAILS, WINDOW_S = 10, 300


def api_token() -> str | None:
    return os.environ.get("NEXUS_API_TOKEN") or None


def _client(req: Request) -> str:
    return req.client.host if req.client else "unknown"


def check(req: Request) -> None:
    tok = api_token()
    who = _client(req)
    q = _fail[who]
    while q and q[0] < time.time() - WINDOW_S:
        q.popleft()
    if len(q) >= MAX_FAILS:
        raise HTTPException(429, "too many failed authentications; wait a few minutes")
    if tok is None:
        if who in LOOPBACK:
            return
        q.append(time.time())
        raise HTTPException(401, "remote access needs NEXUS_API_TOKEN to be configured on the server")
    given = ""
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    given = given or req.cookies.get("nexus_session", "")
    if not (given and hmac.compare_digest(given.encode(), tok.encode())):
        q.append(time.time())
        raise HTTPException(401, "owner authentication required")


async def require_owner(request: Request) -> None:
    check(request)
