"""Post-production filter builders: stabilisation, rotation/flip, slow-motion interpolation, freeze frames, colour
correction, film-look grades, chroma key, masks, split screen and an audio-reactive overlay.

Every builder returns plain FFmpeg filter text (or filter-graph lines) and never raises on bad parameters: unusable
values fall back to defaults and a filter that this FFmpeg build lacks is skipped with a note in `notes` so the render
still succeeds and says what it could not do. Times are clip-local output seconds unless stated otherwise.
"""
from __future__ import annotations

import functools
import hashlib
import math
import subprocess
from pathlib import Path

from .. import config

# Creative looks (distinct from technical correction in `correct`). The first seven are the historical
# timeline/render.GRADES and are kept byte-identical so existing timelines render exactly as before.
GRADES: dict[str, str] = {
    "none": "",
    "cinematic": "eq=contrast=1.08:saturation=0.92,curves=preset=medium_contrast,colorbalance=rs=-0.04:bs=0.06:rh=0.05:bh=-0.05",
    "warm": "colorbalance=rm=0.06:gm=0.02:bm=-0.06,eq=saturation=1.08",
    "cool": "colorbalance=rm=-0.05:bm=0.07,eq=saturation=0.95",
    "bw": "hue=s=0,eq=contrast=1.12",
    "vivid": "eq=saturation=1.3:contrast=1.06",
    "documentary": "eq=contrast=1.04:saturation=0.88,curves=preset=lighter",
    # film looks
    "teal_orange": "colorbalance=rs=-0.08:gs=0.02:bs=0.10:rh=0.08:gh=0.02:bh=-0.08,eq=contrast=1.06:saturation=1.08",
    "bleach_bypass": "eq=saturation=0.58:contrast=1.16,curves=preset=medium_contrast",
    "film_warm": "curves=r='0/0.04 0.5/0.54 1/0.97':g='0/0.03 0.5/0.5 1/0.95':b='0/0.06 0.5/0.45 1/0.9',eq=saturation=0.95",
    "film_cool": "curves=r='0/0.03 0.5/0.47 1/0.93':g='0/0.04 0.5/0.5 1/0.96':b='0/0.07 0.5/0.55 1/0.98',eq=saturation=0.92",
    "noir": "hue=s=0,curves=preset=strong_contrast,eq=brightness=-0.03",
    "vintage": "curves=preset=vintage,eq=saturation=0.85",
    "matte": "curves=all='0/0.08 0.5/0.5 1/0.93',eq=saturation=0.9",
    "golden_hour": "colortemperature=temperature=4300,eq=saturation=1.1:contrast=1.04",
    "moonlight": "colortemperature=temperature=9800,eq=brightness=-0.05:saturation=0.72",
}


def _f(d, k: str, default: float) -> float:
    try:
        v = float(d.get(k, default)) if isinstance(d, dict) else default
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _hex(col, default: str = "000000") -> str:
    c = str(col or default).lstrip("#").replace("0x", "")
    return c if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c) else default


def is_hex(col) -> bool:
    c = str(col or "").lstrip("#").replace("0x", "")
    return len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c) and not Path(str(col)).exists()


def even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


@functools.lru_cache(maxsize=1)
def filters() -> frozenset[str]:
    """Names of the filters this FFmpeg build has."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
        names = set()
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and len(parts[0]) == 3 and "->" in parts[2]:
                names.add(parts[1])
        return frozenset(names)
    except (OSError, subprocess.SubprocessError):
        return frozenset()


def has(name: str) -> bool:
    fs = filters()
    return name in fs if fs else True  # could not list: assume present and let FFmpeg report


def esc_path(p: str | Path) -> str:
    """A path as a filter-option value (Windows drive colons and quotes escaped)."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def is_image(src: str) -> bool:
    return Path(str(src)).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")


# ------------------------------------------------------------------ stabilisation
def stab_opts(c: dict) -> dict | None:
    s = c.get("stabilize")
    if not s or c.get("type") != "video":
        return None
    s = s if isinstance(s, dict) else {}
    method = s.get("method", "auto")
    if method not in ("auto", "vidstab", "deshake"):
        method = "auto"
    return {"method": method, "smoothing": int(min(60, max(1, _f(s, "smoothing", 12)))),
            "shakiness": int(min(10, max(1, _f(s, "shakiness", 6)))), "zoom": min(20.0, max(-5.0, _f(s, "zoom", 0.0)))}


