"""Per-clip visual effects, speed ramps, picture-in-picture and programme-wide overlays as FFmpeg filter fragments.

All effect times are clip-local seconds of the rendered clip (0 = first frame of the clip). The one exception is
`speed_ramp`, whose start/end are clip-local seconds of the clip *before* the ramp is applied (source seconds after
`in`, divided by the clip's base `speed`), because the ramp itself changes where later moments land.

Unknown effect types and malformed parameters are ignored: an effect never makes a render fail.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .. import config

KNOWN = ("punch_in", "zoom", "shake", "vignette", "grain", "letterbox", "blur", "flash", "glitch", "speed_ramp")
# Every property that accepts keyframes [[t, value, "linear"|"ease"], ...] (clip["keyframes"][prop], layer items, and
# for "opacity" also tracks). The editor UI builds its keyframe editor from this (GET /api/fx/catalog).
#   x, y        0..1 crop position (cover fit)          scale     >= 1 zoom           volume  gain (1 = unchanged)
#   opacity     0..1 (clips: over black; layers: over what is below)
#   pos_x/pos_y 0..1 top-left of a layer item's box in the frame
#   rotation    degrees clockwise (clip["rotation_fill"]: "zoom" default, or "black")
KEYFRAME_PROPS = ("x", "y", "scale", "volume", "opacity", "rotation", "pos_x", "pos_y")
# Masks take their own keyframes in clip["mask"]["keyframes"]: MASK_KEYFRAME_PROPS (box position and size, 0..1).
MASK_KEYFRAME_PROPS = ("x", "y", "w", "h")


def _f(d: dict, k: str, default: float) -> float:
    try:
        v = float(d.get(k, default))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _effects(c: dict) -> list[dict]:
    return [e for e in (c.get("effects") or []) if isinstance(e, dict) and e.get("type") in KNOWN]


def _window(e: dict, dur: float, default_len: float | None = None) -> tuple[float, float] | None:
    s = max(0.0, _f(e, "start", 0.0))
    en = _f(e, "end", s + default_len if default_len else dur)
    en = min(en, dur)
    return (s, en) if en - s > 1e-3 else None


def _smooth(x: str) -> str:
    """smoothstep(clip(x,0,1)) as an FFmpeg expression."""
    return f"(clip({x},0,1)*clip({x},0,1)*(3-2*clip({x},0,1)))"


def _envelope(t: str, s: float, e: float, ramp: float) -> str:
    """0 -> 1 (eased over `ramp`) -> hold -> 1 -> 0 (eased) inside [s, e]."""
    r = max(1e-3, min(ramp, (e - s) / 2))
    return f"({_smooth(f'({t}-{s:.4f})/{r:.4f}')}*(1-{_smooth(f'({t}-{e - r:.4f})/{r:.4f}')}))"


# ------------------------------------------------------------------ speed ramp
def ramp_spec(c: dict) -> tuple[float, float, float, float] | None:
    """(from, to, start, end) of the clip's speed ramp in un-ramped clip seconds, or None."""
    if c.get("type") != "video":
        return None
    for e in _effects(c):
        if e["type"] != "speed_ramp":
            continue
        base = float(c.get("speed", 1.0))
        U = (c["out"] - c["in"]) / base
        f = min(8.0, max(0.1, _f(e, "from", 1.0)))
        g = min(8.0, max(0.1, _f(e, "to", 1.0)))
        s = min(max(0.0, _f(e, "start", 0.0)), U)
        en = min(max(s, _f(e, "end", U)), U)
        if en - s < 1e-3 and abs(f - g) < 1e-6 and abs(f - 1) < 1e-6:
            return None
        return f, g, s, en
    return None


