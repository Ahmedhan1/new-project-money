"""Platform knowledge base + per-destination adaptation of a master timeline (PLAT-1).

Data: nexus/registry/platforms.yaml. Every field there is {value, kind, source, read_on}: `documented` (the platform's own
page), `observed` (third-party report), `experimental` (a NEXUS editorial choice), `unverified` (null). This module
never invents a value: when a field is unverified it says so in the notes and leaves the timeline alone for that aspect.
Ranking systems are not public; nothing here models an algorithm.

    names() / get(name) / value(name, field)      profiles (facebook_reels = facebook + its reels sub-profile)
    stale(days=90, today=None)                    profiles that need re-verification
    safe_zone(name, W, H) -> {top,bottom,left,right} pixels covered by the platform's UI
    tune(tl, name) -> (new_tl, notes)             non-destructive adaptation of a master timeline
    check(path_or_probe, name) -> {ok, checks}    a rendered file against the hard limits
    deliverables(names) -> groups                 destinations that can share one render
    import_metrics(path, name) -> records         manually exported analytics -> {features, retention, views}
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import functools
import json
import math
from pathlib import Path

import yaml

from . import config

CANONICAL = ("youtube", "youtube_shorts", "tiktok", "instagram_reels", "instagram_feed", "facebook", "snapchat_spotlight")
ALIASES = {"shorts": "youtube_shorts", "reels": "instagram_reels", "instagram": "instagram_reels", "snapchat": "snapchat_spotlight",
           "yt": "youtube", "ig_feed": "instagram_feed"}


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "platforms.yaml").read_text(encoding="utf-8"))


def names() -> list[str]:
    return [n for n in _data()["platforms"]] + ["facebook_reels"]


def _resolve(name: str) -> str:
    n = ALIASES.get(name, name)
    if n not in _data()["platforms"] and n != "facebook_reels":
        raise KeyError(f"unknown platform '{name}' (known: {', '.join(names())})")
    return n


def get(name: str) -> dict:
    """Profile with raw fields ({value, kind, source, read_on}) and `v` = plain values."""
    n = _resolve(name)
    if n == "facebook_reels":
        base = copy.deepcopy(_data()["platforms"]["facebook"])
        sub = base.pop("sub_profiles", {}).get("reels", {})
        base["fields"].update(sub.get("fields", {}))
        base["display"] = sub.get("display", "Facebook Reels")
    else:
        base = copy.deepcopy(_data()["platforms"][n])
        base.pop("sub_profiles", None)
    base["name"] = n
    base["v"] = {k: f.get("value") for k, f in base["fields"].items()}
    return base


def value(name: str, field: str, default=None):
    v = get(name)["v"].get(field)
    return default if v is None else v


def _cite(prof: dict, field: str) -> str:
    f = prof["fields"].get(field) or {}
    return f"{field}: {f.get('kind', 'unverified')}" + (f" ({f.get('source')}, read {f.get('read_on')})" if f.get("source") else "")


def stale(days: int = 90, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    out = []
    for n, p in _data()["platforms"].items():
        rv = dt.date.fromisoformat(str(p.get("reviewed_on") or _data().get("reviewed_on")))
        if (today - rv).days > days:
            out.append({"platform": n, "reviewed_on": str(rv), "age_days": (today - rv).days})
    return out


def safe_zone(name: str, W: int, H: int) -> dict:
    z = value(name, "safe_zone") or {}
    return {k: int(round(float(z.get(k, 0.0)) * (H if k in ("top", "bottom") else W))) for k in ("top", "bottom", "left", "right")}


def _ratio(a) -> float | None:
    if isinstance(a, str) and ":" in a:
        x, y = a.split(":")
        return float(x) / float(y)
    return None


def _cut_points(tl: dict) -> list[float]:
    """Programme times where a cut keeps sentences whole: ends of subtitle cues that end a sentence, else any cue end."""
    cues = tl.get("subtitles") or []
    ends = [c["end"] for c in cues if str(c.get("text", "")).rstrip().endswith((".", "!", "?", "…", "。", "؟"))]
    return sorted(ends or [c["end"] for c in cues])


def _trim_to(tl: dict, limit: float, notes: list[str]) -> dict:
    from .timeline import ops
    from .timeline.render import clip_duration, clip_starts, total_duration
    total = total_duration(tl)
    if total <= limit:
        return tl
    pts = [t for t in _cut_points(tl) if t <= limit - 0.05]
    cut = (pts[-1] + 0.25) if pts else limit
    cut = min(cut, limit)
    starts = clip_starts(tl)
    keep, last = [], None
    for c, st in zip(tl["clips"], starts):
        if st >= cut - 0.05:
            break
        keep.append(c)
        last = (c, st)
    tl = copy.deepcopy(tl)
    tl["clips"] = copy.deepcopy(keep)
    if last:
        c, st = tl["clips"][-1], last[1]
        over = st + clip_duration(c) - cut
        if over > 0.04:
            if c["type"] == "video":
                speed = float(c.get("speed", 1.0))
                c["out"] = round(c["out"] - over * speed, 3)
            else:
                c["duration"] = round(float(c["duration"]) - over, 3)
            c["fade_out"] = min(0.5, clip_duration(c) / 3)
    end = total_duration(tl)
    tl["subtitles"] = [s for s in tl.get("subtitles", []) if s["start"] < end - 0.1]
    for s in tl["subtitles"]:
        s["end"] = min(s["end"], end)
    tl["audio"] = [a for a in tl.get("audio", []) if float(a.get("start", 0)) < end - 0.1]
    tl["markers"] = [m for m in tl.get("markers", []) if m["t"] < end]
    notes.append(f"duration {total:.1f}s is over the {limit:.0f}s limit: cut at {end:.1f}s "
                 f"({'after a complete sentence' if pts else 'no sentence boundary found before the limit'})")
    _ = ops  # timeline ops stay the editing API; this trim keeps whole cues by construction
    return tl


def tune(tl: dict, name: str) -> tuple[dict, list[str]]:
    """Adapt a master timeline for one destination. Never mutates `tl`. Every change cites the field that caused it."""
    from .timeline import ops
    prof = get(name)
    v = prof["v"]
    notes: list[str] = []
    out = copy.deepcopy(tl)
    res = v.get("resolution_recommended") or {}
    W, H = int(res.get("width") or tl["width"]), int(res.get("height") or tl["height"])
    if (W, H) != (tl["width"], tl["height"]):
        src_ar, dst_ar = tl["width"] / tl["height"], W / H
        out = ops.reframe(out, W, H, "cover")
        changed = 0
        for c in out["clips"]:
            if c["type"] != "video":
                continue
            has_focus = isinstance(c.get("focus"), (list, tuple)) and tuple(round(float(x), 3) for x in c["focus"]) != (0.5, 0.5)
            if abs(src_ar - dst_ar) > 0.3 and not has_focus and not c.get("fit_override"):
                c["fit"] = "blur"  # no measured subject position: keep the whole picture over a blurred fill
                changed += 1
        notes.append(f"reframed {tl['width']}x{tl['height']} -> {W}x{H} [{_cite(prof, 'resolution_recommended')}]" +
                     (f"; {changed} clip(s) without a measured subject position use blur-fill instead of a blind centre crop"
                      if changed else "; clips keep their subject focus"))
    limit = v.get("duration_max_s")
    if limit:
        before = len(notes)
        out = _trim_to(out, float(limit), notes)
        if len(notes) > before:
            notes[-1] += f" [{_cite(prof, 'duration_max_s')}]"
    z = v.get("safe_zone")
    if z:
        cap = dict(out.get("captions") or {"style": "classic", "position": "bottom"})
        cap["safe_bottom"], cap["safe_top"] = float(z.get("bottom", 0)), float(z.get("top", 0))
        if cap.get("position", "bottom") == "bottom" and float(z.get("bottom", 0)) > 0.3:
            cap["position"] = "middle" if float(z.get("bottom", 0)) > 0.4 else "bottom"
        out["captions"] = cap
        notes.append(f"captions kept clear of the platform UI (bottom {z.get('bottom', 0):.0%}, top {z.get('top', 0):.0%}) "
                     f"[{_cite(prof, 'safe_zone')}]")
    else:
        notes.append(f"no safe-zone data for {prof['name']}: caption placement unchanged [{_cite(prof, 'safe_zone')}]")
    lufs = v.get("loudness_target_lufs")
    if lufs is not None:
        out["target_lufs"] = float(lufs)
        notes.append(f"loudness target {lufs} LUFS [{_cite(prof, 'loudness_target_lufs')}]")
    fps = v.get("fps") or {}
    cur = int(out.get("fps", config.FPS))
    allowed = fps.get("list") if isinstance(fps, dict) else None
    if allowed and cur not in allowed:
        out["fps"] = min(allowed, key=lambda x: abs(x - cur))
        notes.append(f"frame rate {cur} -> {out['fps']} [{_cite(prof, 'fps')}]")
    elif isinstance(fps, dict) and fps.get("max") and cur > fps["max"]:
        out["fps"] = int(fps["max"])
        notes.append(f"frame rate {cur} -> {out['fps']} [{_cite(prof, 'fps')}]")
    hw = v.get("hook_window_s")
    if hw:
        first = min([s["start"] for s in out.get("subtitles", [])] or [math.inf])
        ok = first <= float(hw)
        notes.append(f"hook timing: first words at {first if first != math.inf else 'none'}s vs a {hw}s window - "
                     f"{'ok' if ok else 'consider a stronger, earlier opening'} [{_cite(prof, 'hook_window_s')}]")
    out["platform"] = prof["name"]
    return out, notes


def _probe(path_or_probe) -> dict:
    if isinstance(path_or_probe, dict):
        return path_or_probe
    from .media import ff
    p = ff.probe(path_or_probe)
    p["size_bytes"] = Path(path_or_probe).stat().st_size
    return p


def check(path_or_probe, name: str) -> dict:
    """Validate a rendered file against the destination's documented/observed hard limits."""
    prof = get(name)
    v = prof["v"]
    pr = _probe(path_or_probe)
    vid = pr.get("video") or {}
    w, h = int(vid.get("width") or 0), int(vid.get("height") or 0)
    dur = float(pr.get("duration") or 0)
    checks = []

    def add(cid, ok, value, limit, field):
        checks.append({"id": cid, "ok": bool(ok), "value": value, "limit": limit, "basis": _cite(prof, field)})

    acc = v.get("aspect_accepted") or {}
    if w and h and isinstance(acc, dict) and not acc.get("any"):
        ar = w / h
        lo, hi = acc.get("min"), acc.get("max")
        lo = _ratio(lo) if isinstance(lo, str) else lo
        hi = _ratio(hi) if isinstance(hi, str) else hi
        add("aspect", (lo is None or ar >= lo - 0.01) and (hi is None or ar <= hi + 0.01), round(ar, 3), [lo, hi], "aspect_accepted")
    rec = _ratio(v.get("aspect_recommended"))
    if rec and w and h:
        add("aspect_recommended", abs(w / h - rec) < 0.02, round(w / h, 3), round(rec, 3), "aspect_recommended")
    rmin = v.get("resolution_min") or {}
    if rmin and w and h:
        short = min(w, h)
        need = rmin.get("short_side") or min(rmin.get("width", 0) or 0, rmin.get("height", 0) or 0) or 0
        add("resolution_min", short >= need, short, need, "resolution_min")
    if v.get("duration_max_s"):
        add("duration_max", dur <= float(v["duration_max_s"]) + 0.05, round(dur, 2), v["duration_max_s"], "duration_max_s")
    if v.get("duration_min_s"):
        add("duration_min", dur >= float(v["duration_min_s"]) - 0.05, round(dur, 2), v["duration_min_s"], "duration_min_s")
    if v.get("max_file_bytes") and pr.get("size_bytes"):
        add("file_size", pr["size_bytes"] <= v["max_file_bytes"], pr["size_bytes"], v["max_file_bytes"], "max_file_bytes")
    codecs = v.get("video_codec")
    if codecs and vid.get("codec"):
        add("video_codec", str(vid["codec"]).lower() in [str(c).lower() for c in codecs], vid["codec"], codecs, "video_codec")
    fps = v.get("fps") or {}
    if vid.get("fps") and isinstance(fps, dict):
        f = float(vid["fps"])
        ok = (round(f) in fps["list"]) if fps.get("list") else ((fps.get("min") or 0) - 0.5 <= f <= (fps.get("max") or 1e9) + 0.5)
        add("fps", ok, round(f, 3), fps, "fps")
    return {"platform": prof["name"], "ok": all(c["ok"] for c in checks if c["id"] != "aspect_recommended"), "checks": checks}


