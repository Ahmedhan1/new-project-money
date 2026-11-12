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

from . import db, platforms, qa
from .media import ff
from .timeline import render, text as ttext

def _legacy_view() -> dict:
    """{platform: {size, max_s, title_max, desc_max, tags, fit, label, kind}} derived from registry/platforms.yaml."""
    out = {}
    for pid in [*platforms.names(), "square"]:
        sp = platforms.spec(pid)
        out[pid] = {"size": sp["size"], "max_s": sp.get("max_s"), "title_max": sp["title_max"], "desc_max": sp["desc_max"],
                    "tags": sp["hashtags"], "fit": sp["fit"], "label": sp.get("label", pid), "kind": sp.get("kind")}
    return out


PLATFORMS = _legacy_view()
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
    """Tune the timeline to the platform (platforms.tune), check it against the spec, render it and write everything needed for
    a manual upload. Nothing is posted: auto-publishing needs the user's own OAuth apps and is not implemented."""
    sp = platforms.spec(platform)
    W, H = sp["size"]
    ptl, notes = platforms.tune(tl, platform, analyses)
    check = platforms.checklist(ptl, platform)
    d = project.dir / "publish" / platform / lang
    d.mkdir(parents=True, exist_ok=True)
    out = d / "video.mp4"
    stats = render.render(ptl, out, project.dir / "work")
    tech = qa.technical_review(out, qa.expectation(ptl))
    size_mb = out.stat().st_size / 1e6
    if sp.get("file_max_mb") and size_mb > sp["file_max_mb"]:
        notes.append({"change": f"FLAG: file is {size_mb:.0f} MB, above the {sp['file_max_mb']} MB limit", "signal": "spec", "confidence": "spec"})
    md = platforms.metadata(project, ptl, platform, lang, title=title, description=description or None)
    meta = {"platform": platform, "language": lang, "title": md["title"], "description": md["description"], "hashtags": md["hashtags"],
            "video": str(out), "resolution": f"{W}x{H}", "duration": stats["duration"], "size_mb": round(size_mb, 2), "captions": stats["sidecars"],
            "thumbnail": str(thumbnail(str(out), md["title"], d / "thumbnail.jpg", (1280, 720) if W > H else (W, H), lang=lang)),
            "technical_qa": "passed" if tech["passed"] else [c["detail"] for c in tech["checks"] if not c["ok"]],
            "platform_checklist": check, "notes": [n["change"] for n in notes], "platform_notes": notes,
            "signals_note": "Ranking systems are not public; notes cite what the platform itself documented or say 'editorial'.",
            "auto_publish": AUTO_PUBLISH.get(platform, "not implemented - manual upload package only"),
            "upload": "manual - open the platform's upload page and use the files in this folder", "human_approval": "PENDING"}
    (d / "metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
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
