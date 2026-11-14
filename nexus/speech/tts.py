"""Narration: Piper neural voices, eSpeak NG as the always-available fallback.

Every line is mastered (`audio.master.master_voice`: same chain, same loudness, fixed silence pads) and cached by
content hash (text + voice + length_scale + engine + mastering) under `config.CACHE / "tts"`, so a repeated line is
copied instead of re-synthesised.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

from .. import config, costguard, db, models

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


MASTER_LUFS = -18.0


def _cache_key(text: str, voice: str, length_scale: float, engine: str, master: bool) -> str:
    raw = json.dumps([text.strip(), voice, round(float(length_scale), 3), engine, MASTER_LUFS if master else None], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_dir() -> Path:
    return config.CACHE / "tts"


def _cached(key: str, out: Path) -> dict | None:
    wav, meta = _cache_dir() / f"{key}.wav", _cache_dir() / f"{key}.json"
    if not (wav.exists() and meta.exists() and wav.stat().st_size > 44):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None
    shutil.copyfile(wav, out)
    return {**info, "path": str(out), "cached": True}


def _store(key: str, out: Path, info: dict) -> None:
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{key}.tmp.wav"
    shutil.copyfile(out, tmp)
    tmp.replace(d / f"{key}.wav")
    (d / f"{key}.json").write_text(json.dumps({k: v for k, v in info.items() if k not in ("path", "cached")}, ensure_ascii=False), encoding="utf-8")


def synthesize(text: str, out: Path, lang: str = "en", voice: str | None = None, length_scale: float = 1.0,
               master: bool = True, cache: bool = True, reference: dict | None = None) -> dict:
    """Synthesize `text`; returns {path, duration, engine, voice, license, commercial, lufs, mastered, cached}.
    `lufs` is the measured integrated loudness of the delivered file (None when mastering is off).
    Cache: Piper's key is looked up first; the eSpeak key only when eSpeak is the engine that ends up running."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    hit: dict = {}

    def lookup(engine: str, v: str) -> bool:
        if cache and (r := _cached(_cache_key(text, v, length_scale, engine, master), out)):
            hit.update(r)
            return True
        return False

    def piper_run():
        v = voice or models.voice_for(lang)
        if not lookup("piper", v):
            _piper(text, out, v, length_scale)
        meta = models.voice_meta(v)
        return {"voice": v, "license": meta["license"], "commercial": meta["commercial"]}

    def espeak_run():
        if not lookup("espeak-ng", f"espeak-ng:{lang}"):
            _espeak(text, out, lang, length_scale)
        return {"voice": f"espeak-ng:{lang}", "license": "GPL-3.0 engine; output unencumbered", "commercial": True}

    def silma_run():
        from . import silma
        ref_id = "silma:" + hashlib.sha256(Path(reference["wav"]).read_bytes()).hexdigest()[:12]
        if not lookup("silma", ref_id):
            silma.synthesize(text, out, reference["wav"], reference["text"], speed=1.0 / max(0.5, float(length_scale)))
        return {"voice": ref_id, "license": silma.LICENSE, "commercial": True}

    engine = meta = None
    if reference and str(lang)[:2] in ("ar", "en"):
        from . import silma
        if silma.available():
            try:
                costguard.authorize("silma-tts", "narration")
                engine, meta = "silma", silma_run()
            except Exception as e:  # noqa: BLE001 - any SILMA problem: the Piper/eSpeak path below still delivers a voice
                print(f"SILMA TTS failed ({type(e).__name__}: {str(e)[:120]}); falling back to Piper", flush=True)
                engine = meta = None
    if not engine:
        try:
            engine, meta = costguard.run_with_fallback("piper", ["narration"], {"piper": piper_run, "espeak-ng": espeak_run})
        except (KeyError, FileNotFoundError, RuntimeError, ImportError):
            # no Piper voice for this language / model unavailable -> clear-licence fallback
            costguard.authorize("espeak-ng", "narration-fallback")
            engine, meta = "espeak-ng", espeak_run()
    if hit:
        return hit
    db.metric("tts_rtf", (time.time() - t0) / max(_wav_duration(out), 0.1), engine=engine)
    lufs = None
    if master:
        from ..audio import master as mastering
        lufs = mastering.master_voice(out, out, target_lufs=MASTER_LUFS)["after"]["lufs"]
    info = {"path": str(out), "duration": _wav_duration(out), "engine": engine, **meta, "lufs": lufs, "mastered": bool(master), "cached": False}
    if cache:
        _store(_cache_key(text, meta["voice"], length_scale, engine, master), out, info)
    return info
