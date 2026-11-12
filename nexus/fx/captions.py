"""Animated captions as ASS subtitles for libass (burned by FFmpeg's `ass` filter).

Styles (tl["captions"]["style"]):
  classic  the existing look (white bold text, dark outline)       boxed    text on a translucent box
  pop      2-4 words per cue, scale-in pop, current word in colour  karaoke  whole line, words light up as spoken
Word timings come from cue["words"] (programme seconds) when present; otherwise each cue's time is spread over its
words in proportion to their length. Arabic/RTL is shaped by libass; Arabic is never upper-cased.
"""
from __future__ import annotations

from pathlib import Path

STYLES = ("classic", "boxed", "pop", "karaoke")
DEFAULTS = {"style": "classic", "position": "bottom", "words_per_cue": 3, "highlight": "#FFD400", "uppercase": False, "font_scale": 1.0}


def options(tl_captions: dict | None) -> dict | None:
    """Normalised caption options, or None when the timeline has no tl['captions'] (legacy behaviour)."""
    if not isinstance(tl_captions, dict):
        return None
    o = {**DEFAULTS, **{k: v for k, v in tl_captions.items() if v is not None}}
    if o["style"] not in STYLES:
        o["style"] = "classic"
    if o["position"] not in ("bottom", "middle", "top"):
        o["position"] = "bottom"
    try:
        o["words_per_cue"] = min(4, max(2, int(o["words_per_cue"])))
    except (TypeError, ValueError):
        o["words_per_cue"] = 3
    try:
        o["font_scale"] = min(3.0, max(0.3, float(o["font_scale"])))
    except (TypeError, ValueError):
        o["font_scale"] = 1.0
    return o


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> ASS '&HAABBGGRR'."""
    h = str(hex_rgb or "#FFD400").lstrip("#").replace("0x", "")
    if len(h) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
        h = "FFD400"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _t(x: float) -> str:
    cs = int(round(max(0.0, x) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\r", "")


def _is_ar(lang: str) -> bool:
    return str(lang or "en").lower().startswith("ar")


def _font_name(lang: str) -> str:
    """fontconfig family for libass: Noto Naskh Arabic for Arabic; config.font_name_for (LANG-1) for others when present."""
    if _is_ar(lang):
        return "Noto Naskh Arabic"
    from .. import config
    fn = getattr(config, "font_name_for", None)
    if fn is not None:
        try:
            return str(fn(lang)) or "DejaVu Sans"
        except Exception:
            pass
    return "DejaVu Sans"


def _case(s: str, o: dict, lang: str) -> str:
    from ..timeline.text import is_rtl
    return s.upper() if o.get("uppercase") and not _is_ar(lang) and not is_rtl(s) else s


def word_times(cue: dict) -> list[tuple[float, float, str]]:
    """(start, end, word) for every word of the cue, from cue['words'] or spread by word length."""
    s0, e0 = float(cue["start"]), float(cue["end"])
    ws = [w for w in (cue.get("words") or []) if isinstance(w, dict) and str(w.get("word", "")).strip()]
    if ws:
        out = []
        for w in ws:
            a = min(max(float(w.get("start", s0)), s0), e0)
            b = min(max(float(w.get("end", a)), a), e0)
            out.append((a, b, str(w["word"]).strip()))
        return sorted(out, key=lambda x: x[0])
    words = str(cue.get("text", "")).split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    tot = float(sum(weights))
    out, t = [], s0
    for w, k in zip(words, weights):
        d = (e0 - s0) * k / tot
        out.append((t, t + d, w))
        t += d
    return out


def _layout(W: int, H: int, o: dict, style: str) -> dict:
    vertical = H > W
    base = H * 0.048 if not vertical else W * 0.06
    mult = {"classic": 1.0, "boxed": 1.0, "pop": 1.5, "karaoke": 1.1}[style]
    size = max(10, int(base * mult * o["font_scale"]))
    side = int(W * (0.06 if not vertical else 0.09))
    pos = o["position"]
    align = {"bottom": 2, "middle": 5, "top": 8}[pos]
    if pos == "bottom":
        mv = int(H * (0.07 if not vertical else 0.16))  # keep clear of vertical-platform UI
    elif pos == "top":
        mv = int(H * (0.06 if not vertical else 0.12))
    else:
        mv = 0
    return {"size": size, "side": side, "align": align, "mv": mv}


def _header(W: int, H: int, style_line: str) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(cues: list[dict], path: Path, W: int, H: int, lang: str = "en", opts: dict | None = None,
              legacy_style: dict | None = None) -> Path:
    """Write an ASS file for `cues` (programme seconds). opts=None reproduces the pre-FX look exactly."""
    o = options(opts)
    if o is None:
        from ..timeline import subs
        return subs.write_ass(cues, path, W, H, lang, legacy_style)
    style = o["style"]
    L = _layout(W, H, o, style)
    font = _font_name(lang)
    white, black = "&H00FFFFFF", "&H00101010"
    hi = ass_colour(o["highlight"])
    size = L["size"]
    if style == "boxed":
        pad = max(2, int(size * 0.28))
        sl = (f"Style: Default,{font},{size},{white},{white},&H5A000000,&H5A000000,1,0,0,0,100,100,0,0,3,{pad},0,"
              f"{L['align']},{L['side']},{L['side']},{L['mv']},-1")
    elif style == "pop":
        sl = (f"Style: Default,{font},{size},{white},{white},{black},&H80000000,1,0,0,0,100,100,0,0,1,{max(2, int(size * 0.08))},"
              f"{max(1, int(size * 0.04))},{L['align']},{L['side']},{L['side']},{L['mv']},-1")
    elif style == "karaoke":
        sl = (f"Style: Default,{font},{size},{hi},{white},{black},&H96000000,1,0,0,0,100,100,0,0,1,3,1,"
              f"{L['align']},{L['side']},{L['side']},{L['mv']},-1")
    else:  # classic: same values as timeline.subs.write_ass, plus position / scale options
        sl = (f"Style: Default,{font},{size},{white},&H000000FF,{black},&H96000000,1,0,0,0,100,100,0,0,1,3,1,"
              f"{L['align']},{L['side']},{L['side']},{L['mv']},-1")
    lines: list[str] = []
    for c in sorted(cues, key=lambda c: c["start"]):
        if c["end"] <= c["start"] or not str(c.get("text", "")).strip():
            continue
        if style in ("classic", "boxed"):
            txt = _esc(_case(str(c["text"]), o, lang)).replace("\n", "\\N")
            fad = "{\\fad(70,70)}" if style == "boxed" else ""
            lines.append(f"Dialogue: 0,{_t(c['start'])},{_t(c['end'])},Default,,0,0,0,,{fad}{txt}")
            continue
        wt = word_times(c)
        if not wt:
            continue
        words = [_esc(_case(w, o, lang)) for _, _, w in wt]
        if style == "karaoke":
            parts, prev = [], float(c["start"])
            if wt[0][0] - prev >= 0.01:
                parts.append(f"{{\\k{int(round((wt[0][0] - prev) * 100))}}}")
            for i, (a, b, _) in enumerate(wt):
                nxt = wt[i + 1][0] if i + 1 < len(wt) else b
                parts.append(f"{{\\k{max(1, int(round((nxt - a) * 100)))}}}{words[i]}" + (" " if i + 1 < len(wt) else ""))
            lines.append(f"Dialogue: 0,{_t(c['start'])},{_t(c['end'])},Default,,0,0,0,,{''.join(parts)}")
            continue
        n = o["words_per_cue"]
        groups = [list(range(i, min(i + n, len(wt)))) for i in range(0, len(wt), n)]
        if len(groups) > 1 and len(groups[-1]) == 1:  # never leave a single orphan word
            groups[-2].extend(groups.pop())
        for gi, g in enumerate(groups):
            g_start = float(c["start"]) if gi == 0 else wt[g[0]][0]
            g_end = wt[groups[gi + 1][0]][0] if gi + 1 < len(groups) else float(c["end"])
            for j, wi in enumerate(g):
                a = g_start if j == 0 else wt[wi][0]
                b = wt[g[j + 1]][0] if j + 1 < len(g) else g_end
                if b - a < 0.005:
                    continue
                txt = " ".join(f"{{\\1c{hi}}}{words[k]}{{\\1c{white}}}" if k == wi else words[k] for k in g)
                anim = "{\\fscx78\\fscy78\\t(0,90,\\fscx110\\fscy110)\\t(90,170,\\fscx100\\fscy100)}" if j == 0 else ""
                lines.append(f"Dialogue: 0,{_t(a)},{_t(b)},Default,,0,0,0,,{anim}{txt}")
    path.write_text(_header(W, H, sl) + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def cues_in(cues: list[dict], a: float, b: float, pad: float = 0.05) -> list[dict]:
    """Cues that are visible somewhere in programme range [a, b)."""
    return [c for c in cues or [] if c["end"] > a - pad and c["start"] < b + pad]
