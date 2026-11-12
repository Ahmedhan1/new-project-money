"""AnalyticsRecord import: YouTube Analytics API reports + owner CSV exports (YouTube Studio, TikTok Studio, Instagram,
Meta Business Suite). Records are normalised and stored; nothing is estimated or filled in. A value that is missing
in the source stays absent; unknown columns are kept under `metrics.raw`.

Column maps for the CSV exports are built from the exports' visible headers; they are NOT verified against an export
from every account type/locale (see `CSV_MAPS[...]['verified']`). Unmapped headers are reported so a map can be fixed.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

from .. import contracts as C
from . import store

YT_REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
# Verified on https://developers.google.com/youtube/analytics/metrics (2026-11-12). Impressions / CTR are not on that page.
YT_METRICS = ["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage", "subscribersGained",
              "subscribersLost", "likes", "comments", "shares"]
YT_NORMAL = {"views": "views", "estimatedMinutesWatched": "watch_minutes", "averageViewDuration": "avg_view_duration_s",
             "averageViewPercentage": "avg_view_pct", "subscribersGained": "subscribers_gained", "subscribersLost": "subscribers_lost",
             "likes": "likes", "comments": "comments", "shares": "shares"}

# header (lower-case, stripped) -> (normalised metric, converter)
_num = "num"
_hours_to_min = "hours_to_min"
_hms = "hms"
_ms = "ms_to_s"
CSV_MAPS: dict[str, dict] = {
    "youtube_studio": {"platform": "youtube", "verified": False,
                       "id": ["content", "video id"], "title": ["video title"], "date": ["date"], "published": ["video publish time"],
                       "map": {"views": ("views", _num), "watch time (hours)": ("watch_minutes", _hours_to_min),
                               "average view duration": ("avg_view_duration_s", _hms),
                               "average percentage viewed (%)": ("avg_view_pct", _num), "subscribers": ("subscribers_net", _num),
                               "subscribers gained": ("subscribers_gained", _num), "impressions": ("impressions", _num),
                               "impressions click-through rate (%)": ("impressions_ctr_pct", _num), "likes": ("likes", _num),
                               "comments added": ("comments", _num), "shares": ("shares", _num)}},
    "tiktok_studio": {"platform": "tiktok", "verified": False, "id": ["video id", "post id"], "title": ["video title", "title"],
                      "date": ["date"], "published": ["post time", "posted on"],
                      "map": {"video views": ("views", _num), "views": ("views", _num), "likes": ("likes", _num),
                              "comments": ("comments", _num), "shares": ("shares", _num), "profile views": ("profile_views", _num),
                              "total play time (hours)": ("watch_minutes", _hours_to_min),
                              "average watch time (seconds)": ("avg_view_duration_s", _num),
                              "watched full video (%)": ("completion_pct", _num), "reach": ("reach", _num)}},
    "instagram": {"platform": "instagram_reels", "verified": False, "id": ["post id", "media id"], "title": ["description"],
                  "date": ["date"], "published": ["publish time"],
                  "map": {"views": ("views", _num), "plays": ("views", _num), "reach": ("reach", _num), "likes": ("likes", _num),
                          "comments": ("comments", _num), "shares": ("shares", _num), "saves": ("saves", _num),
                          "follows": ("follows", _num), "average watch time": ("avg_view_duration_s", _num)}},
    "facebook": {"platform": "facebook", "verified": False, "id": ["post id", "video id"], "title": ["title", "description"],
                 "date": ["date"], "published": ["publish time"],
                 "map": {"views": ("views", _num), "3-second video views": ("views_3s", _num), "reach": ("reach", _num),
                         "reactions": ("likes", _num), "comments": ("comments", _num), "shares": ("shares", _num),
                         "seconds viewed": ("watch_seconds", _num), "average seconds viewed": ("avg_view_duration_s", _num)}},
}


def _to_num(s) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("%", "")
    if t in ("", "-", "—", "n/a", "N/A"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def _hms_to_s(s) -> float | None:
    t = str(s or "").strip()
    if not t:
        return None
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", t):
        parts = [int(p) for p in t.split(":")]
        sec = 0
        for p in parts:
            sec = sec * 60 + p
        return sec
    return _to_num(t)


def _convert(kind: str, raw):
    if kind == _hours_to_min:
        v = _to_num(raw)
        return None if v is None else round(v * 60, 3)
    if kind == _hms:
        return _hms_to_s(raw)
    if kind == _ms:
        v = _to_num(raw)
        return None if v is None else v / 1000
    return _to_num(raw)


def _pick(row: dict, keys: list[str]) -> str | None:
    for k in keys:
        if row.get(k):
            return str(row[k]).strip()
    return None


def save(records: list[dict]) -> int:
    store.ensure()
    from ... import db
    for r in records:
        C.check(r)
        db.execute("INSERT OR REPLACE INTO pub_analytics(platform,video_id,date,source,project_id,imported,data) VALUES(?,?,?,?,?,?,?)",
                   (r["platform"], r["video_id"], r["date"], r["source"], r.get("project_id"), r["imported"], json.dumps(r, ensure_ascii=False)))
    return len(records)


def records(platform: str | None = None, video_id: str | None = None) -> list[dict]:
    store.ensure()
    from ... import db
    sql, a = "SELECT data FROM pub_analytics WHERE 1=1", []
    if platform:
        sql += " AND platform=?"
        a.append(platform)
    if video_id:
        sql += " AND video_id=?"
        a.append(video_id)
    return [json.loads(r["data"]) for r in db.query(sql + " ORDER BY date", tuple(a))]


def import_csv(path_or_text: str | Path, kind: str, period: str | None = None, project_id: str | None = None,
               persist: bool = True) -> dict:
    """Owner export -> AnalyticsRecords. `period` (e.g. '2026-10-01..2026-10-31') is used as `date` for table exports
    without a date column; without either, `date` is 'unknown' (never guessed)."""
    if kind not in CSV_MAPS:
        raise ValueError(f"unknown export kind {kind!r}; known: {', '.join(CSV_MAPS)}")
    spec = CSV_MAPS[kind]
    p = Path(str(path_or_text))
    text = p.read_text(encoding="utf-8-sig") if len(str(path_or_text)) < 4096 and p.is_file() else str(path_or_text)
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    known = set(spec["map"]) | set(spec["id"]) | set(spec["title"]) | set(spec["date"]) | set(spec["published"])
    unmapped = [h for h in headers if h not in known]
    out, skipped = [], []
    for i, raw in enumerate(reader):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        vid = _pick(row, spec["id"])
        if not vid or vid.lower() == "total":  # Studio's first data row is the channel total
            skipped.append({"row": i + 2, "reason": "total row" if vid and vid.lower() == "total" else "no video id"})
            continue
        metrics = {}
        for h, (name, conv) in spec["map"].items():
            if h in row:
                v = _convert(conv, row[h])
                if v is not None:
                    metrics[name] = v
        extra = {h: row[h] for h in unmapped if row.get(h)}
        if extra:
            metrics["raw"] = extra
        date = _pick(row, spec["date"]) or period or "unknown"
        rec = C.make("AnalyticsRecord", platform=spec["platform"], video_id=vid, date=date, metrics=metrics,
                     source=f"csv:{kind}", imported=time.time(), project_id=project_id)
        title, pub = _pick(row, spec["title"]), _pick(row, spec["published"])
        if title:
            rec["title"] = title
        if pub:
            rec["published"] = pub
        out.append(rec)
    if persist:
        save(out)
    return {"records": out, "imported": len(out), "skipped": skipped, "unmapped_columns": unmapped,
            "map_verified": spec["verified"], "kind": kind}


# ------------------------------------------------------------------ YouTube Analytics API
def _yt_rows(resp: dict) -> list[dict]:
    cols = [c["name"] for c in resp.get("columnHeaders") or []]
    return [dict(zip(cols, r)) for r in resp.get("rows") or []]


def youtube_daily(video_id: str, start: str, end: str, account: dict | None, transport=None, adapter=None,
                  project_id: str | None = None, persist: bool = True) -> list[dict]:
    """Daily metrics for one video (dimensions=day, filters=video==ID). Needs yt-analytics.readonly consent."""
    from .adapters.base import classify
    from .adapters.youtube import YouTubeAdapter
    ad = adapter or YouTubeAdapter(transport)
    ad.require_credentials(account)
    r = ad.t.request("GET", YT_REPORTS, ad._auth(account), params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                                                   "metrics": ",".join(YT_METRICS), "dimensions": "day",
                                                                   "filters": f"video=={video_id}"})
    classify(r, "youtubeAnalytics.reports")
    out = []
    for row in _yt_rows(r.json()):
        m = {YT_NORMAL[k]: v for k, v in row.items() if k in YT_NORMAL and v is not None}
        out.append(C.make("AnalyticsRecord", platform="youtube", video_id=video_id, date=str(row.get("day")), metrics=m,
                          source="api:youtube-analytics/v2", imported=time.time(), project_id=project_id))
    if persist:
        save(out)
    return out


def youtube_retention(video_id: str, start: str, end: str, account: dict | None, transport=None, adapter=None,
                      project_id: str | None = None, persist: bool = True) -> dict:
    """audienceWatchRatio by elapsedVideoTimeRatio (one video per request, as documented)."""
    from .adapters.base import classify
    from .adapters.youtube import YouTubeAdapter
    ad = adapter or YouTubeAdapter(transport)
    ad.require_credentials(account)
    r = ad.t.request("GET", YT_REPORTS, ad._auth(account), params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                                                   "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                                                                   "dimensions": "elapsedVideoTimeRatio", "filters": f"video=={video_id}"})
    classify(r, "youtubeAnalytics.reports retention")
    curve = [[row["elapsedVideoTimeRatio"], row.get("audienceWatchRatio"), row.get("relativeRetentionPerformance")]
             for row in _yt_rows(r.json())]
    rec = C.make("AnalyticsRecord", platform="youtube", video_id=video_id, date=f"{start}..{end}",
                 metrics={"retention_curve": curve, "retention_columns": ["elapsed_ratio", "audienceWatchRatio", "relativeRetentionPerformance"]},
                 source="api:youtube-analytics/v2:retention", imported=time.time(), project_id=project_id)
    if persist:
        save([rec])
    return rec
