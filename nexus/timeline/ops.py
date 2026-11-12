"""Non-destructive timeline edit operations. Each returns a NEW timeline dict."""
from __future__ import annotations

import copy
import uuid

from .render import clip_duration, clip_starts


def _new(tl: dict) -> dict:
    return copy.deepcopy(tl)


def cid() -> str:
    return "c" + uuid.uuid4().hex[:8]


def video_clip(src: str, t_in: float, t_out: float, **kw) -> dict:
    return {"id": cid(), "type": "video", "src": src, "in": round(t_in, 3), "out": round(t_out, 3), **kw}


def image_clip(src: str, duration: float, **kw) -> dict:
    return {"id": cid(), "type": "image", "src": src, "duration": round(duration, 3), **kw}


def color_clip(duration: float, color: str = "0x0b1020", **kw) -> dict:
    return {"id": cid(), "type": "color", "duration": round(duration, 3), "color": color, **kw}


def find(tl: dict, clip_id: str) -> int:
    for i, c in enumerate(tl["clips"]):
        if c["id"] == clip_id:
            return i
    raise KeyError(clip_id)


def _shift_after(tl: dict, t: float, delta: float) -> None:
    """Ripple: move narration, subtitles and markers that start after programme time t."""
    for a in tl.get("audio", []):
        if a.get("start", 0) >= t:
            a["start"] = max(0.0, a["start"] + delta)
    for s in tl.get("subtitles", []):
        if s["start"] >= t:
            s["start"] += delta
            s["end"] += delta
    for m in tl.get("markers", []):
        if m["t"] >= t:
            m["t"] += delta


def ripple_delete(tl: dict, clip_id: str) -> dict:
    tl = _new(tl)
    i = find(tl, clip_id)
    start, dur = clip_starts(tl)[i], clip_duration(tl["clips"][i])
    tl["subtitles"] = [s for s in tl.get("subtitles", []) if not (start <= s["start"] < start + dur)]
    tl["audio"] = [a for a in tl.get("audio", []) if not (start <= a.get("start", 0) < start + dur and a.get("clip") == clip_id)]
    del tl["clips"][i]
    _shift_after(tl, start + dur, -dur)
    return tl


def trim(tl: dict, clip_id: str, t_in: float | None = None, t_out: float | None = None, ripple: bool = True) -> dict:
    tl = _new(tl)
    i = find(tl, clip_id)
    c = tl["clips"][i]
    before, start = clip_duration(c), clip_starts(tl)[i]
    if c["type"] == "video":
        if t_in is not None:
            c["in"] = round(t_in, 3)
        if t_out is not None:
            c["out"] = round(t_out, 3)
        if c["out"] <= c["in"]:
            raise ValueError("trim would produce an empty clip")
    elif t_out is not None:
        c["duration"] = round(t_out, 3)
    if ripple:
        _shift_after(tl, start + before, clip_duration(c) - before)
    return tl


def split(tl: dict, clip_id: str, at: float) -> dict:
    """Split a video clip at source time `at`."""
    tl = _new(tl)
    i = find(tl, clip_id)
    c = tl["clips"][i]
    if c["type"] != "video" or not (c["in"] < at < c["out"]):
        raise ValueError("split point outside clip")
    b = copy.deepcopy(c)
    b["id"], b["in"], c["out"] = cid(), round(at, 3), round(at, 3)
    b.pop("text", None)
    b.pop("fade_in", None)
    c.pop("fade_out", None)
    tl["clips"].insert(i + 1, b)
    return tl


def move(tl: dict, clip_id: str, new_index: int) -> dict:
    tl = _new(tl)
    c = tl["clips"].pop(find(tl, clip_id))
    tl["clips"].insert(new_index, c)
    return tl


def insert(tl: dict, clip: dict, index: int | None = None) -> dict:
    tl = _new(tl)
    index = len(tl["clips"]) if index is None else index
    starts = clip_starts(tl) + [sum(clip_duration(c) for c in tl["clips"])]
    _shift_after(tl, starts[index], clip_duration(clip))
    tl["clips"].insert(index, clip)
    return tl


def replace(tl: dict, clip_id: str, clip: dict) -> dict:
    """Swap one shot for another (alternative clip selection) keeping its slot."""
    tl = _new(tl)
    i = find(tl, clip_id)
    start, before = clip_starts(tl)[i], clip_duration(tl["clips"][i])
    clip = {**clip, "id": clip_id}
    tl["clips"][i] = clip
    _shift_after(tl, start + before, clip_duration(clip) - before)
    return tl


def set_props(tl: dict, clip_id: str, **props) -> dict:
    """Per-scene overrides: speed, grade, volume, fit, focus, fades, transition, text, audio_fx..."""
    tl = _new(tl)
    tl["clips"][find(tl, clip_id)].update(props)
    return tl


def set_global(tl: dict, **props) -> dict:
    tl = _new(tl)
    tl.update(props)
    return tl


