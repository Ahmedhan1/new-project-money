"""Public metadata for references. YouTube: ONLY the official Data API v3 `videos.list` (no page scraping, no media
download). Without a key (env NEXUS_KEY_YOUTUBE / router key store) the owner enters metadata by hand.
Every packaging field records where it came from."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3/videos"
ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    m = ID_RE.search(url or "")
    return m.group(1) if m else None


def iso_duration(s: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _key() -> str | None:
    k = os.environ.get("NEXUS_KEY_YOUTUBE")
    if k:
        return k
    try:
        from ..model_router.router import api_key
        return api_key("youtube")
    except Exception:  # noqa: BLE001
        return None


def _field(value, source: str) -> dict:
    return {"value": value, "source": source}


def parse_videos_list(payload: dict) -> dict:
    """videos.list JSON -> packaging dict (only fields the API returned; hidden counts stay absent)."""
    items = payload.get("items") or []
    if not items:
        return {}
    it = items[0]
    sn, cd, st = it.get("snippet", {}), it.get("contentDetails", {}), it.get("statistics", {})
    src = "youtube_data_api_v3"
    out = {"title": _field(sn.get("title"), src), "published": _field(sn.get("publishedAt"), src),
           "channel": _field(sn.get("channelTitle"), src), "duration_s": _field(iso_duration(cd.get("duration", "")), src),
           "video_id": _field(it.get("id"), src)}
    for k, name in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments")):
        if k in st:  # absent when the owner hid it: never guessed
            out[name] = _field(int(st[k]), src)
    if sn.get("defaultAudioLanguage"):
        out["language"] = _field(sn["defaultAudioLanguage"], src)
    return out


def youtube_metadata(url: str, key: str | None = None, opener=None) -> dict:
    vid = youtube_id(url)
    if not vid:
        return {"status": "not_youtube"}
    key = key or _key()
    if not key:
        return {"status": "no_key", "reason": "NEXUS_KEY_YOUTUBE not configured; enter metadata by hand (meta=...)"}
    try:
        from ... import costguard
        costguard.check_host(API)
    except Exception as e:  # noqa: BLE001
        return {"status": "blocked", "reason": f"cost guard: {e} (www.googleapis.com must be declared as a zero-cost host)"}
    q = urllib.parse.urlencode({"part": "snippet,contentDetails,statistics", "id": vid, "key": key})
    try:
        with (opener or urllib.request.urlopen)(f"{API}?{q}", timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - quota, network, bad key: report, never guess
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
    pk = parse_videos_list(payload)
    return {"status": "ok", "packaging": pk} if pk else {"status": "not_found", "reason": "no item (private/removed?)"}


OWNER_FIELDS = ("title", "published", "views", "likes", "comments", "channel", "thumbnail_text", "duration_s")


def owner_packaging(meta: dict) -> dict:
    return {k: _field(meta[k], "owner_entered") for k in OWNER_FIELDS if meta.get(k) is not None}
