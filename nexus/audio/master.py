"""Voice mastering: every narration line gets the same chain and the same loudness, so lines sit together.

Chain (FFmpeg, local, zero cost): 48 kHz -> high-pass 80 Hz -> gentle de-ess -> light compression -> presence EQ ->
trim leading/trailing silence to a fixed pad (predictable sentence timing) -> measured gain to a common loudness target
-> sample-peak limiter (true peak stays under -1 dBTP) -> 10/20 ms fades against clicks. Before/after loudness and true
peak are measured with EBU R128 (`ff.loudness`) and returned, never assumed.
"""
from __future__ import annotations

import hashlib
import shutil
import wave
from pathlib import Path

import numpy as np

from .. import config, costguard
from ..media import ff

SR = 48000
PAD_HEAD = 0.06          # seconds of silence kept before the first word
PAD_TAIL = 0.12          # ... and after the last one
SILENCE_DB = -45         # anything quieter at the edges is trimmed
LIMIT_DBFS = -2.0        # sample-peak ceiling; keeps true peak below -1 dBTP on speech
PROFILES = {
    # name: (pre-gain chain without resampling/trim, output channels)
    "narration": ("highpass=f=80,deesser=i=0.3:m=0.5:f=0.5:s=o,"
                  "acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120:makeup=1,"
                  "equalizer=f=300:t=q:w=1.0:g=-1.5,equalizer=f=3200:t=q:w=1.2:g=2", 1),
    "dialogue": ("highpass=f=90,deesser=i=0.25:m=0.5:f=0.5:s=o,"
                 "acompressor=threshold=-22dB:ratio=2:attack=10:release=150:makeup=1,equalizer=f=3000:t=q:w=1.2:g=1.5", 2),
    "raw": ("highpass=f=60", 1),
}


def _wav_dur(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def measure(path: str | Path) -> dict:
    m = ff.loudness(path)
    return {"lufs": round(m["lufs"], 2), "true_peak": round(m["true_peak"], 2), "lra": round(m["lra"], 2)}


def master_voice(wav_in: str | Path, wav_out: str | Path, target_lufs: float = -18.0, profile: str = "narration") -> dict:
    """Master one voice clip. Returns {path, duration, before:{lufs,true_peak,lra}, after:{...}, gain_db, trimmed_s, profile}.
    `wav_in` may equal `wav_out` (in-place)."""
    costguard.authorize("ffmpeg", "master-voice")
    wav_in, wav_out = Path(wav_in), Path(wav_out)
    chain, ch = PROFILES.get(profile, PROFILES["narration"])
    before = measure(wav_in)
    in_dur = ff.duration(wav_in)
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    stage1 = wav_out.with_name(wav_out.stem + ".m1.wav")
    trim = (f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB:start_silence=0.02,areverse,"
            f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB:start_silence=0.02,areverse")
    pad = f"adelay={int(PAD_HEAD * 1000)}:all=1,apad=pad_dur={PAD_TAIL}"
    fmt = f"aresample={SR},aformat=sample_fmts=fltp:channel_layouts={'mono' if ch == 1 else 'stereo'}"
    ff.run(["-i", wav_in, "-af", f"{fmt},{chain},{trim},{pad}", "-ar", SR, "-ac", ch, "-c:a", "pcm_s16le", stage1], op="master-voice-chain")
    if _wav_dur(stage1) < PAD_HEAD + PAD_TAIL + 0.05:  # all of it was below the threshold: keep it untrimmed
        ff.run(["-i", wav_in, "-af", f"{fmt},{chain}", "-ar", SR, "-ac", ch, "-c:a", "pcm_s16le", stage1], op="master-voice-chain")
    mid = measure(stage1)
    dur = _wav_dur(stage1)
    gain = 0.0 if mid["lufs"] <= -70 else target_lufs - mid["lufs"]
    lim = 10 ** (LIMIT_DBFS / 20)
    fades = f"afade=t=in:st=0:d=0.01,afade=t=out:st={max(0.0, dur - 0.02):.3f}:d=0.02"
    tmp = wav_out.with_name(wav_out.stem + ".m2.wav")
    after = mid
    for _ in range(2):  # a second, smaller correction only if the limiter pulled the level down
        ff.run(["-i", stage1, "-af", f"volume={gain:.2f}dB,alimiter=limit={lim:.4f}:attack=2:release=40:level=disabled:asc=1,{fades}",
                "-ar", SR, "-ac", ch, "-c:a", "pcm_s16le", tmp], op="master-voice-gain")
        after = measure(tmp)
        if after["lufs"] <= -70 or abs(after["lufs"] - target_lufs) <= 0.5 or after["true_peak"] > LIMIT_DBFS + 0.5:
            break
        gain += min(3.0, target_lufs - after["lufs"])
    shutil.move(str(tmp), str(wav_out))
    stage1.unlink(missing_ok=True)
    return {"path": str(wav_out), "duration": round(_wav_dur(wav_out), 3), "before": before, "after": after,
            "gain_db": round(gain, 2), "trimmed_s": round(max(0.0, in_dur - _wav_dur(wav_out)), 3), "profile": profile,
            "target_lufs": target_lufs, "sample_rate": SR, "channels": ch}


def room_tone(duration: float, seed: int = 0, level_db: float = -62.0, channels: int = 1) -> Path:
    """Very quiet shaped (pink-ish, low-passed) noise so gaps between narration lines are never digital silence.
    `level_db` is the RMS level in dBFS. Cached: same (duration, seed, level) = same file."""
    costguard.authorize("procedural-music", "room-tone")
    key = hashlib.sha1(f"{duration:.2f}|{seed}|{level_db}|{channels}".encode()).hexdigest()[:12]
    out = config.CACHE / "audio" / f"roomtone_{key}.wav"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    n = max(1, int(duration * SR))
    spec = np.fft.rfft(rng.normal(0, 1, (channels, n)), axis=1)
    f = np.fft.rfftfreq(n, 1 / SR)
    shape = 1 / np.sqrt(np.maximum(f, 20.0))                 # pink tilt
    shape *= 1 / np.sqrt(1 + (f / 4000.0) ** 4)              # soft low-pass: room, not hiss
    shape[f < 30] *= (f[f < 30] / 30.0)                      # no sub-rumble
    y = np.fft.irfft(spec * shape, n=n, axis=1)
    y *= 10 ** (level_db / 20) / (np.sqrt(np.mean(y ** 2)) + 1e-12)
    k = min(n // 2, int(0.05 * SR))
    if k:
        ramp = np.linspace(0, 1, k)
        y[:, :k] *= ramp
        y[:, -k:] *= ramp[::-1]
    with wave.open(str(out), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y.T, -1, 1) * 32767).astype(np.int16).tobytes())
    return out


def level_match(paths: list[str | Path], tolerance_lu: float = 1.0) -> dict:
    """Loudness spread across clips: {clips:[{path,lufs,true_peak}], spread_lu, mean_lufs, max_true_peak, ok, tolerance_lu}."""
    rows = []
    for p in paths:
        m = measure(p)
        rows.append({"path": str(p), "lufs": m["lufs"], "true_peak": m["true_peak"]})
    lv = [r["lufs"] for r in rows if r["lufs"] > -70]
    spread = round(max(lv) - min(lv), 2) if lv else 0.0
    return {"clips": rows, "spread_lu": spread, "mean_lufs": round(sum(lv) / len(lv), 2) if lv else None,
            "max_true_peak": max((r["true_peak"] for r in rows), default=None), "ok": spread <= tolerance_lu, "tolerance_lu": tolerance_lu}
