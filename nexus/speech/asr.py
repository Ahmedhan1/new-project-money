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


def whisper_code(lang: str | None) -> str | None:
    """Whisper language code for a registry language or alias ('ar-EG' -> 'ar', 'zh' -> 'zh'); None = auto-detect."""
    if not lang:
        return None
    e = models.language(lang, strict=False)
    return e["whisper"] if e else lang.split("-")[0].lower()


def _prompt(lang: str | None) -> str | None:
    e = models.language(lang, strict=False) if lang else None
    return (e or {}).get("whisper_prompt")   # zh: biases Whisper to Simplified characters instead of Traditional


def _audio(path: str | Path, seconds: float | None = None) -> np.ndarray:
    # Decode with the FFmpeg binary ourselves: independent of the PyAV version faster-whisper ships against.
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000"]
    if seconds:
        cmd += ["-t", str(seconds)]
    raw = subprocess.run(cmd + ["-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def transcribe(path: str | Path, language: str | None = None, words: bool = True, initial_prompt: str | None = None) -> dict:
    """Returns {language, duration, segments:[{start,end,text,words:[{start,end,word}]}]}.
    `language` may be any registry code or alias; it is mapped to Whisper's code."""
    costguard.authorize("faster-whisper", "transcribe")
    t0 = time.time()
    audio = _audio(path)
    code = whisper_code(language)
    segs, info = load().transcribe(audio, language=code, word_timestamps=words, vad_filter=True,
                                   beam_size=5, condition_on_previous_text=False,
                                   initial_prompt=initial_prompt or _prompt(language))
    out = []
    for s in segs:
        out.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                    "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                              for w in (s.words or [])]})
    elapsed = time.time() - t0
    if info.duration:
        db.metric("asr_rtf", elapsed / info.duration)  # <1 means faster than realtime
    sep = "" if code in ("zh", "ja") else " "
    return {"language": info.language, "language_probability": round(info.language_probability, 3),
            "duration": info.duration, "segments": out, "elapsed": round(elapsed, 2),
            "text": sep.join(s["text"] for s in out)}


def detect_language(path: str | Path, seconds: float = 30.0) -> dict:
    """Spoken language of a file: {whisper, code (registry code or None), probability, top: [(whisper code, p), ...]}.
    Whisper cannot tell Arabic dialects apart: 'ar' maps to the MSA entry and `dialect_unknown` is set."""
    costguard.authorize("faster-whisper", "language-detect")
    audio = _audio(path, seconds)
    lang, prob, all_probs = load().detect_language(audio, vad_filter=True)
    return {"whisper": lang, "code": registry_code_for_whisper(lang), "probability": round(float(prob), 3),
            "top": [(c, round(float(p), 3)) for c, p in (all_probs or [])[:5]], "dialect_unknown": lang == "ar"}


def registry_code_for_whisper(whisper: str) -> str | None:
    """First registry language (priority order) whose Whisper code matches; Arabic -> 'ar' (MSA)."""
    if whisper == "ar":
        return "ar"
    return next((c for c, e in models.languages().items() if e["whisper"] == whisper), None)


def same_language(source: str, target: str) -> bool:
    """True when translating `source` -> `target` would be a no-op. Arabic varieties are NOT the same language for
    this purpose (MSA -> Egyptian is a real adaptation), so the comparison is on registry codes, not Whisper codes."""
    a, b = models.resolve_lang(source) or source, models.resolve_lang(target) or target
    return a == b


def check_translation_pair(source: str, target: str, detected: dict | None = None, min_prob: float = 0.6) -> None:
    """Refuse to 'translate' into the language the footage is already in. `detected` (from detect_language) overrides
    the declared source when Whisper is confident."""
    src = source
    if detected and detected.get("code") and detected.get("probability", 0) >= min_prob and not detected.get("dialect_unknown"):
        src = detected["code"]
    if same_language(src, target):
        how = f"detected {detected['whisper']} p={detected['probability']}" if detected and src != source else "declared"
        raise ValueError(f"source and target are the same language ({src}, {how}) - nothing to translate")
