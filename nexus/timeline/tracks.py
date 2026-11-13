"""Multi-track timeline: layers above the main storyline (tl["clips"]), compiled onto the existing renderer.

    tl["tracks"] = [
      {"id": "V2", "kind": "video", "z": 1, "opacity": 1.0, "keyframes": {"opacity": [[t, v], ...]},   # programme time
       "hidden": false, "effects": [...], "grade": ..., "correct": ..., "lut": ..., "chroma_key": ..., "mask": ...,
       "items": [{"id", "type": video|image|color, "src", "in", "out" | "duration", "start" (programme s),
                  "x", "y", "w", "h" (0..1 box in the frame, default full frame), "fit": cover|contain,
                  "opacity", "keyframes": {"opacity" | "pos_x" | "pos_y": [[t, v, "linear"|"ease"], ...] (item time),
                                           + every clip keyframe (x/y/scale/rotation/volume)},
                  "use_audio": false, "volume", ...every other clip key (effects, grade, mask, titles, ...)}]},
      {"id": "A2", "kind": "audio", "volume": 1.0, "mute": false, "role": "music",
       "items": [{"id", "src", "start", "in", "out", "volume", "role", "fade_in", "fade_out"}]},
      {"id": "T1", "kind": "text", "z": 3, "opacity": 1.0,
       "items": [{"id", "text", "subtitle", "style", "start", "end", "anim": fade|slide|rise|none, "from", "opacity",
                  "keyframes": {"opacity": [...]}}]},
    ]

The main storyline `tl["clips"]` is layer 0 (bottom) and defines the programme length; layers are drawn over it in
ascending `z` (ties: list order). A timeline without `tracks` renders exactly as before. Each video-layer item is
rendered by the normal segment renderer at its box size (so every clip effect works on a layer, cached per item);
the track's effect stack (`effects` appended, `grade`/`correct`/`lut`/`chroma_key`/`mask` as defaults) is merged
into each item. Opacity (track x item x keyframes) is an alpha plane built from a tiny `geq` source scaled to the
box, so ramps cost almost nothing per pixel. Audio tracks compile into tl["audio"] items.
"""
from __future__ import annotations

import copy
from pathlib import Path

KINDS = ("video", "audio", "text")
TRACK_STACK_DEFAULTS = ("grade", "correct", "lut", "chroma_key", "mask", "stabilize", "audio_fx")
PLACEMENT_KEYS = ("start", "x", "y", "w", "h", "opacity", "use_audio", "role")
LAYER_KF = ("opacity", "pos_x", "pos_y")


def _f(d: dict, k: str, default: float) -> float:
    try:
        v = float(d.get(k, default))
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def all_tracks(tl: dict) -> list[dict]:
    return [t for t in (tl.get("tracks") or []) if isinstance(t, dict) and t.get("kind") in KINDS]


def visual_tracks(tl: dict) -> list[dict]:
    tr = [t for t in all_tracks(tl) if t["kind"] in ("video", "text") and not t.get("hidden")]
    return [t for _, _, t in sorted((float(t.get("z", 1)), i, t) for i, t in enumerate(tr))]


def has_layers(tl: dict) -> bool:
    return any(t.get("items") for t in visual_tracks(tl))


def item_clip(item: dict, track: dict) -> dict:
    """The clip dict the segment renderer sees for one video-layer item (placement keys removed, track stack merged)."""
    c = {k: copy.deepcopy(v) for k, v in item.items() if k not in PLACEMENT_KEYS}
    c.setdefault("type", "video" if c.get("src") and "in" in c else "image" if c.get("src") else "color")
    kf = {k: v for k, v in (c.get("keyframes") or {}).items() if k not in LAYER_KF}
    if kf:
        c["keyframes"] = kf
    else:
        c.pop("keyframes", None)
    if track.get("effects"):
        c["effects"] = list(c.get("effects") or []) + copy.deepcopy(track["effects"])
    for k in TRACK_STACK_DEFAULTS:
        if k in track and k not in c:
            c[k] = copy.deepcopy(track[k])
    if not item.get("use_audio"):
        c["mute"] = True
    return c