def stab_method(opts: dict) -> str | None:
    m = opts["method"]
    if m in ("auto", "vidstab") and has("vidstabdetect") and has("vidstabtransform"):
        return "vidstab"
    if has("deshake"):
        return "deshake"
    return None


def vidstab_trf(c: dict, opts: dict, cache: Path, src_sig: str, threads: int | None = None) -> Path:
    """Pass 1 of two-pass vidstab: motion analysis of exactly the frames the segment decodes. Cached."""
    from ..media import ff
    key = hashlib.sha1(f"{src_sig}|{c['in']}|{c['out']}|{opts['shakiness']}|v1".encode()).hexdigest()[:16]
    trf = cache / f"stab_{key}.trf"
    if trf.exists() and trf.stat().st_size > 0:
        return trf
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / f"stab_{key}.{hashlib.sha1(str(id(c)).encode()).hexdigest()[:6]}.tmp"
    th = ["-threads", str(threads)] if threads else []
    ff.run(["-ss", f"{c['in']:.3f}", "-t", f"{c['out'] - c['in']:.3f}", "-i", c["src"], *th, "-vf",
            f"vidstabdetect=shakiness={opts['shakiness']}:accuracy=12:result='{esc_path(tmp)}'", "-f", "null", "-"],
           op="stabilize-detect")
    tmp.replace(trf)
    return trf


def stab_filter(opts: dict, method: str, trf: Path | None) -> str:
    if method == "vidstab" and trf is not None:
        return (f"vidstabtransform=input='{esc_path(trf)}':smoothing={opts['smoothing']}:zoom={opts['zoom']:g}:optzoom=1:"
                f"interpol=bilinear,unsharp=5:5:0.6:3:3:0.3")
    return "deshake=rx=32:ry=32:edge=mirror"


# ------------------------------------------------------------------ orientation
def orient_filters(c: dict, notes: list[str]) -> list[str]:
    """rotate (degrees, clockwise) + hflip/vflip. 90/180/270 are lossless transposes; other angles level the
    horizon: rotate, then scale up just enough that no black corner shows."""
    out: list[str] = []
    try:
        deg = float(c.get("rotate", 0) or 0) % 360
    except (TypeError, ValueError):
        deg = 0.0
    if abs(deg - 90) < 1e-6:
        out.append("transpose=1")
    elif abs(deg - 180) < 1e-6:
        out.append("hflip,vflip")
    elif abs(deg - 270) < 1e-6:
        out.append("transpose=2")
    elif deg > 1e-6:
        a = deg if deg <= 180 else deg - 360
        th = math.radians(abs(a))
        if abs(a) > 45:
            notes.append(f"rotate {deg:g}: only angles within 45 deg of upright are levelled; ignored")
        else:
            # largest same-aspect rectangle inside the rotated frame (16:9 assumed worst case 1.78)
            k = math.cos(th) + 1.78 * math.sin(th)
            out.append(f"rotate=a={math.radians(a):.6f}:fillcolor=black,scale=trunc(iw*{k:.5f}/2)*2:trunc(ih*{k:.5f}/2)*2,"
                       f"crop=trunc(iw/{k:.5f}/2)*2:trunc(ih/{k:.5f}/2)*2")
    if c.get("hflip"):
        out.append("hflip")
    if c.get("vflip"):
        out.append("vflip")
    return out


# ------------------------------------------------------------------ slow motion
def interp_filter(c: dict, fps: int, notes: list[str]) -> str | None:
    """Frame synthesis for slowed footage. Off unless c['interpolate'] is 'blend' (fast frame blending) or 'mci'
    (motion-compensated, much slower)."""
    mode = c.get("interpolate")
    if mode in (None, False, "none", ""):
        return None
    if mode == "mci":
        if has("minterpolate"):
            return f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
        notes.append("minterpolate unavailable; slow motion uses frame blending")
        mode = "blend"
    if mode == "blend" or mode is True:
        if has("framerate"):
            return f"framerate=fps={fps}"
        notes.append("framerate filter unavailable; slow motion repeats frames")
    return None


