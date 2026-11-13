"""Procedural sound effects synthesised with NumPy (no recordings, nothing to license).

make(name, **kw) -> Path of a cached 48 kHz stereo 16-bit WAV. Names: whoosh, impact, riser, pop, click, swell.
Common keywords: duration (s), seed (int), gain (linear, applied after normalisation). Each sound has a shaped
amplitude envelope, short raised-cosine fades at both ends (no clicks) and is peak-normalised to -3 dBFS.
"""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import numpy as np

from .. import config, costguard

SR = 48000
PEAK_DBFS = -3.0
VERSION = 1
DEFAULT_DUR = {"whoosh": 0.7, "impact": 1.4, "riser": 2.5, "pop": 0.16, "click": 0.05, "swell": 2.0}
NAMES = tuple(DEFAULT_DUR)


def _t(n: int) -> np.ndarray:
    return np.arange(n) / SR


def _stft_shape(x: np.ndarray, gain, n_fft: int = 1024, hop: int = 256) -> np.ndarray:
    """Time-varying spectral filter. gain(frame_times[:,None], freqs[None,:]) -> magnitude gains."""
    n = len(x)
    pad = np.concatenate([np.zeros(n_fft), x, np.zeros(n_fft)])
    win = np.hanning(n_fft)
    idx = np.arange(0, len(pad) - n_fft, hop)
    frames = np.lib.stride_tricks.sliding_window_view(pad, n_fft)[idx] * win
    spec = np.fft.rfft(frames, axis=1)
    ft = (idx + n_fft / 2 - n_fft) / SR
    fr = np.fft.rfftfreq(n_fft, 1 / SR)
    spec *= gain(ft[:, None], fr[None, :])
    y = np.fft.irfft(spec, n=n_fft, axis=1) * win
    out = np.zeros(len(pad))
    norm = np.zeros(len(pad))
    for k, i in enumerate(idx):
        out[i:i + n_fft] += y[k]
        norm[i:i + n_fft] += win ** 2
    out /= np.maximum(norm, 1e-6)
    return out[n_fft:n_fft + n]


def _band(fc, bw_oct: float):
    """Gaussian band-pass on a log-frequency axis around fc(t)."""
    def g(t, f):
        c = fc(np.clip(t, 0, None))
        return np.exp(-0.5 * (np.log2(np.maximum(f, 20) / c) / (bw_oct / 2)) ** 2)
    return g


def _fades(y: np.ndarray, fin: float = 0.003, fout: float = 0.015) -> np.ndarray:
    y = y.copy()
    a, b = max(2, int(fin * SR)), max(2, int(fout * SR))
    y[:a] *= (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a)))[:, None] if y.ndim == 2 else 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    y[-b:] *= (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, b)))[:, None] if y.ndim == 2 else 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, b))
    return y


def _stereo(mono: np.ndarray, pan: np.ndarray | float = 0.5) -> np.ndarray:
    p = np.clip(pan, 0, 1) * np.pi / 2
    return np.stack([mono * np.cos(p), mono * np.sin(p)], axis=1) * np.sqrt(2)


