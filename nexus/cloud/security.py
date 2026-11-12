"""Owner authentication, CSRF, security headers and per-worker tokens for the cloud API (sub-agent A).

Contract kept for every router: `Depends(security.require_owner)`.

Model: a single owner.
* Hosted (NEXUS_API_TOKEN set by the platform's secret store): requests need `Authorization: Bearer <token>` or the
  `nexus_session` cookie issued by POST /api/v1/session (`open_session`). The cookie holds a signed, expiring session
  value derived from the token (HMAC-SHA256), never the token itself; rotating NEXUS_API_TOKEN ends every session.
* Local / Codespaces dev (no NEXUS_API_TOKEN): only *direct* loopback clients are accepted. A request that arrived
  through a proxy (X-Forwarded-*/Forwarded headers, e.g. Codespaces port forwarding or a tunnel) or whose Host header
  is not a loopback name (DNS rebinding) is treated as remote and refused.
* CSRF: a state-changing request (POST/PUT/PATCH/DELETE) authenticated by cookie must carry `X-NEXUS-CSRF: 1`
  (a custom header forces a CORS preflight, which this app never grants) and, if the browser sent an Origin, it must
  be this server's own origin. Loopback-mode requests from a foreign Origin are refused the same way.
* Brute force: failures are counted per client; after MAX_FAILS within WINDOW_S the client gets 429. Every failure,
  lockout, session and worker-token event is written to the audit log (nexus.cloud.audit), never with a token value.
Tokens and hashes are compared in constant time (hmac.compare_digest).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}          # client addresses (testclient = Starlette TestClient)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}         # Host header names accepted in loopback mode
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
                 "x-original-forwarded-for", "cf-connecting-ip", "x-github-request-id")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-nexus-csrf"
COOKIE = "nexus_session"
SESSION_TTL_S = 12 * 3600
_fail: dict[str, deque] = defaultdict(deque)
_locked_logged: dict[str, float] = {}
MAX_FAILS, WINDOW_S = 10, 300
MAX_TRACKED_CLIENTS = 10_000


def api_token() -> str | None:
    return os.environ.get("NEXUS_API_TOKEN") or None


def _client(req: Request) -> str:
    return req.client.host if req.client else "unknown"


def _audit(event: str, ok: bool, req: Request | None = None, **detail) -> None:
    try:
        from . import audit
        audit.record(event, ok=ok, client=_client(req) if req is not None else None, **detail)
    except Exception:  # noqa: BLE001 - auditing must never break (or open) authentication
        pass


# ---------------------------------------------------------------- rate limiting
def _window(who: str) -> deque:
    if len(_fail) > MAX_TRACKED_CLIENTS:  # bound memory under a spray of source addresses
        cutoff = time.time() - WINDOW_S
        for k in [k for k, q in _fail.items() if not q or q[-1] < cutoff]:
            _fail.pop(k, None)
    q = _fail[who]
    while q and q[0] < time.time() - WINDOW_S:
        q.popleft()
    return q


def _rate_check(req: Request) -> deque:
    who = _client(req)
    q = _window(who)
    if len(q) >= MAX_FAILS:
        if _locked_logged.get(who, 0) < time.time() - WINDOW_S:
            _locked_logged[who] = time.time()
            _audit("auth.locked", False, req, path=req.url.path)
        raise HTTPException(429, "too many failed authentications; wait a few minutes")
    return q


def _fail_auth(req: Request, q: deque, status: int, detail: str, reason: str) -> None:
    q.append(time.time())
    _audit("auth.fail", False, req, path=req.url.path, reason=reason)
    raise HTTPException(status, detail)


def reset_rate_limits() -> None:
    _fail.clear()
    _locked_logged.clear()


# ---------------------------------------------------------------- origin / proxy checks
def via_proxy(req: Request) -> bool:
    """True when the request carries reverse-proxy / tunnel headers (it did not come straight from this machine)."""
    return any(h in req.headers for h in PROXY_HEADERS)


def _host_name(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):  # [::1]:8765
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_direct_loopback(req: Request) -> bool:
    """Direct loopback client, no proxy headers, loopback Host header (defeats DNS rebinding)."""
    who = _client(req)
    if who not in LOOPBACK or via_proxy(req):
        return False
    host = _host_name(req.headers.get("host", ""))
    return host in LOOPBACK_HOSTS or (who == "testclient" and host == "testserver")


def same_origin(req: Request) -> bool:
    """Origin (or, failing that, Referer) is absent or equals this request's own scheme://host."""
    origin = req.headers.get("origin")
    if origin is None:
        ref = req.headers.get("referer")
        if not ref:
            return True
        m = re.match(r"^(https?://[^/]+)", ref)
        origin = m.group(1) if m else "null"
    if origin == "null":
        return False
    allowed = set()
    # Host as seen by the app, and the original host a reverse proxy/tunnel reports (a cross-site browser request
    # cannot set X-Forwarded-Host without a CORS preflight, which is never granted). NEXUS_PUBLIC_ORIGIN adds the
    # deployment's public origin(s), comma-separated.
    for host in (req.headers.get("host", ""), req.headers.get("x-forwarded-host", "").split(",")[0].strip()):
        if host:
            allowed |= {f"http://{host}".lower(), f"https://{host}".lower()}
    allowed |= {o.strip().lower().rstrip("/") for o in os.environ.get("NEXUS_PUBLIC_ORIGIN", "").split(",") if o.strip()}
    return origin.lower().rstrip("/") in allowed


