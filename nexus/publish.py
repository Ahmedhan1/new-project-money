"""Publishing Studio + Content Business: platform packages, channels, calendar, revenue ledger.

No platform API is called. Auto-posting needs each platform's own OAuth app/review and the user's explicit
authorisation, so NEXUS produces a complete upload package per platform instead and says so.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from PIL import Image

from . import db, qa
from .media import ff
from .timeline import ops, render, text as ttext
from .timeline.render import total_duration

PLATFORMS = {
    "youtube": {"size": (1920, 1080), "max_s": None, "title_max": 100, "desc_max": 5000, "tags": 12, "fit": "contain"},
    "shorts": {"size": (1080, 1920), "max_s": 180, "title_max": 100, "desc_max": 5000, "tags": 5, "fit": "blur"},
    "tiktok": {"size": (1080, 1920), "max_s": 600, "title_max": 150, "desc_max": 2200, "tags": 5, "fit": "blur"},
    "instagram-reels": {"size": (1080, 1920), "max_s": 180, "title_max": 125, "desc_max": 2200, "tags": 8, "fit": "blur"},
    "instagram-feed": {"size": (1080, 1350), "max_s": 3600, "title_max": 125, "desc_max": 2200, "tags": 8, "fit": "blur"},
    "facebook": {"size": (1920, 1080), "max_s": None, "title_max": 255, "desc_max": 5000, "tags": 5, "fit": "contain"},
    "facebook-reels": {"size": (1080, 1920), "max_s": 90, "title_max": 255, "desc_max": 2200, "tags": 5, "fit": "blur"},
    "snapchat-spotlight": {"size": (1080, 1920), "max_s": 60, "title_max": 100, "desc_max": 160, "tags": 3, "fit": "blur"},
    "linkedin": {"size": (1920, 1080), "max_s": 900, "title_max": 150, "desc_max": 3000, "tags": 3, "fit": "contain"},
    "square": {"size": (1080, 1080), "max_s": None, "title_max": 100, "desc_max": 2200, "tags": 5, "fit": "blur"},
}
AUTO_PUBLISH = {p: "not implemented - manual upload package only" for p in PLATFORMS}


def _hashtags(title: str, script: dict | None, n: int) -> list[str]:
    words = re.findall(r"[A-Za-z]{4,}", title + " " + " ".join(s.get("on_screen_text", "") for s in (script or {}).get("scenes", [])))
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen and w.lower() not in {"this", "that", "with", "from", "your", "what", "have", "about", "they"}:
            seen.add(w.lower())
            out.append("#" + w.capitalize())
    return out[:n]


def thumbnail(video: str, title: str, out: Path, size=(1280, 720), at: float | None = None, lang: str = "en") -> Path:
    tmp = out.with_suffix(".frame.png")
    ff.run(["-ss", at if at is not None else ff.duration(video) * 0.3, "-i", video, "-frames:v", "1", "-vf",
            f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]}", tmp], op="thumbnail")
    img = Image.open(tmp).convert("RGBA")
    shade = Image.new("RGBA", img.size, (0, 0, 0, 110))
    img = Image.alpha_composite(img, shade)
    ttext.draw_text_block(img, title, "title", lang)
    img.convert("RGB").save(out, quality=90)
    tmp.unlink()
    return out


def package(project, tl: dict, platform: str, title: str, description: str = "", lang: str = "en", analyses: dict | None = None) -> dict:
    """Render a platform-specific version and write everything needed for a manual upload."""
    spec = PLATFORMS[platform]
    W, H = spec["size"]
    ptl = ops.reframe(tl, W, H, spec["fit"])
    for c in ptl["clips"]:
        if c["type"] == "image":
            c["fit"] = "cover"
    notes = []
    if spec["max_s"] and total_duration(ptl) > spec["max_s"]:
        notes.append(f"programme is {total_duration(ptl):.0f}s but {platform} allows {spec['max_s']}s - make a shorter cut before uploading")
    d = project.dir / "publish" / platform / lang
    d.mkdir(parents=True, exist_ok=True)
    out = d / "video.mp4"
    stats = render.render(ptl, out, project.dir / "work")
    tech = qa.technical_review(out, qa.expectation(ptl))
    credits = project.data.get("credits", [])
    desc = (description or (project.data.get("script") or {}).get("topic", "")).strip()
    if credits:
        desc += "\n\nImage credits:\n" + "\n".join(f"- {c}" for c in credits)
    tags = _hashtags(title, project.data.get("script"), spec["tags"])
    meta = {"platform": platform, "language": lang, "title": title[:spec["title_max"]], "description": desc[:spec["desc_max"]], "hashtags": tags,
            "video": str(out), "resolution": f"{W}x{H}", "duration": stats["duration"], "captions": stats["sidecars"],
            "thumbnail": str(thumbnail(str(out), title, d / "thumbnail.jpg", (1280, 720) if W > H else (W, H), lang=lang)),
            "technical_qa": "passed" if tech["passed"] else [c["detail"] for c in tech["checks"] if not c["ok"]], "notes": notes,
            "auto_publish": AUTO_PUBLISH[platform], "upload": "manual - open the platform's upload page and use the files in this folder",
            "human_approval": "PENDING"}
    (d / "metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return meta


# ------------------------------------------------------------------ channels / business
def create_channel(name: str, niche: str, audience: str, languages: list[str], platforms: list[str], pillars: list[str] | None = None) -> dict:
    ch = {"name": name, "niche": niche, "audience": audience, "languages": languages, "platforms": platforms, "pillars": pillars or [],
          "monetization": [{"model": m, "status": "not started", "eligibility": "check the platform's current programme rules for your country/account"}
                           for m in ("platform ad revenue", "affiliate links", "sponsorships", "digital products")],
          "created": time.strftime("%Y-%m-%d")}
    db.put_doc("channel", ch)
    return ch


def calendar(channel_id: str, topics: list[str], per_day: int = 5, start: str | None = None) -> list[dict]:
    ch = db.get_doc("channel", channel_id)
    t0 = time.mktime(time.strptime(start, "%Y-%m-%d")) if start else time.time()
    items = [{"date": time.strftime("%Y-%m-%d", time.localtime(t0 + (i // per_day) * 86400)), "slot": i % per_day + 1, "topic": t,
              "pillar": ch["pillars"][i % len(ch["pillars"])] if ch["pillars"] else None, "languages": ch["languages"],
              "platforms": ch["platforms"], "status": "planned"} for i, t in enumerate(topics)]
    ch["calendar"] = items
    db.put_doc("channel", ch, channel_id)
    return items


def record_revenue(channel_id: str, amount: float, source: str, kind: str, note: str = "") -> dict:
    """kind must be 'actual' (money really received) or 'estimate'. The two are never mixed."""
    if kind not in ("actual", "estimate"):
        raise ValueError("kind must be 'actual' or 'estimate'")
    e = {"channel": channel_id, "amount": amount, "source": source, "kind": kind, "note": note, "date": time.strftime("%Y-%m-%d")}
    db.put_doc("revenue", e)
    return e


def business_summary(channel_id: str) -> dict:
    rows = [r for r in db.list_docs("revenue") if r["channel"] == channel_id]
    return {"actual_revenue": sum(r["amount"] for r in rows if r["kind"] == "actual"),
            "estimated_revenue": sum(r["amount"] for r in rows if r["kind"] == "estimate"),
            "expenses": 0.0, "note": "No income is guaranteed. Estimates are projections, not earnings.", "entries": rows}


# ------------------------------------------------------------------ destination selector (nexus/platforms.py)
def _limit_text(text: str, chars: int | None = None, utf8_bytes: int | None = None) -> str:
    text = text.replace("<", "").replace(">", "") if utf8_bytes else text
    if chars and len(text) > chars:
        text = text[: max(0, chars - 1)].rstrip() + "…"
    while utf8_bytes and len(text.encode("utf-8")) > utf8_bytes:
        text = text[:-2].rstrip() + "…"
    return text


def deliver(project, tl: dict, destinations: list[str], title: str, description: str = "", lang: str = "en",
            on_group=None) -> dict:
    """One master -> every selected destination. Destinations that can share a render share it (platforms.deliverables);
    each destination still gets its own package: tuned timeline notes, file check against its limits, title/description/
    hashtags within its limits, caption sidecars, cover frame and a MANUAL_UPLOAD.md. Nothing is uploaded."""
    from . import platforms
    from .timeline.render import total_duration as _td
    groups = platforms.deliverables(destinations, duration=_td(tl), fps=int(tl.get("fps", 30)))
    out: dict = {"groups": [], "packages": {}}
    for gi, g in enumerate(groups):
        lead = g["platforms"][0]
        ptl, notes = platforms.tune(tl, lead)
        gdir = project.dir / "publish" / f"render_{gi}_{ptl['width']}x{ptl['height']}" / lang
        gdir.mkdir(parents=True, exist_ok=True)
        video = gdir / "video.mp4"
        stats = render.render(ptl, video, project.dir / "work")
        out["groups"].append({"platforms": g["platforms"], "video": str(video.relative_to(project.dir)), "duration": stats["duration"],
                              "size": f"{ptl['width']}x{ptl['height']}", "tuning": notes})
        for name in g["platforms"]:
            prof = platforms.get(name)
            v = prof["v"]
            d = project.dir / "publish" / name / lang
            d.mkdir(parents=True, exist_ok=True)
            chk = platforms.check(video, name)
            tags = _hashtags(title, project.data.get("script"), min(int(v.get("hashtags_max") or 5), 5))
            cap_limit = v.get("caption_max_chars") or v.get("caption_max_utf16")
            meta = {"platform": name, "display": prof["display"], "language": lang, "video": str(video.relative_to(project.dir)),
                    "shared_render_with": [x for x in g["platforms"] if x != name],
                    "title": _limit_text(title, v.get("title_max_chars")),
                    "description": _limit_text(description.strip(), v.get("description_max_chars"), v.get("description_max_bytes")),
                    "hashtags": tags, "check": chk, "tuning": notes, "captions": stats["sidecars"],
                    "music_policy": v.get("music_policy"), "upload_api": v.get("upload_api"),
                    "auto_publish": "not performed: needs your own authorised app/account for this platform",
                    "human_approval": "PENDING", "built": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if cap_limit:
                meta["caption_text"] = _limit_text(" ".join([meta["title"], meta["description"], " ".join(tags)]).strip(), int(cap_limit))
            vw, vh = ptl["width"], ptl["height"]
            meta["cover"] = str(thumbnail(str(video), title, d / "cover.jpg", (1280, 720) if vw > vh else (720, 1280), lang=lang).relative_to(project.dir))
            steps = v.get("manual_upload") or ["Open the platform's upload page and use the files in this folder."]
            (d / "MANUAL_UPLOAD.md").write_text(
                f"# Upload to {prof['display']}\n\nVideo: `{meta['video']}` · cover: `{meta['cover']}`\n\n"
                + "\n".join(f"{k + 1}. {s}" for k, s in enumerate(steps))
                + f"\n\n## Check against {prof['display']} limits\n"
                + "\n".join(f"- [{'x' if c['ok'] else ' '}] {c['id']}: {c['value']} (limit {c['limit']}; {c['basis']})" for c in chk["checks"])
                + (f"\n\n## Music\n{v['music_policy']}\n" if v.get("music_policy") else "\n")
                + "\nNEXUS did not upload anything. Approve the video yourself before posting.\n", encoding="utf-8")
            (d / "metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            out["packages"][name] = {k: meta[k] for k in ("video", "title", "shared_render_with", "cover")} | {"ok": chk["ok"],
                                                                                                         "folder": str(d.relative_to(project.dir))}
        if on_group:
            on_group(gi + 1, len(groups))
    out["renders"] = len(groups)
    out["destinations"] = len(destinations)
    return out
