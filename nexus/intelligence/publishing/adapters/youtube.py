"""YouTube Data API v3 adapter: resumable videos.insert (private unless the owner approved), thumbnails.set, status
polling, quota ledger. Protocol: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
(read 2026-11-12). Not live-tested: no owner credentials exist in this environment.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .. import creds, store, youtube_oauth
from .base import Adapter, AdapterBlocked, PermanentError, TransientError, Transport, classify

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
# https://developers.google.com/youtube/v3/determine_quota_cost (read 2026-11-12)
QUOTA = {"videos_insert_calls_per_day": 100, "daily_units": 10000, "thumbnails_set": 50, "videos_list": 1}
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB for chunked resumable uploads


def quota_day() -> str:
    """Ledger day. Google states quota resets at midnight Pacific in the Cloud console (not re-read this session)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return dt.datetime.utcnow().date().isoformat()


def quota_status() -> dict:
    d = quota_day()
    return {"day": d, "videos_insert_calls": store.quota_used(d, "youtube", "videos_insert"),
            "videos_insert_limit": QUOTA["videos_insert_calls_per_day"], "units": store.quota_used(d, "youtube", "units"),
            "units_limit": QUOTA["daily_units"], "basis": "local ledger of calls NEXUS made; the Cloud console is authoritative"}


def privacy_for(manifest: dict) -> str:
    v = manifest["visibility"]
    if v in ("public", "unlisted") and manifest.get("approval") == "approved_by_owner":
        return v
    return "private"  # draft/private/anything unapproved


def video_body(manifest: dict) -> dict:
    md = manifest["metadata"]
    snippet = {"title": md.get("title", "")[:100], "description": md.get("description", ""),
               "tags": md.get("tags") or [], "categoryId": str(md.get("category_id", "22"))}
    if md.get("language"):
        snippet["defaultLanguage"] = snippet["defaultAudioLanguage"] = md["language"]
    status = {"privacyStatus": privacy_for(manifest), "selfDeclaredMadeForKids": bool(md.get("made_for_kids", False))}
    if md.get("publish_at") and status["privacyStatus"] == "private" and manifest.get("approval") == "approved_by_owner":
        status["publishAt"] = md["publish_at"]  # scheduled publish needs private + owner approval
    status["containsSyntheticMedia"] = synthetic_flag(manifest)
    return {"snippet": snippet, "status": status}


def synthetic_flag(manifest: dict) -> bool:
    """status.containsSyntheticMedia from the release gate. No release check -> True (never silently non-AI)."""
    d = (manifest.get("release_check") or {}).get("disclosure") or {}
    return bool(d.get("flag", True))


