"""Sound design engine (Account 2, Agent C): music-to-cut sync, speech-density ducking, SFX placement with a density
cap, procedural ambience beds, stereo placement and an objective quality pass on generated SFX.

All sounds are generated here or in nexus/fx/sfx.py and nexus/audio/music.py (oscillators and shaped noise): no
recordings, nothing to license. Nothing here "listens": quality checks are spectrogram and envelope measurements, so a
person must still listen before a mix ships.

    onsets(x, sr)                       spectral-flux onset envelope -> onset times + strengths
    beat_grid(x, sr)                    tempo (autocorrelation of the onset envelope, 60-180 BPM) + beat times
    snap_cuts(cuts, beats, max_shift)   per cut: nearest beat, offset (s), whether it may be snapped
    speech_density(segments, dur)       fraction of time with speech + mean pause
    ducking(segments, dur)              duck depth/attack/release from speech density + volume keyframes for music
    sfx_plan(events, profile, dur)      SFX suggestions at transitions/titles/pattern interrupts, density-capped
    ambience(kind, duration)            procedural ambience bed (room, outdoor, city, nature) as a WAV
    pan(stereo, position)               constant-power pan by on-screen x position (0 left .. 1 right)
    sfx_quality(name)                   envelope/spectrum checks per SFX type + optional spectrogram PNG
"""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from .. import config, costguard
from . import measure as M

# ---------------------------------------------------------------- onsets and beats
HOP = 512


