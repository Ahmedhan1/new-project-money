"""Subtitle products and exporters: SRT, WebVTT, ASS (styled, RTL-safe, CJK fonts); full vs condensed captions.

Segmentation reuses the language packs (nexus.speech.packs.segment) when the language has a pack and the matrix
rules (matrix.yaml `subtitles`) otherwise. Limits per language come from the matrix (Netflix guides where verified).
"""
from __future__ import annotations

import re

from . import matrix

RLM = "\u200f"
NO_LINE_START = "，。！？、；：）」』》,.!?;:)"
MIN_DUR, MAX_DUR = 5 / 6, 7.0   # Netflix general requirements (matrix subtitle_sources.netflix_general)
# ASS "Encoding" (Windows charset) per script; libass shapes text itself, players such as VSFilter use it
ASS_ENCODING = {"arabic": 178, "cjk_sc": 134, "cjk_ja": 128, "cjk_kr": 129, "devanagari": 1, "latin": 1}
FILLERS = {"en": ["actually", "basically", "really", "just", "very", "so", "well", "of course"],
           "de": ["eigentlich", "wirklich", "sehr", "also", "ja", "eben"],
           "fr": ["vraiment", "très", "en fait", "donc"], "es": ["realmente", "muy", "pues", "de hecho"]}


def width(text: str, tag: str) -> float:
    """Display width as the guide counts it: Korean/CJK guides count Latin, space and punctuation as 0.5."""
    r = matrix.subtitle_rules(tag)
    lw = float(r.get("latin_weight", 1.0 if r.get("segmentation") == "space" else 0.5))
    if lw == 1.0:
        return float(len(text))
    return sum(lw if (ch.isascii() or ch.isspace()) else 1.0 for ch in text)


def _rules(tag: str) -> dict:
    return matrix.subtitle_rules(tag)


def _wrap(text: str, tag: str) -> list[str]:
    r = _rules(tag)
    cap = r["max_chars_per_line"]
    if r["segmentation"] == "char":
        out, cur = [], ""
        src = text.replace(" ", "") if not re.search(r"[A-Za-z]{2,}", text) else text
        # numbers and Latin words stay whole ("2024", "GPS"); everything else breaks between characters
        for ch in re.findall(r"[A-Za-z0-9][A-Za-z0-9.,'%-]*[A-Za-z0-9%]|[A-Za-z0-9]|.", src):
            # kinsoku: closing punctuation never starts a line (it may overhang the limit by one character)
            if cur and width(cur + ch, tag) > cap and not (ch in NO_LINE_START and width(cur, tag) <= cap):
                out.append(cur)
                cur = ""
            cur += ch
        return out + ([cur] if cur else [])
    return _greedy(text, tag, cap)


def _greedy(text: str, tag: str, cap: float) -> list[str]:
    out, cur = [], ""
    for w in text.split():
        if cur and width(cur + " " + w, tag) > cap:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + ([cur] if cur else [])