def _ramp_T(u: float, f: float, g: float, s: float, e: float) -> float:
    """Output time for un-ramped clip time u (integral of 1/speed)."""
    if u <= s:
        return u / f
    if e - s < 1e-9:
        return s / f + (u - s) / g
    k = (g - f) / (e - s)
    if u <= e:
        return s / f + ((u - s) / f if abs(k) < 1e-9 else math.log((f + k * (u - s)) / f) / k)
    Te = s / f + ((e - s) / f if abs(k) < 1e-9 else math.log(g / f) / k)
    return Te + (u - e) / g


def ramp_duration(c: dict) -> float | None:
    sp = ramp_spec(c)
    if sp is None:
        return None
    U = (c["out"] - c["in"]) / float(c.get("speed", 1.0))
    return _ramp_T(U, *sp)


def ramp_setpts(c: dict) -> str | None:
    """setpts expression mapping source timestamps to ramped clip-local output timestamps."""
    sp = ramp_spec(c)
    if sp is None:
        return None
    f, g, s, e = sp
    base = float(c.get("speed", 1.0))
    u = f"((PTS-STARTPTS)*TB/{base})"
    Te = _ramp_T(e, f, g, s, e)
    if e - s < 1e-9 or abs(g - f) < 1e-9:
        mid = f"({s / f:.6f}+({u}-{s:.6f})/{f})"
    else:
        k = (g - f) / (e - s)
        mid = f"({s / f:.6f}+log(({f}+{k:.6f}*({u}-{s:.6f}))/{f})/{k:.6f})"
    expr = f"if(lt({u},{s:.6f}),{u}/{f},if(lt({u},{e:.6f}),{mid},{Te:.6f}+({u}-{e:.6f})/{g}))/TB"
    return f"setpts='{expr}'"


def atempo_chain(sp: float) -> list[str]:
    out = []
    while sp > 2.0:
        out.append("atempo=2.0")
        sp /= 2
    while sp < 0.5:
        out.append("atempo=0.5")
        sp *= 2
    if abs(sp - 1.0) > 1e-3:
        out.append(f"atempo={sp:.5f}")
    return out


