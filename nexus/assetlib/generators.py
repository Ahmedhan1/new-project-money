"""Locally generated library assets (own work, CC0-1.0): colour LUTs (.cube), frames/overlays (PNG), sound effects,
ambiences and music beds (WAV), and timeline templates (JSON).

Every generator is deterministic for a given NEXUS version and parameters, so an asset can be deleted and re-created
at any time. Integrity is checked against the SHA-256 recorded when the file was generated on this machine (float
maths can differ between CPUs, so generated files are not pinned to one hash across platforms; downloaded assets are).
"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np

SR = 48000


# ------------------------------------------------------------------ LUTs
def _lum(r, g, b):
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _s(x, k):
    """Contrast around mid grey: x^k / (x^k + (1-x)^k). k = 1 identity, k > 1 more contrast, k < 1 softer."""
    x = min(1.0, max(0.0, x))
    if k == 1 or x in (0.0, 1.0):
        return x
    a, b = x ** k, (1 - x) ** k
    return a / (a + b)


def _clamp(v):
    return min(1.0, max(0.0, v))


def _mix(a, b, t):
    return a + (b - a) * t


def lut_fn(look: str):
    """RGB (0..1) -> RGB for every look in the catalog."""
    def cinematic(r, g, b):
        y = _lum(r, g, b)
        r2, g2, b2 = _mix(r, y, 0.12), _mix(g, y, 0.12), _mix(b, y, 0.12)  # slight desaturation
        r2, g2, b2 = r2 + 0.05 * (y - 0.4), g2 + 0.01, b2 - 0.04 * (y - 0.45)  # warm highlights, cool shadows
        return tuple(_clamp(_s(v * 0.96 + 0.02, 1.15)) for v in (r2, g2, b2))

    def warm_film(r, g, b):
        y = _lum(r, g, b)
        return tuple(_clamp(_s(v, 1.08)) for v in (r * 1.06 + 0.02, g * 1.01 + 0.01, b * 0.88 + 0.02 * (1 - y)))

    def cool_cinematic(r, g, b):
        y = _lum(r, g, b)
        return tuple(_clamp(_s(v, 1.1)) for v in (_mix(r, y, 0.15) * 0.94, _mix(g, y, 0.1) * 1.0 + 0.01, _mix(b, y, 0.05) * 1.06 + 0.03))

    def documentary(r, g, b):
        y = _lum(r, g, b)
        return tuple(_clamp(_s(_mix(v, y, 0.12) * 0.97 + 0.025, 1.04)) for v in (r, g, b))

    def vintage(r, g, b):
        y = _lum(r, g, b)
        r2, g2, b2 = _mix(r, y, 0.35), _mix(g, y, 0.3), _mix(b, y, 0.4)
        return tuple(_clamp(0.06 + 0.86 * v) for v in (r2 * 1.07 + 0.02, g2 * 1.02 + 0.01, b2 * 0.85 + 0.04))  # faded, yellowed

    def black_white(r, g, b):
        y = _clamp(_s(0.25 * r + 0.65 * g + 0.10 * b, 1.18))
        return (y, y, y)

    def luxury_commercial(r, g, b):
        y = _lum(r, g, b)
        r2, g2, b2 = _mix(y, r, 1.12), _mix(y, g, 1.05), _mix(y, b, 1.0)  # rich but controlled saturation
        gold = 0.035 * max(0.0, y - 0.55)
        return tuple(_clamp(_s(v, 1.12)) for v in (r2 + gold, g2 + gold * 0.7, b2 - gold * 0.5))

    def natural_skin(r, g, b):
        # protect the skin-tone hue band, gently desaturate greens/blues, small warmth
        y = _lum(r, g, b)
        skin = max(0.0, 1 - abs((r - g) - 0.12) * 6) * (1 if r > g > b else 0)
        k = 0.92 + 0.08 * skin
        return tuple(_clamp(_mix(y, v, k)) for v in (r * 1.02, g, b * 0.98))

    def teal_orange(r, g, b):
        y = _lum(r, g, b)
        k = 0.18
        return tuple(_clamp(v) for v in (r + k * (y - 0.35), g + 0.03 * (0.5 - abs(y - 0.5)), b - k * (y - 0.45)))

    def soft_contrast(r, g, b):
        return tuple(_clamp(0.05 + 0.9 * _s(v, 0.85)) for v in (r, g, b))

    def high_contrast(r, g, b):
        return tuple(_clamp(_s(v, 1.45)) for v in (r, g, b))

    def bleach_bypass(r, g, b):
        y = _lum(r, g, b)
        return tuple(_clamp((0.55 * v + 0.45 * y - 0.5) * 1.15 + 0.5) for v in (r, g, b))

    def day_for_night(r, g, b):
        y = _lum(r, g, b)
        return tuple(_clamp(v) for v in (y * 0.35 + r * 0.1, y * 0.45 + g * 0.1, y * 0.6 + b * 0.25 + 0.02))

    def sepia(r, g, b):
        y = _lum(r, g, b)
        return (_clamp(y * 1.07 + 0.05), _clamp(y * 0.95 + 0.02), _clamp(y * 0.75))

    looks = {k: v for k, v in locals().items() if callable(v) and not k.startswith("_")}
    if look not in looks:
        raise KeyError(f"unknown look {look}")
    return looks[look]


def lut(path: Path, look: str, size: int = 33) -> Path:
    from ..fx import lut as fxlut
    return fxlut.write_cube(path, lut_fn(look), size, f"NEXUS {look} (generated, CC0-1.0)")


# ------------------------------------------------------------------ frames / overlays (RGBA PNG, 1920x1080 or 1080x1920)
def _canvas(w, h):
    from PIL import Image
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _font(size):
    from PIL import ImageFont

    from .. import config
    return ImageFont.truetype(config.FONT_LATIN, size)


def overlay(path: Path, kind: str, w: int = 1920, h: int = 1080, **p) -> Path:
    from PIL import ImageDraw, ImageFilter
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    if kind == "letterbox":
        ratio = float(p.get("ratio", 2.39))
        bar = max(0, int(round((h - w / ratio) / 2)))
        d.rectangle([0, 0, w, bar], fill=(0, 0, 0, 255))
        d.rectangle([0, h - bar, w, h], fill=(0, 0, 0, 255))
    elif kind == "film_frame":
        side = int(w * 0.055)
        d.rectangle([0, 0, side, h], fill=(12, 10, 8, 255))
        d.rectangle([w - side, 0, w, h], fill=(12, 10, 8, 255))
        hole_w, hole_h, gap = int(side * 0.42), int(h * 0.045), int(h * 0.085)
        for y in range(int(gap * 0.4), h, gap):
            for x0 in (int((side - hole_w) / 2), w - side + int((side - hole_w) / 2)):
                d.rounded_rectangle([x0, y, x0 + hole_w, y + hole_h], radius=int(hole_w * 0.25), fill=(0, 0, 0, 0))
        inner = _canvas(w, h)
        di = ImageDraw.Draw(inner)
        di.rounded_rectangle([side, 0, w - side, h], radius=int(h * 0.03), outline=(0, 0, 0, 200), width=int(h * 0.012))
        img.alpha_composite(inner.filter(ImageFilter.GaussianBlur(2)))
    elif kind == "viewfinder":
        m, L, t = int(h * 0.06), int(h * 0.08), max(2, int(h * 0.004))
        col = (255, 255, 255, 230)
        for (x, y, sx, sy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
            d.line([x, y, x + sx * L, y], fill=col, width=t)
            d.line([x, y, x, y + sy * L], fill=col, width=t)
        r = int(h * 0.016)
        d.ellipse([m + L * 0.2, m + L * 0.6, m + L * 0.2 + 2 * r, m + L * 0.6 + 2 * r], fill=(235, 40, 40, 255))
        f = _font(int(h * 0.032))
        d.text((m + L * 0.2 + 3 * r, m + L * 0.6 - r * 0.2), "REC", font=f, fill=col)
        d.text((w - m - int(h * 0.3), m + L * 0.6 - r * 0.2), str(p.get("label", "4K  25P  F2.8")), font=f, fill=col)
        cx, cy, c = w // 2, h // 2, int(h * 0.025)
        d.line([cx - c, cy, cx + c, cy], fill=col, width=t)
        d.line([cx, cy - c, cx, cy + c], fill=col, width=t)
        for k in (1, 2):  # rule-of-thirds guides, faint
            d.line([w * k // 3, m + L, w * k // 3, h - m - L], fill=(255, 255, 255, 50), width=1)
            d.line([m + L, h * k // 3, w - m - L, h * k // 3], fill=(255, 255, 255, 50), width=1)
        bw, bh = int(h * 0.05), int(h * 0.022)
        bx, by = w - m - bw - int(h * 0.01), h - m - L * 0.6 - bh
        d.rectangle([bx, by, bx + bw, by + bh], outline=col, width=t)
        d.rectangle([bx + t * 2, by + t * 2, bx + bw * 0.75, by + bh - t * 2], fill=col)
    elif kind == "vignette_frame":
        from PIL import Image
        g = Image.radial_gradient("L").resize((w, h))
        a = g.point(lambda v: int(min(255, max(0, (v - 110) * 1.6))))
        img = Image.merge("RGBA", (Image.new("L", (w, h), 0),) * 3 + (a,))
    elif kind == "lower_third_bar":
        y0, y1 = int(h * 0.74), int(h * 0.88)
        d.rectangle([int(w * 0.05), y0, int(w * 0.52), y1], fill=(10, 14, 24, 200))
        d.rectangle([int(w * 0.05), y0, int(w * 0.058), y1], fill=tuple(p.get("accent", (0, 190, 255))) + (255,))
    elif kind == "location_tag":
        x0, y0 = int(w * 0.05), int(h * 0.07)
        d.rectangle([x0, y0, x0 + int(w * 0.004), y0 + int(h * 0.075)], fill=(255, 200, 0, 255))
        d.rectangle([x0 + int(w * 0.004), y0, x0 + int(w * 0.24), y0 + int(h * 0.075)], fill=(0, 0, 0, 150))
    elif kind == "callout_circle":
        cx, cy, r = int(w * float(p.get("x", 0.6))), int(h * float(p.get("y", 0.45))), int(h * float(p.get("r", 0.12)))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 214, 0, 255), width=max(3, int(h * 0.008)))
    elif kind == "callout_arrow":
        x0, y0, x1, y1 = (int(w * 0.25), int(h * 0.7), int(w * 0.48), int(h * 0.48))
        tw = max(4, int(h * 0.01))
        d.line([x0, y0, x1, y1], fill=(255, 214, 0, 255), width=tw)
        ang = math.atan2(y1 - y0, x1 - x0)
        L = h * 0.05
        for s in (-0.5, 0.5):
            d.line([x1, y1, x1 - L * math.cos(ang + s), y1 - L * math.sin(ang + s)], fill=(255, 214, 0, 255), width=tw)
    elif kind == "callout_box":
        x0, y0, x1, y1 = (int(w * 0.55), int(h * 0.2), int(w * 0.9), int(h * 0.55))
        d.rounded_rectangle([x0, y0, x1, y1], radius=int(h * 0.02), outline=(255, 255, 255, 235), width=max(3, int(h * 0.006)))
    elif kind == "social_safe_zone":
        # 9:16 layout: title band at the top, caption band low, right-side icon column kept clear (YouTube Shorts / TikTok)
        d.rectangle([0, 0, w, int(h * 0.12)], fill=(0, 0, 0, 120))
        d.rectangle([0, int(h * 0.78), w, int(h * 0.84)], fill=(0, 0, 0, 90))
    elif kind == "split_frame":
        t = max(4, int(min(w, h) * 0.006))
        if w >= h:
            d.rectangle([w // 2 - t // 2, 0, w // 2 + t // 2, h], fill=(255, 255, 255, 255))
        else:
            d.rectangle([0, h // 2 - t // 2, w, h // 2 + t // 2], fill=(255, 255, 255, 255))
    elif kind == "typography_grid":
        f1, f2 = _font(int(h * 0.11)), _font(int(h * 0.035))
        d.text((int(w * 0.07), int(h * 0.30)), str(p.get("headline", "HEADLINE")), font=f1, fill=(255, 255, 255, 255))
        d.rectangle([int(w * 0.07), int(h * 0.45), int(w * 0.32), int(h * 0.455)], fill=(255, 214, 0, 255))
        d.text((int(w * 0.07), int(h * 0.48)), str(p.get("kicker", "Supporting line of text")), font=f2, fill=(230, 230, 230, 255))
    else:
        raise KeyError(f"unknown overlay kind {kind}")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=False)
    return path


# ------------------------------------------------------------------ audio
def _write_wav(path: Path, y: np.ndarray, sr: int = SR) -> Path:
    if y.ndim == 1:
        y = np.stack([y, y], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    tmp.replace(path)
    return path


def _norm(y: np.ndarray, peak_db: float = -3.0) -> np.ndarray:
    y = y - y.mean(axis=0)
    return y / (np.abs(y).max() + 1e-12) * 10 ** (peak_db / 20)


def _env(n, a, d):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / max(d, 1e-4))


def _bp(x, lo, hi):
    from scipy.signal import butter, sosfilt
    return sosfilt(butter(2, [lo, min(hi, SR / 2 - 100)], "band", fs=SR, output="sos"), x, axis=0)


def _lp(x, hi):
    from scipy.signal import butter, sosfilt
    return sosfilt(butter(2, hi, "low", fs=SR, output="sos"), x, axis=0)


def sound(path: Path, kind: str, duration: float | None = None, seed: int = 0) -> Path:
    """Sound effects: the six NEXUS synths (nexus/fx/sfx.py) plus cinematic and UI sounds made here."""
    from ..fx import sfx
    rng = np.random.default_rng(seed)
    if kind in sfx.SYNTHS:
        return _write_wav(path, sfx.render_array(kind, duration, seed))
    d = float(duration or {"cinematic_hit": 3.5, "braam": 3.0, "sub_drop": 2.0, "reverse_whoosh": 1.2, "glitch_stutter": 0.8,
                            "ui_notification": 0.5, "ui_swipe": 0.35, "typewriter": 2.0, "camera_shutter": 0.4}.get(kind, 1.0))
    n = int(d * SR)
    t = np.arange(n) / SR
    if kind == "cinematic_hit":  # low boom + noise transient + long tail
        boom = np.sin(2 * np.pi * (45 * t + 30 * (1 - np.exp(-t * 8)) / 8)) * _env(n, 0.003, 0.9)
        crack = _bp(rng.normal(0, 1, n), 800, 9000) * _env(n, 0.001, 0.06)
        tail = _lp(rng.normal(0, 1, n), 400) * _env(n, 0.05, 1.6) * 0.6
        y = boom * 1.0 + crack * 0.5 + tail
    elif kind == "braam":  # detuned low saws, swelling then decaying
        saw = sum(2 * ((t * f) % 1) - 1 for f in (55, 55.6, 82.4, 110.3))
        y = _lp(saw, 900) * np.minimum(1, t / 0.25) * np.exp(-np.maximum(0, t - 0.6) / 1.1)
    elif kind == "sub_drop":
        y = np.sin(2 * np.pi * np.cumsum(90 * np.exp(-t * 1.8) + 28) / SR) * _env(n, 0.005, d * 0.6)
    elif kind == "reverse_whoosh":
        y = sfx.render_array("whoosh", d, seed)[::-1, 0]
    elif kind == "glitch_stutter":
        y = np.zeros(n)
        seg = int(0.04 * SR)
        src = _bp(rng.normal(0, 1, seg), 300, 6000) * np.hanning(seg)
        for k in range(0, n - seg, int(seg * 1.4)):
            y[k:k + seg] += src * rng.uniform(0.3, 1.0) * (1 if rng.random() > 0.25 else 0)
        y += np.sign(np.sin(2 * np.pi * 220 * t)) * 0.08 * (rng.random(n) > 0.6)
    elif kind == "ui_notification":
        y = sum(np.sin(2 * np.pi * f * t) * _env(n, 0.004, 0.18) * (t >= s0) for f, s0 in ((880, 0.0), (1318.5, 0.09)))
    elif kind == "ui_swipe":
        y = _bp(rng.normal(0, 1, n), 1500, 9000) * np.sin(np.pi * t / d) ** 2
    elif kind == "typewriter":
        y = np.zeros(n)
        for s0 in np.cumsum(rng.uniform(0.08, 0.2, 40)):
            k = int(s0 * SR)
            if k >= n - 600:
                break
            y[k:k + 600] += _bp(rng.normal(0, 1, 600), 1500, 7000) * _env(600, 0.0005, 0.01)
    elif kind == "camera_shutter":
        y = np.zeros(n)
        for s0, g in ((0.0, 1.0), (0.11, 0.8)):
            k = int(s0 * SR)
            m = int(0.05 * SR)
            y[k:k + m] += _bp(rng.normal(0, 1, m), 900, 8000) * _env(m, 0.0005, 0.012) * g
    else:
        raise KeyError(f"unknown sound {kind}")
    y = np.asarray(y, dtype=np.float64)
    fade = min(n // 4, int(0.01 * SR))
    if fade:
        y[-fade:] *= np.linspace(1, 0, fade)
    return _write_wav(path, _norm(y))


def ambience(path: Path, kind: str, duration: float = 30.0, seed: int = 0) -> Path:
    """Loopable beds: room/outdoor/city/nature from nexus/audio/design.py, plus rain, wind, ocean and stream."""
    from ..audio import design
    if kind in design.AMBIENCES:
        y = design.ambience_array(kind, duration, SR, seed, -24.0)
        return _write_wav(path, _norm(y, -6))
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    t = np.arange(n) / SR
    w = rng.normal(0, 1, (n, 2))
    if kind == "rain":
        y = _bp(w, 1500, 12000) * 0.5
        drops = (rng.random((n, 2)) > 0.9993) * rng.uniform(0.3, 1.0, (n, 2))
        y += _bp(drops, 2000, 9000) * 6
    elif kind == "wind":
        g = 0.4 + 0.6 * (0.5 + 0.5 * np.sin(2 * np.pi * t / 7.3 + rng.uniform(0, 6))) * (0.6 + 0.4 * np.sin(2 * np.pi * t / 2.9))
        y = _bp(np.cumsum(w, axis=0) * 0.01, 60, 900) * g[:, None]
    elif kind == "ocean":
        g = (0.5 + 0.5 * np.sin(2 * np.pi * t / 8.0)) ** 2
        y = _lp(w, 2500) * (0.25 + g[:, None])
    elif kind == "stream":
        y = _bp(w, 400, 6000) * (0.7 + 0.3 * np.sin(2 * np.pi * t * 3.1)[:, None] * rng.random((n, 1)))
    else:
        raise KeyError(f"unknown ambience {kind}")
    k = min(n // 2, SR)
    if k:
        r = np.linspace(0, 1, k)[:, None]
        y[:k] *= r
        y[-k:] *= r[::-1]
    return _write_wav(path, _norm(y, -6))


def music(path: Path, mood: str, duration: float = 60.0, seed: int = 0) -> Path:
    import shutil

    from ..audio import music as m
    src = m.music_bed(duration, mood, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, path)
    return path


# ------------------------------------------------------------------ templates (timeline fragments applied by assetlib.apply)
TEMPLATES = {
    "lower_third_basic": {"titles": [{"text": "Name Surname", "subtitle": "Role or place", "style": "lower_third", "start": 0.5, "end": 4.5, "anim": "slide"}]},
    "lower_third_documentary": {"titles": [{"text": "Location", "subtitle": "Date or context", "style": "lower_third", "start": 0.3, "end": 5.0, "anim": "fade"}],
                                "overlay": "overlay.lower_third_bar"},
    "title_card_center": {"insert_color_clip": {"duration": 3.0, "color": "0x0b1020"},
                          "titles": [{"text": "Title", "subtitle": "", "style": "title", "start": 0.2, "end": 2.8, "anim": "zoom"}]},
    "chapter_card": {"insert_color_clip": {"duration": 2.5, "color": "0x000000"},
                     "titles": [{"text": "Chapter 1", "subtitle": "The beginning", "style": "title", "start": 0.2, "end": 2.3, "anim": "fade"}]},
    "quote_card": {"insert_color_clip": {"duration": 4.0, "color": "0x101418"},
                   "titles": [{"text": "“A short quote goes here.”", "subtitle": "Speaker", "style": "title", "start": 0.3, "end": 3.7, "anim": "rise"}]},
    "end_card": {"append_color_clip": {"duration": 4.0, "color": "0x0b1020"},
                 "titles": [{"text": "Thanks for watching", "subtitle": "Subscribe for more", "style": "title", "start": 0.3, "end": 3.8, "anim": "pop"}]},
    "credits_roll": {"append_color_clip": {"duration": 8.0, "color": "0x000000"},
                     "credits": {"lines": ["Directed by", "Your Name", "", "Music", "NEXUS procedural bed"], "speed": 1.0}},
    "social_vertical": {"reframe": {"width": 1080, "height": 1920, "fit": "cover"}, "overlay": "overlay.social_safe_zone",
                        "captions": {"style": "pop", "position": "middle", "words_per_cue": 3}},
}


def template(path: Path, name: str) -> Path:
    if name not in TEMPLATES:
        raise KeyError(f"unknown template {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"template": name, "version": 1, **TEMPLATES[name]}, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


GENERATORS = {"lut": lambda p, a: lut(p, a["look"], int(a.get("size", 33))),
              "overlay": lambda p, a: overlay(p, a["kind"], int(a.get("w", 1920)), int(a.get("h", 1080)),
                                              **{k: v for k, v in a.items() if k not in ("kind", "w", "h")}),
              "sound": lambda p, a: sound(p, a["kind"], a.get("duration"), int(a.get("seed", 0))),
              "ambience": lambda p, a: ambience(p, a["kind"], float(a.get("duration", 30)), int(a.get("seed", 0))),
              "music": lambda p, a: music(p, a["mood"], float(a.get("duration", 60)), int(a.get("seed", 0))),
              "template": lambda p, a: template(p, a["name"])}