def onset_envelope(x: np.ndarray, sr: int, n_fft: int = 2048, hop: int = HOP) -> tuple[np.ndarray, float]:
    """Half-wave rectified log-spectral flux, mean-removed; returns (envelope, frame rate)."""
    m = M.mono(M.as2d(x)).astype(np.float64)
    if len(m) < n_fft * 2:
        return np.zeros(0), sr / hop
    win = np.hanning(n_fft)
    idx = np.arange(0, len(m) - n_fft, hop)
    S = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(m, n_fft)[idx] * win, axis=1))
    L = np.log1p(1000 * S)
    flux = np.maximum(np.diff(L, axis=0), 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    k = 16
    local = np.convolve(flux, np.ones(k) / k, "same")
    return np.maximum(flux - local, 0), sr / hop


def onsets(x: np.ndarray, sr: int, min_gap: float = 0.1) -> list[dict]:
    env, fr = onset_envelope(x, sr)
    if not len(env):
        return []
    thr = np.median(env) + 2.0 * np.std(env)
    out, last = [], -1e9
    for i in range(1, len(env) - 1):
        if env[i] > thr and env[i] >= env[i - 1] and env[i] >= env[i + 1] and (i - last) / fr >= min_gap:
            out.append({"t": round((i * HOP + 1024) / sr, 3), "strength": round(float(env[i] / (thr + 1e-9)), 2)})
            last = i
    return out


def beat_grid(x: np.ndarray, sr: int, bpm_range=(60, 180)) -> dict:
    """Tempo from the onset envelope's autocorrelation; beat phase from the comb that collects most onset energy."""
    env, fr = onset_envelope(x, sr)
    dur = len(M.as2d(x)) / sr
    if len(env) < fr * 4:
        return {"bpm": None, "beats": [], "confidence": 0.0}
    e = env - env.mean()
    ac = np.correlate(e, e, "full")[len(e) - 1:]
    ac /= ac[0] + 1e-12
    lags = np.arange(len(ac)) / fr
    sel = (lags >= 60 / bpm_range[1]) & (lags <= 60 / bpm_range[0])
    if not sel.any():
        return {"bpm": None, "beats": [], "confidence": 0.0}
    i = np.argmax(np.where(sel, ac, -1))
    # parabolic refinement of the period
    if 0 < i < len(ac) - 1:
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        i = i + 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
    period = i / fr
    conf = float(ac[int(round(i))])
    nph = 32
    scores = []
    for p in range(nph):
        ph = p / nph * period
        t = np.arange(ph, dur, period)
        fi = np.clip((t * sr - 1024) / HOP, 0, len(env) - 1).astype(int)
        scores.append(env[fi].sum())
    ph = int(np.argmax(scores)) / nph * period
    beats = [round(float(t), 3) for t in np.arange(ph, dur, period)]
    return {"bpm": round(60 / period, 2), "period_s": round(period, 4), "beats": beats, "confidence": round(conf, 3)}


def snap_cuts(cuts: list[float], beats: list[float], max_shift: float = 0.25, protect: list[tuple[float, float]] | None = None) -> list[dict]:
    """For each cut: nearest beat and offset. `snap` is True when |offset| <= max_shift and the moved cut would not land
    inside a protected span (e.g. a word). The caller decides whether to move picture, music, or neither."""
    out = []
    b = np.asarray(beats) if beats else np.zeros(0)
    for c in cuts:
        if not len(b):
            out.append({"cut": c, "beat": None, "offset_s": None, "snap": False})
            continue
        j = int(np.argmin(np.abs(b - c)))
        off = float(b[j] - c)
        ok = abs(off) <= max_shift and not any(s < b[j] < e for s, e in (protect or []))
        out.append({"cut": round(c, 3), "beat": float(b[j]), "offset_s": round(off, 3), "snap": ok})
    return out


# ---------------------------------------------------------------- ducking by speech density
def speech_density(segments: list[dict], duration: float) -> dict:
    if duration <= 0:
        return {"density": 0.0, "mean_pause_s": None, "segments": 0}
    segs = sorted(segments, key=lambda s: s["start"])
    speech = sum(max(0.0, min(s["end"], duration) - max(0.0, s["start"])) for s in segs)
    pauses = [b["start"] - a["end"] for a, b in zip(segs, segs[1:]) if b["start"] > a["end"]]
    return {"density": round(speech / duration, 3), "mean_pause_s": round(float(np.mean(pauses)), 3) if pauses else None,
            "segments": len(segs)}


def ducking(segments: list[dict], duration: float, base_db: float = 0.0) -> dict:
    """Duck depth and timing from speech density: dense talk gets a deeper, slower-releasing duck (no pumping between
    words); sparse talk a shallower duck that recovers in pauses. Returns parameters plus volume keyframes for the
    music bed ([[t, gain], ...], linear gain, programme seconds) and an equivalent sidechaincompress setting."""
    d = speech_density(segments, duration)
    dens = d["density"]
    if dens >= 0.6:
        depth, attack, release, hold = -14.0, 0.12, 0.8, 0.6
    elif dens >= 0.3:
        depth, attack, release, hold = -11.0, 0.10, 0.5, 0.4
    else:
        depth, attack, release, hold = -8.0, 0.08, 0.35, 0.25
    spans = []
    for s in sorted(segments, key=lambda s: s["start"]):
        a, b = max(0.0, s["start"] - attack), min(duration, s["end"] + hold)
        if spans and a - spans[-1][1] < attack + release:      # a short pause: stay ducked
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    g0, g1 = 10 ** (base_db / 20), 10 ** ((base_db + depth) / 20)
    kf = [[0.0, round(g0, 4)]]
    for a, b in spans:
        kf += [[round(a, 3), round(g0, 4)], [round(a + attack, 3), round(g1, 4)], [round(b, 3), round(g1, 4)],
               [round(min(duration, b + release), 3), round(g0, 4)]]
    kf = [k for i, k in enumerate(kf) if i == 0 or k[0] > kf[i - 1][0]]
    ratio = 4 if depth > -10 else 8 if depth > -13 else 12
    return {"density": dens, "depth_db": depth, "attack_s": attack, "release_s": release, "hold_s": hold, "spans": len(spans),
            "volume_keyframes": kf,
            "sidechaincompress": f"sidechaincompress=threshold=0.02:ratio={ratio}:attack={int(attack * 1000)}:release={int(release * 1000)}:makeup=1"}


def volume_expr(kf: list[list[float]]) -> str:
    """FFmpeg `volume` expression (eval=frame) of linear keyframes; escape commas for use inside a filter graph."""
    if not kf:
        return "1"
    parts = []
    for (t0, g0), (t1, g1) in zip(kf, kf[1:]):
        if t1 <= t0:
            continue
        parts.append(f"between(t,{t0},{t1})*({g0}+({g1}-{g0})*(t-{t0})/{t1 - t0:.4f})")
    tail = kf[-1]
    expr = "+".join(parts) or str(kf[0][1])
    return f"if(gte(t,{tail[0]}),{tail[1]},{expr})"


# ---------------------------------------------------------------- SFX placement
PROFILES = {
    # max SFX per minute, min spacing (s), sounds per event kind, gain (linear) of the SFX relative to -3 dBFS peak
    "documentary": {"per_min": 2, "spacing": 12.0, "transition": "swell", "title": "swell", "interrupt": None, "gain": 0.25},
    "corporate": {"per_min": 3, "spacing": 8.0, "transition": "whoosh", "title": "pop", "interrupt": "click", "gain": 0.3},
    "cinematic": {"per_min": 4, "spacing": 6.0, "transition": "whoosh", "title": "impact", "interrupt": "riser", "gain": 0.4},
    "energetic": {"per_min": 6, "spacing": 4.0, "transition": "whoosh", "title": "pop", "interrupt": "impact", "gain": 0.45},
}
PRIORITY = {"title": 3, "interrupt": 2, "transition": 1}


def sfx_plan(events: list[dict], profile: str = "corporate", duration: float | None = None,
             speech: list[dict] | None = None) -> dict:
    """events: [{"t", "kind": transition|title|interrupt, "x"?: 0..1 on-screen position}]. Returns placements (SFX audio
    items in docs/TIMELINE.md `audio` format, role "sfx") and the events that were dropped by the density cap.
    Risers end on their event (start = t - duration); impacts/pops/clicks start on it; whooshes are centred."""
    from ..fx import sfx
    p = PROFILES.get(profile, PROFILES["corporate"])
    dur = duration or (max((e["t"] for e in events), default=0) + 1)
    cap = max(1, int(np.floor(p["per_min"] * dur / 60.0 + 1e-9))) if dur >= 10 else 1
    ranked = sorted(events, key=lambda e: (-PRIORITY.get(e.get("kind"), 0), e["t"]))
    chosen, dropped = [], []
    for e in ranked:
        name = p.get(e.get("kind"))
        if not name:
            dropped.append({**e, "why": f"profile {profile} uses no sound for {e.get('kind')}"})
            continue
        if len(chosen) >= cap:
            dropped.append({**e, "why": f"density cap {p['per_min']}/min reached"})
            continue
        if any(abs(c["t"] - e["t"]) < p["spacing"] for c in chosen):
            dropped.append({**e, "why": f"closer than {p['spacing']} s to another SFX"})
            continue
        chosen.append({**e, "sfx": name})
    items = []
    for c in sorted(chosen, key=lambda c: c["t"]):
        d = sfx.DEFAULT_DUR[c["sfx"]]
        start = c["t"] - d if c["sfx"] in ("riser", "swell") else c["t"] - d / 2 if c["sfx"] == "whoosh" else c["t"]
        over = any(s["start"] < start + d and s["end"] > start for s in (speech or []))
        gain = p["gain"] * (0.5 if over else 1.0)        # -6 dB more under speech
        items.append({"src": None, "sfx": c["sfx"], "start": round(max(0.0, start), 3), "volume": round(gain, 3), "role": "sfx",
                      "pan": round(float(c["x"]), 3) if c.get("x") is not None else 0.5, "event": c.get("kind"),
                      "event_t": c["t"], "under_speech": over})
    return {"profile": profile, "cap": cap, "placed": items, "dropped": dropped}


def render_sfx_items(items: list[dict]) -> list[dict]:
    """Synthesise each planned SFX (panned) to a cached WAV and return renderer-ready audio items."""
    from ..fx import sfx
    out = []
    for it in items:
        y = sfx.render_array(it["sfx"], seed=int(it["start"] * 10))
        y = pan(y, it.get("pan", 0.5))
        key = hashlib.sha1(f"{it['sfx']}|{it['start']}|{it.get('pan')}".encode()).hexdigest()[:12]
        p = config.CACHE / "sfx" / f"placed_{it['sfx']}_{key}.wav"
        _write(p, y, sfx.SR)
        out.append({k: v for k, v in it.items() if k not in ("sfx", "pan", "event", "event_t", "under_speech")} | {"src": str(p)})
    return out


# ---------------------------------------------------------------- spatialisation
def pan(y: np.ndarray, position: float = 0.5, width: float = 1.0) -> np.ndarray:
    """Constant-power pan of a stereo (or mono) sound by on-screen x (0 = left edge, 1 = right edge). `width` < 1
    narrows the source's own stereo image first."""
    y = M.as2d(np.asarray(y, dtype=np.float64))
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    mid, side = (y[:, 0] + y[:, 1]) / 2, (y[:, 0] - y[:, 1]) / 2 * width
    th = np.clip(position, 0, 1) * np.pi / 2
    gl, gr = np.cos(th) * np.sqrt(2), np.sin(th) * np.sqrt(2)
    return np.stack([(mid + side) * gl, (mid - side) * gr], axis=1)


# ---------------------------------------------------------------- ambience beds
AMBIENCES = ("room", "outdoor", "city", "nature")


def _write(path: Path, y: np.ndarray, sr: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    y = M.as2d(y)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(y.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def ambience_array(kind: str, duration: float, sr: int = 48000, seed: int = 0, level_db: float = -40.0) -> np.ndarray:
    from scipy.signal import butter, sosfilt
    costguard.authorize("procedural-music", "ambience")
    if kind not in AMBIENCES:
        raise ValueError(f"unknown ambience {kind!r}; available: {', '.join(AMBIENCES)}")
    n = int(duration * sr)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr

    def band(lo, hi, x):
        return sosfilt(butter(2, [lo, hi], "band", fs=sr, output="sos"), x, axis=0)
    w = rng.normal(0, 1, (n, 2))
    if kind == "room":
        y = band(60, 3000, w) * 0.5 + band(100, 400, rng.normal(0, 1, (n, 1))) * 0.8   # air + a little mains-free hum-like body
    elif kind == "outdoor":
        g = np.interp(t, np.linspace(0, duration, max(4, int(duration / 1.5))), 0.5 + rng.random(max(4, int(duration / 1.5))))
        y = band(40, 500, np.cumsum(w, axis=0) * 0.02) * g[:, None] + band(800, 6000, w) * 0.15
    elif kind == "city":
        rumble = band(30, 200, rng.normal(0, 1, (n, 1))) * (1 + 0.3 * np.sin(2 * np.pi * t / 9))[:, None]
        y = rumble + band(300, 2500, w) * 0.25
    else:  # nature: soft air + sparse chirps (FM sine bursts)
        y = band(200, 4000, w) * 0.2
        for _ in range(max(1, int(duration / 2.5))):
            s = int(rng.uniform(0, max(0.01, duration - 0.3)) * sr)
            m = int(rng.uniform(0.06, 0.18) * sr)
            tt = np.arange(m) / sr
            f = rng.uniform(2500, 4500) + 900 * np.sin(2 * np.pi * rng.uniform(12, 30) * tt)
            ch = np.sin(2 * np.pi * np.cumsum(f) / sr) * np.hanning(m) * 1.5
            side = rng.random()
            y[s:s + m, 0] += ch[: len(y[s:s + m])] * (1 - side)
            y[s:s + m, 1] += ch[: len(y[s:s + m])] * side
    y = y - y.mean(axis=0)
    y *= 10 ** (level_db / 20) / (np.sqrt(np.mean(y ** 2)) + 1e-12)
    k = min(n // 2, int(1.0 * sr))
    if k:
        r = np.linspace(0, 1, k)[:, None]
        y[:k] *= r
        y[-k:] *= r[::-1]
    return y.astype(np.float32)


def ambience(kind: str, duration: float, seed: int = 0, level_db: float = -40.0) -> Path:
    key = hashlib.sha1(f"{kind}|{duration:.2f}|{seed}|{level_db}".encode()).hexdigest()[:12]
    out = config.CACHE / "ambience" / f"{kind}_{key}.wav"
    if out.exists():
        return out
    return _write(out, ambience_array(kind, duration, 48000, seed, level_db), 48000)


# ---------------------------------------------------------------- SFX quality pass
def _envelope(y: np.ndarray, sr: int, block: float = 0.005) -> np.ndarray:
    return M.block_db(y, sr, block)


def _centroid(y: np.ndarray, sr: int, n_fft: int = 1024, hop: int = 512) -> np.ndarray:
    m = M.mono(M.as2d(y)).astype(np.float64)
    if len(m) < n_fft:
        return np.zeros(0)
    idx = np.arange(0, len(m) - n_fft, hop)
    S = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(m, n_fft)[idx] * np.hanning(n_fft), axis=1)) ** 2
    f = np.fft.rfftfreq(n_fft, 1 / sr)
    return (S * f).sum(1) / (S.sum(1) + 1e-20)


def sfx_quality(name: str, y: np.ndarray | None = None, sr: int = 48000, png: str | Path | None = None) -> dict:
    """Objective checks of a generated SFX (no listening): technical (peak, DC, clicks at the ends, clipping) and
    shape per type (riser rises in level and brightness, impact has a fast attack and low-frequency weight, whoosh is
    one hump, swell grows, pop/click are short). Writes a small spectrogram PNG when `png` is given."""
    from ..fx import sfx
    if y is None:
        y = sfx.render_array(name)
        sr = sfx.SR
    y = M.as2d(y)
    checks = []

    def ck(label, ok, detail):
        checks.append({"check": label, "ok": bool(ok), "detail": detail})
    pk = float(np.abs(y).max())
    ck("peak below -1 dBFS", pk < 10 ** (-1 / 20), f"{20 * np.log10(pk + 1e-12):.1f} dBFS")
    dc = float(np.abs(y.mean(axis=0)).max())
    ck("no DC offset", dc < 1e-3, f"{dc:.2e}")
    # a click is a discontinuity: the first/last samples must be ~0 (a deliberate 2 ms attack ramp is fine)
    e0 = float(np.abs(y[:3]).max()) / (pk + 1e-12)
    e1 = float(np.abs(y[-3:]).max()) / (pk + 1e-12)
    ck("silent ends (no click)", e0 < 0.02 and e1 < 0.02, f"first/last 3 samples at {e0:.3f}/{e1:.3f} of peak")
    ck("not clipped", M.clipping(y)["clipped_runs"] == 0, f"{M.clipping(y)['clipped_runs']} flat-top runs")
    env = _envelope(y, sr)
    t = np.arange(len(env)) * 0.005
    cen = _centroid(y, sr)
    dur = len(y) / sr
    ipk = int(np.argmax(env)) if len(env) else 0
    if name in ("riser", "swell"):
        live = env > env.max() - 40
        r = float(np.corrcoef(t[live], env[live])[0, 1]) if live.sum() > 5 else 0.0
        ck("level rises", r > 0.7 and ipk > 0.7 * len(env), f"corr(level, time) {r:.2f}, peak at {ipk / max(1, len(env)):.0%}")
        if name == "riser" and len(cen) > 8:
            third = len(cen) // 3
            ck("brightness rises", cen[-third:].mean() > 1.5 * cen[:third].mean(), f"centroid {cen[:third].mean():.0f} -> {cen[-third:].mean():.0f} Hz")
    if name == "impact":
        attack = int(np.argmax(env >= env.max() - 3)) * 0.005
        ck("fast attack", attack <= 0.015, f"within 3 dB of peak after {attack * 1000:.0f} ms")
        m = M.mono(y)
        F = np.abs(np.fft.rfft(m)) ** 2
        f = np.fft.rfftfreq(len(m), 1 / sr)
        low = F[f < 200].sum() / (F.sum() + 1e-20)
        ck("low-frequency weight", low > 0.3, f"{low:.0%} of energy below 200 Hz")
        after = env[ipk:]
        drop = after[0] - after[min(len(after) - 1, int(0.5 / 0.005))] if len(after) > 10 else 0
        ck("decays", drop > 12, f"{drop:.0f} dB down 0.5 s after the peak")
    if name == "whoosh":
        rel = ipk / max(1, len(env))
        ck("single hump", 0.3 <= rel <= 0.8 and env[0] < env.max() - 20 and env[-1] < env.max() - 20, f"peak at {rel:.0%} of {dur:.2f} s")
    if name in ("pop", "click"):
        above = (env > env.max() - 20).sum() * 0.005
        ck("short", above <= (0.08 if name == "pop" else 0.02), f"{above * 1000:.0f} ms within 20 dB of peak")
    if png:
        spectrogram_png(y, sr, png)
    return {"name": name, "duration": round(dur, 3), "checks": checks, "passed": all(c["ok"] for c in checks),
            "method": "envelope + spectrogram measurements; human listening still required"}


def spectrogram_png(y: np.ndarray, sr: int, path: str | Path, width: int = 480, height: int = 200, fmax: float = 16000) -> Path:
    """Log-magnitude spectrogram as a small grayscale PNG (Pillow, no matplotlib). Brighter = louder; 80 dB range."""
    from PIL import Image
    m = M.mono(M.as2d(y)).astype(np.float64)
    n_fft = 1024
    hop = max(64, (len(m) - n_fft) // width) if len(m) > n_fft else 64
    idx = np.arange(0, max(1, len(m) - n_fft), hop)
    pad = np.concatenate([m, np.zeros(n_fft)])
    S = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(pad, n_fft)[idx] * np.hanning(n_fft), axis=1))
    f = np.fft.rfftfreq(n_fft, 1 / sr)
    S = 20 * np.log10(S[:, f <= fmax] + 1e-9)
    S = np.clip((S - (S.max() - 80)) / 80, 0, 1)
    img = Image.fromarray((S.T[::-1] * 255).astype(np.uint8)).resize((width, height))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    return path