def _fps_ok(v: dict, fps: int) -> bool:
    f = v.get("fps") or {}
    if isinstance(f, dict) and f.get("list"):
        return fps in f["list"]
    if isinstance(f, dict):
        return (f.get("min") or 0) <= fps <= (f.get("max") or 1e9)
    return True


def deliverables(platforms: list[str], duration: float | None = None, fps: int = 30) -> list[dict]:
    """Group destinations that can share one encoded file: same frame size and loudness target, the frame rate is
    accepted by all, and the programme fits every member's duration limit (destinations that need a shorter cut get
    their own trimmed render). Packages stay separate: titles, descriptions and captions differ per platform."""
    groups: dict = {}
    for n in platforms:
        v = get(n)["v"]
        res = v.get("resolution_recommended") or {}
        cap = v.get("duration_max_s")
        trim = float(cap) if (duration and cap and duration > float(cap)) else None
        key = (res.get("width"), res.get("height"), v.get("loudness_target_lufs"), trim, _fps_ok(v, fps))
        groups.setdefault(key, []).append(_resolve(n))
    return [{"platforms": ps, "width": k[0], "height": k[1], "loudness_target_lufs": k[2], "trim_to_s": k[3], "fps": fps if k[4] else None,
             "note": "one render, separate packages (titles, descriptions and captions differ per platform)"}
            for k, ps in groups.items()]


