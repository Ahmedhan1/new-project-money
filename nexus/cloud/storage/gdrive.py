"""GoogleDriveProvider: Drive API v3 over plain REST (httpx), one instance per connection (drive_a/b/c).

Docs (verified 2026-11-12):
  uploads    https://developers.google.com/workspace/drive/api/guides/manage-uploads
             POST /upload/drive/v3/files?uploadType=resumable -> Location = session URI (valid one week);
             PUT chunks (multiples of 256 KiB) with Content-Range; 308 + Range = bytes received so far;
             status query = empty PUT with `Content-Range: bytes */<size>`; 404 = session expired -> restart.
  files      https://developers.google.com/workspace/drive/api/reference/rest/v3/files
             fields md5Checksum / sha1Checksum / sha256Checksum are returned for binary files.
  quota      about.get fields=storageQuota(limit,usage,...) - `limit` is absent when the storage is unlimited.
Scope drive.file: NEXUS only sees files and folders it created (marked appProperties.nexus = "1").
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from ...contracts import ProviderHealth, QuotaStatus, StorageProviderInfo
from ...costguard import CostGuardError
from . import connections, oauth
from .base import (NotConfigured, QuotaExceeded, ReauthRequired, RemoteFile, StorageError, StorageProvider,
                   TransientError, safe_rel, sha256_file)

FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,md5Checksum,sha256Checksum,parents,appProperties,modifiedTime,trashed"

INFO = StorageProviderInfo(provider="gdrive", display="Google Drive (drive.file scope)",
                           capabilities=["upload", "resumable_upload", "download", "list", "delete", "move", "checksum",
                                         "versions", "quota", "folders"],
                           max_connections=3, billing_required=False,
                           notes="15 GB free per Google account, shared with Gmail and Photos. Each connection is a "
                                 "separate account authorised by the owner; NEXUS never rotates accounts to bypass a limit.")


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveProvider(StorageProvider):
    INFO = INFO

    def __init__(self, connection_id: str):
        if connection_id not in connections.DRIVE_IDS:
            raise KeyError(f"not a Drive connection: {connection_id!r}")
        self.connection_id = connection_id
        self._h = oauth.http()

    # ------------------------------------------------------------ HTTP with per-connection auth
    def _req(self, method: str, url: str, *, params: dict | None = None, json_body=None, headers: dict | None = None,
             content: bytes | None = None, ok=(200,), retry: int = 3, stream: bool = False) -> httpx.Response:
        oauth.guard(url)
        refreshed = False
        delay = 1.0
        for attempt in range(retry + 2):  # +1 for a single auth refresh
            tok = oauth.access_token(self.connection_id, force_refresh=refreshed)
            hdr = {"Authorization": f"Bearer {tok}", **(headers or {})}
            try:
                req = self._h.build_request(method, url, params=params, json=json_body, headers=hdr, content=content)
                r = self._h.send(req, stream=stream)
            except httpx.TransportError as e:
                if attempt < retry:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise TransientError(f"network error: {type(e).__name__}") from e
            if r.status_code == 401 and not refreshed:  # expired/revoked access token: refresh once
                r.close()
                refreshed = True
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retry:
                wait = float(r.headers.get("Retry-After") or delay)
                r.close()
                if wait > 30:
                    raise TransientError(f"Drive asks to wait {wait:.0f}s")
                time.sleep(wait)
                delay *= 2
                continue
            if r.status_code == 401:
                connections.failure(self.connection_id, "Drive rejected a freshly refreshed token", status="reauth_required")
                raise ReauthRequired(f"{self.connection_id}: unauthorised")
            if r.status_code == 403 and "storageQuotaExceeded" in (r.text if not stream else ""):
                raise QuotaExceeded(f"{self.connection_id}: Drive storage quota exceeded")
            if r.status_code not in ok:
                body = r.text[:300] if not stream else ""
                r.close()
                if r.status_code >= 500:
                    raise TransientError(f"Drive HTTP {r.status_code}")
                raise StorageError(f"Drive {method} {url.split('?')[0].rsplit('/', 2)[-1]} -> HTTP {r.status_code} {body}")
            return r
        raise TransientError("retries exhausted")

    def _file(self, d: dict, path: str = "") -> RemoteFile:
        props = d.get("appProperties") or {}
        return RemoteFile(ref=d["id"], name=d.get("name", ""), path=path, size=int(d["size"]) if d.get("size") else None,
                          mime=d.get("mimeType", ""), sha256=d.get("sha256Checksum"), md5=d.get("md5Checksum"),
                          modified=d.get("modifiedTime"), is_folder=d.get("mimeType") == FOLDER_MIME,
                          app_created=props.get("nexus") == "1", props=props)

    # ------------------------------------------------------------ health / quota
    def connect(self) -> ProviderHealth:
        return self.health()

    def health(self) -> ProviderHealth:
        row = connections.get(self.connection_id)
        base = {"provider": "gdrive", "connection_id": self.connection_id, "identity": row["identity"],
                "scopes": row["scopes"], "last_success": row["last_success"], "last_error": row["last_error"]}
        try:
            if not oauth.has_tokens(self.connection_id):
                raise NotConfigured(f"{self.connection_id} is not connected: start the device or web flow")
            q = self.quota()
        except NotConfigured as e:
            return ProviderHealth(status="not_configured", detail=str(e), **base)
        except ReauthRequired as e:
            return ProviderHealth(status="unauthorized", detail=str(e) + " - reconnect with the device or web flow", **base)
        except CostGuardError as e:
            return ProviderHealth(status="unavailable", detail=f"egress blocked by costguard: {e}", **base)
        except StorageError as e:
            connections.failure(self.connection_id, str(e))
            return ProviderHealth(status="unavailable", detail=str(e), **{**base, "last_error": str(e)})
        connections.success(self.connection_id)
        if row["status"] != "connected":
            connections.update(self.connection_id, status="connected")
        return ProviderHealth(status="quota_exhausted" if q.exhausted else "ok", quotas=[q],
                              **{**base, "last_success": time.time(), "last_error": None})

    def quota(self) -> QuotaStatus:
        r = self._req("GET", oauth.endpoints().api + "/about", params={"fields": "storageQuota,user(emailAddress)"})
        d = r.json()
        sq = d.get("storageQuota") or {}
        limit = float(sq["limit"]) if sq.get("limit") is not None else None
        used = float(sq.get("usage", 0))
        rem = None if limit is None else max(0.0, limit - used)
        email = (d.get("user") or {}).get("emailAddress")
        row = connections.get(self.connection_id)
        if email and row["identity"] and email.lower() != row["identity"].lower():
            raise StorageError(f"{self.connection_id}: token belongs to {email}, connection was authorised as {row['identity']}")
        return QuotaStatus(provider="gdrive", connection_id=self.connection_id, resource="storage_bytes", limit=limit,
                           used=used, remaining=rem, source="provider_api", exhausted=rem is not None and rem < 1)

    # ------------------------------------------------------------ folders
    def _folders(self) -> dict:
        return connections.get(self.connection_id)["data"].get("folders", {})

    def _find_child(self, parent: str, name: str, folder: bool | None = None) -> dict | None:
        q = f"name = '{_q(name)}' and '{_q(parent)}' in parents and trashed = false"
        if folder is True:
            q += f" and mimeType = '{FOLDER_MIME}'"
        r = self._req("GET", oauth.endpoints().api + "/files", params={"q": q, "fields": f"files({FIELDS})", "pageSize": 10,
                                                                     "spaces": "drive"})
        files = r.json().get("files", [])
        return files[0] if files else None

    def _folder_id(self, path: str, create: bool) -> str | None:
        rel = safe_rel(path)
        if not rel:
            return "root"
        cache = self._folders()
        if rel in cache:
            return cache[rel]
        parent, walked = "root", []
        for part in rel.split("/"):
            walked.append(part)
            key = "/".join(walked)
            if key in cache:
                parent = cache[key]
                continue
            found = self._find_child(parent, part, folder=True)
            if found is None:
                if not create:
                    return None
                found = self._req("POST", oauth.endpoints().api + "/files", params={"fields": "id"},
                                  json_body={"name": part, "mimeType": FOLDER_MIME, "parents": [parent],
                                             "appProperties": {"nexus": "1"}}).json()
            parent = found["id"]
            cache[key] = parent
        connections.merge_data(self.connection_id, folders=cache)
        return parent

    def mkdirs(self, path: str) -> str:
        return self._folder_id(path, create=True)

    # ------------------------------------------------------------ namespace
    def list(self, path: str = "") -> list[RemoteFile]:
        fid = self._folder_id(path, create=False)
        if fid is None:
            return []
        out, token = [], None
        while True:
            params = {"q": f"'{_q(fid)}' in parents and trashed = false", "fields": f"nextPageToken,files({FIELDS})",
                      "pageSize": 200, "spaces": "drive"}
            if token:
                params["pageToken"] = token
            d = self._req("GET", oauth.endpoints().api + "/files", params=params).json()
            out += [self._file(f, (safe_rel(path) + "/" + f["name"]).lstrip("/")) for f in d.get("files", [])]
            token = d.get("nextPageToken")
            if not token:
                return out

    def stat(self, ref: str) -> RemoteFile | None:
        r = self._req("GET", f"{oauth.endpoints().api}/files/{ref}", params={"fields": FIELDS}, ok=(200, 404))
        if r.status_code == 404:
            return None
        d = r.json()
        return None if d.get("trashed") else self._file(d)

    def checksum(self, ref: str) -> dict:
        f = self.stat(ref)
        if f is None:
            raise StorageError(f"no such Drive file {ref}")
        return {"sha256": f.sha256, "md5": f.md5, "source": "provider"}

    def delete(self, ref: str, *, only_app_created: bool = True) -> bool:
        f = self.stat(ref)
        if f is None:
            return False
        if only_app_created and not f.app_created:
            raise StorageError(f"refusing to delete Drive file {ref}: not created by NEXUS")
        self._req("DELETE", f"{oauth.endpoints().api}/files/{ref}", ok=(200, 204, 404))
        return True

    def move(self, ref: str, dest_path: str) -> RemoteFile:
        rel = safe_rel(dest_path)
        folder, _, name = rel.rpartition("/")
        new_parent = self._folder_id(folder, create=True)
        cur = self._req("GET", f"{oauth.endpoints().api}/files/{ref}", params={"fields": "parents"}).json()
        params = {"addParents": new_parent, "removeParents": ",".join(cur.get("parents", [])), "fields": FIELDS}
        d = self._req("PATCH", f"{oauth.endpoints().api}/files/{ref}", params=params, json_body={"name": name}).json()
        return self._file(d, rel)

    def versions(self, ref: str) -> list[dict]:
        d = self._req("GET", f"{oauth.endpoints().api}/files/{ref}/revisions",
                      params={"fields": "revisions(id,modifiedTime,size,md5Checksum,keepForever)"}).json()
        return d.get("revisions", [])

    def download(self, ref: str, dest: Path, *, resume: bool = True) -> Path:
        meta = self.stat(ref)
        if meta is None:
            raise StorageError(f"no such Drive file {ref}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        for _ in range(4):
            have = part.stat().st_size if resume and part.exists() else 0
            if meta.size is not None and have >= meta.size:
                break
            hdr = {"Range": f"bytes={have}-"} if have else {}
            r = self._req("GET", f"{oauth.endpoints().api}/files/{ref}", params={"alt": "media"}, headers=hdr,
                          ok=(200, 206), stream=True)
            try:
                mode = "ab" if (have and r.status_code == 206) else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                break
            except httpx.TransportError:
                continue  # resume from what we have
            finally:
                r.close()
        got = sha256_file(part)
        if (meta.sha256 and got != meta.sha256.lower()) or (meta.size is not None and part.stat().st_size != meta.size):
            part.unlink()
            raise StorageError(f"download of {ref} failed checksum verification")
        os.replace(part, dest)
        return dest

    # ------------------------------------------------------------ resumable upload primitives
    def start_upload(self, dest_path: str, size: int, mime: str, props: dict) -> str:
        rel = safe_rel(dest_path)
        folder, _, name = rel.rpartition("/")
        parent = self._folder_id(folder, create=True)
        meta = {"name": name, "parents": [parent], "mimeType": mime,
                "appProperties": {k: str(v)[:100] for k, v in props.items()}}
        r = self._req("POST", oauth.endpoints().upload + "/files", params={"uploadType": "resumable", "fields": FIELDS},
                      json_body=meta, headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        loc = r.headers.get("Location")
        if not loc:
            raise StorageError("Drive returned no upload session URI")
        oauth.guard(loc)
        return loc

    def _parse(self, r: httpx.Response) -> int | RemoteFile:
        if r.status_code in (200, 201):
            return self._file(r.json())
        rng = r.headers.get("Range")  # "bytes=0-N" -> N+1 received; absent -> nothing received
        return int(rng.split("-")[-1]) + 1 if rng else 0

    def query_upload(self, session: str, size: int) -> int | RemoteFile | None:
        r = self._req("PUT", session, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}, content=b"",
                      ok=(200, 201, 308, 404, 410))
        if r.status_code in (404, 410):
            return None
        return self._parse(r)

    def put_chunk(self, session: str, offset: int, data: bytes, size: int) -> int | RemoteFile:
        end = offset + len(data) - 1
        hdr = {"Content-Range": f"bytes {offset}-{end}/{size}", "Content-Length": str(len(data))}
        try:
            r = self._req("PUT", session, headers=hdr, content=data, ok=(200, 201, 308), retry=0)
        except (TransientError, QuotaExceeded, ReauthRequired, NotConfigured):
            raise
        except StorageError as e:  # per the upload guide: 403 rateLimitExceeded -> retry; other 4xx -> fail the session
            if "ateLimitExceeded" in str(e):
                raise TransientError(str(e)) from e
            raise
        return self._parse(r)

    def abort_upload(self, session: str) -> None:
        try:
            self._req("DELETE", session, ok=(200, 204, 404, 410, 499), retry=0)
        except StorageError:
            pass


def describe() -> dict:
    return json.loads(INFO.model_dump_json())
