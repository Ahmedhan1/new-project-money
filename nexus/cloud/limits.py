"""Request size limits (Account 1). Pure ASGI middleware, so it also covers chunked bodies with no Content-Length.

    NEXUS_MAX_BODY_BYTES     non-multipart requests (JSON APIs)            default 8 MiB
    NEXUS_MAX_UPLOAD_BYTES   multipart/form-data (media and worker uploads) default 20 GiB

A request that declares a larger Content-Length is refused with 413 before the application reads a byte. A streamed
body is counted as it arrives; when it crosses the limit the application's read fails and, if no response has started,
the client gets 413. Uploaded media is checked again by type in the upload handlers.
"""
from __future__ import annotations

import json
import os

DEFAULT_BODY = 8 << 20
DEFAULT_UPLOAD = 20 << 30


CHUNK_BYTES = 64 * 2 ** 20  # PUT /api/upload/chunk (nexus/api.py CHUNK_MAX)


def _limit(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def limits() -> dict:
    return {"body_bytes": _limit("NEXUS_MAX_BODY_BYTES", DEFAULT_BODY), "upload_bytes": _limit("NEXUS_MAX_UPLOAD_BYTES", DEFAULT_UPLOAD)}


class _TooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        multipart = headers.get("content-type", "").lower().startswith("multipart/form-data")
        lim = limits()["upload_bytes" if multipart else "body_bytes"]
        if scope.get("path") == "/api/upload/chunk":  # one piece of a big upload (raw bytes); the handler caps it again
            lim = max(lim, CHUNK_BYTES)
        cl = headers.get("content-length", "")
        if cl.isdigit() and int(cl) > lim:
            return await self._reject(send, lim)
        seen = 0
        state = {"exceeded": False, "started": False, "replaced": False}

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > lim:
                    state["exceeded"] = True
                    raise _TooLarge()
            return msg

        async def guarded_send(msg):
            # A framework may catch the read error and answer 400 itself (FastAPI: "error parsing the body"): once the
            # limit was crossed, whatever the app sends is replaced by the 413.
            if state["exceeded"]:
                if not state["replaced"] and msg["type"] == "http.response.start" and not state["started"]:
                    state["replaced"] = True
                    await self._reject(send, lim)
                return
            if msg["type"] == "http.response.start":
                state["started"] = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:  # noqa: BLE001 - _TooLarge itself or a parser error wrapping it
            if not state["exceeded"]:
                raise
        if state["exceeded"] and not state["replaced"] and not state["started"]:
            await self._reject(send, lim)

    @staticmethod
    async def _reject(send, lim: int) -> None:
        body = json.dumps({"detail": f"request body too large (limit {lim} bytes)"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