FIELD_MAP = {"views": ("views", "plays", "video views", "impressions"),
             "retention": ("average percentage viewed", "avg. percentage viewed", "retention", "average_percentage_viewed",
                           "completion rate", "watched full video"),
             "avg_view_s": ("average view duration", "avg. view duration", "average watch time", "average_view_duration"),
             "duration_s": ("duration", "video duration", "length"), "title": ("title", "video title", "caption"),
             "published": ("published", "date", "publish time", "video publish time")}


def _num(x) -> float | None:
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("%", "")
    if ":" in s:  # h:mm:ss or m:ss
        parts = [float(p) for p in s.split(":")]
        return sum(p * 60 ** k for k, p in enumerate(reversed(parts)))
    try:
        return float(s)
    except ValueError:
        return None


def import_metrics(path: str | Path, name: str) -> list[dict]:
    """Normalise a manually exported analytics file (CSV or JSON list) to {platform, title, features, retention, views}.
    retention is a 0..1 share; when only average view duration and video length exist, retention = avg / length."""
    path = Path(path)
    rows = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else \
        list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    out = []
    for r in rows:
        low = {str(k).strip().lower(): v for k, v in r.items()}
        pick = {f: next((low[a] for a in al if a in low and str(low[a]).strip() != ""), None) for f, al in FIELD_MAP.items()}
        views, ret = _num(pick["views"]), _num(pick["retention"])
        avg, length = _num(pick["avg_view_s"]), _num(pick["duration_s"])
        if ret is not None and ret > 1:
            ret /= 100.0
        if ret is None and avg is not None and length:
            ret = min(1.0, avg / length)
        if views is None and ret is None:
            continue
        out.append({"platform": _resolve(name), "title": pick["title"], "views": views, "retention": ret,
                    "features": {"duration_s": length, "avg_view_s": avg}, "published": pick["published"]})
    return out
