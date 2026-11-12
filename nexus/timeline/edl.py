"""Interchange: CMX3600 EDL and OpenTimelineIO JSON export/import of the main storyline (tl["clips"]).

Both round-trip cuts, crossfades (with their xfade name), speed and source in/out at frame accuracy; other clip keys
travel in comments (EDL `* NEXUS CLIP:`) / metadata (OTIO `metadata.nexus`) so a NEXUS -> EDL -> NEXUS round trip is
lossless, while other editors see standard events.

Transition model: a NEXUS crossfade into clip B overlaps the last `d` frames of clip A. In the EDL, A's event ends
where the dissolve starts and B's event is a `D <frames>` line starting there (CMX convention: the outgoing clip's
media for the dissolve comes after its event's source out). In OTIO the cut sits at the start of the dissolve:
Transition(in_offset=0, out_offset=d), A's source range is shortened by d.
The OTIO JSON is written by hand (no dependency); if `opentimelineio` is installed it can read it (tests check that
when the package is available).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config

REC_START = 3600  # record timecode starts at 01:00:00:00 (seconds)
WIPES = {"wipeleft": "W001", "wiperight": "W002", "wipeup": "W003", "wipedown": "W004"}


def tc(frames: int, fps: int) -> str:
    f = int(round(frames))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def frames_of(t: str, fps: int) -> int:
    h, m, s, f = (int(x) for x in re.split(r"[:;]", t.strip()))
    return ((h * 60 + m) * 60 + s) * fps + f


def _reel(src: str, used: dict) -> str:
    if src in used:
        return used[src]
    base = re.sub(r"[^A-Z0-9]", "", Path(src).stem.upper())[:6] or "REEL"
    name, k = base, 1
    while name in used.values():
        name = f"{base[:5]}{k}"[:8]
        k += 1
    used[src] = name
    return name


def _layout(tl: dict):
    from . import pieces
    return pieces.layout(tl)


def to_edl(tl: dict, title: str = "NEXUS") -> str:
    """CMX3600 text (non-drop frame, integer fps)."""
    fps = int(tl.get("fps", config.FPS))
    clips = tl["clips"]
    n, d, names = _layout(tl)
    lines = [f"TITLE: {title[:60]}", "FCM: NON-DROP FRAME", ""]
    reels: dict = {}
    rec = REC_START * fps
    ev = 0
    for i, c in enumerate(clips):
        own = n[i] - (d[i + 1] if i + 1 < len(clips) else 0)  # record frames of this event (the dissolve belongs to B)
        sp = float(c.get("speed", 1.0)) if c["type"] == "video" else 1.0
        if c["type"] == "video":
            reel, src_in = _reel(c["src"], reels), round(float(c["in"]) * fps)
        elif c["type"] == "image":
            reel, src_in = "AX", 0
        else:
            reel, src_in = "BL", 0
        src_len = round(own * sp)
        ch = "B" if c["type"] == "video" and not c.get("mute") else "V"
        ev += 1
        if d[i]:
            p = clips[i - 1]
            p_sp = float(p.get("speed", 1.0)) if p["type"] == "video" else 1.0
            p_reel = _reel(p["src"], reels) if p["type"] == "video" else ("AX" if p["type"] == "image" else "BL")
            p_out = (round(float(p["in"]) * fps) if p["type"] == "video" else 0) + round((n[i - 1] - d[i]) * p_sp)
            code = WIPES.get(names[i] or "", "D")
            lines.append(f"{ev:03d}  {p_reel:8s} {ch:5s} C        {tc(p_out, fps)} {tc(p_out, fps)} {tc(rec, fps)} {tc(rec, fps)}")
            lines.append(f"{ev:03d}  {reel:8s} {ch:5s} {code:4s} {d[i]:03d} {tc(src_in, fps)} {tc(src_in + src_len, fps)} "
                         f"{tc(rec, fps)} {tc(rec + own, fps)}")
            lines.append(f"* NEXUS XFADE: {names[i]}")
        else:
            lines.append(f"{ev:03d}  {reel:8s} {ch:5s} C        {tc(src_in, fps)} {tc(src_in + src_len, fps)} "
                         f"{tc(rec, fps)} {tc(rec + own, fps)}")
        if abs(sp - 1.0) > 1e-6:
            lines.append(f"M2   {reel:8s}       {sp * fps:05.1f}                {tc(src_in, fps)}")
        lines.append(f"* FROM CLIP NAME: {Path(c.get('src', c['type'])).name}")
        if c.get("src"):
            lines.append(f"* SOURCE FILE: {c['src']}")
        extra = {k: v for k, v in c.items() if k not in ("src", "in", "out", "speed", "transition", "transition_dur", "xfade")}
        lines.append("* NEXUS CLIP: " + json.dumps(extra, ensure_ascii=False, separators=(",", ":")))
        lines.append("")
        rec += own
    return "\n".join(lines)


_EV = re.compile(r"^(\d{3,6})\s+(\S+)\s+(\S+)\s+(C|D|W\d{3})\s*(\d{3})?\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")


def from_edl(text: str, fps: int | None = None, reels: dict | None = None, base: dict | None = None) -> dict:
    """Timeline from CMX3600 text. Sources: `* SOURCE FILE:` comments, else `reels` {reel: path}. Events without a
    known source become colour clips (BL) or are refused. `base` supplies width/height/etc. (default 1920x1080)."""
    fps = int(fps or (base or {}).get("fps", config.FPS))
    events: list[dict] = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _EV.match(line)
        if m:
            num, reel, _ch, kind, dur, s_in, s_out, r_in, r_out = m.groups()
            e = {"num": num, "reel": reel, "kind": kind, "dur": int(dur or 0), "s_in": frames_of(s_in, fps),
                 "s_out": frames_of(s_out, fps), "r_in": frames_of(r_in, fps), "r_out": frames_of(r_out, fps), "meta": {}}
            if cur and cur["num"] == num and kind != "C":
                e["from"] = cur          # second line of a transition event: the incoming clip
                events[-1] = e
            else:
                events.append(e)
            cur = e
        elif line.startswith("M2") and cur:
            parts = line.split()
            try:
                cur["speed"] = float(parts[2]) / fps
            except (IndexError, ValueError):
                pass
        elif line.startswith("* SOURCE FILE:") and cur:
            cur["meta"]["src"] = line.split(":", 1)[1].strip()
        elif line.startswith("* NEXUS XFADE:") and cur:
            cur["meta"]["xfade"] = line.split(":", 1)[1].strip()
        elif line.startswith("* NEXUS CLIP:") and cur:
            try:
                cur["meta"]["clip"] = json.loads(line.split(":", 1)[1])
            except ValueError:
                pass
    tl = {"version": 1, "width": 1920, "height": 1080, "fps": fps, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -14.0, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True, "markers": [],
          **{k: v for k, v in (base or {}).items() if k != "clips"}, "clips": []}
    clips = tl["clips"]
    inv = {v: k for k, v in (reels or {}).items()}
    for k, e in enumerate(events):
        nxt = events[k + 1] if k + 1 < len(events) else None
        tail = nxt["dur"] if nxt and nxt["kind"] != "C" else 0       # frames the next dissolve takes from this clip
        sp = float(e.get("speed", 1.0))
        own = e["r_out"] - e["r_in"]
        c = dict(e["meta"].get("clip") or {})
        src = e["meta"].get("src") or (reels or {}).get(e["reel"]) or inv.get(e["reel"])
        if e["reel"] == "BL" or (not src and c.get("type") == "color"):
            c.update(type="color", duration=round((own + tail) / fps, 6))
            c.setdefault("color", "0x000000")
        elif c.get("type") == "image" and src:
            c.update(type="image", src=src, duration=round((own + tail) / fps, 6))
        else:
            if not src:
                raise ValueError(f"EDL event {e['num']}: no source for reel {e['reel']} (pass reels={{reel: path}})")
            c.update(type="video", src=src, **{"in": round(e["s_in"] / fps, 6),
                                                 "out": round((e["s_in"] + round((own + tail) * sp)) / fps, 6)})
            if abs(sp - 1.0) > 1e-6:
                c["speed"] = round(sp, 6)
        c.setdefault("id", f"e{e['num']}")
        if e["kind"] != "C":
            c["transition"] = "crossfade"
            c["transition_dur"] = round(e["dur"] / fps, 6)
            c["xfade"] = e["meta"].get("xfade") or {v: k for k, v in WIPES.items()}.get(e["kind"], "fade")
        clips.append(c)
    return tl


# ------------------------------------------------------------------ OpenTimelineIO
def _rt(v: float, fps: int) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(fps), "value": float(v)}


def _tr(start: float, dur: float, fps: int) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, fps), "duration": _rt(dur, fps)}


def to_otio(tl: dict, name: str = "NEXUS") -> dict:
    fps = int(tl.get("fps", config.FPS))
    clips = tl["clips"]
    n, d, names = _layout(tl)
    children: list[dict] = []
    for i, c in enumerate(clips):
        if d[i]:
            children.append({"OTIO_SCHEMA": "Transition.1", "name": names[i] or "fade", "metadata": {"nexus": {"xfade": names[i]}},
                             "transition_type": "SMPTE_Dissolve", "in_offset": _rt(0, fps), "out_offset": _rt(d[i], fps)})
        own = n[i] - (d[i + 1] if i + 1 < len(clips) else 0)
        sp = float(c.get("speed", 1.0)) if c["type"] == "video" else 1.0
        start = round(float(c.get("in", 0)) * fps) if c["type"] == "video" else 0
        if c["type"] == "video":
            ref = {"OTIO_SCHEMA": "ExternalReference.1", "name": Path(c["src"]).name, "available_range": None,
                   "available_image_bounds": None, "metadata": {}, "target_url": Path(c["src"]).resolve().as_uri()}
        elif c["type"] == "image":
            ref = {"OTIO_SCHEMA": "ExternalReference.1", "name": Path(c["src"]).name, "available_range": None,
                   "available_image_bounds": None, "metadata": {}, "target_url": Path(c["src"]).resolve().as_uri()}
        else:
            ref = {"OTIO_SCHEMA": "GeneratorReference.1", "name": "color", "generator_kind": "SolidColor",
                   "parameters": {"color": c.get("color", "0x000000")}, "available_range": None,
                   "available_image_bounds": None, "metadata": {}}
        effects = ([{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "speed", "effect_name": "LinearTimeWarp",
                     "time_scalar": sp, "metadata": {}}] if abs(sp - 1.0) > 1e-6 else [])
        meta = {k: v for k, v in c.items() if k not in ("src", "in", "out", "speed", "transition", "transition_dur", "xfade")}
        children.append({"OTIO_SCHEMA": "Clip.2", "name": c.get("label") or c.get("id") or f"clip{i}",
                         "source_range": _tr(start, own, fps), "effects": effects, "markers": [], "enabled": True,
                         "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA",
                         "metadata": {"nexus": meta}})
    track = {"OTIO_SCHEMA": "Track.1", "name": "V1", "kind": "Video", "children": children, "source_range": None,
             "effects": [], "markers": [], "enabled": True, "metadata": {}}
    return {"OTIO_SCHEMA": "Timeline.1", "name": name, "global_start_time": None,
            "metadata": {"nexus": {k: v for k, v in tl.items() if k not in ("clips",)}},
            "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": [track], "source_range": None,
                       "effects": [], "markers": [], "enabled": True, "metadata": {}}}


def from_otio(doc: dict) -> dict:
    """Timeline from OTIO JSON (the first video track; transitions -> crossfades)."""
    meta = (doc.get("metadata") or {}).get("nexus") or {}
    track = next(t for t in doc["tracks"]["children"] if t.get("kind", "Video") == "Video")
    items = track["children"]
    fps = int(round(next((it["source_range"]["duration"]["rate"] for it in items if it.get("source_range")), meta.get("fps", 30))))
    tl = {"version": 1, "width": 1920, "height": 1080, "lang": "en", "grade": "none", "audio_fx": "none", "target_lufs": -14.0,
          "audio": [], "music": None, "subtitles": [], "burn_subtitles": True, "markers": [], **meta, "fps": fps, "clips": []}
    pending = None
    for k, it in enumerate(items):
        sch = it.get("OTIO_SCHEMA", "")
        if sch.startswith("Transition"):
            pending = it
            continue
        if not sch.startswith("Clip"):
            continue
        nxt = items[k + 1] if k + 1 < len(items) else None
        tail = round(nxt["out_offset"]["value"] + nxt["in_offset"]["value"]) if nxt and nxt.get("OTIO_SCHEMA", "").startswith("Transition") else 0
        sr = it["source_range"]
        start, own = round(sr["start_time"]["value"]), round(sr["duration"]["value"])
        sp = next((e.get("time_scalar", 1.0) for e in it.get("effects") or [] if "TimeWarp" in e.get("OTIO_SCHEMA", "")), 1.0)
        refs = it.get("media_references") or {}
        ref = refs.get(it.get("active_media_reference_key", "DEFAULT_MEDIA")) or it.get("media_reference") or {}
        c = dict((it.get("metadata") or {}).get("nexus") or {})
        if ref.get("OTIO_SCHEMA", "").startswith("GeneratorReference"):
            c.update(type="color", duration=round((own + tail) / fps, 6), color=(ref.get("parameters") or {}).get("color", "0x000000"))
        else:
            url = ref.get("target_url", "")
            src = url[7:] if url.startswith("file://") else url
            from urllib.parse import unquote
            src = unquote(src)
            if c.get("type") == "image":
                c.update(src=src, duration=round((own + tail) / fps, 6))
            else:
                c.update(type="video", src=src, **{"in": round(start / fps, 6), "out": round((start + round((own + tail) * sp)) / fps, 6)})
                if abs(sp - 1.0) > 1e-6:
                    c["speed"] = sp
        c.setdefault("id", it.get("name") or f"c{k}")
        if pending is not None:
            c["transition"] = "crossfade"
            c["transition_dur"] = round((pending["in_offset"]["value"] + pending["out_offset"]["value"]) / fps, 6)
            c["xfade"] = ((pending.get("metadata") or {}).get("nexus") or {}).get("xfade") or "fade"
            pending = None
        tl["clips"].append(c)
    return tl


def write(tl: dict, path: str | Path) -> Path:
    """Write .edl or .otio by extension."""
    p = Path(path)
    if p.suffix.lower() == ".edl":
        p.write_text(to_edl(tl, p.stem), encoding="utf-8")
    else:
        p.write_text(json.dumps(to_otio(tl, p.stem), indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def read(path: str | Path, **kw) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".edl":
        return from_edl(p.read_text(encoding="utf-8"), **kw)
    return from_otio(json.loads(p.read_text(encoding="utf-8")))