class YouTubeAdapter(Adapter):
    platform = "youtube"
    status = "implemented_untested_live"

    def __init__(self, transport=None, chunk: int = CHUNK):
        self.t = transport or Transport()
        self.chunk = chunk
        self._token: str | None = None

    # ------------------------------------------------------------ credentials
    def _secrets(self, account: dict | None) -> tuple[dict, list[str]]:
        label = (account or {}).get("label") or "main"
        n = youtube_oauth.names(label)
        vals = {k: creds.get(v) for k, v in n.items()}
        missing = [n[k] for k, v in vals.items() if not v]
        return vals, missing

    def require_credentials(self, account: dict | None) -> None:
        label = (account or {}).get("label") or "main"
        if account is None:
            raise AdapterBlocked("account_not_connected", "no YouTube account is connected",
                                 youtube_oauth.owner_instructions(label))
        _, missing = self._secrets(account)
        if missing:
            raise AdapterBlocked("credentials_missing", f"missing credentials: {', '.join(missing)} (backend: {creds.backend()})",
                                 youtube_oauth.owner_instructions(label))

    def preflight(self, manifest: dict, account: dict | None) -> None:
        self.require_credentials(account)
        path = Path(manifest["artifact"]["uri"].removeprefix("file://"))
        if not path.is_file():
            raise PermanentError(f"artifact not found: {path}")
        q = quota_status()
        if q["videos_insert_calls"] >= q["videos_insert_limit"] and not (manifest.get("tracking") or {}).get("upload_session"):
            raise AdapterBlocked("quota_exceeded", f"{q['videos_insert_calls']} videos.insert calls already used on {q['day']}",
                                 ["Wait for the next quota day or request more quota (quota extension form)."])

    def access_token(self, account: dict) -> str:
        if self._token:
            return self._token
        s, missing = self._secrets(account)
        if missing:
            raise AdapterBlocked("credentials_missing", "missing credentials", youtube_oauth.owner_instructions(account.get("label", "main")))
        self._token = youtube_oauth.refresh(s["client_id"], s["client_secret"], s["refresh_token"], self.t)["access_token"]
        return self._token

    def _auth(self, account) -> dict:
        return {"Authorization": f"Bearer {self.access_token(account)}"}

    # ------------------------------------------------------------ upload
    def _status_query(self, uri: str, total: int, account) -> tuple[int | None, dict | None]:
        """(next_offset, None) if incomplete, (None, video) if complete, raises on expired session."""
        r = self.t.request("PUT", uri, {**self._auth(account), "Content-Length": "0", "Content-Range": f"bytes */{total}"})
        if r.status in (200, 201):
            return None, r.json()
        if r.status == 308:
            rng = r.header("Range")
            return (int(rng.split("-")[1]) + 1 if rng else 0), None
        if r.status == 404:
            return -1, None
        classify(r, "upload status")
        return 0, None

    def upload(self, manifest: dict, account: dict | None, ctx: dict) -> dict:
        path = Path(manifest["artifact"]["uri"].removeprefix("file://"))
        total = path.stat().st_size
        session = (manifest.get("tracking") or {}).get("upload_session") or {}
        uri, offset, video = session.get("uri"), 0, None
        if uri:
            offset, video = self._status_query(uri, total, account)
            if offset == -1:  # session expired -> documented: start a new session from the beginning
                uri, offset = None, 0
        if not uri and video is None:
            r = self.t.request("POST", UPLOAD_URL, {**self._auth(account), "Content-Type": "application/json; charset=UTF-8",
                                                    "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/*"},
                               json.dumps(video_body(manifest)).encode(), params={"uploadType": "resumable", "part": "snippet,status"})
            classify(r, "videos.insert initiate")
            uri = r.header("Location")
            if not uri:
                raise PermanentError("videos.insert initiate: no Location header")
            store.quota_use(quota_day(), "youtube", "videos_insert", 1)
            ctx["save"]({"uri": uri, "total": total})
        while video is None:
            n = min(self.chunk, total - offset)
            r = self.t.request("PUT", uri, {**self._auth(account), "Content-Length": str(n), "Content-Type": "video/*",
                                            "Content-Range": f"bytes {offset}-{offset + n - 1}/{total}"}, (str(path), offset, n))
            if r.status in (200, 201):
                video = r.json()
            elif r.status == 308:
                rng = r.header("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                ctx["save"]({"uri": uri, "total": total, "offset": offset})
            else:
                classify(r, "videos.insert upload")  # 5xx -> TransientError; the saved session resumes next time
        vid = video.get("id")
        out = {"remote_id": vid, "remote_status": (video.get("status") or {}).get("uploadStatus"),
               "privacy": (video.get("status") or {}).get("privacyStatus"), "url": f"https://www.youtube.com/watch?v={vid}"}
        thumb = (manifest["metadata"].get("thumbnail") or {}).get("uri")
        if thumb:
            out["thumbnail"] = self.set_thumbnail(vid, thumb.removeprefix("file://"), account)
        out["quota"] = quota_status()
        return out

    def set_thumbnail(self, video_id: str, image: str, account) -> dict:
        if not os.path.isfile(image):
            return {"ok": False, "reason": f"thumbnail file missing: {image}"}
        data = Path(image).read_bytes()
        ctype = "image/png" if image.lower().endswith(".png") else "image/jpeg"
        r = self.t.request("POST", THUMB_URL, {**self._auth(account), "Content-Type": ctype, "Content-Length": str(len(data))},
                           data, params={"videoId": video_id})
        store.quota_use(quota_day(), "youtube", "units", QUOTA["thumbnails_set"])
        if 200 <= r.status < 300:
            return {"ok": True}
        # Custom thumbnails need a verified channel; a refusal must not fail the (already uploaded) video.
        return {"ok": False, "reason": f"thumbnails.set HTTP {r.status}: {r.body[:200].decode('utf-8', 'replace')}"}

    def poll(self, remote_id: str, account: dict | None) -> dict:
        r = self.t.request("GET", VIDEOS_URL, self._auth(account), params={"part": "status,processingDetails", "id": remote_id})
        store.quota_use(quota_day(), "youtube", "units", QUOTA["videos_list"])
        try:
            classify(r, "videos.list")
        except TransientError:
            raise
        items = r.json().get("items") or []
        if not items:
            return {"remote_id": remote_id, "found": False}
        it = items[0]
        return {"remote_id": remote_id, "found": True, "upload_status": it.get("status", {}).get("uploadStatus"),
                "privacy": it.get("status", {}).get("privacyStatus"),
                "processing": it.get("processingDetails", {}).get("processingStatus"),
                "failure_reason": it.get("status", {}).get("failureReason") or it.get("status", {}).get("rejectionReason")}
