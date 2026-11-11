"""Motion graphics rendered frame-by-frame with Pillow and piped into FFmpeg (no intermediate image files)."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .. import config, costguard
from ..timeline import text as ttext

BG = (11, 16, 32)
ACCENTS = [(0, 190, 255), (255, 176, 32), (120, 220, 130), (240, 96, 120), (170, 130, 255), (255, 255, 255)]


def ease(t: float) -> float:
    """easeOutCubic"""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def _encode(frames, out: Path, W: int, H: int, fps: int) -> Path:
    costguard.authorize("pillow-motion", "render")
    costguard.authorize("ffmpeg", "encode-frames")
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", *config.VCODEC, "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    for im in frames:
        p.stdin.write(im.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg failed encoding motion graphics")
    return out


def _bg(W: int, H: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y in range(0, H, 2):  # subtle vertical gradient
        c = int(18 * y / H)
        d.line([(0, y), (W, y + 1)], fill=(BG[0] + c // 2, BG[1] + c, BG[2] + c * 2))
    return img


def bar_chart(out: Path, title: str, data: list[tuple[str, float]], unit: str = "", source: str = "", duration: float = 6.0,
              size=(1280, 720), fps: int = config.FPS, lang: str = "en") -> Path:
    """Animated infographic: bars grow one after another, values count up. `data` is the single source of truth."""
    W, H = size
    n = int(duration * fps)
    vmax = max(v for _, v in data) or 1.0
    base = _bg(W, H)
    f_title, f_lab, f_val, f_src = ttext.font(int(H * 0.07), lang, title), ttext.font(int(H * 0.04), lang), ttext.font(int(H * 0.05)), ttext.font(int(H * 0.026))
    left, right, top, bottom = int(W * 0.26), int(W * 0.72), int(H * 0.26), int(H * 0.86)  # room for value labels
    row = (bottom - top) / len(data)

    def frames():
        for i in range(n):
            t = i / fps
            img = base.copy()
            d = ImageDraw.Draw(img)
            tt, kw = ttext._prep(title)
            a = ease(t / 0.6)
            d.text((int(W * 0.06), int(H * 0.09 - 20 * (1 - a))), tt, font=f_title, fill=tuple(int(255 * a) for _ in range(3)), **kw)
            d.rectangle([int(W * 0.06), int(H * 0.19), int(W * 0.06 + W * 0.12 * a), int(H * 0.19) + 5], fill=ACCENTS[0])
            for k, (label, value) in enumerate(data):
                g = ease((t - 0.5 - k * 0.35) / 1.1)  # staggered growth
                y0 = top + k * row + row * 0.18
                y1 = top + (k + 1) * row - row * 0.18
                lt, lkw = ttext._prep(label)
                lw = d.textlength(lt, font=f_lab, **lkw)
                d.text((left - 18 - lw, (y0 + y1) / 2 - H * 0.025), lt, font=f_lab, fill=(215, 222, 235), **lkw)
                d.rounded_rectangle([left, y0, right, y1], radius=8, fill=(26, 34, 58))
                if g > 0:
                    x1 = left + (right - left) * (value / vmax) * g
                    d.rounded_rectangle([left, y0, max(left + 10, x1), y1], radius=8, fill=ACCENTS[k % len(ACCENTS)])
                    val = value * g
                    txt = f"{val:,.0f}{unit}" if vmax >= 20 else f"{val:,.1f}{unit}"
                    d.text((max(left + 10, x1) + 14, (y0 + y1) / 2 - H * 0.032), txt, font=f_val, fill=(255, 255, 255))
            if source:
                d.text((int(W * 0.06), int(H * 0.93)), f"Source: {source}", font=f_src, fill=(140, 150, 170))
            yield img

    return _encode(frames(), out, W, H, fps)


def kinetic_text(out: Path, lines: list[str], duration: float | None = None, size=(1280, 720), fps: int = config.FPS, lang: str = "en") -> Path:
    """Kinetic typography: each line slides up and scales in, the previous line dims."""
    W, H = size
    per = 1.6
    duration = duration or per * len(lines) + 0.8
    n = int(duration * fps)
    base = _bg(W, H)

    def frames():
        for i in range(n):
            t = i / fps
            img = base.copy()
            d = ImageDraw.Draw(img)
            k = min(len(lines) - 1, int(t / per))
            a = ease((t - k * per) / 0.5)
            for j in range(max(0, k - 1), k + 1):
                cur = j == k
                size_px = int(H * (0.085 + 0.035 * (a if cur else 0)))
                f = ttext.font(size_px, lang, lines[j])
                wrapped = ttext.wrap(d, lines[j], f, int(W * 0.84))
                y = H * 0.5 - (len(wrapped) * size_px * 1.2) / 2 + ((1 - a) * H * 0.12 if cur else -H * 0.22 * a - 0)
                alpha = a if cur else max(0.0, 0.35 * (1 - a))
                col = tuple(int(BG[c] + ((255, 255, 255)[c] - BG[c]) * alpha) for c in range(3))
                for line in wrapped:
                    lt, kw = ttext._prep(line)
                    w = d.textlength(lt, font=f, **kw)
                    d.text(((W - w) / 2, y), lt, font=f, fill=col, **kw)
                    y += size_px * 1.2
            bar = ease(t / duration)
            d.rectangle([0, H - 6, int(W * bar), H], fill=ACCENTS[k % len(ACCENTS)])
            yield img

    return _encode(frames(), out, W, H, fps)


def timeline_animation(out: Path, title: str, events: list[tuple[str, str]], duration: float = 7.0, size=(1280, 720), fps: int = config.FPS) -> Path:
    """Documentary-style timeline: a line draws across the screen and dated events pop in."""
    W, H = size
    n = int(duration * fps)
    base = _bg(W, H)
    f_t, f_y, f_e = ttext.font(int(H * 0.065)), ttext.font(int(H * 0.05)), ttext.font(int(H * 0.032))
    x0, x1, y = int(W * 0.08), int(W * 0.92), int(H * 0.55)

    def frames():
        for i in range(n):
            t = i / fps
            img = base.copy()
            d = ImageDraw.Draw(img)
            d.text((x0, int(H * 0.1)), title, font=f_t, fill=(255, 255, 255))
            prog = ease(t / (duration * 0.75))
            d.line([(x0, y), (x0 + (x1 - x0) * prog, y)], fill=ACCENTS[0], width=6)
            for k, (year, label) in enumerate(events):
                pos = (k + 0.5) / len(events)
                if prog < pos:
                    continue
                a = ease((prog - pos) * 6)
                cx = x0 + (x1 - x0) * pos
                r = int(14 * a)
                d.ellipse([cx - r, y - r, cx + r, y + r], fill=ACCENTS[(k + 1) % len(ACCENTS)])
                up = k % 2 == 0
                yw = d.textlength(year, font=f_y)
                d.text((cx - yw / 2, y - H * 0.13 if up else y + H * 0.05), year, font=f_y, fill=tuple(int(255 * a) for _ in range(3)))
                for li, line in enumerate(ttext.wrap(d, label, f_e, int(W * 0.8 / len(events)))[:3]):
                    lw = d.textlength(line, font=f_e)
                    yy = (y - H * 0.13 - (3 - li) * H * 0.042) if up else (y + H * 0.115 + li * H * 0.042)
                    d.text((cx - lw / 2, yy), line, font=f_e, fill=tuple(int(c * a) for c in (200, 208, 224)))
            yield img

    return _encode(frames(), out, W, H, fps)
