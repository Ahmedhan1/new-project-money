"""Subtitle cues: build from transcripts, write SRT / VTT / ASS. Script-aware: Han is split per character
(no spaces, ~16 characters per cue), sentence/clause marks per script come from registry/languages.yaml."""
from __future__ import annotations

import re
from pathlib import Path

HAN_CUE_CHARS = 16
_TERM = ".!?\u061f\u06d4\u2026\u0964\u0965\u3002\uff01\uff1f"     # . ! ? ؟ ۔ … । ॥ 。 ！ ？
_CLAUSE = ",;:\u060c\u061b\uff0c\u3001\uff1b\uff1a"                    # , ; : ، ؛ ， 、 ； ：
_NO_START = set("。，、；：！？）》」』】…,.;:!?)%")


def _is_han(text: str) -> bool:
    han = sum("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    return letters > 0 and han / letters > 0.5


def _limit(text: str, max_chars: int | None, lang: str | None) -> tuple[bool, int]:
    """(unspaced Han?, max characters per cue) for this text."""
    han = _is_han(text) if lang is None or lang == "en" else (lang.split("-")[0] in ("zh", "ja") or _is_han(text))
    if max_chars is None:
        max_chars = HAN_CUE_CHARS if han else 42
    elif han and max_chars > 2 * HAN_CUE_CHARS:   # a Latin-sized limit makes unreadable Han cues
        max_chars = HAN_CUE_CHARS
    return han, max_chars


def _units(text: str, han: bool) -> list[str]:
    if not han:
        return text.split()
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9.,%:+\-]*|\S", text)


def _ts(t: float, sep: str = ",") -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{int(round((t - int(t)) * 1000)) % 1000:03d}"


def cues_from_transcript(segments: list[dict], max_chars: int | None = None, max_dur: float = 5.0, lang: str | None = None) -> list[dict]:
    """Split whisper segments into readable cues using word timestamps (Han: joined without spaces)."""
    cues: list[dict] = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            cues.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
            continue
        han, limit = _limit(seg.get("text") or "".join(w["word"] for w in words), max_chars, lang)
        sep = "" if han else " "
        cur: list[dict] = []
        for w in words:
            trial = sep.join(x["word"].strip() for x in cur + [w])
            if cur and (len(trial) > limit or w["end"] - cur[0]["start"] > max_dur) and not (han and w["word"].strip()[:1] in _NO_START):
                cues.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": sep.join(x["word"].strip() for x in cur)})
                cur = []
            cur.append(w)
        if cur:
            cues.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": sep.join(x["word"].strip() for x in cur)})
    return fix_timing(cues)


def cues_from_text(text: str, start: float, end: float, max_chars: int | None = None, lang: str | None = None) -> list[dict]:
    """Spread a known narration text over [start,end] proportionally to length (characters; Han per character)."""
    han, limit = _limit(text, max_chars, lang)
    sep = "" if han else " "
    chunks, cur = [], ""
    for w in _units(text, han):
        if cur and len(cur) + len(sep) + len(w) > limit and not (han and w in _NO_START):
            chunks.append(cur)
            cur = ""
        cur = f"{cur}{sep}{w}".strip() if not han else cur + w
        # Break where a viewer expects it: a sentence never shares a cue with the start of the next one,
        # and a long cue ends at a clause boundary rather than mid-phrase.
        if (cur[-1] in _TERM and len(cur) >= limit * 0.35) or (cur[-1] in _CLAUSE and len(cur) >= limit * 0.7):
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    total = sum(len(c) for c in chunks) or 1
    t, cues = start, []
    for c in chunks:
        d = (end - start) * len(c) / total
        cues.append({"start": round(t, 3), "end": round(t + d, 3), "text": c})
        t += d
    return cues


def fix_timing(cues: list[dict], min_dur: float = 0.7, gap: float = 0.04) -> list[dict]:
    cues = sorted((dict(c) for c in cues if c["text"].strip()), key=lambda c: c["start"])
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        if c["end"] - c["start"] < min_dur:
            c["end"] = c["start"] + min_dur
        if nxt is not None and c["end"] > nxt - gap:
            c["end"] = max(c["start"] + 0.2, nxt - gap)
    return cues


def shift(cues: list[dict], offset: float) -> list[dict]:
    return [{**c, "start": c["start"] + offset, "end": c["end"] + offset} for c in cues]


def write_srt(cues: list[dict], path: Path) -> Path:
    path.write_text("\n".join(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n" for i, c in enumerate(cues, 1)),
                    encoding="utf-8")
    return path


def write_vtt(cues: list[dict], path: Path) -> Path:
    path.write_text("WEBVTT\n\n" + "\n".join(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}\n{c['text']}\n" for c in cues),
                    encoding="utf-8")
    return path


def write_ass(cues: list[dict], path: Path, W: int, H: int, lang: str = "en", style: dict | None = None) -> Path:
    st = {"size": 0.048, "margin_v": 0.07, "outline": 3, "primary": "&H00FFFFFF", "back": "&H96000000", **(style or {})}
    from .. import config, models
    sample = " ".join(c["text"] for c in cues[:20])
    # The cue text decides when it disagrees with `lang` (e.g. Han subtitles written with the default lang="en").
    font = config.font_name_for(lang if models.script_of(sample) == models.script_for(lang) or not sample.strip() else models.script_of(sample))
    size = int(H * st["size"]) if W >= H else int(W * st["size"] * 1.25)
    mv = int(H * st["margin_v"]) if W >= H else int(H * 0.16)  # keep clear of vertical-platform UI
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{st['primary']},&H000000FF,&H00101010,{st['back']},1,0,0,0,100,100,0,0,1,{st['outline']},1,2,{int(W*0.06)},{int(W*0.06)},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def t(x: float) -> str:
        x = max(0.0, x)
        return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"

    lines = [f"Dialogue: 0,{t(c['start'])},{t(c['end'])},Default,,0,0,0,,{c['text'].replace(chr(10), ' ')}" for c in cues]
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return path