def ramp_audio(c: dict, src: str, dst: str, steps: int = 12) -> str | None:
    """Audio sub-graph for a speed-ramped clip: piecewise-constant time-stretch (pitch kept by atempo) whose steps
    end exactly where the video mapping puts them, so sound stays in sync with picture."""
    sp = ramp_spec(c)
    if sp is None:
        return None
    f, g, s, e = sp
    base = float(c.get("speed", 1.0))
    U = (c["out"] - c["in"]) / base
    bounds = [0.0, s] + [s + (e - s) * k / steps for k in range(1, steps)] + [e, U]
    bounds = sorted({round(b, 6) for b in bounds if 0 <= b <= U})
    pieces = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 1e-4]
    lines = [f"{src}asplit={len(pieces)}" + "".join(f"[rq{i}]" for i in range(len(pieces)))]
    for i, (a, b) in enumerate(pieces):
        out_len = _ramp_T(b, f, g, s, e) - _ramp_T(a, f, g, s, e)
        tempo = (b - a) * base / max(out_len, 1e-6)
        chain = [f"atrim=start={a * base:.6f}:end={b * base:.6f}", "asetpts=PTS-STARTPTS", *atempo_chain(tempo)]
        lines.append(f"[rq{i}]{','.join(chain)}[rp{i}]")
    lines.append("".join(f"[rp{i}]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=0:a=1{dst}")
    return ";".join(lines)


# ------------------------------------------------------------------ video effects
def _geometry(c: dict, W: int, H: int, fps: int, dur: float, scale: float) -> str | None:
    """zoom + punch_in + shake folded into one sub-pixel `perspective` warp (no integer-crop jitter)."""
    fx, fy = (float(v) for v in tuple(c.get("focus", (0.5, 0.5)))[:2])
    t = f"(in/{fps})"
    zs, shake = [], []
    for e in _effects(c):
        ty = e["type"]
        if ty == "zoom":
            z0, z1 = max(0.5, _f(e, "from", 1.0)), max(0.5, _f(e, "to", 1.15))
            w = _window(e, dur) or (0.0, dur)
            zs.append(f"({z0}+({z1 - z0:.5f})*clip(({t}-{w[0]:.4f})/{max(w[1] - w[0], 1e-3):.4f},0,1))")
        elif ty == "punch_in":
            w = _window(e, dur, 1.0)
            if w:
                sc = max(1.0, _f(e, "scale", 1.15))
                zs.append(f"(1+{sc - 1:.5f}*{_envelope(t, w[0], w[1], _f(e, 'ramp', 0.18))})")
        elif ty == "shake":
            w = _window(e, dur)
            if w:
                amt = max(0.0, _f(e, "amount", 6.0)) * scale
                env = _envelope(t, w[0], w[1], 0.12)
                zs.append(f"(1+{2.4 * amt / min(W, H):.5f}*{env})")
                shake.append((amt, env, len(shake)))
    if not zs:
        return None
    z = "*".join(zs)
    dx = "+".join(f"{a:.3f}*{env}*(0.6*sin({t}*{43.9 + 7 * k:.2f})+0.4*sin({t}*{81.7 + 5 * k:.2f}+1.3))" for a, env, k in shake) or "0"
    dy = "+".join(f"{a:.3f}*{env}*(0.6*sin({t}*{51.3 + 3 * k:.2f}+0.7)+0.4*sin({t}*{97.1 + 9 * k:.2f}))" for a, env, k in shake) or "0"
    w_, h_ = f"({W}/({z}))", f"({H}/({z}))"
    x0 = f"clip({fx}*({W}-{w_})+{dx},0,{W}-{w_})"
    y0 = f"clip({fy}*({H}-{h_})+{dy},0,{H}-{h_})"
    x1, y1 = f"({x0}+{w_})", f"({y0}+{h_})"
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y1}':x3='{x1}':y3='{y1}'"
            f":interpolation=linear:sense=source:eval=frame")


def video_filters(c: dict, W: int, H: int, fps: int, dur: float, scale: float = 1.0) -> tuple[list[str], list[str]]:
    """(geometry filters, look filters) for the clip. Geometry goes right after fit; look filters after the grade.
    `scale` is the draft-resolution factor applied to pixel-sized parameters."""
    geo = [g for g in [_geometry(c, W, H, fps, dur, scale)] if g]
    look: list[str] = []
    letterbox = None
    for e in _effects(c):
        ty = e["type"]
        if ty == "vignette":
            look.append(f"vignette=angle={min(1.5, max(0.1, _f(e, 'angle', math.pi / 5))):.4f}")
        elif ty == "grain":
            look.append(f"noise=c0s={int(min(60, max(1, _f(e, 'amount', 8))))}:c0f=t+u")
        elif ty == "blur":
            w = _window(e, dur)
            if w:
                look.append(f"gblur=sigma={max(0.5, _f(e, 'amount', 14)) * scale:.2f}:enable='between(t,{w[0]:.3f},{w[1]:.3f})'")
        elif ty == "flash":
            at = max(0.0, _f(e, "at", _f(e, "start", 0.0)))
            d = max(0.05, _f(e, "duration", 0.25))
            if at < dur:
                look.append(f"fade=t=in:st={at:.3f}:d={d:.3f}:color=white:enable='between(t,{at:.3f},{at + d:.3f})'")
        elif ty == "glitch":
            w = _window(e, dur, 0.6)
            if w:
                h = int(hashlib.sha1(repr(sorted(e.items())).encode()).hexdigest()[:8], 16)
                step = max(1.0 / fps, 0.07)
                n = min(14, max(1, int((w[1] - w[0]) / step)))
                amt = max(2, int(_f(e, "amount", 10) * scale))
                for k in range(n):
                    a, b = w[0] + k * (w[1] - w[0]) / n, w[0] + (k + 1) * (w[1] - w[0]) / n
                    r = (h >> (k % 24)) & 0xFF
                    sh = int(amt * (0.4 + 0.6 * (r / 255.0))) * (1 if k % 2 else -1)
                    look.append(f"chromashift=crh={sh}:cbh={-sh}:crv={(r % 5) - 2}:enable='between(t,{a:.3f},{b:.3f})'")
                look.append(f"noise=alls={int(min(60, max(5, _f(e, 'noise', 28))))}:allf=t:enable='between(t,{w[0]:.3f},{w[1]:.3f})'")
        elif ty == "letterbox":
            letterbox = max(0.2, _f(e, "ratio", 2.39))
    if letterbox:
        bar = int(round(max(0.0, (H - W / letterbox) / 2)))
        if bar >= 2:
            look.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    return geo, look


