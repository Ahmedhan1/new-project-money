"""Protocol-faithful LOCAL fake of Google OAuth 2.0 + Drive v3, for tests and offline evidence only.

It is NOT evidence that real Google Drive works (that needs the owner's OAuth client and consent - Demo B). It exists
so that the client code is exercised end to end over real HTTP: the same requests, status codes and headers as the
documented Google protocol.

Implemented (subset of the documented behaviour that NEXUS relies on):
  POST /token                 authorization_code (+ PKCE S256 check, redirect_uri check), refresh_token (optional
                              rotation), urn:ietf:params:oauth:grant-type:device_code (428 authorization_pending,
                              403 slow_down / access_denied); access tokens expire after `access_ttl` seconds
  POST /device/code           device_code, user_code, verification_url, expires_in, interval
  POST /revoke                revokes a refresh token (and its access tokens) or an access token
  GET  /drive/v3/about        storageQuota {limit, usage}, user {emailAddress}
  GET  /drive/v3/files        q: name = '..', '<id>' in parents, mimeType = / != '..', trashed = false; pageToken
  POST /drive/v3/files        metadata-only create (folders)
  GET  /drive/v3/files/{id}   metadata (fields ignored: all returned), alt=media with Range -> 206
  PATCH/DELETE /drive/v3/files/{id}, GET /drive/v3/files/{id}/revisions
  POST /upload/drive/v3/files?uploadType=resumable  -> 200 + Location (session URI)
  PUT  <session URI>          Content-Range bytes a-b/total -> 308 + Range: bytes=0-N, final 200 with metadata
                              incl. md5Checksum + sha256Checksum; `bytes */total` = status query; 404 unknown session;
                              403 storageQuotaExceeded when the user's limit would be exceeded
Every Drive request needs `Authorization: Bearer <access token>`; 401 when expired/unknown. Files are private to the
user that created them (404 for anyone else) - this is what the isolation test relies on.

Test hooks (not part of Google's API): POST /__fake/approve {user_code, email, deny?}, GET /__fake/log,
POST /__fake/config {...}. Run standalone: python -m nexus.cloud.storage.fake_drive --port 8765
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FOLDER = "application/vnd.google-apps.folder"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


class FakeGoogle:
    def __init__(self, access_ttl: float = 3600, quota_limit: int = 15 * 10 ** 9, rotate_refresh: bool = False):
        self.lock = threading.RLock()
        self.clients: dict[str, str] = {}
        self.access_ttl = access_ttl
        self.quota_limit = quota_limit
        self.limits: dict[str, int] = {}
        self.rotate_refresh = rotate_refresh
        self.access: dict[str, tuple[str, float, str]] = {}  # token -> (email, expires_at, refresh_token)
        self.refresh: dict[str, tuple[str, str]] = {}  # refresh -> (email, client_id)
        self.devices: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.log: list[dict] = []
        self.drop_next_put_after: int | None = None  # simulate a connection cut mid-chunk
        self.fail_next_puts = 0  # respond 503 to the next N chunk PUTs
        self.server: ThreadingHTTPServer | None = None
        self.base = ""

    # ------------------------------------------------------------ helpers used by tests
    def add_client(self, client_id: str, secret: str) -> None:
        self.clients[client_id] = secret

    def issue_code(self, email: str, client_id: str, redirect_uri: str, challenge: str, scope: str) -> str:
        code = "code_" + secrets.token_urlsafe(16)
        self.codes[code] = {"email": email, "client": client_id, "redirect_uri": redirect_uri, "challenge": challenge,
                            "scope": scope, "exp": time.time() + 600}
        return code

    def approve(self, user_code: str, email: str, deny: bool = False) -> bool:
        for d in self.devices.values():
            if d["user_code"] == user_code:
                d["email"], d["denied"] = email, deny
                return True
        return False

    def usage(self, email: str) -> int:
        return sum(len(f["data"]) for f in self.files.values() if f["owner"] == email and f.get("data") is not None)

    def _grant(self, email: str, client_id: str, scope: str, with_refresh: bool = True) -> dict:
        at = "fake_at_" + secrets.token_urlsafe(24)
        body = {"access_token": at, "expires_in": int(self.access_ttl), "token_type": "Bearer", "scope": scope,
                "id_token": _b64({"alg": "none"}) + "." + _b64({"email": email, "email_verified": True, "iss": "fake"}) + ".sig"}
        rt = None
        if with_refresh:
            rt = "fake_rt_" + secrets.token_urlsafe(24)
            self.refresh[rt] = (email, client_id)
            body["refresh_token"] = rt
        self.access[at] = (email, time.time() + self.access_ttl, rt or "")
        return body

    def start(self, port: int = 0) -> str:
        fake = self

        class H(_Handler):
            g = fake
        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.base

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class _Handler(BaseHTTPRequestHandler):
    g: FakeGoogle
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ------------------------------------------------------------ plumbing
    def _send(self, code: int, body: dict | bytes | None = None, headers: dict | None = None) -> None:
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if not isinstance(body, bytes):
            self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        return {k: v[0] for k, v in parse_qs(self._body().decode()).items()}

    def _user(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        with self.g.lock:
            a = self.g.access.get(tok)
        if not a or a[1] < time.time():
            return None
        return a[0]

    def _err(self, code: int, reason: str, msg: str = "") -> None:
        self._send(code, {"error": {"code": code, "message": msg or reason, "errors": [{"reason": reason}]}})

    def _meta(self, f: dict) -> dict:
        out = {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "parents": f["parents"],
               "appProperties": f.get("appProperties", {}), "modifiedTime": f["modified"], "trashed": False,
               "kind": "drive#file"}
        if f.get("data") is not None:
            out.update(size=str(len(f["data"])), md5Checksum=hashlib.md5(f["data"], usedforsecurity=False).hexdigest(),
                       sha256Checksum=hashlib.sha256(f["data"]).hexdigest())
        return out

    def _dispatch(self, method: str) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        g = self.g
        if p.startswith("/__fake/"):
            return self._hooks(method, p, q)
        if p == "/token" and method == "POST":
            return self._token(self._form())
        if p == "/device/code" and method == "POST":
            f = self._form()
            if f.get("client_id") not in g.clients:
                return self._send(401, {"error": "invalid_client"})
            dc = "dev_" + secrets.token_urlsafe(16)
            uc = "-".join(secrets.token_hex(2).upper() for _ in range(2))
            g.devices[dc] = {"user_code": uc, "client": f["client_id"], "scope": f.get("scope", ""), "email": None,
                             "denied": False, "exp": time.time() + 1800, "last": 0.0}
            return self._send(200, {"device_code": dc, "user_code": uc, "verification_url": "https://www.google.com/device",
                                    "expires_in": 1800, "interval": 5})
        if p == "/revoke" and method == "POST":
            tok = self._form().get("token", "")
            with g.lock:
                if tok in g.refresh:
                    g.refresh.pop(tok)
                    for k in [k for k, v in g.access.items() if v[2] == tok]:
                        g.access.pop(k)
                    return self._send(200, {})
                if tok in g.access:
                    g.access.pop(tok)
                    return self._send(200, {})
            return self._send(400, {"error": "invalid_token"})
        user = self._user()
        g.log.append({"method": method, "path": p, "user": user, "at": time.time()})
        if p.startswith("/drive/v3") or p.startswith("/upload/drive/v3"):
            if user is None:
                return self._err(401, "authError", "Invalid Credentials")
        else:
            return self._send(404, {"error": "not found"})
        if p == "/drive/v3/about":
            lim = g.limits.get(user, g.quota_limit)
            return self._send(200, {"storageQuota": {"limit": str(lim), "usage": str(g.usage(user)),
                                                     "usageInDrive": str(g.usage(user))}, "user": {"emailAddress": user}})
        if p == "/upload/drive/v3/files" and method == "POST" and q.get("uploadType") == "resumable":
            meta = json.loads(self._body() or b"{}")
            size = int(self.headers.get("X-Upload-Content-Length") or -1)
            sid = uuid.uuid4().hex
            g.sessions[sid] = {"owner": user, "meta": meta, "size": size, "data": bytearray(),
                               "mime": self.headers.get("X-Upload-Content-Type") or meta.get("mimeType")}
            return self._send(200, None, {"Location": f"{g.base}/upload/drive/v3/files?uploadType=resumable&upload_id={sid}"})
        if p == "/upload/drive/v3/files" and q.get("upload_id"):
            return self._session(method, q["upload_id"], user)
        if p == "/drive/v3/files" and method == "GET":
            return self._list(user, q)
        if p == "/drive/v3/files" and method == "POST":
            meta = json.loads(self._body() or b"{}")
            f = self._create(user, meta, None)
            return self._send(200, self._meta(f))
        m = re.fullmatch(r"/drive/v3/files/([^/]+)(/revisions)?", p)
        if m:
            f = g.files.get(m.group(1))
            if f is None or f["owner"] != user:
                return self._err(404, "notFound", f"File not found: {m.group(1)}")
            if m.group(2):
                return self._send(200, {"revisions": [{"id": "1", "modifiedTime": f["modified"], "size": str(len(f["data"] or b"")),
                                                       "md5Checksum": self._meta(f).get("md5Checksum")}]})
            if method == "GET" and q.get("alt") == "media":
                data = bytes(f["data"] or b"")
                rng = self.headers.get("Range")
                if rng:
                    a, _, b = rng.split("=", 1)[1].partition("-")
                    a = int(a)
                    b = int(b) if b else len(data) - 1
                    return self._send(206, data[a:b + 1], {"Content-Range": f"bytes {a}-{b}/{len(data)}",
                                                           "Content-Type": "application/octet-stream"})
                return self._send(200, data, {"Content-Type": "application/octet-stream"})
            if method == "GET":
                return self._send(200, self._meta(f))
            if method == "DELETE":
                g.files.pop(f["id"])
                return self._send(204, b"")
            if method == "PATCH":
                body = json.loads(self._body() or b"{}")
                if "name" in body:
                    f["name"] = body["name"]
                if q.get("addParents"):
                    rem = set(filter(None, q.get("removeParents", "").split(",")))
                    f["parents"] = [x for x in f["parents"] if x not in rem] + [q["addParents"]]
                f["modified"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                return self._send(200, self._meta(f))
        return self._send(404, {"error": "not found"})

    def _create(self, user: str, meta: dict, data: bytes | None) -> dict:
        fid = "f" + uuid.uuid4().hex[:20]
        f = {"id": fid, "owner": user, "name": meta.get("name", "Untitled"), "parents": meta.get("parents") or ["root"],
             "mimeType": meta.get("mimeType") or "application/octet-stream", "appProperties": meta.get("appProperties", {}),
             "data": data, "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.g.files[fid] = f
        return f

    def _list(self, user: str, q: dict) -> None:
        expr = q.get("q", "")
        files = [f for f in self.g.files.values() if f["owner"] == user]
        for name in re.findall(r"name = '((?:[^'\\]|\\.)*)'", expr):
            name = name.replace("\\'", "'").replace("\\\\", "\\")
            files = [f for f in files if f["name"] == name]
        for parent in re.findall(r"'([^']+)' in parents", expr):
            files = [f for f in files if parent in f["parents"]]
        for mt in re.findall(r"mimeType = '([^']+)'", expr):
            files = [f for f in files if f["mimeType"] == mt]
        for mt in re.findall(r"mimeType != '([^']+)'", expr):
            files = [f for f in files if f["mimeType"] != mt]
        files.sort(key=lambda f: f["id"])
        start = int(q.get("pageToken") or 0)
        size = int(q.get("pageSize") or 100)
        page = files[start:start + size]
        out = {"files": [self._meta(f) for f in page]}
        if start + size < len(files):
            out["nextPageToken"] = str(start + size)
        return self._send(200, out)

    def _session(self, method: str, sid: str, user: str) -> None:
        g = self.g
        s = g.sessions.get(sid)
        if s is None or s["owner"] != user:
            self._body()
            return self._err(404, "notFound", "upload session not found")
        if method == "DELETE":
            g.sessions.pop(sid)
            return self._send(499, b"")
        if method != "PUT":
            return self._err(400, "badRequest")
        cr = self.headers.get("Content-Range", "")
        n = int(self.headers.get("Content-Length") or 0)
        if cr.startswith("bytes */"):
            self._body()
            return self._status(sid, s)
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr)
        if not m:
            self._body()
            return self._err(400, "badContentRange")
        a, b, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if g.fail_next_puts > 0:
            g.fail_next_puts -= 1
            self._body()
            return self._err(503, "backendError")
        if g.drop_next_put_after is not None:  # read part of the chunk, keep what a real server would persist, cut
            cut = g.drop_next_put_after
            g.drop_next_put_after = None
            got = self.rfile.read(min(cut, n))
            if a == len(s["data"]):
                keep = (len(got) // (256 * 1024)) * (256 * 1024)
                s["data"] += got[:keep]
            self.close_connection = True
            self.connection.shutdown(2)
            return None
        data = self.rfile.read(n)
        if a != len(s["data"]):
            return self._status(sid, s)  # out of order: tell the client what we really have
        if len(data) != b - a + 1 or (b + 1 < total and len(data) % (256 * 1024)):
            return self._err(400, "badChunk", "chunk must be a multiple of 256 KiB except the last")
        lim = g.limits.get(user, g.quota_limit)
        if g.usage(user) + total > lim:
            return self._err(403, "storageQuotaExceeded", "The user's Drive storage quota has been exceeded.")
        s["data"] += data
        return self._status(sid, s)

    def _status(self, sid: str, s: dict) -> None:
        if "file" in s:
            return self._send(200, self._meta(self.g.files[s["file"]]))
        if s["size"] >= 0 and len(s["data"]) >= s["size"]:
            meta = {**s["meta"], "mimeType": s["mime"] or s["meta"].get("mimeType")}
            f = self._create(s["owner"], meta, bytes(s["data"]))
            s["file"] = f["id"]
            return self._send(200, self._meta(f))
        hdr = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return self._send(308, b"", hdr)

    def _token(self, f: dict) -> None:
        g = self.g
        cid = f.get("client_id", "")
        if g.clients.get(cid) is None or g.clients[cid] != f.get("client_secret", ""):
            return self._send(401, {"error": "invalid_client"})
        gt = f.get("grant_type")
        with g.lock:
            if gt == "authorization_code":
                c = g.codes.pop(f.get("code", ""), None)
                if not c or c["exp"] < time.time() or c["client"] != cid or c["redirect_uri"] != f.get("redirect_uri"):
                    return self._send(400, {"error": "invalid_grant"})
                ch = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if ch != c["challenge"]:
                    return self._send(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
                return self._send(200, g._grant(c["email"], cid, c["scope"]))
            if gt == "refresh_token":
                r = g.refresh.get(f.get("refresh_token", ""))
                if not r or r[1] != cid:
                    return self._send(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                body = g._grant(r[0], cid, "https://www.googleapis.com/auth/drive.file openid email", with_refresh=g.rotate_refresh)
                body.pop("id_token", None)
                if g.rotate_refresh:
                    g.refresh.pop(f["refresh_token"])
                else:
                    at = body["access_token"]
                    g.access[at] = (g.access[at][0], g.access[at][1], f["refresh_token"])
                return self._send(200, body)
            if gt == "urn:ietf:params:oauth:grant-type:device_code":
                d = g.devices.get(f.get("device_code", ""))
                if not d or d["client"] != cid:
                    return self._send(400, {"error": "invalid_grant"})
                if d["exp"] < time.time():
                    return self._send(400, {"error": "expired_token"})
                if d["denied"]:
                    g.devices.pop(f["device_code"])
                    return self._send(403, {"error": "access_denied"})
                if d["email"] is None:
                    return self._send(428, {"error": "authorization_pending"})
                g.devices.pop(f["device_code"])
                return self._send(200, g._grant(d["email"], cid, d["scope"]))
        return self._send(400, {"error": "unsupported_grant_type"})

    def _hooks(self, method: str, p: str, q: dict) -> None:
        g = self.g
        if p == "/__fake/approve":
            b = json.loads(self._body() or b"{}")
            return self._send(200, {"ok": g.approve(b["user_code"], b["email"], b.get("deny", False))})
        if p == "/__fake/log":
            return self._send(200, {"log": g.log[-500:]})
        if p == "/__fake/config":
            b = json.loads(self._body() or b"{}")
            for k in ("access_ttl", "drop_next_put_after", "fail_next_puts", "rotate_refresh", "quota_limit"):
                if k in b:
                    setattr(g, k, b[k])
            if "client" in b:
                g.add_client(b["client"]["client_id"], b["client"]["client_secret"])
            return self._send(200, {"ok": True})
        if p == "/__fake/expire_access":
            with g.lock:
                for k, v in list(g.access.items()):
                    g.access[k] = (v[0], 0.0, v[2])
            return self._send(200, {"ok": True})
        return self._send(404, {})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Local fake of Google OAuth + Drive v3 (tests/evidence only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--access-ttl", type=float, default=3600)
    a = ap.parse_args()
    g = FakeGoogle(access_ttl=a.access_ttl)
    print(g.start(a.port), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        g.stop()


if __name__ == "__main__":
    main()
