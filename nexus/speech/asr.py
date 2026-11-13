"""Transcription with faster-whisper (CPU int8). Lazy-loaded, unloadable."""
from __future__ import annotations

import gc
import subprocess
import time
from pathlib import Path

import numpy as np

from .. import costguard, db, models

_model = None


def load():
    global _model
    if _model is None:
        costguard.authorize("faster-whisper", "load")
        from faster_whisper import WhisperModel
        _model = WhisperModel(str(models.ensure_model("whisper-small")), device="cpu", compute_type="int8", cpu_threads=4)
    return _model


def unload() -> None:
    global _model
    _model = None
    gc.collect()


def merge_split_tokens(words: list[dict]) -> list[dict]:
    """Whisper splits hyphenated tokens ('160', '-meter'; 'T', '-38'): merge them into one word spanning both."""
    out: list[dict] = []
    for w in words:
        t = str(w.get("word", "")).strip()
        if out and t.startswith("-") and len(t) > 1 and str(out[-1]["word"])[-1:].isalnum():
            out[-1] = {**out[-1], "word": out[-1]["word"] + t, "end": w["end"]}
        else:
            out.append(dict(w))
    return out


def normalize_segment(s: dict) -> dict:
    """Merge split hyphen tokens in a segment's words and text ('160 -meter' -> '160-meter')."""
    import re
    s = dict(s)
    if s.get("words"):
        ws = merge_split_tokens(s["words"])
        prev_end = float(s.get("start", 0)) if ws else 0.0
        for w in ws:  # zero-length words (Whisper gives some) borrow up to 120 ms from the gap before them
            if float(w["end"]) - float(w["start"]) < 0.02:
                w["start"] = round(max(prev_end, float(w["end"]) - 0.12), 3)
                if float(w["end"]) - float(w["start"]) < 0.02:
                    w["end"] = round(float(w["start"]) + 0.08, 3)
            prev_end = float(w["end"])
        s["words"] = ws
    s["text"] = re.sub(r"(?<=[A-Za-z0-9]) -(?=[A-Za-z0-9])", "-", s.get("text", ""))
    return s


def transcribe(path: str | Path, language: str | None = None, words: bool = True) -> dict:
    """Returns {language, duration, segments:[{start,end,text,words:[{start,end,word}]}]}."""
    costguard.authorize("faster-whisper", "transcribe")
    t0 = time.time()
    # Decode with the FFmpeg binary ourselves: independent of the PyAV version faster-whisper ships against.
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    segs, info = load().transcribe(audio, language=language, word_timestamps=words, vad_filter=True,
                                   beam_size=5, condition_on_previous_text=False)
    from .. import jobs
    out = []
    total = info.duration or 0
    jobs.note(f"transcribing speech: 0 / {total:.0f} s of audio", None)
    for s in segs:
        if total:
            jobs.note(f"transcribing speech: {s.end:.0f} / {total:.0f} s of audio")
        out.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                    "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                              for w in (s.words or [])]})
    out = [normalize_segment(s) for s in out]
    elapsed = time.time() - t0
    if info.duration:
        db.metric("asr_rtf", elapsed / info.duration)  # <1 means faster than realtime
    return {"language": info.language, "language_probability": round(info.language_probability, 3),
            "duration": info.duration, "segments": out, "elapsed": round(elapsed, 2),
            "text": " ".join(s["text"] for s in out)}