# ------------------------------------------------------------------ picture in picture
def _src_aspect(src: str) -> float:
    try:
        from PIL import Image
        with Image.open(src) as im:
            return im.size[1] / im.size[0]
    except Exception:
        from ..media import ff
        v = ff.probe(src)["video"] or {"width": 16, "height": 9}
        return v["height"] / v["width"]


def _pip_masks(pw: int, ph: int, shape: str) -> tuple[Path, Path]:
    """Anti-aliased alpha mask + border/shadow frame for a PiP window (cached PNGs)."""
    from PIL import Image, ImageDraw, ImageFilter
    key = hashlib.sha1(f"pip|{pw}x{ph}|{shape}|2".encode()).hexdigest()[:14]
    d = config.CACHE / "overlays"
    mask, frame = d / f"pipmask_{key}.png", d / f"pipframe_{key}.png"
    if mask.exists() and frame.exists():
        return mask, frame
    d.mkdir(parents=True, exist_ok=True)
    ss = 4
    bw = max(2, int(min(pw, ph) * 0.025))
    m = Image.new("L", (pw * ss, ph * ss), 0)
    dr = ImageDraw.Draw(m)
    box = [0, 0, pw * ss - 1, ph * ss - 1]
    if shape == "circle":
        dr.ellipse(box, fill=255)
    else:
        dr.rounded_rectangle(box, radius=int(min(pw, ph) * 0.06 * ss), fill=255)
    m.resize((pw, ph), Image.LANCZOS).save(mask)
    pad = bw * 4
    fr = Image.new("RGBA", ((pw + 2 * pad) * ss, (ph + 2 * pad) * ss), (0, 0, 0, 0))
    sh = Image.new("L", fr.size, 0)
    ds, df = ImageDraw.Draw(sh), ImageDraw.Draw(fr)
    ib = [pad * ss, pad * ss, (pad + pw) * ss - 1, (pad + ph) * ss - 1]
    ob = [ib[0] - bw * ss, ib[1] - bw * ss, ib[2] + bw * ss, ib[3] + bw * ss]
    rad = int(min(pw, ph) * 0.06 * ss)
    if shape == "circle":
        ds.ellipse([ob[0] + bw * ss, ob[1] + 2 * bw * ss, ob[2] + bw * ss, ob[3] + 2 * bw * ss], fill=150)
        df.ellipse(ob, fill=(255, 255, 255, 255))
        df.ellipse(ib, fill=(0, 0, 0, 0))
    else:
        ds.rounded_rectangle([ob[0] + bw * ss, ob[1] + 2 * bw * ss, ob[2] + bw * ss, ob[3] + 2 * bw * ss], radius=rad + bw * ss, fill=150)
        df.rounded_rectangle(ob, radius=rad + bw * ss, fill=(255, 255, 255, 255))
        df.rounded_rectangle(ib, radius=rad, fill=(0, 0, 0, 0))
    sh = sh.filter(ImageFilter.GaussianBlur(bw * ss * 1.5))
    hole = Image.new("L", fr.size, 255)
    hd = ImageDraw.Draw(hole)
    (hd.ellipse if shape == "circle" else hd.rectangle)(ib, fill=0)
    shadow = Image.new("RGBA", fr.size, (0, 0, 0, 0))
    shadow.putalpha(Image.composite(sh, Image.new("L", fr.size, 0), hole))
    out = Image.alpha_composite(shadow, fr)
    out.resize((pw + 2 * pad, ph + 2 * pad), Image.LANCZOS).save(frame)
    return mask, frame


