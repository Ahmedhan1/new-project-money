"""Lower thirds, animated titles and credits rolls: Pillow-drawn transparent PNGs (via timeline/text.py, so Arabic
shaping/RTL and font fallback are shared with every other overlay) animated by FFmpeg overlay expressions.

clip["titles"] = [{"text", "subtitle"?, "style": lower_third|title|subtitle|callout|kinetic|caption,
                   "anim": fade|slide|rise|none, "from": left|right (slide), "start", "end", "lang"?}]
clip type "credits": {"type": "credits", "duration", "title"?, "lines": [str], "color"?: "#RRGGBB"}
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .. import config
from ..timeline import text as ttext

ANIMS = ("fade", "slide", "rise", "none")


def _f(d: dict, k: str, default: float) -> float:
    try:
        v = float(d.get(k, default))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def title_png(t: dict, W: int, H: int, lang: str) -> Path:
    """Full-frame transparent PNG of one title (main line + optional smaller second line under it)."""
    from PIL import Image, ImageDraw
    style = t.get("style") if t.get("style") in ttext.STYLES else "lower_third"
    main, sub = str(t.get("text", "")), str(t.get("subtitle", "") or "")
    lang = t.get("lang", lang)
    key = hashlib.sha1(f"title|{main}|{sub}|{style}|{W}x{H}|{lang}|v1".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"title_{key}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ttext.draw_text_block(img, main, style, lang)
    if sub.strip():
        rel, y_anchor, box, _colour, _bar = ttext.STYLES[style]
        dr = ImageDraw.Draw(img)
        size_main = max(14, int(min(W, H * 16 / 9) * rel))
        fnt = ttext.font(max(10, int(size_main * 0.62)), lang, sub)
        s, kw = ttext._prep(sub)
        tw = dr.textlength(s, font=fnt, **kw)
        asc, desc = fnt.getmetrics()
        y = int(H * y_anchor + size_main * 0.95)
        rtl = ttext.is_rtl(sub)
        if style == "lower_third":
            x = W - int(W * 0.06) - tw if rtl else int(W * 0.06)
            pad = int((asc + desc) * 0.3)
            dr.rounded_rectangle([x - pad, y - pad // 2, x + tw + pad, y + asc + desc + pad // 2], radius=pad // 2,
                                 fill=(0, 190, 255, 225))
            dr.text((x, y), s, font=fnt, fill=(10, 14, 24, 255), **kw)
        else:
            x = (W - tw) / 2
            dr.text((x + 2, y + 2), s, font=fnt, fill=(0, 0, 0, 160), **kw)
            dr.text((x, y), s, font=fnt, fill=(225, 232, 244, 255), **kw)
    img.save(out)
    return out


def titles(c: dict) -> list[dict]:
    return [t for t in (c.get("titles") or []) if isinstance(t, dict) and str(t.get("text", "")).strip()]


def title_graph(c: dict, W: int, H: int, fps: int, dur: float, lang: str, n_in: int, src: str, tag: str = "ti") -> tuple[list, list[str], str]:
    """Inputs + filter lines overlaying every animated title. Returns (args, lines, output label)."""
    args: list = []
    lines: list[str] = []
    cur = src
    for k, t in enumerate(titles(c)):
        png = title_png(t, W, H, lang)
        st = min(max(0.0, _f(t, "start", 0.0)), dur)
        en = min(max(st + 0.1, _f(t, "end", min(dur, st + 4.0))), dur)
        if en - st < 0.05:
            continue
        anim = t.get("anim") if t.get("anim") in ANIMS else "slide" if t.get("style", "lower_third") == "lower_third" else "fade"
        d = min(0.45, max(0.08, (en - st) / 4))
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
        chain = "format=rgba"
        if anim != "none":
            chain += f",fade=t=in:st={st:.3f}:d={d:.3f}:alpha=1,fade=t=out:st={en - d:.3f}:d={d:.3f}:alpha=1"
        lines.append(f"[{n_in}:v]{chain}[{tag}p{k}]")
        # eased in/out progress p in [0,1]: 0 = off-screen position, 1 = resting position
        p_in = f"clip((t-{st:.3f})/{d:.3f},0,1)"
        p_out = f"clip(({en:.3f}-t)/{d:.3f},0,1)"
        p = f"(1-pow(1-min({p_in},{p_out}),3))"
        x, y = "0", "0"
        if anim == "slide":
            sign = 1 if t.get("from") == "right" else -1
            x = f"'{sign}*{int(W * 0.5)}*(1-{p})'"
        elif anim == "rise":
            y = f"'{int(H * 0.06)}*(1-{p})'"
        lines.append(f"{cur}[{tag}p{k}]overlay=x={x}:y={y}:eval=frame:enable='between(t,{st:.3f},{en:.3f})'[{tag}o{k}]")
        cur = f"[{tag}o{k}]"
        n_in += 1
    return args, lines, cur


def credits_png(c: dict, W: int, lang: str) -> Path:
    """Tall transparent PNG with the credits text, centred."""
    from PIL import Image, ImageDraw
    title = str(c.get("title", "") or "")
    lines = [str(x) for x in (c.get("lines") or [])]
    if not lines and c.get("text"):
        lines = str(c["text"]).split("\n")
    key = hashlib.sha1(f"credits|{title}|{lines}|{W}|{lang}|v1".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"credits_{key}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    base = max(12, int(W * 0.03))
    ft = ttext.font(int(base * 1.6), lang, title)
    fb = ttext.font(base, lang, " ".join(lines))
    lh = int(base * 1.7)
    h = int(base * 3.2) * bool(title) + lh * max(1, len(lines)) + base
    img = Image.new("RGBA", (W, max(2, h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    y = 0
    if title:
        s, kw = ttext._prep(title)
        dr.text(((W - dr.textlength(s, font=ft, **kw)) / 2, y), s, font=ft, fill=(255, 255, 255, 255), **kw)
        y += int(base * 3.2)
    for ln in lines:
        if ln.strip():
            s, kw = ttext._prep(ln)
            dr.text(((W - dr.textlength(s, font=fb, **kw)) / 2, y), s, font=fb, fill=(228, 234, 244, 255), **kw)
        y += lh
    img.save(out)
    return out


def credits_lines(c: dict, W: int, H: int, dur: float, lang: str, n_in: int, src: str, tag: str = "cr") -> tuple[list, list[str], str]:
    """Roll the credits from below the frame to above it over the clip's duration."""
    png = credits_png(c, W, lang)
    from PIL import Image
    with Image.open(png) as im:
        ph = im.size[1]
    args = ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
    y = f"'{H}-(t/{max(dur, 0.1):.4f})*({H}+{ph})'"
    return args, [f"[{n_in}:v]format=rgba[{tag}p]", f"{src}[{tag}p]overlay=x=0:y={y}:eval=frame:shortest=1[{tag}o]"], f"[{tag}o]"