# ------------------------------------------------------------------ freeze frames
def freezes(c: dict) -> list[tuple[float, float]]:
    """[(at, duration)] in clip-local output seconds (before earlier freezes are inserted), sorted."""
    if c.get("type") != "video":
        return []
    raw = c.get("freeze")
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out = []
    for f in items:
        if isinstance(f, dict):
            d = min(30.0, max(0.0, _f(f, "duration", 1.0)))
            if d > 1e-3:
                out.append((max(0.0, _f(f, "at", 0.0)), d))
    return sorted(out)


def freeze_total(c: dict) -> float:
    return sum(d for _, d in freezes(c))


def freeze_video(lines: list[str], src: str, base_dur: float, c: dict, fps: int, tag: str = "fz") -> str:
    """Append lines that hold the frame at each freeze point. Returns the output label."""
    cur, shift = src, 0.0
    for k, (at, d) in enumerate(freezes(c)):
        at = min(at, max(0.0, base_dur - 1.0 / fps)) + shift
        lines.append(f"{cur}split[{tag}a{k}][{tag}b{k}]")
        lines.append(f"[{tag}a{k}]trim=end={at:.4f},setpts=PTS-STARTPTS[{tag}c{k}]")
        lines.append(f"[{tag}b{k}]trim=start={at:.4f},setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration={d:.4f}[{tag}d{k}]")
        lines.append(f"[{tag}c{k}][{tag}d{k}]concat=n=2:v=1:a=0[{tag}v{k}]")
        cur, shift = f"[{tag}v{k}]", shift + d
    return cur


def freeze_audio(lines: list[str], src: str, base_dur: float, c: dict, tag: str = "fa") -> str:
    """Silence under each held frame so sound stays in sync with picture."""
    cur, shift = src, 0.0
    for k, (at, d) in enumerate(freezes(c)):
        at = min(at, base_dur) + shift
        lines.append(f"{cur}asplit[{tag}a{k}][{tag}b{k}]")
        lines.append(f"[{tag}a{k}]atrim=end={at:.4f},asetpts=PTS-STARTPTS[{tag}c{k}]")
        lines.append(f"[{tag}b{k}]atrim=start={at:.4f},asetpts=PTS-STARTPTS,adelay={int(round(d * 1000))}:all=1[{tag}d{k}]")
        lines.append(f"[{tag}c{k}][{tag}d{k}]concat=n=2:v=0:a=1[{tag}o{k}]")
        cur, shift = f"[{tag}o{k}]", shift + d
    return cur


# ------------------------------------------------------------------ colour correction
def _rgb3(v, default: float) -> tuple[float, float, float]:
    if isinstance(v, (list, tuple)) and len(v) == 3:
        try:
            return tuple(float(x) for x in v)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return (default,) * 3
    try:
        x = float(v) if v is not None else default
        return (x, x, x) if math.isfinite(x) else (default,) * 3
    except (TypeError, ValueError):
        return (default,) * 3


def correct_filters(cc, notes: list[str]) -> list[str]:
    """Technical correction applied before the creative grade.
    keys: exposure (stops), contrast, brightness, saturation, temperature (K, 6500 = neutral), vibrance,
    lift / gamma / gain (scalar or [r,g,b]), denoise (0..1), sharpen (0..1)."""
    if not isinstance(cc, dict) or not cc:
        return []
    out: list[str] = []
    dn = min(1.0, max(0.0, _f(cc, "denoise", 0.0)))
    if dn > 0:
        if has("hqdn3d"):
            out.append(f"hqdn3d={4 * dn:.2f}:{3 * dn:.2f}:{6 * dn:.2f}:{4.5 * dn:.2f}")
        else:
            notes.append("hqdn3d unavailable; denoise skipped")
    ev = min(3.0, max(-3.0, _f(cc, "exposure", 0.0)))
    lift, gamma, gain = _rgb3(cc.get("lift"), 0.0), _rgb3(cc.get("gamma"), 1.0), _rgb3(cc.get("gain"), 1.0)
    gain = tuple(g * 2 ** ev for g in gain)
    if any(abs(x) > 1e-4 for x in lift) or any(abs(x - 1) > 1e-4 for x in gamma + gain):
        ch = []
        for name, lo, ga, gn in zip("rgb", lift, gamma, gain):
            lo, ga, gn = min(0.5, max(-0.5, lo)), min(4.0, max(0.2, ga)), min(8.0, max(0.0, gn))
            ch.append(f"{name}='clip(255*pow(clip(({gn:.5f}*(val/255)+{lo:.5f}*(1-val/255)),0,1),{1 / ga:.5f}),0,255)'")
        out.append("lutrgb=" + ":".join(ch))
    eq = []
    for k, lo, hi, d in (("contrast", 0.2, 3.0, 1.0), ("brightness", -0.5, 0.5, 0.0), ("saturation", 0.0, 3.0, 1.0)):
        v = min(hi, max(lo, _f(cc, k, d)))
        if abs(v - d) > 1e-4:
            eq.append(f"{k}={v:.4f}")
    if eq:
        out.append("eq=" + ":".join(eq))
    temp = _f(cc, "temperature", 6500.0)
    if abs(temp - 6500) > 1:
        if has("colortemperature"):
            out.append(f"colortemperature=temperature={min(20000.0, max(1500.0, temp)):.0f}")
        else:
            notes.append("colortemperature unavailable; temperature skipped")
    vib = min(2.0, max(-2.0, _f(cc, "vibrance", 0.0)))
    if abs(vib) > 1e-4 and has("vibrance"):
        out.append(f"vibrance=intensity={vib:.3f}")
    sh = min(1.0, max(0.0, _f(cc, "sharpen", 0.0)))
    if sh > 0:
        out.append(f"cas=strength={sh:.3f}" if has("cas") else f"unsharp=5:5:{1.5 * sh:.2f}")
    return out