def _balanced(text: str, tag: str) -> list[str]:
    """Wrap so that lines are evenly filled (no one-word orphan such as 'gebaut.' in its own cue): find the narrowest
    width that still needs no more lines than plain greedy wrapping at the limit."""
    r = _rules(tag)
    cap = r["max_chars_per_line"]
    if r["segmentation"] == "char":
        return _wrap(text, tag)
    need = len(_greedy(text, tag, cap))
    per = int(r.get("max_lines", 2))
    need = -(-need // per) * per if need > per else need  # fill whole cues
    lo = max(1, int(width(text, tag) / max(1, need)))
    for w in range(lo, cap + 1):
        lines = _greedy(text, tag, w)
        if len(lines) <= need:
            return lines
    return _greedy(text, tag, cap)


def build_cues(lines: list[dict], tag: str) -> list[dict]:
    """lines = [{text, start, end}] -> cues obeying chars/line, lines/cue and reading speed (split by share of text)."""
    from ...speech import packs as lp
    cues: list[dict] = []
    for ln in lines:
        if lp.get(tag) and _rules(tag)["segmentation"] == "char":
            # CJK: packs.segment breaks at CJK punctuation first; then re-wrap to the matrix (guide) limits
            for sp in lp.segment(ln["text"], ln["start"], ln["end"], tag):
                cues += _fit(sp["text"].replace("\n", ""), sp["start"], sp["end"], tag)
        else:
            # spaced scripts: balanced wrapping over the whole line (packs.segment left one-word orphans: "gebaut.")
            cues += _fit(ln["text"], ln["start"], ln["end"], tag)
    return cues


def _fit(text: str, start: float, end: float, tag: str) -> list[dict]:
    r = _rules(tag)
    wrapped = _balanced(text, tag)
    per = int(r.get("max_lines", 2))
    chunks = [wrapped[i:i + per] for i in range(0, len(wrapped), per)] or [[""]]
    total = sum(width("".join(c), tag) for c in chunks) or 1
    out, t = [], start
    for i, c in enumerate(chunks):
        d = (end - start) * width("".join(c), tag) / total
        out.append({"start": round(t, 3), "end": round(t + d - (0.08 if i < len(chunks) - 1 else 0), 3), "text": "\n".join(c)})
        t += d
    return out


def qa(cues: list[dict], tag: str) -> dict:
    """Reading speed, line length, lines per cue, duration bounds, overlap and RTL marks, by the matrix limits."""
    r = _rules(tag)
    f = []
    for i, c in enumerate(cues):
        d = c["end"] - c["start"]
        lines = c["text"].split("\n")
        cps = width(c["text"].replace("\n", ""), tag) / max(0.05, d)
        if cps > r["max_cps"] + 1e-6:
            f.append({"cue": i, "check": "reading_speed", "severity": "major", "detail": f"{cps:.1f} cps > {r['max_cps']}"})
        if any(width(x.rstrip(NO_LINE_START) if r["segmentation"] == "char" else x, tag) > r["max_chars_per_line"] for x in lines):
            f.append({"cue": i, "check": "line_length", "severity": "major", "detail": f"line over {r['max_chars_per_line']}"})
        if len(lines) > r.get("max_lines", 2):
            f.append({"cue": i, "check": "lines_per_cue", "severity": "major", "detail": f"{len(lines)} lines"})
        if d < MIN_DUR - 1e-6:
            f.append({"cue": i, "check": "min_duration", "severity": "minor", "detail": f"{d:.2f}s < 5/6 s"})
        if d > MAX_DUR:
            f.append({"cue": i, "check": "max_duration", "severity": "minor", "detail": f"{d:.2f}s > 7 s"})
        if i and c["start"] < cues[i - 1]["end"] - 1e-3:
            f.append({"cue": i, "check": "overlap", "severity": "major", "detail": "overlaps previous cue"})
    return {"tag": tag, "cues": len(cues), "findings": f, "rules": r,
            "passed": not any(x["severity"] == "major" for x in f)}


# ------------------------------------------------------------------ products
def condensed(cues: list[dict], tag: str, max_share: float = 0.75) -> tuple[list[dict], list[dict]]:
    """Condensed caption product: one line per cue, filler words removed, reading speed held to `max_share` of the
    language's max cps. Cues that still read too fast are returned in `todo` for a human or the router to shorten -
    nothing is cut silently."""
    r = _rules(tag)
    out, todo = [], []
    fill = FILLERS.get(tag.split("-")[0], [])
    for i, c in enumerate(cues):
        txt = c["text"].replace("\n", " " if r["segmentation"] == "space" else "")
        for w in fill:
            txt = re.sub(rf"(?i)(?<!\w){re.escape(w)}(?!\w),?\s*", "", txt)
        txt = re.sub(r"\s{2,}", " ", txt).strip()
        lines = _wrap(txt, tag)
        cps = width(txt, tag) / max(0.05, c["end"] - c["start"])
        item = {"start": c["start"], "end": c["end"], "text": "\n".join(lines[:2]) if len(lines) > 1 else txt}
        if len(lines) > 1 or cps > r["max_cps"] * max_share:
            todo.append({"cue": i, "text": txt, "cps": round(cps, 1), "reason": "needs condensing to one line / slower read"})
        out.append(item)
    return out, todo


# ------------------------------------------------------------------ exporters
def _ts(t: float, sep: str) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _rtl_line(line: str) -> str:
    """RTL safety: start each line with RLM so a line beginning with digits/Latin keeps right-to-left order, and
    isolate Latin runs (packs.isolate_ltr_runs)."""
    from ...speech import packs as lp
    return RLM + lp.isolate_ltr_runs(lp.strip_bidi(line))


def _prep(text: str, tag: str) -> list[str]:
    rtl = _rules(tag)["direction"] == "rtl"
    return [_rtl_line(x) if rtl else x for x in text.split("\n")]


def to_srt(cues: list[dict], tag: str) -> str:
    blocks = [f"{i}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n" + "\n".join(_prep(c["text"], tag))
              for i, c in enumerate(cues, 1)]
    return "\n\n".join(blocks) + "\n"


def to_vtt(cues: list[dict], tag: str) -> str:
    lang = tag
    out = ["WEBVTT", f"Language: {lang}", ""]
    for i, c in enumerate(cues, 1):
        text = "\n".join(_prep(c["text"], tag)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out += [str(i), f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')} line:85%", text, ""]
    return "\n".join(out)


def ass_font(tag: str) -> str:
    from ...speech import packs as lp
    if lp.get(tag):
        return lp.font_family(tag)
    fam = matrix.load()["fonts"].get(matrix.subtitle_rules(tag).get("font", "latin"), "DejaVu Sans")
    return fam.split(" / ")[0].split(" (")[0]


def to_ass(cues: list[dict], tag: str, title: str = "NEXUS", play_res: tuple[int, int] = (1920, 1080)) -> str:
    r = _rules(tag)
    font = ass_font(tag)
    enc = ASS_ENCODING.get(r.get("font", "latin"), 1)
    w, h = play_res
    size = int(h * (0.055 if r["segmentation"] == "char" else 0.05))
    head = [
        "[Script Info]", f"Title: {title} ({tag})", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,"
        f"{int(w * 0.08)},{int(w * 0.08)},{int(h * 0.06)},{enc}", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def ts(t: float) -> str:
        cs = int(round(max(0.0, t) * 100))
        hh, cs = divmod(cs, 360000)
        mm, cs = divmod(cs, 6000)
        ss, cs = divmod(cs, 100)
        return f"{hh}:{mm:02d}:{ss:02d}.{cs:02d}"

    ev = []
    for c in cues:
        # braces start override blocks in ASS: replace with full-width forms so text can never inject styling
        lines = [x.replace("{", "｛").replace("}", "｝").replace("\\", "＼") for x in _prep(c["text"], tag)]
        joined = "\\N".join(lines)
        ev.append(f"Dialogue: 0,{ts(c['start'])},{ts(c['end'])},Default,,0,0,0,,{joined}")
    return "\n".join(head + ev) + "\n"


def export_all(cues: list[dict], tag: str, out_dir, stem: str) -> dict:
    """Write full + condensed SRT/VTT/ASS; returns paths + QA of both products."""
    from pathlib import Path
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    cond, todo = condensed(cues, tag)
    paths = {}
    for product, cs in (("full", cues), ("condensed", cond)):
        for ext, fn in (("srt", to_srt), ("vtt", to_vtt), ("ass", to_ass)):
            p = d / f"{stem}.{tag}.{product}.{ext}"
            p.write_text(fn(cs, tag), encoding="utf-8")
            paths[f"{product}.{ext}"] = str(p)
    return {"paths": paths, "qa_full": qa(cues, tag), "qa_condensed": qa(cond, tag), "condense_todo": todo}