def pip_graph(pip: dict, W: int, H: int, fps: int, dur: float, n_in: int, src_label: str, dst_label: str) -> tuple[list, list[str]]:
    """Inputs + filter lines that overlay a picture-in-picture window. Returns ([], []) when the PiP is unusable."""
    src = pip.get("src") if isinstance(pip, dict) else None
    if not src or not Path(src).exists():
        return [], []
    if pip.get("full"):  # full-frame cutaway (B-roll): cover-crop to the frame, short fades, no border, clip audio continues
        st, en = max(0.0, _f(pip, "start", 0.0)), min(dur, _f(pip, "end", dur))
        if en - st < 0.2:
            return [], []
        fd = min(0.25, (en - st) / 4)
        is_img = Path(src).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        args = (["-loop", "1", "-t", f"{dur:.3f}", "-i", src] if is_img else
                ["-ss", f"{max(0.0, _f(pip, 'in', 0.0)):.3f}", "-t", f"{en - st + 0.5:.3f}", "-i", src])
        fx, fy = (pip.get("focus") or [0.5, 0.5])[:2]
        lines = [f"[{n_in}:v]setpts=PTS-STARTPTS+{st:.3f}/TB,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H}:(iw-{W})*{float(fx):.3f}:(ih-{H})*{float(fy):.3f},setsar=1,format=yuva420p,"
                 f"fade=t=in:st={st:.3f}:d={fd:.3f}:alpha=1,fade=t=out:st={en - fd:.3f}:d={fd:.3f}:alpha=1[pq]",
                 f"{src_label}[pq]overlay=0:0:eof_action=pass:enable='between(t,{st:.3f},{en:.3f})'{dst_label}"]
        return args, lines
    shape = "circle" if pip.get("shape") == "circle" else "rect"
    pw = max(16, int(round(W * min(0.9, max(0.05, _f(pip, "w", 0.28))) / 2)) * 2)
    if shape == "circle":
        ph = pw
    elif pip.get("h"):
        ph = max(16, int(round(H * min(0.9, max(0.05, _f(pip, "h", 0.28))) / 2)) * 2)
    else:
        ph = max(16, int(round(pw * _src_aspect(src) / 2)) * 2)
    mask, frame = _pip_masks(pw, ph, shape)
    pad = max(2, int(min(pw, ph) * 0.025)) * 4
    x = int(min(max(0.0, _f(pip, "x", 0.68)), 1.0) * W)
    y = int(min(max(0.0, _f(pip, "y", 0.06)), 1.0) * H)
    x, y = min(x, W - pw), min(y, H - ph)
    st, en = max(0.0, _f(pip, "start", 0.0)), min(dur, _f(pip, "end", dur))
    is_img = Path(src).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    args: list = (["-loop", "1", "-t", f"{dur:.3f}", "-i", src] if is_img else
                  ["-ss", f"{max(0.0, _f(pip, 'in', 0.0)):.3f}", "-t", f"{dur:.3f}", "-i", src])
    args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(mask), "-loop", "1", "-t", f"{dur:.3f}", "-i", str(frame)]
    i0, i1, i2 = n_in, n_in + 1, n_in + 2
    en_expr = f"enable='between(t,{st:.3f},{en:.3f})'"
    lines = [
        f"[{i0}:v]setpts=PTS-STARTPTS,fps={fps},scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},setsar=1,format=rgba[pq]",
        f"[{i1}:v]format=gray,scale={pw}:{ph}[pm]",
        "[pq][pm]alphamerge[pa]",
        f"{src_label}[pa]overlay={x}:{y}:eof_action=repeat:{en_expr}[pv]",
        f"[{i2}:v]format=rgba[pf]",
        f"[pv][pf]overlay={x - pad}:{y - pad}:eof_action=repeat:{en_expr}{dst_label}",
    ]
    return args, lines