def reframe(tl: dict, width: int, height: int, fit: str = "cover") -> dict:
    """Change aspect ratio. Clips keep their own `focus` if smart reframing set one."""
    tl = _new(tl)
    tl["width"], tl["height"] = width, height
    for c in tl["clips"]:
        if c["type"] == "video":
            c["fit"] = c.get("fit_override") or fit
    return tl


def add_marker(tl: dict, t: float, label: str) -> dict:
    tl = _new(tl)
    tl.setdefault("markers", []).append({"t": t, "label": label})
    return tl


# ------------------------------------------------------------------ multi-track (docs/TIMELINE.md "Tracks")
def track(tl: dict, track_id: str) -> dict:
    for t in tl.get("tracks") or []:
        if t.get("id") == track_id:
            return t
    raise KeyError(track_id)


def add_track(tl: dict, kind: str = "video", track_id: str | None = None, z: float | None = None, **props) -> dict:
    """New empty layer. Video/text layers default to the top of the stack; ids V2.., A2.., T1.. when not given."""
    from .tracks import KINDS
    if kind not in KINDS:
        raise ValueError(f"track kind must be one of {KINDS}")
    tl = _new(tl)
    trs = tl.setdefault("tracks", [])
    if track_id is None:
        pre = {"video": "V", "audio": "A", "text": "T"}[kind]
        n = 2 if kind != "text" else 1
        ids = {t.get("id") for t in trs}
        while f"{pre}{n}" in ids:
            n += 1
        track_id = f"{pre}{n}"
    if any(t.get("id") == track_id for t in trs):
        raise ValueError(f"track {track_id} exists")
    if z is None:
        z = max([float(t.get("z", 1)) for t in trs if t.get("kind") != "audio"] + [0.0]) + 1
    trs.append({"id": track_id, "kind": kind, "z": z, "items": [], **props})
    return tl


def remove_track(tl: dict, track_id: str) -> dict:
    tl = _new(tl)
    track(tl, track_id)
    tl["tracks"] = [t for t in tl["tracks"] if t.get("id") != track_id]
    return tl


def set_track(tl: dict, track_id: str, **props) -> dict:
    """z, opacity, keyframes, hidden, mute, volume, effects, grade, correct, lut, chroma_key, mask..."""
    tl = _new(tl)
    track(tl, track_id).update(props)
    return tl


def find_item(tl: dict, item_id: str) -> tuple[dict, int]:
    for t in tl.get("tracks") or []:
        for i, it in enumerate(t.get("items") or []):
            if it.get("id") == item_id:
                return t, i
    raise KeyError(item_id)


def add_item(tl: dict, track_id: str, item: dict, start: float | None = None) -> dict:
    tl = _new(tl)
    t = track(tl, track_id)
    it = {"id": item.get("id") or cid(), **copy.deepcopy(item)}
    if start is not None:
        it["start"] = round(float(start), 3)
    t.setdefault("items", []).append(it)
    t["items"].sort(key=lambda x: float(x.get("start", 0)))
    return tl


def remove_item(tl: dict, item_id: str) -> dict:
    tl = _new(tl)
    t, i = find_item(tl, item_id)
    del t["items"][i]
    return tl


def move_item(tl: dict, item_id: str, start: float | None = None, track_id: str | None = None) -> dict:
    """Move a layer item in time and/or onto another track of a compatible kind."""
    tl = _new(tl)
    t, i = find_item(tl, item_id)
    dst = track(tl, track_id) if track_id else t
    if dst.get("kind") != t.get("kind"):
        raise ValueError(f"cannot move a {t.get('kind')} item onto a {dst.get('kind')} track")
    it = t["items"].pop(i)
    if start is not None:
        it["start"] = round(max(0.0, float(start)), 3)
    dst.setdefault("items", []).append(it)
    dst["items"].sort(key=lambda x: float(x.get("start", 0)))
    return tl


def set_item(tl: dict, item_id: str, **props) -> dict:
    tl = _new(tl)
    t, i = find_item(tl, item_id)
    t["items"][i].update(props)
    return tl


def set_keyframes(tl: dict, target_id: str, prop: str, points: list) -> dict:
    """Keyframes on a main clip, a layer item or a track (prop in fx.effects.KEYFRAME_PROPS). Empty list removes them."""
    from ..fx.effects import KEYFRAME_PROPS
    if prop not in KEYFRAME_PROPS:
        raise ValueError(f"{prop!r} is not keyframable; use one of {KEYFRAME_PROPS}")
    tl = _new(tl)
    try:
        obj = tl["clips"][find(tl, target_id)]
    except KeyError:
        try:
            t, i = find_item(tl, target_id)
            obj = t["items"][i]
        except KeyError:
            obj = track(tl, target_id)
    kf = obj.setdefault("keyframes", {})
    if points:
        kf[prop] = [list(p) for p in points]
    else:
        kf.pop(prop, None)
    return tl
