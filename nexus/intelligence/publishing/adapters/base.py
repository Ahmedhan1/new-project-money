"""Adapter contract + the one HTTP transport the publishing adapters use.

Every outbound request passes `nexus.costguard.check_host` (zero-spend rule). Tests inject a `FakeTransport` that
replays recorded responses; no adapter imports a platform SDK.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class AdapterBlocked(Exception):
    """The owner (or the lead) must do something before this adapter can work. Never retried automatically."""

    def __init__(self, code: str, message: str, owner_action: list[str] | None = None):
        self.code, self.owner_action = code, owner_action or []
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "owner_action": self.owner_action}


class TransientError(Exception):
    """Network loss / 5xx / rate limit: retry later with backoff."""

    def __init__(self, message: str, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(message)


class PermanentError(Exception):
    """The platform rejected the request (4xx other than auth/rate limit). Needs a human to look."""


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8") or "{}") if self.body else {}

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


class Transport:
    """urllib transport. `body` may be bytes or a (path, offset, length) tuple streamed from disk."""

    def request(self, method: str, url: str, headers: dict | None = None, body=None, params: dict | None = None,
                timeout: int = 120) -> Response:
        from .... import costguard
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        try:
            costguard.check_host(url)
        except Exception as e:  # undeclared host: the lead must declare the component (see the capabilities doc)
            raise AdapterBlocked("host_not_declared", str(e), [
                f"Lead: declare {urllib.parse.urlparse(url).hostname} as a zero-cost component in nexus/registry/components.yaml "
                "(proposal in docs/ACCOUNT3_PLATFORM_CAPABILITIES.md, Integration requests)."]) from e
        data = body
        if isinstance(body, tuple):
            path, off, n = body
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(n)  # chunked by the caller; chunks stay small enough for memory
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, dict(r.headers.items()), r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TransientError(f"network: {e}") from e


class FakeTransport:
    """Replays recorded responses in order: [(method, url_prefix, Response)]. Records every call for assertions."""

    def __init__(self, script: list[tuple[str, str, Response]]):
        self.script, self.calls = list(script), []

    def request(self, method, url, headers=None, body=None, params=None, timeout=120) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body_len": (body[2] if isinstance(body, tuple) else len(body or b""))})
        if not self.script:
            raise AssertionError(f"unexpected request {method} {url}")
        m, prefix, resp = self.script.pop(0)
        assert m == method and url.startswith(prefix), f"expected {m} {prefix}, got {method} {url}"
        if isinstance(resp, Exception):
            raise resp
        return resp


def classify(resp: Response, what: str) -> None:
    """Raise the right error for a non-2xx response."""
    if 200 <= resp.status < 300:
        return
    detail = resp.body[:300].decode("utf-8", "replace")
    if resp.status in (500, 502, 503, 504, 429):
        ra = resp.header("Retry-After")
        raise TransientError(f"{what}: HTTP {resp.status}", float(ra) if ra and ra.isdigit() else None)
    if resp.status == 401:
        raise AdapterBlocked("auth_expired", f"{what}: HTTP 401 - the stored authorisation is no longer valid",
                             ["Reconnect the account (run the OAuth flow again)."])
    if resp.status == 403 and "quota" in detail.lower():
        raise AdapterBlocked("quota_exceeded", f"{what}: daily quota exhausted ({detail})",
                             ["Wait for the daily quota reset (midnight Pacific for YouTube) or request more quota."])
    raise PermanentError(f"{what}: HTTP {resp.status} {detail}")


class Adapter:
    platform = ""
    status = "unknown"  # implemented_untested_live | requires_approved_app | manual_export_only

    def preflight(self, manifest: dict, account: dict | None) -> None:
        """Raise AdapterBlocked when the upload cannot start (missing credentials, unapproved app, ...)."""
        raise NotImplementedError

    def upload(self, manifest: dict, account: dict | None, ctx: dict) -> dict:
        """Upload; returns {remote_id, remote_status, url?}. `ctx['save'](dict)` persists resumable state."""
        raise NotImplementedError

    def poll(self, remote_id: str, account: dict | None) -> dict:
        raise NotImplementedError
