"""Read AnalyticsRecord documents (agent E's importers produce them) into comparable numbers.

An AnalyticsRecord is one cumulative snapshot of one video on `date`. Metric names vary between platform exports,
so `metric()` accepts the common aliases and derives rates (ctr, like_rate, avg_view_pct, views_per_day, ...).
Descriptive fields (niche, language, format, hook_type, published, duration_s, title) may sit in `meta` or in `metrics`.
Unknown values stay None - nothing is filled in.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import time
from pathlib import Path

from .. import contracts as C

ALIASES = {
    "views": ("views", "view_count", "plays"),
    "impressions": ("impressions", "reach_impressions"),
    "clicks": ("clicks", "impression_clicks"),
    "ctr": ("ctr", "impressions_ctr", "impressions_click_through_rate", "impressions click-through rate (%)"),
    "likes": ("likes", "like_count"),
    "comments": ("comments", "comment_count"),
    "shares": ("shares", "share_count"),
    "subscribers_gained": ("subscribers_gained", "subscribers"),
    "avg_view_s": ("avg_view_s", "average_view_duration", "average_view_duration_s", "avg_view_duration"),
    "avg_view_pct": ("avg_view_pct", "average_view_percentage", "average_percentage_viewed"),
    "duration_s": ("duration_s", "duration_seconds", "length_s"),
}
META = ("published", "niche", "language", "format", "hook_type", "title", "duration_s", "category", "opening", "caption_style")


def rid(r: dict) -> str:
    return f"{r.get('platform')}:{r.get('video_id')}@{r.get('date')}"


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _raw(r: dict, name: str):
    m = r.get("metrics") or {}
    for a in ALIASES.get(name, (name,)):
        if a in m and m[a] not in (None, ""):
            return m[a]
    return None


def meta(r: dict, key: str):
    v = (r.get("meta") or {}).get(key)
    return v if v not in (None, "") else (r.get("metrics") or {}).get(key)


def parse_date(s) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def age_days(r: dict) -> float | None:
    a = _num((r.get("metrics") or {}).get("age_days"))
    if a is not None:
        return a
    pub, obs = parse_date(meta(r, "published")), parse_date(r.get("date"))
    return float((obs - pub).days) if pub and obs else None


def retention_at(r: dict, t: float) -> float | None:
    """Share of starting viewers still watching at t seconds, from metrics.retention_curve.
    Curve forms: [[t_seconds, share], ...] or {"ratio": [[elapsed_ratio, share], ...]} with duration_s known."""
    curve = (r.get("metrics") or {}).get("retention_curve")
    if not curve:
        return None
    if isinstance(curve, dict):
        dur = metric(r, "duration_s")
        if not dur:
            return None
        pts = [(float(x) * dur, float(y)) for x, y in curve.get("ratio") or []]
    else:
        pts = [(float(x), float(y)) for x, y in curve]
    pts.sort()
    if not pts or t > pts[-1][0]:
        return None
    base = pts[0][1] or 1.0
    for (t0, y0), (t1, y1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            y = y0 if t1 == t0 else y0 + (y1 - y0) * (t - t0) / (t1 - t0)
            return y / base
    return pts[0][1] / base if t <= pts[0][0] else None


def metric(r: dict, name: str) -> float | None:
    """A metric or derived rate for one record; None if it cannot be computed honestly."""
    if name == "ctr":
        c = _num(_raw(r, "ctr"))
        if c is not None:
            return c / 100 if c > 1 else c
        clk, imp = _num(_raw(r, "clicks")), _num(_raw(r, "impressions"))
        return clk / imp if clk is not None and imp else None
    if name == "clicks":
        clk = _num(_raw(r, "clicks"))
        if clk is not None:
            return clk
        c, imp = metric(r, "ctr"), _num(_raw(r, "impressions"))
        return round(c * imp) if c is not None and imp is not None else None
    if name in ("like_rate", "comment_rate", "subscribe_rate"):
        num = {"like_rate": "likes", "comment_rate": "comments", "subscribe_rate": "subscribers_gained"}[name]
        x, v = _num(_raw(r, num)), _num(_raw(r, "views"))
        return x / v if x is not None and v else None
    if name == "avg_view_pct":
        p = _num(_raw(r, "avg_view_pct"))
        if p is not None:
            return p / 100 if p > 1 else p
        s, d = _num(_raw(r, "avg_view_s")), metric(r, "duration_s")
        return min(1.0, s / d) if s is not None and d else None
    if name == "duration_s":
        d = _num(_raw(r, "duration_s"))
        return d if d is not None else _num(meta(r, "duration_s"))
    if name == "log_views":
        v = _num(_raw(r, "views"))
        return math.log1p(v) if v is not None else None
    if name == "views_per_day":
        v, a = _num(_raw(r, "views")), age_days(r)
        return v / max(a, 1.0) if v is not None and a is not None else None
    if name.startswith("retention_") and name.endswith("s"):
        return retention_at(r, float(name[len("retention_"):-1]))
    if name == "age_days":
        return age_days(r)
    return _num(_raw(r, name))


def at_age(records: list[dict], day: float, tol: float | None = None) -> list[dict]:
    """One snapshot per video, the one closest to `day` days after publication (within tol, default max(1, 15 %)).
    Videos whose age is unknown or that have no snapshot near that age are left out, so arms are compared at equal age."""
    tol = max(1.0, 0.15 * day) if tol is None else tol
    best: dict[tuple, tuple[float, dict]] = {}
    for r in records:
        a = age_days(r)
        if a is None or abs(a - day) > tol:
            continue
        k = (r.get("platform"), r.get("video_id"))
        if k not in best or abs(a - day) < best[k][0]:
            best[k] = (abs(a - day), r)
    return [v[1] for v in best.values()]


def latest(records: list[dict]) -> list[dict]:
    out: dict[tuple, dict] = {}
    for r in records:
        k = (r.get("platform"), r.get("video_id"))
        if k not in out or str(r.get("date")) > str(out[k].get("date")):
            out[k] = r
    return list(out.values())


def segment(records: list[dict], keys: tuple[str, ...] = ("platform", "niche", "language", "format")) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        k = tuple(r.get(x) if x == "platform" else meta(r, x) for x in keys)
        groups.setdefault(k, []).append(r)
    return groups


def load_csv(path: str | Path, platform: str, source: str = "owner_csv_export", id_col: str = "video_id",
             date_col: str = "date", meta_cols: tuple[str, ...] = META) -> list[dict]:
    """Tiny loader for a flat CSV (one row = one video snapshot) into AnalyticsRecord dicts. Numeric columns go to
    `metrics`, descriptive ones to `meta`. Not an importer: agent E owns platform importers."""
    rows = list(csv.DictReader(Path(path).read_text(encoding="utf-8-sig").splitlines()))
    out, now = [], time.time()
    for row in rows:
        vid = (row.get(id_col) or "").strip()
        if not vid:
            continue
        metrics = {k: _num(v) for k, v in row.items() if k not in (id_col, date_col) and k not in meta_cols and _num(v) is not None}
        doc = C.make("AnalyticsRecord", platform=platform, video_id=vid, date=(row.get(date_col) or "").strip() or "unknown",
                     metrics=metrics, source=source, imported=now)
        doc["meta"] = {k: row[k] for k in meta_cols if row.get(k) not in (None, "")}
        out.append(doc)
    return out
