"""AnalyticsRecord import: YouTube Analytics API reports + owner CSV exports (YouTube Studio, TikTok Studio, Instagram,
Meta Business Suite). Records are normalised and stored; nothing is estimated or filled in. A value that is missing
in the source stays absent; unknown columns are kept under `metrics.raw`.

Every record carries `data_origin` (DATA_ORIGINS): data from a platform API is real_platform_data, an owner export is
user_uploaded_data, and fixtures/demos must say simulated_test_data (public_benchmark_data for published third-party
figures). Growth code can filter with records(origin=...) / real_only(); simulated rows are never real metrics.
Import reports: duplicate (video, date) rows, invalid values (dropped, never "fixed"), normalised ISO dates, publish
times converted to UTC only when the owner states the export's time zone, and a reconciliation of the rows against
the export's own "Total" row.

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

DATA_ORIGINS = ("real_platform_data", "user_uploaded_data", "public_benchmark_data", "simulated_test_data")
PCT_METRICS = {"avg_view_pct", "impressions_ctr_pct", "completion_pct"}
SIGNED_METRICS = {"subscribers_net"}          # may be negative (net change); every other metric is a count/duration >= 0
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# header (lower-case, stripped) -> (normalised metric, converter)
_num = "num"
_hours_to_min = "hours_to_min"
_hms = "hms"
_ms = "ms_to_s"
CSV_MAPS: dict[str, dict] = {
    "youtube_studio": {"platform": "youtube", "verified": False,
                       "id": ["content", "video id"], "title": ["video title"], "date": ["date"], "published": ["video publish time"],
                       "map": {"duration": ("duration_s", _num), "views": ("views", _num), "watch time (hours)": ("watch_minutes", _hours_to_min),
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


def norm_date(s: str | None) -> str | None:
    """ISO date for unambiguous forms (2026-10-03, 2026/10/03, 'Oct 3, 2026', '3 Oct 2026'); None otherwise
    (01/02/2026 is ambiguous between locales and is not guessed)."""
    import datetime as dt
    t = (s or "").strip()
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ].*)?", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", t) or re.fullmatch(r"(\d{1,2}) ([A-Za-z]{3})[a-z]*\.? (\d{4})", t)
        if not m:
            return None
        a, b, y = m.groups()
        mon, d = (a, b) if a[0].isalpha() else (b, a)
        mo, d, y = MONTHS.get(mon.lower()[:3]), int(d), int(y)
        if not mo:
            return None
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def zone(tz: str | None):
    """ZoneInfo for an IANA name, or None when no name is given or this machine has no tz database for it
    (Windows without the tzdata package): the time is then left unconverted, never guessed."""
    if not tz:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError for malformed keys
        return None


def to_utc(s: str | None, tz: str | None) -> str | None:
    """Publish time -> UTC ISO. Needs the export's time zone (IANA name) unless the value carries an offset."""
    import datetime as dt
    t = (s or "").strip()
    if not t:
        return None
    try:
        v = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        d = norm_date(t)
        if not d:
            return None
        v = dt.datetime.fromisoformat(d)
    if v.tzinfo is None:
        z = zone(tz)
        if z is None:
            return None
        v = v.replace(tzinfo=z)
    return v.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _valid(name: str, v) -> bool:
    if name in SIGNED_METRICS:
        return True
    if v < 0:
        return False
    return not (name in PCT_METRICS and v > 100)


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


def real_only(rows: list[dict]) -> list[dict]:
    """Rows that are real owner/platform data (API or owner export) - what growth decisions may learn from."""
    return [r for r in rows if r.get("data_origin") in ("real_platform_data", "user_uploaded_data")]


def origin_summary(rows: list[dict]) -> dict:
    from collections import Counter
    return dict(Counter(r.get("data_origin", "unlabelled") for r in rows))


def records(platform: str | None = None, video_id: str | None = None, origin: str | None = None) -> list[dict]:
    store.ensure()
    from ... import db
    sql, a = "SELECT data FROM pub_analytics WHERE 1=1", []
    if platform:
        sql += " AND platform=?"
        a.append(platform)
    if video_id:
        sql += " AND video_id=?"
        a.append(video_id)
    rows = [json.loads(r["data"]) for r in db.query(sql + " ORDER BY date", tuple(a))]
    return [r for r in rows if origin is None or r.get("data_origin") == origin]


