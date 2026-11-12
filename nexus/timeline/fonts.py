"""Glyph-coverage font fallback for burned-in text (titles, lower thirds, credits, captions).

Found on a real render (Account 3 Demo 4): a credits line containing one Arabic name was set entirely in an Arabic-only
font, so every Latin letter came out as an empty box. Text is now split into runs; each run gets the first font in the
fallback chain whose character map really contains its characters (checked with fontTools, not guessed from the
language tag), and runs are shaped and ordered for right-to-left lines. `missing(text)` lists characters no installed
font can draw, so a quality gate can refuse the render instead of shipping boxes."""
from __future__ import annotations

import functools
import os
import unicodedata
from pathlib import Path

_WIN = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
# Bold faces first (titles are bold), regular as a fallback; Linux paths, then Windows, then macOS.
CHAIN = [
    ("latin", ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
               str(_WIN / "segoeuib.ttf"), str(_WIN / "arialbd.ttf"), "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]),
    ("arabic", ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
                str(_WIN / "tahomabd.ttf"), str(_WIN / "segoeuib.ttf")]),
    ("devanagari", ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
                    str(_WIN / "Nirmala.ttc"), str(_WIN / "mangal.ttf")]),
    ("cjk", ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             str(_WIN / "msyhbd.ttc"), str(_WIN / "msyh.ttc"), str(_WIN / "simhei.ttf"), "/System/Library/Fonts/PingFang.ttc"]),
    ("symbols", ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                 str(_WIN / "seguisym.ttf")]),
]
IGNORABLE = {"\u200c", "\u200d", "\u200e", "\u200f", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff"}


def chain() -> list[str]:
    seen, out = set(), []
    for _, files in CHAIN:
        for f in files:
            if f not in seen and Path(f).exists():
                seen.add(f)
                out.append(f)
    return out


@functools.lru_cache(maxsize=32)
def _cmap(path: str) -> frozenset[int]:
    from fontTools.ttLib import TTCollection, TTFont
    if path.lower().endswith(".ttc"):
        fonts = TTCollection(path).fonts
        cps = set()
        cps.update(fonts[0].getBestCmap() or {})
        return frozenset(cps)
    return frozenset(TTFont(path, lazy=True).getBestCmap() or {})


def covers(path: str, ch: str) -> bool:
    return ch in IGNORABLE or ch.isspace() or ord(ch) in _cmap(path)


def _group(name: str) -> list[str]:
    return [f for g, files in CHAIN if g == name for f in files if Path(f).exists()]


def font_for_char(ch: str, preferred: str | None = None) -> str | None:
    """The script's own typeface first (a Latin font that happens to contain basic Arabic letters is not used for
    Arabic text), then the preferred font, then the whole chain."""
    sc = script_of(ch)
    own = _group(sc) if sc in ("arabic", "devanagari", "cjk") else []
    cands = own + ([preferred] if preferred and Path(preferred).exists() else []) + chain()
    return next((f for f in cands if covers(f, ch)), None)


def missing(text: str) -> list[str]:
    """Characters that no installed font in the chain can draw (would render as empty boxes)."""
    return sorted({ch for ch in text if not ch.isspace() and ch not in IGNORABLE and font_for_char(ch) is None})


def script_of(ch: str) -> str:
    o = ord(ch)
    if 0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "arabic"
    if 0x0900 <= o <= 0x097F:
        return "devanagari"
    if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F:
        return "cjk"
    if unicodedata.category(ch)[0] in "LM":
        return "latin"
    return "neutral"


def runs(text: str, preferred: str | None = None) -> list[tuple[str, str, str]]:
    """Split a line into (text, font_path, script) runs in LOGICAL order. Neutral characters (spaces, digits,
    punctuation) join the run before them, so words keep their spacing and digits stay with their script."""
    out: list[list] = []
    for ch in text:
        sc = script_of(ch)
        if sc == "neutral" and out:
            fnt = out[-1][1] if covers(out[-1][1], ch) else (font_for_char(ch, preferred) or out[-1][1])
            if fnt == out[-1][1]:
                out[-1][0] += ch
                continue
        else:
            fnt = font_for_char(ch, preferred) if sc != "latin" or not preferred or not covers(preferred, ch) else preferred
            fnt = fnt or (preferred or chain()[0])
        if out and out[-1][1] == fnt and (out[-1][2] == sc or sc == "neutral"):
            out[-1][0] += ch
        else:
            out.append([ch, fnt, sc if sc != "neutral" else (out[-1][2] if out else "latin")])
    return [(t, f, s) for t, f, s in out]


def _settle_neutrals(rs: list[tuple[str, str, str]], base: str) -> list[tuple[str, str, str]]:
    """Trailing spaces/punctuation of a run whose direction differs from the line's base direction belong to the base
    direction (bidi rule for neutrals between opposite-direction runs): 'by شرف الدين, licensed' keeps ', ' on the
    Latin side instead of letting the Arabic shaper put the comma on the wrong end."""
    out = []
    for i, (t, f, sc) in enumerate(rs):
        foreign = (sc == "arabic") != (base == "arabic")
        stripped = t.rstrip(" ,.;:!?)\"'-–—")
        if foreign and stripped != t and i < len(rs) - 1:
            tail = t[len(stripped):]
            out.append((stripped, f, sc))
            nt, nf, ns = rs[i + 1]
            rs[i + 1] = (tail + nt, nf, ns)
        else:
            out.append((t, f, sc))
    return [r for r in out if r[0]]


def visual_runs(text: str, preferred: str | None = None) -> list[tuple[str, str, str]]:
    """Runs in VISUAL (left-to-right drawing) order. A line whose first strong character is right-to-left is laid out
    right-to-left at the run level; each Arabic run is shaped by the text engine (raqm) with direction=rtl."""
    first = next((script_of(c) for c in text if script_of(c) != "neutral"), "latin")
    rs = _settle_neutrals(runs(text, preferred), "arabic" if first == "arabic" else "latin")
    if first == "arabic":
        rs = list(reversed(rs))
        # spaces that ended a run in logical order sit on the wrong side after reversal
        rs = [(t.strip() + (" " if i < len(rs) - 1 else ""), f, s) if s != "arabic" else (" " + t.strip() if i else t.strip(), f, s)
              for i, (t, f, s) in enumerate(rs)]
    return rs