# ------------------------------------------------------------------ chroma key
def chroma_opts(c: dict) -> dict | None:
    k = c.get("chroma_key")
    if not k:
        return None
    k = k if isinstance(k, dict) else {}
    return {"color": _hex(k.get("color"), "00FF00"), "similarity": min(1.0, max(0.01, _f(k, "similarity", 0.15))),
            "blend": min(1.0, max(0.0, _f(k, "blend", 0.05))), "despill": k.get("despill", True) is not False,
            "bg": k.get("bg")}


def chroma_graph(k: dict, lines: list[str], fg: str, W: int, H: int, fps: int, dur: float, n_in: int,
                 tag: str, notes: list[str]) -> tuple[list, str]:
    """Key `fg` and composite it over k['bg'] (image, video or '#RRGGBB'; default black).
    Returns (extra input args, output label)."""
    args: list = []
    bg = k.get("bg")
    if bg and not is_hex(bg) and Path(str(bg)).exists():
        if is_image(bg):
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(bg)]
        else:
            args += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(bg)]
        lines.append(f"[{n_in}:v]setpts=PTS-STARTPTS,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},setsar=1,format=yuv420p[{tag}bg]")
    else:
        if bg and not is_hex(bg):
            notes.append(f"chroma_key bg {bg!r} not found; black used")
        lines.append(f"color=c=0x{_hex(bg, '000000')}:s={W}x{H}:r={fps}:d={dur:.3f}[{tag}bg]")
    if not has("chromakey"):
        notes.append("chromakey unavailable; clip shown unkeyed")
        return args, fg
    sp = ""
    if k["despill"] and has("despill"):
        g, b = int(k["color"][2:4], 16), int(k["color"][4:6], 16)
        sp = f",despill=type={'blue' if b > g else 'green'}"
    lines.append(f"{fg}format=yuva420p,chromakey=0x{k['color']}:{k['similarity']:.3f}:{k['blend']:.3f}{sp}[{tag}fg]")
    lines.append(f"[{tag}bg][{tag}fg]overlay=0:0:shortest=1:format=auto,format=yuv420p[{tag}o]")
    return args, f"[{tag}o]"


# ------------------------------------------------------------------ masks
MASK_EFFECTS = ("black", "blur", "dim", "pixelate")


def masks(c: dict) -> list[dict]:
    raw = c.get("mask")
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        out.append({"shape": "ellipse" if m.get("shape") == "ellipse" else "rect",
                    "x": min(1.0, max(0.0, _f(m, "x", 0.25))), "y": min(1.0, max(0.0, _f(m, "y", 0.25))),
                    "w": min(1.0, max(0.01, _f(m, "w", 0.5))), "h": min(1.0, max(0.01, _f(m, "h", 0.5))),
                    "feather": min(0.5, max(0.0, _f(m, "feather", 0.03))),
                    "effect": m.get("effect") if m.get("effect") in MASK_EFFECTS else "black",
                    "region": "inside" if m.get("region") == "inside" else "outside",
                    "amount": min(1.0, max(0.0, _f(m, "amount", 0.6)))})
        kf = {k: v for k, v in (m.get("keyframes") or {}).items() if k in MASK_KF and isinstance(v, list) and v}
        if kf:  # a moving / tracked mask: x, y, w, h follow keyframes over clip-local time
            out[-1]["keyframes"] = kf
    return out


