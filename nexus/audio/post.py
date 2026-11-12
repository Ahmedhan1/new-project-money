"""Dialogue clean-up (AUD-2). Measured on 90 s of the real NASA interview (docs/results/audio_post.json):

    chain                      noise floor in pauses   speech     SNR proxy   >12 dB block jumps
    raw source                 -48.4 dBFS              -24.4      24.1 dB     0.112
    voice_level (previous)     -39.9                   -14.6      25.3        0.137   <- leveller lifts room noise
    clean_speech (this)        -43.8                   -14.7      29.1        0.127

clean_speech = RNNoise (arnndn, model 'somnolent-hogwash': speech signal / recording noise) -> a gentle gate BEFORE the
leveller (so dynaudnorm cannot re-amplify the residual noise in pauses) -> leveller -> compressor -> presence EQ.
Without the model file it falls back to voice_level (an afftdn variant measured worse: -36.7 dBFS floor).
RNNoise models (github.com/GregorR/rnnoise-models): the author states the models "are not creative and thus not subject
to copyright"; RNNoise itself is BSD-3-Clause. Dialogue isolation from music (source separation) is NOT implemented.
"""
from __future__ import annotations

from pathlib import Path

from .. import config, costguard

RNN_URL = "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/somnolent-hogwash-2018-09-01/sh.rnnn"
LEVEL = "dynaudnorm=f=250:g=15:p=0.9:m=8:s=6,acompressor=threshold=-20dB:ratio=3:attack=10:release=150:makeup=2,equalizer=f=3200:t=q:w=1.2:g=2.5"
VOICE_LEVEL = "highpass=f=80,afftdn=nr=10:nf=-30," + LEVEL
GATE = "agate=threshold=0.02:ratio=3:attack=5:release=250:range=0.1"
MASTER = {"streaming": -14.0, "podcast": -16.0, "broadcast": -23.0}


def model_path() -> Path:
    return config.MODELS / "rnnoise" / "sh.rnnn"


def ensure_model() -> Path | None:
    """Download the RNNoise model once (300 KB) through the declared host; None if offline."""
    p = model_path()
    if p.exists() and p.stat().st_size > 1000:
        return p
    try:
        from .. import net
        costguard.authorize("rnnoise-models", "download")
        net.download(RNN_URL, p, timeout=60, retries=2)
        return p if p.read_bytes()[:20].startswith(b"rnnoise") else None
    except Exception:  # noqa: BLE001 - offline / blocked: callers fall back to voice_level
        return None


def _esc(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def segment_chain(name: str = "clean_speech") -> str:
    """FFmpeg audio filter chain for one clip. 'clean_speech' needs the RNNoise model file; otherwise voice_level."""
    if name == "clean_speech":
        p = model_path()
        if p.exists():
            return f"highpass=f=80,arnndn=m='{_esc(p)}':mix=1,{GATE},{LEVEL}"
        return VOICE_LEVEL
    if name == "voice_level":
        return VOICE_LEVEL
    raise KeyError(name)


def chain_name_in_use(name: str = "clean_speech") -> str:
    return "clean_speech (RNNoise)" if name == "clean_speech" and model_path().exists() else "voice_level (fallback)"