def item_window(item: dict, track: dict | None = None) -> tuple[float, float]:
    from .render import clip_duration
    st = max(0.0, _f(item, "start", 0.0))
    if (track or {}).get("kind") == "text":
        return st, max(st + 0.05, _f(item, "end", st + 3.0))
    return st, st + clip_duration(item_clip(item, track or {}))


def box(item: dict, W: int, H: int) -> tuple[int, int, int, int]:
    """Pixel box (x, y, w, h), even sizes, clamped to the frame."""
    w = max(2, int(round(W * min(1.0, max(0.01, _f(item, "w", 1.0))) / 2)) * 2)
    h = max(2, int(round(H * min(1.0, max(0.01, _f(item, "h", 1.0))) / 2)) * 2)
    x = int(round(W * min(1.0, max(-1.0, _f(item, "x", 0.0)))))
    y = int(round(H * min(1.0, max(-1.0, _f(item, "y", 0.0)))))
    return x, y, w, h


def sub_timeline(tl: dict, w: int, h: int) -> dict:
    keep = ("fps", "grade", "lang", "audio_fx", "lut")
    return {"width": w, "height": h, **{k: tl[k] for k in keep if k in tl}, "clips": []}


def plan(tl: dict, W: int, H: int) -> list[dict]:
    """Visual layers in drawing order. Video items carry the clip + sub-timeline to render (at the full-size box;
    a draft render halves it like any segment) and the box on the output frame W x H used for compositing."""
    out = []
    for t in visual_tracks(tl):
        for k, it in enumerate(t.get("items") or []):
            if not isinstance(it, dict):
                continue
            st, en = item_window(it, t)
            if en - st < 1e-3:
                continue
            L = {"track": t, "item": it, "start": st, "end": en, "id": it.get("id") or f"{t.get('id', 'L')}_{k}"}
            if t["kind"] == "text":
                if not str(it.get("text", "")).strip():
                    continue
                L["kind"] = "text"
            else:
                bx, full = box(it, W, H), box(it, int(tl["width"]), int(tl["height"]))
                L.update(kind="video", box=bx, clip=item_clip(it, t), sub_tl=sub_timeline(tl, full[2], full[3]))
            out.append(L)
    return out


def with_audio(tl: dict) -> dict:
    """A copy of tl whose `audio` list also holds the items of every audio track (and of video items with use_audio)."""
    extra = []
    for t in all_tracks(tl):
        if t.get("mute") or t.get("hidden"):
            continue
        tv = _f(t, "volume", 1.0)
        if t["kind"] == "audio":
            for it in t.get("items") or []:
                if isinstance(it, dict) and it.get("src"):
                    a = {k: it[k] for k in ("src", "start", "in", "out", "fade_in", "fade_out") if k in it}
                    a["volume"] = round(_f(it, "volume", 1.0) * tv, 4)
                    a["role"] = it.get("role", t.get("role", "music"))
                    a["track"] = t.get("id")
                    extra.append(a)
        elif t["kind"] == "video":
            for it in t.get("items") or []:
                if isinstance(it, dict) and it.get("use_audio") and it.get("src") and "in" in it:
                    extra.append({"src": it["src"], "start": _f(it, "start", 0.0), "in": it["in"], "out": it.get("out"),
                                  "volume": round(_f(it, "volume", 1.0) * tv, 4), "role": it.get("role", "dialogue"),
                                  "track": t.get("id")})
    if not extra:
        return tl
    tl = copy.deepcopy(tl)
    tl["audio"] = list(tl.get("audio") or []) + extra
    return tl