# ---------------------------------------------------------------- owner sessions (cookie never holds the token)
def _session_sig(tok: str, issued: int) -> str:
    return hmac.new(tok.encode(), f"nexus-session|{issued}".encode(), hashlib.sha256).hexdigest()


def make_session_value(tok: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    return f"s1.{issued}.{_session_sig(tok, issued)}"


def session_valid(value: str, tok: str, now: float | None = None) -> bool:
    if not value:
        return False
    if value.startswith("s1."):
        try:
            _, issued_s, sig = value.split(".", 2)
            issued = int(issued_s)
        except ValueError:
            return False
        t = now if now is not None else time.time()
        if not (issued <= t + 60 and t - issued <= SESSION_TTL_S):
            return False
        return hmac.compare_digest(sig.encode(), _session_sig(tok, issued).encode())
    # legacy cookie (raw token, set by routes_core before open_session existed) - still constant-time
    return hmac.compare_digest(value.encode(), tok.encode())


def open_session(request: Request, response: Response, token: str) -> dict:
    """Exchange the owner token for a signed HttpOnly SameSite=Strict cookie. Rate limited and audited.
    routes_core's POST /api/v1/session should delegate here."""
    tok = api_token()
    if tok is None:
        return {"mode": "loopback", "detail": "no NEXUS_API_TOKEN configured: only local clients are accepted"}
    q = _rate_check(request)
    if not (token and hmac.compare_digest(token.encode(), tok.encode())):
        q.append(time.time())
        _audit("session.open", False, request)
        raise HTTPException(401, "wrong token")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https" or via_proxy(request)
    response.set_cookie(COOKIE, make_session_value(tok), httponly=True, secure=secure, samesite="strict",
                        max_age=SESSION_TTL_S, path="/")
    _audit("session.open", True, request)
    return {"mode": "token", "ok": True, "csrf_header": "X-NEXUS-CSRF", "expires_in": SESSION_TTL_S}


def close_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


# ---------------------------------------------------------------- the owner check
def check(req: Request) -> None:
    """Raise HTTPException unless the request comes from the owner. Returns None on success."""
    tok = api_token()
    q = _rate_check(req)
    unsafe = req.method.upper() not in SAFE_METHODS
    if tok is None:
        if not is_direct_loopback(req):
            _fail_auth(req, q, 401, "remote access needs NEXUS_API_TOKEN to be configured on the server", "remote_without_token")
        if unsafe and not same_origin(req):
            _fail_auth(req, q, 403, "cross-origin request refused", "cross_origin")
        return
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
        if given and hmac.compare_digest(given.encode(), tok.encode()):
            return  # bearer clients (CLI, other accounts) are not CSRF-exposed: browsers never add this header
        _fail_auth(req, q, 401, "owner authentication required", "bad_bearer")
    cookie = req.cookies.get(COOKIE, "")
    if not session_valid(cookie, tok):
        _fail_auth(req, q, 401, "owner authentication required", "bad_cookie" if cookie else "missing")
    if unsafe:
        if req.headers.get(CSRF_HEADER, "") != "1":
            _fail_auth(req, q, 403, "missing X-NEXUS-CSRF header on a cookie-authenticated request", "csrf_header")
        if not same_origin(req):
            _fail_auth(req, q, 403, "cross-origin request refused", "cross_origin")


async def require_owner(request: Request) -> None:
    check(request)


def auth_mode() -> dict:
    """Non-secret description of the active policy (for the Studio and /api/v1/health)."""
    return {"mode": "token" if api_token() else "loopback", "cookie": COOKIE, "csrf_header": "X-NEXUS-CSRF",
            "session_ttl_s": SESSION_TTL_S, "max_failures": MAX_FAILS, "window_s": WINDOW_S}


# ---------------------------------------------------------------- per-worker tokens (pull workers: Colab, user GPU)
WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
WORKER_PREFIX = "nxw_"


def _worker_secret_name(worker_id: str) -> str:
    if not WORKER_ID_RE.match(worker_id or ""):
        raise ValueError("worker_id must be lower-case letters, digits, '_' or '-', 2-48 chars")
    return f"worker-token.{worker_id}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_worker_token(worker_id: str, scopes: list[str] | None = None) -> str:
    """Create (or rotate) the token for one worker. Only its SHA-256 is stored (encrypted in the vault); the token is
    returned once, to be shown to the owner a single time."""
    from .. import vault
    name = _worker_secret_name(worker_id)
    token = WORKER_PREFIX + secrets.token_urlsafe(32)
    vault.put(name, _hash(token), {"kind": "worker_token", "worker_id": worker_id, "scopes": scopes or ["pull", "report"],
                                   "issued_at": time.time()})
    _audit("worker_token.issued", True, None, worker_id=worker_id)
    return token


def verify_worker_token(worker_id: str, token: str) -> bool:
    from .. import vault
    try:
        name = _worker_secret_name(worker_id)
    except ValueError:
        return False
    stored = vault.get(name)
    if not (stored and token and token.startswith(WORKER_PREFIX)):
        return False
    return hmac.compare_digest(_hash(token).encode(), stored.encode())


def revoke_worker_token(worker_id: str) -> bool:
    from .. import vault
    ok = vault.delete(_worker_secret_name(worker_id))
    _audit("worker_token.revoked", ok, None, worker_id=worker_id)
    return ok


def worker_tokens() -> list[dict]:
    from .. import vault
    out = []
    for n in vault.names():
        if n.startswith("worker-token."):
            m = vault.meta(n) or {}
            out.append({"worker_id": m.get("worker_id"), "issued_at": m.get("issued_at"), "scopes": m.get("scopes", [])})
    return out


def require_worker(request: Request) -> str:
    """FastAPI dependency for pull-worker endpoints: `X-NEXUS-Worker: <id>` + `Authorization: Bearer nxw_...`.
    Returns the authenticated worker id. Rate limited and audited like owner auth."""
    q = _rate_check(request)
    wid = request.headers.get("x-nexus-worker", "")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not verify_worker_token(wid, token):
        q.append(time.time())
        _audit("worker.auth_fail", False, request, worker_id=wid[:48])
        raise HTTPException(401, "worker authentication required")
    return wid


# ---------------------------------------------------------------- security headers
CSP_HTML = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; media-src 'self' blob:; connect-src 'self'; font-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
CSP_API = "default-src 'none'; frame-ancestors 'none'"


def apply_headers(request: Request, response: Response) -> Response:
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    ctype = h.get("content-type", "")
    h.setdefault("Content-Security-Policy", CSP_HTML if ctype.startswith("text/html") else CSP_API)
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


async def security_headers(request: Request, call_next):
    """HTTP middleware: `app.middleware("http")(security.security_headers)`."""
    return apply_headers(request, await call_next(request))


def install(app) -> None:
    """Install the security-headers middleware on a FastAPI app (idempotent)."""
    if getattr(app.state, "nexus_security_headers", False):
        return
    app.middleware("http")(security_headers)
    app.state.nexus_security_headers = True
