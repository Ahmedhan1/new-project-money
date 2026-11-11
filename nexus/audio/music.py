"""Procedural music beds and simple sound design, synthesised locally with NumPy.

Everything here is generated from oscillators, so there is no third-party
recording and no licence to clear. It is deliberately modest: soft pads with a
slow chord progression. It is a usable bed under narration, not composed music.
"""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from .. import config, costguard

SR = 44100
MOODS = {
    # root midi note, chord progression (scale degrees as semitone triads), tempo (sec per chord), brightness
    "calm": (57, [[0, 4, 7], [-3, 0, 4], [-7, -3, 0], [-5, -1, 2]], 6.0, 0.35),
    "inspiring": (60, [[0, 4, 7], [7, 11, 14], [9, 12, 16], [5, 9, 12]], 4.0, 0.55),
    "tense": (50, [[0, 3, 7], [1, 5, 8], [0, 3, 7], [-2, 2, 5]], 5.0, 0.45),
    "documentary": (55, [[0, 3, 7], [-4, 0, 3], [-2, 2, 5], [-5, -2, 2]], 7.0, 0.3),
    "upbeat": (62, [[0, 4, 7], [5, 9, 12], [7, 11, 14], [5, 9, 12]], 2.5, 0.6),
}


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _pad(freq: float, n: int, bright: float, rng) -> np.ndarray:
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k, amp in enumerate([1.0, 0.5 * bright, 0.3 * bright, 0.15 * bright], start=1):
        det = 1 + rng.uniform(-0.003, 0.003)  # gentle detune = chorus-like warmth
        out += amp * np.sin(2 * np.pi * freq * k * det * t + rng.uniform(0, 6.28))
    out *= 1 + 0.08 * np.sin(2 * np.pi * 0.2 * t + rng.uniform(0, 6.28))  # slow tremolo
    return out


def _write(path: Path, stereo: np.ndarray) -> None:
    stereo = stereo / (np.abs(stereo).max() + 1e-9) * 0.7
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((stereo * 32767).astype(np.int16).tobytes())


def music_bed(duration: float, mood: str = "calm", seed: int = 0) -> Path:
    """A seamless-ish bed of at least `duration` seconds. Same (mood, seed) = same music (series identity)."""
    costguard.authorize("procedural-music", "generate")
    root, prog, per, bright = MOODS.get(mood, MOODS["calm"])
    key = hashlib.sha1(f"{mood}|{seed}|{int(duration)}".encode()).hexdigest()[:12]
    out = config.CACHE / "music" / f"{mood}_{key}.wav"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    n_total = int((duration + 2) * SR)
    L, R = np.zeros(n_total), np.zeros(n_total)
    seg = int(per * SR)
    xf = int(1.2 * SR)
    pos, i = 0, 0
    while pos < n_total:
        chord = prog[i % len(prog)]
        n = min(seg + xf, n_total - pos)
        env = np.ones(n)
        k = min(xf, n // 2)
        env[:k] = np.linspace(0, 1, k) ** 2
        env[-k:] = np.linspace(1, 0, k) ** 2
        for j, st in enumerate(chord + [chord[0] - 12]):
            v = _pad(_hz(root + st), n, bright, rng) * env * (0.9 if j < 3 else 1.2)
            pan = 0.5 + 0.35 * np.sin(j * 2.1)
            L[pos:pos + n] += v * (1 - pan)
            R[pos:pos + n] += v * pan
        if mood in ("upbeat", "inspiring"):  # a soft pulse on each beat
            beat = 60 / (110 if mood == "upbeat" else 84)
            for b in np.arange(0, n / SR, beat):
                s = pos + int(b * SR)
                m = min(int(0.18 * SR), n_total - s)
                if m > 0:
                    tt = np.arange(m) / SR
                    click = np.sin(2 * np.pi * _hz(root - 12 + chord[0]) * tt) * np.exp(-tt * 22) * 1.6
                    L[s:s + m] += click
                    R[s:s + m] += click
        pos += seg
        i += 1
    _write(out, np.stack([L, R], axis=1))
    return out


def whoosh(duration: float = 0.6, seed: int = 1) -> Path:
    """Filtered-noise sweep for transitions."""
    costguard.authorize("procedural-music", "sfx")
    out = config.CACHE / "music" / f"whoosh_{int(duration * 100)}_{seed}.wav"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    noise = rng.normal(0, 1, n)
    # moving one-pole low-pass: cutoff sweeps up then down
    sweep = np.sin(np.linspace(0, np.pi, n)) ** 2
    y, acc = np.zeros(n), 0.0
    for i in range(n):
        a = 0.02 + 0.5 * sweep[i]
        acc += a * (noise[i] - acc)
        y[i] = acc
    y *= sweep
    _write(out, np.stack([y, y[::-1] * 0.8 + y * 0.2], axis=1))
    return out