def validate(tl: dict) -> list[str]:
    """Problems a user should see (the renderer still draws what it can)."""
    out, seen = [], set()
    for i, t in enumerate(tl.get("tracks") or []):
        if not isinstance(t, dict) or t.get("kind") not in KINDS:
            out.append(f"track {i}: kind must be one of {KINDS}")
            continue
        tid = t.get("id") or f"#{i}"
        if tid in seen:
            out.append(f"track {tid}: duplicate id")
        seen.add(tid)
        spans = []
        for it in t.get("items") or []:
            if t["kind"] == "text":
                if not str(it.get("text", "")).strip():
                    out.append(f"track {tid}: text item without text")
            elif t["kind"] in ("video", "audio") and it.get("type", "video") != "color" and not it.get("src"):
                out.append(f"track {tid}: item {it.get('id')} has no src")
                continue
            elif it.get("src") and not Path(str(it["src"])).exists():
                out.append(f"track {tid}: item {it.get('id')} source missing: {it['src']}")
            if t["kind"] != "audio":
                try:
                    spans.append((*item_window(it, t), it.get("id")))
                except (KeyError, TypeError, ValueError) as e:
                    out.append(f"track {tid}: item {it.get('id')}: {e}")
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            if b[0] < a[1] - 1e-3:
                out.append(f"track {tid}: items {a[2]} and {b[2]} overlap ({b[0]:.2f}s < {a[1]:.2f}s)")
    return out


# ------------------------------------------------------------------ compositor
def opacity_expr(L: dict, var: str) -> str | None:
    """Combined opacity of a layer as an FFmpeg expression in programme time `var`, or None when always 1."""
    from .render import kf_expr
    t, it = L["track"], L["item"]
    parts = []
    const = _f(t, "opacity", 1.0) * _f(it, "opacity", 1.0)
    if abs(const - 1.0) > 1e-6:
        parts.append(f"{const:.5f}")
    te = kf_expr((t.get("keyframes") or {}).get("opacity"), var)
    if te:
        parts.append(f"({te})")
    ie = kf_expr((it.get("keyframes") or {}).get("opacity"), f"({var}-{L['start']:.4f})")
    if ie:
        parts.append(f"({ie})")
    if L["kind"] == "text" and it.get("anim", "fade") != "none":
        d = min(0.45, max(0.08, (L["end"] - L["start"]) / 4))
        parts.append(f"clip(min(({var}-{L['start']:.4f})/{d:.4f},({L['end']:.4f}-{var})/{d:.4f}),0,1)")
    return "*".join(parts) if parts else None


def alpha_source(expr: str, w: int, h: int, fps: int, dur: float, label: str) -> str:
    """A grey stream whose value is 255*clip(expr(T),0,1), built at 16x16 and scaled (cheap at any frame size)."""
    return (f"color=c=white:s=16x16:r={fps}:d={dur:.4f},format=gray,geq=lum='255*clip({expr},0,1)',"
            f"scale={w}:{h}:flags=neighbor{label}")


def _pos(L: dict, W: int, H: int, t0: float) -> tuple[str, str]:
    from .render import kf_expr
    kf = L["item"].get("keyframes") or {}
    x, y = L["box"][:2] if L["kind"] == "video" else (0, 0)
    local = f"(t+{t0:.4f}-{L['start']:.4f})"
    xe, ye = kf_expr(kf.get("pos_x"), local), kf_expr(kf.get("pos_y"), local)
    return (f"'{W}*({xe})'" if xe else str(x)), (f"'{H}*({ye})'" if ye else str(y))