def import_csv(path_or_text: str | Path, kind: str, period: str | None = None, project_id: str | None = None,
               persist: bool = True, origin: str = "user_uploaded_data", tz: str | None = None) -> dict:
    """Owner export -> AnalyticsRecords. `period` (e.g. '2026-10-01..2026-10-31') is used as `date` for table exports
    without a date column; without either, `date` is 'unknown' (never guessed). `origin`: who produced the file
    (fixtures and demos MUST pass simulated_test_data). `tz`: the export's time zone, used to convert publish times."""
    if origin not in DATA_ORIGINS:
        raise ValueError(f"origin must be one of {DATA_ORIGINS}")
    if kind not in CSV_MAPS:
        raise ValueError(f"unknown export kind {kind!r}; known: {', '.join(CSV_MAPS)}")
    spec = CSV_MAPS[kind]
    p = Path(str(path_or_text))
    text = p.read_text(encoding="utf-8-sig") if len(str(path_or_text)) < 4096 and p.is_file() else str(path_or_text)
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    known = set(spec["map"]) | set(spec["id"]) | set(spec["title"]) | set(spec["date"]) | set(spec["published"])
    unmapped = [h for h in headers if h not in known]
    out, skipped, invalid, dups, seen, total = [], [], [], [], {}, None
    for i, raw in enumerate(reader):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        vid = _pick(row, spec["id"])
        if vid and vid.lower() == "total":  # Studio's first data row is the channel total: kept for reconciliation only
            total = {name: _convert(conv, row[h]) for h, (name, conv) in spec["map"].items() if h in row and _convert(conv, row[h]) is not None}
            skipped.append({"row": i + 2, "reason": "total row"})
            continue
        if not vid:
            skipped.append({"row": i + 2, "reason": "no video id"})
            continue
        metrics = {}
        for h, (name, conv) in spec["map"].items():
            if h in row and row[h]:
                v = _convert(conv, row[h])
                if v is None:
                    invalid.append({"row": i + 2, "column": h, "value": row[h][:40], "reason": "not a number"})
                elif not _valid(name, v):
                    invalid.append({"row": i + 2, "column": h, "value": row[h][:40], "reason": "out of range"})
                else:
                    metrics[name] = v
        extra = {h: row[h] for h in unmapped if row.get(h)}
        if extra:
            metrics["raw"] = extra
        raw_date = _pick(row, spec["date"])
        date = (norm_date(raw_date) or raw_date) if raw_date else (period or "unknown")
        if (vid, date) in seen:
            dups.append({"row": i + 2, "video_id": vid, "date": date, "first_row": seen[(vid, date)], "kept": "first"})
            continue
        seen[(vid, date)] = i + 2
        rec = C.make("AnalyticsRecord", platform=spec["platform"], video_id=vid, date=date, metrics=metrics,
                     source=f"csv:{kind}", imported=time.time(), project_id=project_id)
        rec["data_origin"] = origin
        if raw_date and not norm_date(raw_date):
            rec["date_unparsed"] = True
        title, pub = _pick(row, spec["title"]), _pick(row, spec["published"])
        if title:
            rec["title"] = title
        if pub:
            rec["published"] = pub
            rec["published_date"] = norm_date(pub)
            rec["published_utc"] = to_utc(pub, tz)
            rec["published_tz"] = (tz if zone(tz) else f"{tz} (no tz database for it on this machine - not converted)") if tz \
                else "unknown (export time zone not stated)"
        out.append(rec)
    replaced = 0
    if persist:
        existing = {(r["video_id"], r["date"]) for r in records(spec["platform"])}
        replaced = sum((r["video_id"], r["date"]) in existing for r in out)
        save(out)
    return {"records": out, "imported": len(out), "skipped": skipped, "unmapped_columns": unmapped, "duplicates": dups,
            "invalid": invalid, "replaced_existing": replaced, "reconciliation": reconcile_total(out, total),
            "map_verified": spec["verified"], "kind": kind, "data_origin": origin}


ADDITIVE = ("views", "watch_minutes", "likes", "comments", "shares", "impressions", "subscribers_net", "subscribers_gained")


def reconcile_total(rows: list[dict], total: dict | None, tolerance: float = 0.01) -> dict:
    """Compare the sum of per-video rows with the export's own Total row (additive metrics only). A mismatch is
    reported, never corrected: a table export can be filtered or truncated."""
    if not total:
        return {"status": "no_total_row"}
    out = {}
    for k in ADDITIVE:
        if k in total:
            s = sum(r["metrics"].get(k) or 0 for r in rows)
            diff = s - total[k]
            out[k] = {"rows_sum": round(s, 3), "total_row": total[k], "diff": round(diff, 3),
                      "ok": abs(diff) <= max(1.0, tolerance * abs(total[k]))}
    return {"status": "ok" if all(v["ok"] for v in out.values()) else "mismatch", "metrics": out}


def reconcile_api_vs_export(api_rows: list[dict], export_row: dict, metric: str = "views", tolerance: float = 0.02) -> dict:
    """Daily API rows for one video vs the owner's export row for the same period. Reported, never merged."""
    s = sum((r["metrics"].get(metric) or 0) for r in api_rows)
    e = export_row["metrics"].get(metric)
    if e is None:
        return {"status": "missing_in_export", "metric": metric}
    ok = abs(s - e) <= max(1.0, tolerance * abs(e))
    return {"status": "ok" if ok else "mismatch", "metric": metric, "api_sum": s, "export": e, "diff": s - e,
            "days": len(api_rows), "note": "API data can lag the Studio export by up to a few days"}


# ------------------------------------------------------------------ YouTube Analytics API
def _yt_rows(resp: dict) -> list[dict]:
    cols = [c["name"] for c in resp.get("columnHeaders") or []]
    return [dict(zip(cols, r)) for r in resp.get("rows") or []]


def youtube_daily(video_id: str, start: str, end: str, account: dict | None, transport=None, adapter=None,
                  project_id: str | None = None, persist: bool = True, origin: str = "real_platform_data") -> list[dict]:
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
        rec = C.make("AnalyticsRecord", platform="youtube", video_id=video_id, date=str(row.get("day")), metrics=m,
                     source="api:youtube-analytics/v2", imported=time.time(), project_id=project_id)
        rec["data_origin"] = origin
        out.append(rec)
    if persist:
        save(out)
    return out


def youtube_retention(video_id: str, start: str, end: str, account: dict | None, transport=None, adapter=None,
                      project_id: str | None = None, persist: bool = True, origin: str = "real_platform_data") -> dict:
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
    rec["data_origin"] = origin
    if persist:
        save([rec])
    return rec
