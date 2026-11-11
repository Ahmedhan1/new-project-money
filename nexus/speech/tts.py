"""Narration: Piper neural voices, eSpeak NG as the always-available fallback."""
from __future__ import annotations

import subprocess
import time
import wave
from pathlib import Path

from .. import costguard, db, models

_voices: dict = {}


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _piper(text: str, out: Path, voice: str, length_scale: float) -> None:
    from piper import PiperVoice, SynthesisConfig
    if voice not in _voices:
        _voices[voice] = PiperVoice.load(str(models.ensure_voice(voice)))
    with wave.open(str(out), "wb") as w:
        _voices[voice].synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=length_scale))


def _espeak(text: str, out: Path, lang: str, length_scale: float) -> None:
    code = {"ar": "ar", "de": "de", "en": "en-us"}.get(lang.split("-")[0], lang)
    subprocess.run(["espeak-ng", "-v", code, "-s", str(int(165 / length_scale)), "-w", str(out), text], check=True)


def synthesize(text: str, out: Path, lang: str = "en", voice: str | None = None, length_scale: float = 1.0) -> dict:
    """Synthesize `text`; returns {path, duration, engine, voice, license, commercial}."""
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    def piper_run():
        v = voice or models.voice_for(lang)
        _piper(text, out, v, length_scale)
        meta = models.catalog()["voices"][v]
        return {"voice": v, "license": meta["license"], "commercial": meta["commercial"]}

    def espeak_run():
        _espeak(text, out, lang, length_scale)
        return {"voice": f"espeak-ng:{lang}", "license": "GPL-3.0 engine; output unencumbered", "commercial": True}

    try:
        engine, meta = costguard.run_with_fallback("piper", ["narration"], {"piper": piper_run, "espeak-ng": espeak_run})
    except (KeyError, FileNotFoundError, RuntimeError, ImportError):
        # no Piper voice for this language / model unavailable -> clear-licence fallback
        costguard.authorize("espeak-ng", "narration-fallback")
        engine, meta = "espeak-ng", espeak_run()
    dur = _wav_duration(out)
    db.metric("tts_rtf", (time.time() - t0) / max(dur, 0.1), engine=engine)
    return {"path": str(out), "duration": dur, "engine": engine, **meta}