def layer_graph(layers: list[dict], W: int, H: int, fps: int, t0: float, t1: float, lang: str, n_in: int,
                src: str, tag: str = "ly") -> tuple[list, list[str], str]:
    """Composite every layer visible in programme window [t0, t1) over `src` (whose frame time 0 = programme t0).
    Video layers need L["seg"] (the rendered item). Returns (input args, filter lines, output label)."""
    args: list = []
    lines: list[str] = []
    cur = src
    k = 0
    for L in layers:
        a, b = max(t0, L["start"]), min(t1, L["end"])
        if b - a < 0.5 / fps:
            continue
        span = b - a
        shift = a - t0
        expr = opacity_expr(L, f"(T+{a:.4f})")
        px, py = _pos(L, W, H, t0)
        if L["kind"] == "video":
            x, y, w, h = L["box"]
            args += ["-ss", f"{a - L['start']:.4f}", "-t", f"{span + 1.0 / fps:.4f}", "-i", str(L["seg"])]
            chain = f"[{n_in}:v]setpts=PTS-STARTPTS,fps={fps},trim=duration={span:.4f},scale={w}:{h},setsar=1"
            if expr:
                lines.append(alpha_source(expr, w, h, fps, span, f"[{tag}a{k}]"))
                lines.append(f"{chain},format=yuva420p[{tag}c{k}]")
                lines.append(f"[{tag}c{k}][{tag}a{k}]alphamerge,setpts=PTS+{shift:.4f}/TB[{tag}l{k}]")
            else:
                lines.append(f"{chain},setpts=PTS+{shift:.4f}/TB[{tag}l{k}]")
        else:
            from ..fx import titles
            it = L["item"]
            png = titles.title_png(it, W, H, it.get("lang", lang))
            args += ["-loop", "1", "-framerate", fps, "-t", f"{span:.4f}", "-i", str(png)]
            if expr:
                lines.append(f"[{n_in}:v]format=rgba,split[{tag}t{k}][{tag}u{k}]")
                lines.append(alpha_source(expr, W, H, fps, span, f"[{tag}a{k}]"))
                lines.append(f"[{tag}u{k}]alphaextract[{tag}x{k}]")
                lines.append(f"[{tag}x{k}][{tag}a{k}]blend=all_mode=multiply[{tag}m{k}]")
                lines.append(f"[{tag}t{k}][{tag}m{k}]alphamerge,setpts=PTS-STARTPTS+{shift:.4f}/TB[{tag}l{k}]")
            else:
                lines.append(f"[{n_in}:v]format=rgba,setpts=PTS-STARTPTS+{shift:.4f}/TB[{tag}l{k}]")
            anim = it.get("anim") if it.get("anim") in ("fade", "slide", "rise", "none") else "fade"
            if anim in ("slide", "rise") and px == "0" and py == "0":
                d = min(0.45, max(0.08, (L["end"] - L["start"]) / 4))
                P = f"(t+{t0:.4f})"
                p = (f"(1-pow(1-clip(min(({P}-{L['start']:.4f})/{d:.4f},({L['end']:.4f}-{P})/{d:.4f}),0,1),3))")
                if anim == "slide":
                    px = f"'{1 if it.get('from') == 'right' else -1}*{int(W * 0.5)}*(1-{p})'"
                else:
                    py = f"'{int(H * 0.06)}*(1-{p})'"
        lines.append(f"{cur}[{tag}l{k}]overlay=x={px}:y={py}:eval=frame:eof_action=pass:"
                     f"enable='between(t,{shift:.4f},{shift + span:.4f})'[{tag}o{k}]")
        cur = f"[{tag}o{k}]"
        n_in += 1
        k += 1
    return args, lines, cur


def signature(layers: list[dict], t0: float, t1: float) -> list:
    """What the layers visible in [t0, t1) contribute to the picture (for piece cache keys)."""
    out = []
    for L in layers:
        if min(t1, L["end"]) - max(t0, L["start"]) <= 0:
            continue
        out.append({"k": L["kind"], "it": {k: v for k, v in L["item"].items() if k not in ("id", "label", "notes")},
                    "tr": {k: v for k, v in L["track"].items() if k not in ("items", "id", "label")},
                    "seg": Path(str(L.get("seg", ""))).name, "box": L.get("box")})
    return out