# ------------------------------------------------------------------ programme-wide overlays
def _hex(col: str, default: str = "3EC5FF") -> str:
    c = str(col or default).lstrip("#").replace("0x", "")
    return c if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c) else default


def watermark_png(text: str, W: int, H: int, lang: str = "en", opacity: float = 0.55, position: str = "top_right") -> Path:
    from PIL import Image, ImageDraw

    from ..timeline import text as ttext
    key = hashlib.sha1(f"wm|{text}|{W}x{H}|{lang}|{opacity}|{position}".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"wm_{key}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    size = max(12, int(min(W, H) * 0.032))
    fnt = ttext.font(size, lang, text)
    t, kw = ttext._prep(text)
    tw = dr.textlength(t, font=fnt, **kw)
    mx, my = int(W * 0.04), int(H * (0.035 if W >= H else 0.05))
    x = W - mx - tw if "right" in position else mx
    y = H - my - size * 1.3 if "bottom" in position else my
    a = int(255 * min(1.0, max(0.05, opacity)))
    dr.text((x + 2, y + 2), t, font=fnt, fill=(0, 0, 0, int(a * 0.6)), **kw)
    dr.text((x, y), t, font=fnt, fill=(255, 255, 255, a), **kw)
    img.save(out)
    return out


def overlay_graph(overlays: list[dict], W: int, H: int, fps: int, total: float, offset: float, dur: float,
                  lang: str, n_in: int, src_label: str) -> tuple[list, list[str], str]:
    """Programme-wide overlays for one piece that starts `offset` seconds into a `total`-second programme.
    Returns (input args, filter lines, output label). Frame timestamps of the piece start at 0."""
    args: list = []
    lines: list[str] = []
    last = src_label
    k = 0
    for o in overlays or []:
        if not isinstance(o, dict):
            continue
        ty = o.get("type")
        if ty == "progress_bar":
            h = max(2, int(round(_f(o, "height", 6) * H / 720 / 2)) * 2)  # even: 4:2:0 video cannot place odd rows exactly
            pos = o.get("position") or ("top" if H > W else "bottom")
            y = 0 if pos == "top" else H - h
            col = _hex(o.get("color", "#3EC5FF"))
            lines.append(f"{last}drawbox=x=0:y={y}:w=iw:h={h}:color=black@0.35:t=fill[ob{k}]")
            lines.append(f"color=c=0x{col}:s={W}x{h}:r={fps}[pbc{k}]")
            lines.append(f"[ob{k}][pbc{k}]overlay=x='-w+w*clip((t+{offset:.4f})/{max(total, 0.01):.4f},0,1)':y={y}:shortest=1[ov{k}]")
        elif ty == "watermark" and str(o.get("text", "")).strip():
            png = watermark_png(str(o["text"]), W, H, o.get("lang", lang), _f(o, "opacity", 0.55), str(o.get("position", "top_right")))
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
            lines.append(f"[{n_in}:v]format=rgba[wm{k}]")
            lines.append(f"{last}[wm{k}]overlay=0:0:eof_action=repeat[ov{k}]")
            n_in += 1
        else:
            continue
        last = f"[ov{k}]"
        k += 1
    return args, lines, last


def overlays_signature(tl: dict) -> list:
    """The parts of tl['overlays'] that change pixels (for cache keys)."""
    return [o for o in (tl.get("overlays") or []) if isinstance(o, dict) and o.get("type") in ("progress_bar", "watermark")]
