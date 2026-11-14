"""Transcription with faster-whisper (CUDA float16 when a GPU works, else CPU int8). Lazy-loaded, unloadable."""
from __future__ import annotations

import gc
import os
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
        path = str(models.ensure_model("whisper-small"))
        _model = _gpu_model(WhisperModel, path) or WhisperModel(path, device="cpu", compute_type="int8",
                                                                  cpu_threads=max(4, min(16, os.cpu_count() or 4)))
    return _model


def _gpu_model(cls, path: str):
    """Same model on a CUDA GPU when one is usable (cloud studio: Kaggle T4), else None. A probe transcription runs first,
    because a missing CUDA/cuDNN library only fails at the first inference; NEXUS_ASR_DEVICE=cpu forces the CPU."""
    if os.environ.get("NEXUS_ASR_DEVICE", "auto").lower() == "cpu":
        return None
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return None
        m = cls(path, device="cuda", compute_type="float16")
        list(m.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
        return m
    except Exception as e:  # noqa: BLE001 - any CUDA problem -> the CPU path, which always works
        print(f"faster-whisper: GPU not usable ({type(e).__name__}: {str(e)[:120]}); using the CPU", flush=True)
        return None


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
    out = []
    for s in segs:
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
