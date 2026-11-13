"""Subtitle cues: build from transcripts, write SRT / VTT / ASS."""
from __future__ import annotations

from pathlib import Path


def _ts(t: float, sep: str = ",") -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{int(round((t - int(t)) * 1000)) % 1000:03d}"


def join_words(words) -> str:
    """Join word tokens; Whisper's split tokens such as "160 -meter" are glued back ("160-meter")."""
    out = ""
    for w in words:
        t = str(w["word"] if isinstance(w, dict) else w).strip()
        if not t:
            continue
        if out and (t.startswith("-") and out[-1].isalnum() or t in (",", ".", "?", "!", ";", ":")):
            out += t
        else:
            out = f"{out} {t}" if out else t
    return out


SENT_END = ".!?\u061f\u06d4\u2026\u3002\uff01\uff1f"


def cues_from_transcript(segments: list[dict], max_chars: int = 42, max_dur: float = 5.0,
                         breaks: list[float] | None = None, pause: float = 0.6) -> list[dict]:
    """Split whisper segments into readable cues using word timestamps.

    A cue also ends at a pause (> `pause` s), at a programme cut in `breaks` (a caption never spans a shot change or a
    title card), and after a sentence end once it holds a few words (a sentence never shares a cue with the next one)."""
    cues: list[dict] = []
    bk = sorted(breaks or [])

    def crosses(a: float, b: float) -> bool:
        return any(a < t <= b for t in bk)

    for seg in segments:
        words = seg.get("words") or []
        if not words:
            cues.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
            continue
        cur: list[dict] = []
        for w in words:
            if cur:
                trial = join_words(cur + [w])
                prev = cur[-1]
                text = join_words(cur)
                end_sent = text[-1:] in SENT_END and len(text) >= max_chars * 0.35
                clause = text[-1:] in ",;:\u060c" and len(text) >= max_chars * 0.7
                if (len(trial) > max_chars or w["end"] - cur[0]["start"] > max_dur or w["start"] - prev["end"] > pause
                        or crosses(prev["end"], w["start"] + 1e-3) or end_sent or clause):
                    cues.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text})
                    cur = []
            cur.append(w)
        if cur:
            cues.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": join_words(cur)})
    return fix_timing(cues, breaks=bk)


def cues_from_text(text: str, start: float, end: float, max_chars: int = 42) -> list[dict]:
    """Spread a known narration text over [start,end] proportionally to length."""
    words = text.split()
    chunks, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {w}".strip()
        # Break where a viewer expects it: a sentence never shares a cue with the start of the next one,
        # and a long cue ends at a clause boundary rather than mid-phrase.
        if (cur[-1] in ".!?\u061f\u06d4\u2026" and len(cur) >= max_chars * 0.35) or (cur[-1] in ",;:\u060c" and len(cur) >= max_chars * 0.7):
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


def fix_timing(cues: list[dict], min_dur: float = 0.7, gap: float = 0.04, linger: float = 0.5, target_cps: float = 17.0,
               breaks: list[float] | None = None) -> list[dict]:
    """Minimum duration, no overlaps, and reading time: a cue faster than `target_cps` may stay up to `linger` s into
    the following silence (never past the next cue or a cut in `breaks`)."""
    cues = sorted((dict(c) for c in cues if c["text"].strip()), key=lambda c: c["start"])
    bk = sorted(breaks or [])
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        d = c["end"] - c["start"]
        if d > 0 and len(c["text"]) / d > target_cps:
            want = c["start"] + len(c["text"]) / target_cps
            limit = min([t for t in bk if t > c["end"] + 1e-3] or [c["end"] + linger])
            c["end"] = max(c["end"], min(want, c["end"] + linger, limit))
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
    font = "Noto Naskh Arabic" if lang.startswith("ar") else "DejaVu Sans"
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
