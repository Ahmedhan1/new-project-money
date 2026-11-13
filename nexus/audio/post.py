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
# RMS-targeted (r=0.08) over a 500 ms x 31-frame window. The previous peak-based setting gave the interviewer's line
# (higher crest factor) 7.5 dB less gain than the guest on equal input; this one leaves them within 0.5 dB.
LEVEL = "dynaudnorm=f=500:g=31:p=0.9:m=8:s=6:r=0.08,acompressor=threshold=-20dB:ratio=3:attack=10:release=150:makeup=2,equalizer=f=3200:t=q:w=1.2:g=2.5"
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


def chain_signature(name: str = "clean_speech") -> str:
    """Cache key part: the chain's name AND its exact filter text, so tuning a filter re-renders affected segments."""
    import hashlib
    try:
        text = segment_chain(name)
    except KeyError:
        text = name
    return f"{chain_name_in_use(name)}|{hashlib.sha1(text.encode()).hexdigest()[:10]}"


# ---------------------------------------------------------------- speaker level matching (interviewer vs guest mic)
SPK_MAX_DB, SPK_MIN_DIFF = 12.0, 3.0


def speaker_levels(x, sr: int, turns: list[dict], block: float = 0.05) -> dict:
    """Speech level per speaker (dBFS): median of 50 ms blocks within 25 dB of each turn's loud end (pauses ignored),
    pooled over that speaker's turns. Returns {speaker: {"db", "seconds"}}."""
    import numpy as np
    w = int(block * sr)
    pool: dict[str, list] = {}
    for t in turns:
        a, b = int(float(t["start"]) * sr), int(float(t["end"]) * sr)
        seg = x[a:b]
        k = len(seg) // w
        if k < 10:
            continue
        db = 10 * np.log10((seg[: k * w].reshape(k, w) ** 2).mean(1) + 1e-12)
        speech = db[db > np.percentile(db, 90) - 25]
        pool.setdefault(t["speaker"], []).extend(speech.tolist())
    return {s: {"db": round(float(np.median(v)), 2), "seconds": round(len(v) * block, 1)} for s, v in pool.items() if len(v) >= 20}


def speaker_gains(levels: dict, max_db: float = SPK_MAX_DB, min_diff: float = SPK_MIN_DIFF) -> dict:
    """Gain (dB) per speaker to match the speaker with the most speech; small differences are left alone."""
    if len(levels) < 2:
        return {}
    ref = max(levels, key=lambda s: levels[s]["seconds"])
    out = {}
    for s, v in levels.items():
        g = levels[ref]["db"] - v["db"]
        if abs(g) >= min_diff:
            out[s] = round(max(-max_db, min(max_db, g)), 2)
    return out


def apply_speaker_gains(tl: dict, analyses: dict) -> tuple[dict, dict]:
    """Set clip["gain_spans"] (source-time spans with a gain in dB) wherever a speaker whose level differs from the main
    speaker talks inside a clip. Needs diarisation turns in the analysis; otherwise nothing changes."""
    import copy
    from ..media import diarize as dz
    out = copy.deepcopy(tl)
    report: dict = {"sources": {}, "clips": 0}
    cache: dict = {}
    for c in out["clips"]:
        an = (analyses or {}).get(c.get("src"))
        spk = (an or {}).get("speakers") or {}
        if c.get("type") != "video" or not spk.get("available") or not spk.get("turns"):
            continue
        if c["src"] not in cache:
            x = dz._audio(c["src"])
            lv = speaker_levels(x, dz.SR, spk["turns"])
            cache[c["src"]] = speaker_gains(lv)
            report["sources"][c["src"]] = {"levels": lv, "gains_db": cache[c["src"]]}
        gains = cache[c["src"]]
        spans = [{"in": round(max(float(t["start"]), c["in"]), 3), "out": round(min(float(t["end"]), c["out"]), 3), "db": gains[t["speaker"]]}
                 for t in spk["turns"] if t["speaker"] in gains and float(t["end"]) > c["in"] and float(t["start"]) < c["out"]]
        spans = [s for s in spans if s["out"] - s["in"] > 0.05]
        if spans:
            c["gain_spans"] = spans
            report["clips"] += 1
        else:
            c.pop("gain_spans", None)
    return out, report
