"""Text rendering to transparent PNG overlays (Pillow). Script-aware fonts (registry/languages.yaml), complex shaping
through Raqm (Arabic joining/RTL, Devanagari conjuncts and matras), Han wrapping without spaces."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from .. import config, models

_RAQM = features.check("raqm")
# Han may not start a line with closing punctuation or end one with an opening bracket (basic kinsoku rules).
_NO_LINE_START = set("。，、；：！？）》」』】〕…·,.;:!?)%")
_NO_LINE_END = set("（《「『【〔(")


def is_rtl(text: str) -> bool:
    return any("\u0590" <= ch <= "\u08ff" or "\ufb1d" <= ch <= "\ufefc" for ch in text)


def script_of(text: str, lang: str = "en") -> str:
    """Script used to choose font and shaping: the text's own dominant script, else the language's."""
    return models.script_of(text) if any(ch.isalpha() for ch in text) else models.script_for(lang)


def _prep(text: str) -> tuple[str, dict]:
    """Return text + ImageDraw kwargs so that complex scripts render correctly."""
    if is_rtl(text):
        if _RAQM:
            return text, {"direction": "rtl", "language": "ar"}
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text)), {}
    if _RAQM and any("\u0900" <= ch <= "\u097f" for ch in text):
        return text, {"language": "hi"}   # Raqm/HarfBuzz forms conjuncts and reorders matras (no Raqm: unshaped - see report)
    return text, {}


def font(size: int, lang: str = "en", text: str = "") -> ImageFont.FreeTypeFont:
    path, index, _family = config.font_face_for(script_of(text, lang) if text else lang)
    kw = {"layout_engine": ImageFont.Layout.RAQM} if _RAQM else {}
    return ImageFont.truetype(path, size, index=index, **kw)


def _han_tokens(text: str) -> list[str]:
    """Break units for unspaced text: single Han characters, but Latin words / numbers stay whole."""
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9.,%:+\-]*|\s+|.", text)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        han = models.script_of(para) == "han"
        units = _han_tokens(para) if han else para.split()
        sep = "" if han else " "
        cur = ""
        for u in units:
            if han and u.isspace():
                u = " "
            trial = (cur + sep + u) if cur else u.strip()
            t, kw = _prep(trial)
            fits = draw.textlength(t, font=fnt, **kw) <= max_w
            if fits or not cur or (han and (u in _NO_LINE_START or cur[-1] in _NO_LINE_END)):
                cur = trial
            else:
                lines.append(cur.rstrip())
                cur = u.strip()
        lines.append(cur)
    return lines


STYLES = {
    # name: (relative font size, y anchor 0..1, box fill RGBA or None, text colour, accent bar)
    "title": (0.085, 0.42, None, (255, 255, 255, 255), False),
    "subtitle": (0.042, 0.62, None, (220, 228, 240, 255), False),
    "lower_third": (0.032, 0.74, (12, 18, 32, 215), (255, 255, 255, 255), True),
    "callout": (0.045, 0.14, (255, 196, 0, 235), (20, 20, 20, 255), False),
    "caption": (0.050, 0.84, (0, 0, 0, 150), (255, 255, 255, 255), False),
    "kinetic": (0.110, 0.45, None, (255, 255, 255, 255), False),
}


def draw_text_block(img: Image.Image, text: str, style: str = "title", lang: str = "en",
                    accent=(0, 190, 255, 255)) -> None:
    W, H = img.size
    rel, y_anchor, box, colour, bar = STYLES.get(style, STYLES["title"])
    draw = ImageDraw.Draw(img)
    fnt = font(max(14, int(min(W, H * 16 / 9) * rel)), lang, text)
    lines = wrap(draw, text, fnt, int(W * 0.84))
    asc, desc = fnt.getmetrics()
    lh = int((asc + desc) * 1.18)
    total_h = lh * len(lines)
    y = int(H * y_anchor - total_h / 2)
    prepped = [_prep(l) for l in lines]
    widths = [draw.textlength(t, font=fnt, **kw) for t, kw in prepped]
    bw = int(max(widths)) if widths else 0
    pad = int(lh * 0.35)
    rtl = is_rtl(text)
    left_aligned = style == "lower_third"
    if left_aligned:
        x0 = W - int(W * 0.06) - bw if rtl else int(W * 0.06)
    else:
        x0 = (W - bw) // 2
    if box:
        draw.rounded_rectangle([x0 - pad, y - pad, x0 + bw + pad, y + total_h + pad], radius=pad // 2, fill=box)
    if bar:
        bx = x0 + bw + pad if rtl else x0 - pad - 8
        draw.rectangle([bx, y - pad, bx + 8, y + total_h + pad], fill=accent)
    for (t, kw), w in zip(prepped, widths):
        if left_aligned:
            x = x0 + (bw - w if rtl else 0)
        else:
            x = (W - w) / 2
        if not box:  # soft shadow for legibility over footage
            draw.text((x + 3, y + 3), t, font=fnt, fill=(0, 0, 0, 170), **kw)
        draw.text((x, y), t, font=fnt, fill=colour, **kw)
        y += lh


def overlay_png(text: str, style: str, W: int, H: int, lang: str = "en") -> Path:
    key = hashlib.sha1(f"{text}|{style}|{W}x{H}|{lang}".encode()).hexdigest()[:16]
    out = config.CACHE / "overlays" / f"{key}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_text_block(img, text, style, lang)
        img.save(out)
    return out
