"""SILMA TTS v1 (silma-ai/silma-tts): natural Arabic (MSA) and English speech with voice cloning, F5-TTS architecture,
150 M parameters. Weights Apache-2.0, code MIT per the model card (read 2026-11-14) - commercial use allowed.

Used for dubbing when it is installed AND a CUDA GPU is present (cloud studio): measured on Kaggle 2026-11-14,
CPU took ~87 s per 5 s sentence (too slow), the output was transcribed back by Whisper almost word for word
("مرحبا بكم في هذا الفيلم الوثائقي عن رحلة رواد الفضاء إلى القمر"). The voice is cloned from a short clip of the
original speaker (< 8 s) plus its transcript, so the dub keeps the speaker's timbre.
NEXUS_SILMA=0 switches it off; NEXUS_SILMA_CPU=1 allows CPU (slow, for tests).
"""
from __future__ import annotations

import os
import threading
import wave
from pathlib import Path

_model = None
_lock = threading.Lock()
LANGS = ("ar", "en")
LICENSE = "SILMA TTS v1: weights Apache-2.0, code MIT (silma-ai/silma-tts); voice cloned from the owner's own video"


def available() -> bool:
    if os.environ.get("NEXUS_SILMA", "1") == "0":
        return False
    try:
        import importlib.util
        if importlib.util.find_spec("silma_tts") is None:
            return False
        if os.environ.get("NEXUS_SILMA_CPU") == "1":
            return True
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _load():
    global _model
    if _model is None:
        from silma_tts.api import SilmaTTS
        _model = SilmaTTS()
    return _model


def synthesize(text: str, out: Path, ref_wav: str, ref_text: str, speed: float = 1.0, seed: int = 1) -> float:
    """Write `out` (24 kHz mono WAV); returns its duration in seconds."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with _lock:  # one model, one GPU: lines are synthesised one after another
        _load().infer(ref_file=str(ref_wav), ref_text=ref_text, gen_text=text, file_wave=str(out), seed=seed,
                      speed=max(0.7, min(1.4, float(speed))))
    with wave.open(str(out)) as w:
        return w.getnframes() / float(w.getframerate())


def pick_reference(analysis: dict, min_s: float = 4.0, max_s: float = 7.5) -> dict | None:
    """A clean single-speaker line of the original video to clone the voice from: {start, end, text} or None."""
    segs = [s for s in ((analysis or {}).get("transcript") or {}).get("segments") or []
            if min_s <= float(s.get("end", 0)) - float(s.get("start", 0)) <= max_s and len(str(s.get("text", "")).split()) >= 6]
    if not segs:
        return None
    # prefer confident, wordy lines away from the very start (intros often have music under them)
    segs.sort(key=lambda s: (float(s.get("start", 0)) < 15, -len(str(s.get("text", "")))))
    s = segs[0]
    return {"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"]).strip()}


def extract_reference(src: str, ref: dict, out: Path) -> Path:
    """Cut the reference line from the source as 24 kHz mono WAV."""
    from ..media import ff
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-y", "-ss", f"{ref['start']:.3f}", "-t", f"{ref['end'] - ref['start']:.3f}", "-i", str(src), "-vn", "-ac", "1",
            "-ar", "24000", "-af", "highpass=f=70,lowpass=f=9000", str(out)], op="silma-reference", timeout=120)
    return out