MASK_KF = ("x", "y", "w", "h")


def kf_value(points, t: float, default: float = 0.0) -> float:
    """The value of keyframes [[t, v, "linear"|"ease"], ...] at time t (same curve as render.kf_expr)."""
    pts = sorted((float(p[0]), float(p[1]), (p[2] if len(p) > 2 else "linear")) for p in points or [] if len(p) >= 2)
    if not pts:
        return default
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0, mode), (t1, v1, _) in zip(pts, pts[1:]):
        if t < t1:
            u = (t - t0) / max(t1 - t0, 1e-4)
            if mode == "ease":
                u = u * u * (3 - 2 * u)
            return v0 + (v1 - v0) * u
    return pts[-1][1]


def mask_at(m: dict, t: float) -> dict:
    """The mask's geometry at clip time t."""
    kf = m.get("keyframes") or {}
    g = {k: kf_value(kf[k], t, m[k]) if k in kf else m[k] for k in MASK_KF}
    g["x"], g["y"] = min(1.0, max(-1.0, g["x"])), min(1.0, max(-1.0, g["y"]))
    g["w"], g["h"] = min(2.0, max(0.005, g["w"])), min(2.0, max(0.005, g["h"]))
    return g


def mask_video(m: dict, W: int, H: int, dur: float, fps: int) -> tuple[Path, int, int]:
    """Raw grey matte video (one frame per output frame) of a moving mask, drawn at quarter size (the renderer scales
    it up; the feather hides the steps). Cached by its parameters. Returns (path, width, height)."""
    from PIL import Image, ImageDraw, ImageFilter
    mw, mh = max(32, even(W / 4)), max(32, even(H / 4))
    n = max(1, round(dur * fps))
    key = hashlib.sha1(f"mmask|{sorted((k, str(v)) for k, v in m.items())}|{mw}x{mh}|{n}|{fps}|v1".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"mmask_{key}_{mw}x{mh}.gray"
    if out.exists() and out.stat().st_size == mw * mh * n:
        return out, mw, mh
    out.parent.mkdir(parents=True, exist_ok=True)
    inside_keep = m["region"] == "outside"
    fill, bg = (255, 0) if inside_keep else (0, 255)
    rad = m["feather"] * min(mw, mh)
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        for i in range(n):
            g = mask_at(m, i / fps)
            img = Image.new("L", (mw, mh), bg)
            box = [g["x"] * mw, g["y"] * mh, (g["x"] + g["w"]) * mw, (g["y"] + g["h"]) * mh]
            (ImageDraw.Draw(img).ellipse if m["shape"] == "ellipse" else ImageDraw.Draw(img).rectangle)(box, fill=fill)
            if rad > 0:
                img = img.filter(ImageFilter.GaussianBlur(rad))
            f.write(img.tobytes())
    tmp.replace(out)
    return out, mw, mh


def mask_from_track(samples: list[dict], c: dict, W: int, H: int, src_w: int, src_h: int, pad: float = 1.5,
                    effect: str = "blur", region: str = "inside", shape: str = "ellipse", amount: float = 0.8,
                    step: float = 0.2) -> dict | None:
    """A moving mask that follows a face/object track (nexus.media.faces samples: t in source seconds, x/y centre and
    w/h size as fractions of the source frame) through the clip's cover crop at its static `focus`. `pad` enlarges
    the box (1.5 = 50 % margin round the face). Typical use: blur or pixelate a bystander's face (effect inside), or
    dim everything but the speaker (region outside). Returns None when the track has no sample inside the clip."""
    t0, t1 = float(c.get("in", 0)), float(c.get("out", 0))
    sp = float(c.get("speed", 1.0)) or 1.0
    pts = [p for p in samples or [] if t0 - 0.5 <= float(p["t"]) <= t1 + 0.5 and "x" in p and "y" in p]
    if not pts:
        return None
    sc = max(W / src_w, H / src_h)
    fx, fy = (c.get("focus") or (0.5, 0.5))[:2]
    ox, oy = (src_w * sc - W) * float(fx), (src_h * sc - H) * float(fy)
    kf: dict[str, list] = {"x": [], "y": [], "w": [], "h": []}
    last = -1e9
    for p in pts:
        if float(p["t"]) - last < step and p is not pts[-1]:
            continue
        last = float(p["t"])
        tt = round(max(0.0, (float(p["t"]) - t0) / sp), 3)
        bw = float(p.get("w", 0.15)) * src_w * sc / W * pad
        bh = float(p.get("h", 0.2)) * src_h * sc / H * pad
        cx = (float(p["x"]) * src_w * sc - ox) / W
        cy = (float(p["y"]) * src_h * sc - oy) / H
        kf["x"].append([tt, round(cx - bw / 2, 4)])
        kf["y"].append([tt, round(cy - bh / 2, 4)])
        kf["w"].append([tt, round(bw, 4)])
        kf["h"].append([tt, round(bh, 4)])
    first = {k: v[0][1] for k, v in kf.items()}
    return {"shape": shape, **first, "feather": 0.04, "effect": effect, "region": region, "amount": amount, "keyframes": kf}


def mask_png(m: dict, W: int, H: int) -> Path:
    """Greyscale matte: 255 where the picture is KEPT untouched, 0 where the mask effect shows."""
    from PIL import Image, ImageDraw, ImageFilter
    key = hashlib.sha1(f"mask|{sorted(m.items())}|{W}x{H}|v1".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"mask_{key}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    inside_keep = m["region"] == "outside"  # effect outside the shape -> keep the inside
    img = Image.new("L", (W, H), 0 if inside_keep else 255)
    dr = ImageDraw.Draw(img)
    box = [m["x"] * W, m["y"] * H, (m["x"] + m["w"]) * W, (m["y"] + m["h"]) * H]
    fill = 255 if inside_keep else 0
    (dr.ellipse if m["shape"] == "ellipse" else dr.rectangle)(box, fill=fill)
    if m["feather"] > 0:
        img = img.filter(ImageFilter.GaussianBlur(m["feather"] * min(W, H)))
    img.save(out)
    return out


def mask_graph(c: dict, lines: list[str], src: str, W: int, H: int, dur: float, n_in: int, scale: float,
               tag: str, fps: int | None = None) -> tuple[list, str]:
    """Apply every mask of the clip in order. Returns (extra input args, output label)."""
    args: list = []
    cur = src
    for k, m in enumerate(masks(c)):
        if m.get("keyframes"):
            fps = int(fps or config.FPS)
            raw, mw, mh = mask_video(m, W, H, dur, fps)
            args += ["-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{mw}x{mh}", "-framerate", fps, "-i", str(raw)]
        else:
            png = mask_png(m, W, H)
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
        if m["effect"] == "blur":
            fx = f"gblur=sigma={max(1.0, 30 * m['amount'] * scale):.2f}"
        elif m["effect"] == "dim":
            fx = f"eq=brightness={-0.45 * m['amount']:.3f}:saturation={1 - 0.6 * m['amount']:.3f}"
        elif m["effect"] == "pixelate":
            b = max(2, int(round(min(W, H) / (8 + 40 * (1 - m["amount"])))))
            fx = f"scale=trunc(iw/{b}/2)*2:trunc(ih/{b}/2)*2,scale={W}:{H}:flags=neighbor"
        else:
            fx = "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill"
        lines.append(f"{cur}split[{tag}a{k}][{tag}b{k}]")
        lines.append(f"[{tag}b{k}]{fx},format=yuv420p[{tag}e{k}]")
        lines.append(f"[{n_in}:v]format=gray,scale={W}:{H}:flags=bilinear,setpts=PTS-STARTPTS[{tag}m{k}]")
        lines.append(f"[{tag}a{k}]format=yuva420p[{tag}c{k}]")
        lines.append(f"[{tag}c{k}][{tag}m{k}]alphamerge[{tag}k{k}]")
        lines.append(f"[{tag}e{k}][{tag}k{k}]overlay=0:0:format=auto,format=yuv420p[{tag}o{k}]")
        cur = f"[{tag}o{k}]"
        n_in += 1
    return args, cur


# ------------------------------------------------------------------ split screen
def split_opts(c: dict) -> dict | None:
    s = c.get("split")
    if not isinstance(s, dict):
        return None
    srcs = [x if isinstance(x, dict) else {"src": x} for x in (s.get("srcs") or [])]
    srcs = [x for x in srcs if x.get("src") and Path(str(x["src"])).exists()][:3]
    if not srcs:
        return None
    layout = s.get("layout", "h")
    if layout not in ("h", "v", "grid"):
        layout = "h"
    if layout == "grid" or len(srcs) >= 3:
        layout = "grid"
    return {"layout": layout, "srcs": srcs, "gap": int(min(64, max(0, _f(s, "gap", 6)))), "color": _hex(s.get("color"), "000000")}


def split_cells(opts: dict, W: int, H: int, scale: float = 1.0) -> list[tuple[int, int, int, int]]:
    n = 1 + len(opts["srcs"])
    g = int(opts["gap"] * scale) // 2 * 2
    if opts["layout"] == "h":
        w = (W - g * (n - 1)) // n // 2 * 2
        return [(i * (w + g), 0, w, H) for i in range(n)]
    if opts["layout"] == "v":
        h = (H - g * (n - 1)) // n // 2 * 2
        return [(0, i * (h + g), W, h) for i in range(n)]
    w, h = (W - g) // 2 // 2 * 2, (H - g) // 2 // 2 * 2
    return [((i % 2) * (w + g), (i // 2) * (h + g), w, h) for i in range(n)]


def split_graph(opts: dict, lines: list[str], main: str, W: int, H: int, fps: int, dur: float, n_in: int,
                scale: float, tag: str = "sp") -> tuple[list, str]:
    """`main` is the clip's own picture *before* fitting; it becomes panel 1. Returns (input args, label)."""
    cells = split_cells(opts, W, H, scale)
    args: list = []
    lines.append(f"color=c=0x{opts['color']}:s={W}x{H}:r={fps}:d={dur:.3f}[{tag}bg]")
    cur = f"[{tag}bg]"
    panels = [main]
    for k, s in enumerate(opts["srcs"]):
        if is_image(s["src"]):
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(s["src"])]
        else:
            args += ["-ss", f"{max(0.0, _f(s, 'in', 0.0)):.3f}", "-t", f"{dur:.3f}", "-i", str(s["src"])]
        panels.append(f"[{n_in + k}:v]")
    for k, (p, (x, y, w, h)) in enumerate(zip(panels, cells)):
        lines.append(f"{p}setpts=PTS-STARTPTS,fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                     f"setsar=1,tpad=stop_mode=clone:stop_duration={dur:.3f}[{tag}p{k}]")
        lines.append(f"{cur}[{tag}p{k}]overlay={x}:{y}:shortest=1[{tag}o{k}]")
        cur = f"[{tag}o{k}]"
    return args, cur


# ------------------------------------------------------------------ audio-reactive overlay (programme level)
def audio_viz(overlays) -> dict | None:
    for o in overlays or []:
        if isinstance(o, dict) and o.get("type") == "audio_waves":
            return {"style": "freqs" if o.get("style") == "freqs" else "waves",
                    "position": o.get("position") if o.get("position") in ("top", "middle", "bottom") else "bottom",
                    "height": min(0.5, max(0.04, _f(o, "height", 0.16))), "color": _hex(o.get("color"), "FFFFFF"),
                    "opacity": min(1.0, max(0.1, _f(o, "opacity", 0.85)))}
    return None


def audio_viz_lines(v: dict, audio: str, video: str, W: int, H: int, fps: int, out: str, notes: list[str]) -> list[str]:
    """Waveform / spectrum drawn from the final mix over the programme picture."""
    h = even(H * v["height"])
    y = {"top": int(H * 0.04), "middle": (H - h) // 2, "bottom": H - h - int(H * 0.06)}[v["position"]]
    filt = "showfreqs" if v["style"] == "freqs" else "showwaves"
    if not has(filt):
        notes.append(f"{filt} unavailable; audio_waves overlay skipped")
        return [f"{audio}anullsink", f"{video}null{out}"]
    if filt == "showfreqs":
        src = f"showfreqs=s={W}x{h}:mode=bar:ascale=log:fscale=log:colors=0x{v['color']}:win_size=2048,fps={fps}"
    else:
        src = f"showwaves=s={W}x{h}:mode=cline:colors=0x{v['color']}:r={fps}:scale=sqrt"
    return [f"{audio}{src},format=rgba,colorchannelmixer=aa={v['opacity']:.3f}[awv]",
            f"{video}[awv]overlay=0:{y}:shortest=1:format=auto,format=yuv420p{out}"]