def whoosh(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    x = rng.normal(0, 1, n)
    fc = lambda t: 350 * (9.0 ** np.sin(np.pi * np.clip(t / d, 0, 1)) ** 1.5)  # 350 Hz -> ~3 kHz -> back down
    y = _stft_shape(x, _band(fc, 1.6))
    u = _t(n) / d
    env = (u ** 1.6) * ((1 - u) ** 1.0)
    return _stereo(y * env / env.max(), 0.15 + 0.7 * u)


def impact(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    t = _t(n)
    f = 38 + 62 * np.exp(-t * 9)  # 100 Hz thump gliding to 38 Hz
    thump = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 3.2)
    noise = rng.normal(0, 1, n)
    crack = _stft_shape(noise, lambda tt, ff: 1 / (1 + (ff / 2500) ** 2)) * np.exp(-t * 45) * 0.9
    body = _stft_shape(rng.normal(0, 1, n), _band(lambda tt: 180 + 0 * tt, 1.5)) * np.exp(-t * 7) * 0.6
    tail = _stft_shape(rng.normal(0, 1, n), lambda tt, ff: 1 / (1 + (ff / 600) ** 2)) * np.exp(-t * 2.2) * 0.12
    y = np.tanh(1.6 * (thump + crack + body + tail))
    return np.stack([y, np.roll(y, int(0.0007 * SR))], axis=1)


def riser(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    t = _t(n)
    u = t / d
    fc = lambda tt: 400 * 15 ** np.clip(tt / d, 0, 1)
    air = _stft_shape(rng.normal(0, 1, n), _band(fc, 1.2)) * 0.8
    f0 = 110 * 8 ** u * (1 + 0.006 * np.sin(2 * np.pi * (4 + 6 * u) * t))
    ph = 2 * np.pi * np.cumsum(f0) / SR
    tone = sum(np.sin(k * ph) / k for k in range(1, 7)) * 0.35
    env = u ** 2.2
    y = (air / (np.abs(air).max() + 1e-9) + tone) * env
    return _stereo(y, 0.5 + 0.3 * np.sin(2 * np.pi * 0.7 * t) * u)


def pop(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    t = _t(n)
    f = 260 + 900 * np.exp(-t * 28)
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 32)
    y += _stft_shape(rng.normal(0, 1, n), _band(lambda tt: 1800 + 0 * tt, 1.0)) * np.exp(-t * 120) * 0.25
    return _stereo(y)


def click(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    t = _t(n)
    y = np.sin(2 * np.pi * 3200 * t) * np.exp(-t * 260)
    y += _stft_shape(rng.normal(0, 1, n), lambda tt, ff: (ff / 4000) ** 2 / (1 + (ff / 4000) ** 2)) * np.exp(-t * 400) * 0.5
    return _stereo(y)


def swell(d: float, rng) -> np.ndarray:
    n = int(d * SR)
    t = _t(n)
    u = t / d
    shimmer = _stft_shape(rng.normal(0, 1, n), lambda tt, ff: (ff / 3500) ** 4 / (1 + (ff / 3500) ** 4) / (1 + (ff / 14000) ** 2))
    shimmer /= np.abs(shimmer).max() + 1e-9
    pad = sum(np.sin(2 * np.pi * f * t + k) for k, f in enumerate([220.0, 261.63, 329.63, 440.0])) / 4
    env = (np.expm1(3.5 * u) / np.expm1(3.5)) * (1 - np.clip((u - 0.96) / 0.04, 0, 1))
    y = (0.7 * shimmer + 0.5 * pad) * env
    return np.stack([y, 0.85 * y + 0.15 * np.roll(y, int(0.011 * SR))], axis=1)


SYNTHS = {"whoosh": whoosh, "impact": impact, "riser": riser, "pop": pop, "click": click, "swell": swell}


def _write(path: Path, stereo: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes())


def render_array(name: str, duration: float | None = None, seed: int = 0, gain: float = 1.0) -> np.ndarray:
    if name not in SYNTHS:
        raise ValueError(f"unknown sfx {name!r}; available: {', '.join(NAMES)}")
    d = float(duration or DEFAULT_DUR[name])
    d = min(30.0, max(0.02, d))
    rng = np.random.default_rng(seed)
    y = SYNTHS[name](d, rng)
    y = y - y.mean(axis=0)  # no DC offset
    y = _fades(y, 0.002 if name in ("impact", "click", "pop") else 0.004, 0.012 if name in ("click", "pop") else 0.03)
    y = y / (np.abs(y).max() + 1e-12) * 10 ** (PEAK_DBFS / 20)
    return y * float(gain)


def make(name: str, **kw) -> Path:
    """Synthesise (or reuse from cache) a sound effect; returns the WAV path."""
    costguard.authorize("procedural-music", "sfx")
    allowed = {k: kw[k] for k in ("duration", "seed", "gain") if k in kw}
    key = hashlib.sha1(json.dumps({"n": name, "v": VERSION, **allowed}, sort_keys=True).encode()).hexdigest()[:12]
    out = config.CACHE / "sfx" / f"{name}_{key}.wav"
    if out.exists() and out.stat().st_size > 44:
        return out
    y = render_array(name, allowed.get("duration"), int(allowed.get("seed", 0)), float(allowed.get("gain", 1.0)))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    _write(tmp, y)
    tmp.replace(out)
    return out
