"""Google OAuth 2.0 for the Drive connections (drive_a / drive_b / drive_c).

Follows the official documents (verified 2026-11-12):
  * Web server flow  https://developers.google.com/identity/protocols/oauth2/web-server
      state (random, single-use, 10 min, bound to the connection id), PKCE S256, access_type=offline,
      prompt=consent, include_granted_scopes=true; redirect URI must be HTTPS unless the host is localhost.
  * Device flow      https://developers.google.com/identity/protocols/oauth2/limited-input-device
      no redirect URI - the owner enters a code at google.com/device from any browser, so it works while NEXUS runs
      in a cloud container. drive.file, openid and email are on the page's "Allowed scopes" list (drive.appdata too;
      the full `drive` scope is NOT). Polling: 428 authorization_pending, 403 slow_down / access_denied.
  * Revocation       POST https://oauth2.googleapis.com/revoke  token=<refresh or access token>

Scopes: drive.file (NEXUS sees only files it created or the owner opened with it) + openid email (identity only).

Secrets: the OAuth client JSON is the vault entry "google_oauth_client" (`python -m nexus.cloud.storage.setup_google`).
Per connection, the vault entry "gdrive_<cid>" holds {refresh_token, access_token, expires_at, scope}; a call for one
connection can only ever read its own entry (`_secret_name` is derived from the validated connection id, and the
in-memory cache is keyed by connection id). Pending web-flow state and device codes are vault entries too.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, replace
from urllib.parse import urlencode, urlparse

import httpx

from ... import costguard, vault
from . import connections
from .base import NotConfigured, ReauthRequired, StorageError

SCOPES = ["https://www.googleapis.com/auth/drive.file", "openid", "email"]
CLIENT_SECRET_NAME = "google_oauth_client"
STATE_TTL = 600
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Endpoints:
    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    device: str = "https://oauth2.googleapis.com/device/code"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    api: str = "https://www.googleapis.com/drive/v3"
    upload: str = "https://www.googleapis.com/upload/drive/v3"


GOOGLE = Endpoints()
_endpoints = GOOGLE


def endpoints() -> Endpoints:
    return _endpoints


def set_endpoints(e: Endpoints | None) -> None:
    """Point the client at another implementation of the same protocol (the local fake Drive in tests)."""
    global _endpoints
    _endpoints = e or GOOGLE


def fake_endpoints(base: str) -> Endpoints:
    b = base.rstrip("/")
    return replace(GOOGLE, auth=b + "/o/oauth2/v2/auth", token=b + "/token", device=b + "/device/code",
                   revoke=b + "/revoke", api=b + "/drive/v3", upload=b + "/upload/drive/v3")


def guard(url: str) -> None:
    """Zero-spend egress rule: every non-loopback host must be declared in registry/components.yaml."""
    host = (urlparse(url).hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return
    costguard.check_host(url)


def http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False)


class OAuthError(StorageError):
    def __init__(self, msg: str, code: str = "oauth_error"):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- client + per-connection tokens
def client() -> dict:
    raw = vault.get(CLIENT_SECRET_NAME)
    if not raw:
        raise NotConfigured("no Google OAuth client: the owner runs `python -m nexus.cloud.storage.setup_google <client.json>`")
    c = json.loads(raw)
    if not c.get("client_id"):
        raise NotConfigured("google_oauth_client has no client_id")
    return c


def _secret_name(cid: str) -> str:
    if cid not in connections.DRIVE_IDS:
        raise KeyError(f"not a Drive connection: {cid!r}")
    return f"gdrive_{cid}"


_cache: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = {c: threading.Lock() for c in connections.DRIVE_IDS}


def load_tokens(cid: str) -> dict | None:
    raw = vault.get(_secret_name(cid))
    if not raw:
        return None
    tok = json.loads(raw)
    if tok.get("connection_id") != cid:  # defence in depth: a secret copied under the wrong name is never used
        raise StorageError(f"token entry for {cid} is bound to {tok.get('connection_id')!r}; refusing to use it")
    return tok


def _save_tokens(cid: str, tok: dict, identity: str | None = None) -> None:
    tok = {**tok, "connection_id": cid}
    vault.put(_secret_name(cid), json.dumps(tok), {"connection_id": cid, "identity": identity, "scopes": tok.get("scope", "")})
    _cache[cid] = tok


def has_tokens(cid: str) -> bool:
    return vault.get(_secret_name(cid)) is not None


def _token_request(data: dict) -> httpx.Response:
    url = endpoints().token
    guard(url)
    with http() as h:
        return h.post(url, data=data, headers={"Accept": "application/json"})


def _identity_from_id_token(id_token: str | None) -> str | None:
    """The id_token comes straight from Google's token endpoint over TLS, so (per Google's OpenID Connect doc) its
    payload can be read without re-verifying the signature for this purpose (display + duplicate-account check)."""
    if not id_token or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    return claims.get("email")


def _store_grant(cid: str, body: dict) -> dict:
    if "refresh_token" not in body:
        raise OAuthError("Google returned no refresh token (access_type=offline + prompt=consent are required)", "no_refresh_token")
    identity = _identity_from_id_token(body.get("id_token"))
    granted = (body.get("scope") or "").split()
    if "https://www.googleapis.com/auth/drive.file" not in granted and granted:
        raise OAuthError("the owner did not grant the drive.file permission", "scope_not_granted")
    if identity:
        other = connections.identity_in_use(identity, cid)
        if other:
            _revoke_token(body["refresh_token"])
            raise OAuthError(f"this Google account is already connected as {other}; each connection must be a different account",
                             "duplicate_identity")
    tok = {"refresh_token": body["refresh_token"], "access_token": body.get("access_token"),
           "expires_at": time.time() + float(body.get("expires_in", 0)), "scope": body.get("scope", ""), "token_type": "Bearer"}
    _save_tokens(cid, tok, identity)
    connections.update(cid, status="connected", identity=identity, scopes=granted, last_error=None, last_success=time.time())
    connections.merge_data(cid, authorized_by_owner_at=time.time())
    return connections.public(connections.get(cid))


def access_token(cid: str, force_refresh: bool = False) -> str:
    """A valid access token for exactly this connection (refreshing and storing rotated tokens as needed)."""
    with _locks[cid]:
        tok = _cache.get(cid) or load_tokens(cid)
        if tok is None:
            raise NotConfigured(f"{cid} is not connected")
        if not force_refresh and tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
            _cache[cid] = tok
            return tok["access_token"]
        c = client()
        r = _token_request({"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        body = _json(r)
        if r.status_code != 200:
            err = body.get("error", f"http_{r.status_code}")
            if err in ("invalid_grant", "unauthorized_client"):
                _cache.pop(cid, None)
                connections.failure(cid, f"refresh failed: {err}", status="reauth_required")
                raise ReauthRequired(f"{cid}: Google refused the refresh token ({err}); the owner must reconnect")
            connections.failure(cid, f"refresh failed: {err}")
            raise OAuthError(f"token refresh failed: {err}", err)
        tok = {**tok, "access_token": body["access_token"], "expires_at": time.time() + float(body.get("expires_in", 3600))}
        if body.get("refresh_token"):  # rotated
            tok["refresh_token"] = body["refresh_token"]
        _save_tokens(cid, tok, (vault.meta(_secret_name(cid)) or {}).get("identity"))
        return tok["access_token"]


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------- web-server flow (state + PKCE)
def _state_name(state: str) -> str:
    return "goauth_state_" + hashlib.sha256(state.encode()).hexdigest()[:24]


def redirect_uri_ok(uri: str) -> bool:
    u = urlparse(uri)
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK_HOSTS)


def web_start(cid: str, redirect_uri: str) -> dict:
    _secret_name(cid)
    if not redirect_uri_ok(redirect_uri):
        raise OAuthError("redirect URI must use HTTPS (plain http is only allowed for localhost)", "insecure_redirect")
    c = client()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    vault.put(_state_name(state), json.dumps({"cid": cid, "verifier": verifier, "redirect_uri": redirect_uri,
                                              "expires_at": time.time() + STATE_TTL}), {"kind": "oauth_state", "cid": cid})
    q = {"client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    connections.update(cid, status="pending")
    return {"auth_url": endpoints().auth + "?" + urlencode(q), "expires_in": STATE_TTL}


def web_finish(state: str, code: str | None, error: str | None = None) -> dict:
    name = _state_name(state or "")
    raw = vault.get(name)
    vault.delete(name)  # single use, whatever happens next
    if not raw:
        raise OAuthError("unknown or already used state", "bad_state")
    st = json.loads(raw)
    cid = st["cid"]
    if st["expires_at"] < time.time():
        connections.update(cid, status="not_configured" if not has_tokens(cid) else "connected")
        raise OAuthError("authorisation took too long; start again", "state_expired")
    if error or not code:
        connections.failure(cid, f"consent refused: {error}", status="not_configured" if not has_tokens(cid) else None)
        raise OAuthError(f"Google returned {error or 'no code'}", error or "no_code")
    c = client()
    r = _token_request({"client_id": c["client_id"], "client_secret": c.get("client_secret", ""), "code": code,
                        "code_verifier": st["verifier"], "grant_type": "authorization_code", "redirect_uri": st["redirect_uri"]})
    body = _json(r)
    if r.status_code != 200:
        connections.failure(cid, f"code exchange failed: {body.get('error')}")
        raise OAuthError(f"code exchange failed: {body.get('error', r.status_code)}", body.get("error", "exchange_failed"))
    return _store_grant(cid, body)


# ---------------------------------------------------------------- device flow
def _device_name(cid: str) -> str:
    return f"gdevice_{cid}"


def device_start(cid: str) -> dict:
    _secret_name(cid)
    c = client()
    url = endpoints().device
    guard(url)
    with http() as h:
        r = h.post(url, data={"client_id": c["client_id"], "scope": " ".join(SCOPES)})
    body = _json(r)
    if r.status_code != 200:
        raise OAuthError(f"device code request failed: {body.get('error') or body.get('error_code') or r.status_code}",
                         body.get("error", "device_failed"))
    vault.put(_device_name(cid), json.dumps({"device_code": body["device_code"], "interval": int(body.get("interval", 5)),
                                             "expires_at": time.time() + int(body.get("expires_in", 1800)), "next_poll": 0}),
              {"kind": "device_code", "cid": cid})
    connections.update(cid, status="pending")
    return {"connection_id": cid, "user_code": body["user_code"],
            "verification_url": body.get("verification_url") or body.get("verification_uri"),
            "expires_in": int(body.get("expires_in", 1800)), "interval": int(body.get("interval", 5))}


def device_poll(cid: str) -> dict:
    raw = vault.get(_device_name(cid))
    if not raw:
        return {"status": "no_pending_request"}
    d = json.loads(raw)
    if d["expires_at"] < time.time():
        vault.delete(_device_name(cid))
        connections.update(cid, status="connected" if has_tokens(cid) else "not_configured")
        return {"status": "expired", "detail": "the code expired; start again"}
    if time.time() < d.get("next_poll", 0):
        return {"status": "pending", "retry_after": round(d["next_poll"] - time.time(), 1)}
    c = client()
    r = _token_request({"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                        "device_code": d["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    body = _json(r)
    if r.status_code == 200:
        vault.delete(_device_name(cid))
        return {"status": "connected", "connection": _store_grant(cid, body)}
    err = body.get("error", f"http_{r.status_code}")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            d["interval"] = d["interval"] + 5
        d["next_poll"] = time.time() + d["interval"]
        vault.put(_device_name(cid), json.dumps(d), {"kind": "device_code", "cid": cid})
        return {"status": "pending" if err == "authorization_pending" else "slow_down", "retry_after": d["interval"]}
    vault.delete(_device_name(cid))
    connections.failure(cid, f"device authorisation failed: {err}", status="connected" if has_tokens(cid) else "not_configured")
    return {"status": "denied" if err == "access_denied" else "failed", "detail": err}


# ---------------------------------------------------------------- disconnect
def _revoke_token(token: str) -> bool:
    url = endpoints().revoke
    guard(url)
    with http() as h:
        r = h.post(url, data={"token": token}, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return r.status_code == 200


def disconnect(cid: str) -> dict:
    """Revoke at Google (refresh token revokes the whole grant), then delete the secret either way."""
    revoked, detail = False, ""
    tok = None
    try:
        tok = load_tokens(cid)
    except StorageError as e:
        detail = str(e)
    if tok:
        try:
            revoked = _revoke_token(tok.get("refresh_token") or tok.get("access_token"))
            detail = "revoked at Google" if revoked else "Google did not confirm revocation (token may already be invalid)"
        except Exception as e:  # network / costguard: still delete locally
            detail = f"revocation request failed: {type(e).__name__}"
    vault.delete(_secret_name(cid))
    vault.delete(_device_name(cid))
    _cache.pop(cid, None)
    connections.update(cid, status="disconnected", identity=None, scopes=[], data={})
    return {"connection_id": cid, "revoked": revoked, "secret_deleted": True, "detail": detail}


def reset_cache() -> None:
    _cache.clear()
