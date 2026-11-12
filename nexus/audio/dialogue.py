"""Dialogue chain v2 (Account 2, Agent C): measured, adaptive clean-up of a dialogue/voice source.

process(x, sr, material=None) analyses the signal (measure.profile) and runs only the steps the measurements call for:

  step            what it is (honest label)                                         runs when
  dc_highpass     DC removal (mean subtraction) + 2nd-order 70 Hz high-pass          always
  channels        dead channel -> copy of the live one; level-match L/R; dual mono   dead channel / |imbalance| > 1 dB
  declip          FFmpeg `adeclip` (autoregressive interpolation of clipped runs)    clipped runs found
  dehum           narrow notches at the hum fundamental and 3 harmonics             hum prominence > 10 dB
  denoise         denoise.default_for(material class): tool + strength measured in  noise class != studio
                  docs/results/a2_denoise_compare.json
  dereverb        WPE (weighted prediction error, Nakatani 2010 / Yoshioka 2012):    RT60 estimate > 0.5 s
                  own single-channel numpy implementation, STFT 1536/384 at 48 kHz,
                  10 taps, delay 3, 2 iterations. Not a neural de-reverberator.
  breaths         breath events (unvoiced, flat spectrum, between noise floor+6 dB    always (attenuation only:
                  and speech-12 dB, 0.12-0.9 s) attenuated by 6 dB; never removed     timing is never changed)
  deess           FFmpeg `deesser`                                                   sibilance ratio high
  eq              gentle tilt towards a long-term average speech spectrum            any band off by > 2 dB
                  (LTASS-like curve), max +-4 dB per octave band, FFmpeg equalizers
  speakers        per-speaker gain from voice-clustered segments                     2+ voices, spread >= 1.5 dB
                  (measure.speaker_levels), 60 ms ramps
  compress        FFmpeg acompressor 2:1 at -24 dB                                   always
  roomtone        digital-silence gaps >= 30 ms filled with room tone cut from the  digital silence found
                  source's own quietest stretch (looped with crossfades)
  loudness        gain to target LUFS (BS.1770) + FFmpeg alimiter (true peak         always
                  <= -1.5 dBTP, checked)

Every step records what it changed; process() returns the processed signal and an AudioMasterManifest (contracts)
with before/after measurements. Objective numbers only: a person must still listen before anything ships.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from . import denoise as D
from . import measure as M

TP_LIMIT = -1.5
BREATH_DB = -6.0


# ---------------------------------------------------------------- small helpers
def _ramp_env(n: int, sr: int, spans: list[tuple[float, float, float]], ramp: float = 0.06, base_db: float = 0.0) -> np.ndarray:
    """Gain envelope (linear) of length n: base_db everywhere, `db` inside each (start, end, db) span, linear ramps."""
    g = np.full(n, base_db, dtype=np.float64)
    r = max(1, int(ramp * sr))
    for s, e, db in spans:
        a, b = max(0, int(s * sr)), min(n, int(e * sr))
        if b <= a:
            continue
        g[a:b] = db
        lo = max(0, a - r)
        g[lo:a] = np.linspace(base_db, db, a - lo, endpoint=False) if a > lo else g[lo:a]
        hi = min(n, b + r)
        g[b:hi] = np.linspace(db, base_db, hi - b, endpoint=False) if hi > b else g[b:hi]
    return 10 ** (g / 20)


def dc_highpass(x: np.ndarray, sr: int, hz: float = 70.0) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    x = M.as2d(x).astype(np.float64)
    x = x - x.mean(axis=0, keepdims=True)
    return sosfiltfilt(butter(2, hz, "high", fs=sr, output="sos"), x, axis=0).astype(np.float32)


def fix_channels(x: np.ndarray, sr: int) -> tuple[np.ndarray, dict]:
    x = M.as2d(x).copy()
    if x.shape[1] < 2:
        return x, {"applied": False}
    c = M.channels(x, sr)
    if c["dead_channel"]:
        live = 0 if c["dead_channel"] == "R" else 1
        x[:, 1 - live] = x[:, live]
        return x, {"applied": True, "action": f"dead channel {c['dead_channel']} replaced by the live one (dual mono)"}
    imb = c["channel_imbalance_db"]
    if abs(imb) > 1.0:
        half = imb / 2
        x[:, 0] *= 10 ** (-half / 20)
        x[:, 1] *= 10 ** (half / 20)
        return x, {"applied": True, "action": f"L/R level matched ({imb:+.2f} dB imbalance)"}
    return x, {"applied": False, "dual_mono": c["dual_mono"]}


def declip(x: np.ndarray, sr: int) -> np.ndarray:
    # FFmpeg defaults: on Piper speech clipped at 0.6 FS (1565 clipped samples) the squared error vs the unclipped
    # signal fell 10x; on an unnatural 70-harmonic buzz with half the waveform clipped it rose (tests/fast/test_c2_dialogue.py)
    return D.ffilter(x, sr, "adeclip")


def dehum(x: np.ndarray, sr: int, hz: float) -> np.ndarray:
    af = ",".join(f"bandreject=f={hz * k:.1f}:width_type=q:w={12 + 4 * k}" for k in range(1, 5))
    return D.ffilter(x, sr, af)


# ---------------------------------------------------------------- reverberation: estimate + WPE
def estimate_rt60(x: np.ndarray, sr: int) -> dict:
    """Reverberation estimate from free decays after speech offsets (5 ms block levels). For every offset whose peak
    (last 100 ms) is >= 30 dB above the noise floor: the time the level takes to fall from 5 to 25 dB below that peak
    (T20-style), RT60 = 3 x that time; median over offsets. Offsets that are interrupted by new speech are skipped.
    An estimate (the decay is partly articulation), meant for 'dry vs reverberant', not for room acoustics."""
    blk = 0.005
    db = M.block_db(x, sr, blk)
    if len(db) < 400:
        return {"rt60_s": None, "decays": 0}
    nf, _ = M.noise_floor(db)
    t20 = []
    i, n, back = 20, len(db), 20
    while i < n - 2:
        pk = db[i - back:i].max()
        if pk >= nf + 30 and db[i] < pk - 5 <= db[i - 1]:
            j = i
            while j < min(n, i + 240) and db[j] > pk - 25 and db[j] <= pk - 4:
                j += 1
            if j < n and db[j] <= pk - 25:
                t20.append((j - i) * blk)
            i = j + 1
        else:
            i += 1
    if len(t20) < 5:
        return {"rt60_s": None, "decays": len(t20)}
    return {"rt60_s": round(3.0 * float(np.median(t20)), 3), "decays": len(t20)}


def wpe(x: np.ndarray, sr: int, taps: int = 10, delay: int = 3, iterations: int = 2) -> np.ndarray:
    """Single-channel WPE dereverberation per channel. Time-aligned output of the same length."""
    from scipy.signal import istft, stft
    x = M.as2d(x)
    n_fft = int(round(1536 * sr / 48000 / 2) * 2)
    hop = n_fft // 4
    out = np.zeros_like(x, dtype=np.float32)
    for c in range(x.shape[1]):
        _, _, Y = stft(x[:, c].astype(np.float64), sr, nperseg=n_fft, noverlap=n_fft - hop, boundary="even")
        F, T = Y.shape
        Dd = np.zeros_like(Y)
        chunk = max(1, int(2.5e7 // max(1, T * taps * 16)))
        for f0 in range(0, F, chunk):
            y = Y[f0:f0 + chunk]                                     # (f, T)
            Xs = np.zeros((y.shape[0], T, taps), dtype=complex)
            for k in range(taps):
                s = delay + k
                Xs[:, s:, k] = y[:, : T - s]
            d = y.copy()
            for _ in range(iterations):
                lam = np.maximum(np.abs(d) ** 2, 1e-10)
                lam = np.maximum(lam, 1e-4 * lam.max(axis=1, keepdims=True))
                Xw = Xs / lam[:, :, None]
                R = np.einsum("ftk,ftl->fkl", Xw.conj(), Xs) + 1e-8 * np.eye(taps)[None]
                r = np.einsum("ftk,ft->fk", Xw.conj(), y)
                g = np.linalg.solve(R, r[..., None])[..., 0]
                d = y - np.einsum("ftk,fk->ft", Xs, g)
            Dd[f0:f0 + chunk] = d
        _, z = istft(Dd, sr, nperseg=n_fft, noverlap=n_fft - hop, boundary=True)
        z = z[: len(x)]
        out[: len(z), c] = z
    return out


def clarity_c50(x: np.ndarray, sr: int) -> float:
    """Speech-offset clarity proxy: mean level of the 50 ms after offsets vs the 50-300 ms after (higher = drier)."""
    db = M.block_db(x, sr, 0.01)
    nf, _ = M.noise_floor(db)
    vals = []
    for i in range(15, len(db) - 31):
        if db[i - 15:i].max() >= nf + 25 and db[i] < db[i - 15:i].max() - 5 <= db[i - 1]:
            early = M._pmean_db(db[i - 5:i])
            late = M._pmean_db(db[i + 5:i + 30])
            vals.append(early - late)
    return round(float(np.median(vals)), 2) if vals else 0.0


# ---------------------------------------------------------------- breaths
def _voicing(frames: np.ndarray, sr: int) -> np.ndarray:
    """Normalised autocorrelation peak in the 70-400 Hz pitch range per frame (1 = strongly periodic)."""
    f = frames - frames.mean(axis=1, keepdims=True)
    n = f.shape[1]
    F = np.fft.rfft(f, 2 * n, axis=1)
    ac = np.fft.irfft(np.abs(F) ** 2, axis=1)[:, :n]
    ac /= ac[:, :1] + 1e-12
    lo, hi = int(sr / 400), min(n - 1, int(sr / 70))
    return ac[:, lo:hi].max(axis=1)


def find_breaths(x: np.ndarray, sr: int) -> list[dict]:
    m = M.mono(M.as2d(x)).astype(np.float64)
    w = int(0.02 * sr)
    k = len(m) // w
    if k < 50:
        return []
    fr = m[: k * w].reshape(k, w)
    db = 10 * np.log10((fr ** 2).mean(1) + 1e-20)
    nf, _ = M.noise_floor(M.block_db(m, sr))
    live = db[db > -100]
    speech = float(np.median(live[live >= np.percentile(live, 70)]))
    spec = np.abs(np.fft.rfft(fr * np.hanning(w), axis=1)) ** 2
    f = np.fft.rfftfreq(w, 1 / sr)
    band = (f > 300) & (f < 8000)
    flat = np.exp(np.mean(np.log(spec[:, band] + 1e-20), axis=1)) / (np.mean(spec[:, band], axis=1) + 1e-20)
    hf = spec[:, f > 1000].sum(1) / (spec[:, f > 80].sum(1) + 1e-20)
    voi = _voicing(fr, sr)
    cand = (db > nf + 6) & (db < speech - 12) & (flat > 0.2) & (hf > 0.5) & (voi < 0.45)
    out, i = [], 0
    while i < k:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < k and (cand[j] or (j + 1 < k and cand[j + 1])):
            j += 1
        dur = (j - i) * 0.02
        # a breath sits next to speech (within 0.6 s on at least one side)
        near = db[max(0, i - 30):i].max(initial=-200) >= speech - 10 or db[j:j + 30].max(initial=-200) >= speech - 10
        if 0.12 <= dur <= 0.9 and near:
            out.append({"start": round(i * 0.02, 3), "end": round(j * 0.02, 3), "level_db": round(float(db[i:j].max()), 1)})
        i = j + 1
    return out


def attenuate(x: np.ndarray, sr: int, events: list[dict], db: float = BREATH_DB) -> np.ndarray:
    env = _ramp_env(len(x), sr, [(e["start"], e["end"], db) for e in events], ramp=0.03)
    return (M.as2d(x) * env[:, None]).astype(np.float32)


# ---------------------------------------------------------------- de-ess / EQ
def sibilance_ratio(x: np.ndarray, sr: int) -> float:
    """dB of 5-9 kHz energy relative to 300-3400 Hz energy, on the loudest 30 % of 20 ms frames."""
    m = M.mono(M.as2d(x)).astype(np.float64)
    if sr < 20000:
        return -99.0
    w = int(0.02 * sr)
    k = len(m) // w
    fr = m[: k * w].reshape(k, w)
    db = 10 * np.log10((fr ** 2).mean(1) + 1e-20)
    sel = fr[db >= np.percentile(db, 70)]
    spec = np.abs(np.fft.rfft(sel * np.hanning(w), axis=1)) ** 2
    f = np.fft.rfftfreq(w, 1 / sr)
    s = spec[:, (f >= 5000) & (f <= 9000)].sum(1)
    v = spec[:, (f >= 300) & (f <= 3400)].sum(1) + 1e-20
    # the 95th percentile frame: sibilant bursts, not the average
    return round(float(10 * np.log10(np.percentile(s / v, 95) + 1e-20)), 2)


# LTASS-like target (relative dB per octave band, 0 at 500 Hz); speech-active frames only
LTASS = {125: -4.0, 250: 0.0, 500: 0.0, 1000: -4.0, 2000: -9.0, 4000: -14.0, 8000: -21.0}


def ltas(x: np.ndarray, sr: int) -> dict:
    m = M.mono(M.as2d(x)).astype(np.float64)
    n_fft = 4096
    S, f = D._stft_db(m, sr, n_fft, n_fft // 2)
    if not len(S):
        return {}
    lv = 10 * np.log10(np.mean(10 ** (S / 10), axis=1) + 1e-14)
    P = 10 ** (S[lv >= np.percentile(lv, 70)] / 10)
    out = {}
    for c in LTASS:
        band = (f >= c / np.sqrt(2)) & (f < c * np.sqrt(2))
        if c * np.sqrt(2) > sr / 2 or not band.any():
            continue
        out[c] = float(10 * np.log10(P[:, band].sum(1).mean() + 1e-20))
    ref = out.get(500)
    return {c: round(v - ref, 2) for c, v in out.items()} if ref is not None else {}


def eq_plan(x: np.ndarray, sr: int, max_db: float = 4.0, dead_band: float = 2.0, bandwidth_hz: float | None = None) -> list[dict]:
    """Gentle corrections (half the measured deviation, max +-max_db) for octave bands off the LTASS-like curve by
    more than dead_band. Bands above the source's bandwidth are never boosted (boosting would only lift hiss)."""
    cur = ltas(x, sr)
    plan = []
    for c, v in cur.items():
        dev = LTASS[c] - v
        if abs(dev) <= dead_band or c == 500:
            continue
        g = float(np.clip(dev * 0.5, -max_db, max_db))
        if g > 0 and bandwidth_hz and c * 1.5 > bandwidth_hz:
            continue
        if g > 0 and c <= 125:
            continue  # never boost rumble
        plan.append({"f": c, "gain_db": round(g, 1), "measured_db": v, "target_db": LTASS[c]})
    return plan


def eq_chain(plan: list[dict]) -> str:
    return ",".join(f"equalizer=f={p['f']}:t=o:w=1:g={p['gain_db']}" for p in plan)


# ---------------------------------------------------------------- speakers
def speaker_plan(x: np.ndarray, sr: int, turns: list[dict] | None = None, min_diff: float = 1.5, max_db: float = 9.0) -> dict:
    lv = M.speaker_levels(x, sr, turns=turns)
    spk = lv["speakers"]
    gains = {}
    if len(spk) >= 2:
        ref = max(spk, key=lambda s: spk[s]["seconds"])
        for s, v in spk.items():
            g = spk[ref]["db"] - v["db"]
            if abs(g) >= min_diff:
                gains[s] = round(float(np.clip(g, -max_db, max_db)), 2)
    return {"levels": spk, "method": lv["method"], "spread_db": lv["spread_db"], "gains_db": gains, "labelled": lv.get("labelled", [])}


def apply_speaker_gains(x: np.ndarray, sr: int, labelled: list[dict], gains: dict) -> np.ndarray:
    spans = [(s["start"], s["end"], gains[s["speaker"]]) for s in labelled if s.get("speaker") in gains]
    if not spans:
        return M.as2d(x)
    return (M.as2d(x) * _ramp_env(len(x), sr, spans)[:, None]).astype(np.float32)


# ---------------------------------------------------------------- room tone
def room_tone_from(x: np.ndarray, sr: int, duration: float, seed: int = 0) -> np.ndarray:
    """Room tone of `duration` s cut from the source's quietest non-silent stretch (>= 0.3 s), looped with 50 ms
    equal-power crossfades at random offsets. Falls back to shaped noise at the measured floor."""
    x = M.as2d(x)
    db = M.block_db(x, sr)
    live = db > -100
    n = max(1, int(duration * sr))
    if live.sum() >= 6:
        thr = np.percentile(db[live], 10) + 3
        q = (db <= thr) & live
        best, cur, start = (0, 0), 0, 0
        for i, v in enumerate(q):
            if v:
                if cur == 0:
                    start = i
                cur += 1
                if cur > best[1]:
                    best = (start, cur)
            else:
                cur = 0
        w = int(M.BLOCK_S * sr)
        if best[1] * M.BLOCK_S >= 0.3:
            src = x[best[0] * w + w: (best[0] + best[1]) * w - w] if best[1] > 4 else x[best[0] * w:(best[0] + best[1]) * w]
            rng = np.random.default_rng(seed)
            xf = min(int(0.05 * sr), len(src) // 4)
            out = np.zeros((n + len(src), x.shape[1]), np.float32)
            pos = 0
            fade = np.sqrt(np.linspace(0, 1, xf))[:, None] if xf else None
            while pos < n:
                off = int(rng.integers(0, max(1, len(src) // 3)))
                piece = src[off:].copy()
                if xf and len(piece) > 2 * xf:
                    piece[:xf] *= fade
                    piece[-xf:] *= fade[::-1]
                out[pos:pos + len(piece)] += piece
                pos += max(1, len(piece) - xf)
            return out[:n]
    from . import master
    nf, _ = M.noise_floor(db)
    p = master.room_tone(duration, seed=seed, level_db=max(-80.0, nf), channels=x.shape[1])
    return M.decode(p, sr, x.shape[1])[:n]


def digital_silence_gaps(x: np.ndarray, sr: int, min_s: float = 0.03, thr: float = 1e-5) -> list[tuple[float, float]]:
    a = np.abs(M.as2d(x)).max(axis=1) < thr
    d = np.diff(np.concatenate([[0], a.astype(np.int8), [0]]))
    s, e = np.where(d == 1)[0], np.where(d == -1)[0]
    return [(round(i / sr, 4), round(j / sr, 4)) for i, j in zip(s, e) if (j - i) / sr >= min_s]


def fill_gaps(x: np.ndarray, sr: int, gaps: list[tuple[float, float]] | None = None, xfade: float = 0.01) -> tuple[np.ndarray, list]:
    """Fill gaps (default: digital-silence runs >= 30 ms) with the source's own room tone; the signal length and every
    pause duration stay unchanged (the gap is filled, not closed)."""
    x = M.as2d(x).copy()
    gaps = digital_silence_gaps(x, sr) if gaps is None else gaps
    if not gaps:
        return x, []
    total = sum(e - s for s, e in gaps) + 0.2
    tone = room_tone_from(x, sr, total)
    pos = 0
    r = int(xfade * sr)
    for s, e in gaps:
        a, b = int(s * sr), int(e * sr)
        seg = tone[pos:pos + (b - a)].copy()
        pos += b - a
        if r and len(seg) > 2 * r:
            ramp = np.linspace(0, 1, r)[:, None]
            seg[:r] *= ramp
            seg[-r:] *= ramp[::-1]
        x[a:a + len(seg)] += seg
    return x, gaps


# ---------------------------------------------------------------- loudness
def to_loudness(x: np.ndarray, sr: int, target_lufs: float, tp_limit: float = TP_LIMIT) -> tuple[np.ndarray, dict]:
    lv = M.loudness(x, sr)["lufs"]
    if lv <= -70:
        return M.as2d(x), {"gain_db": 0.0}
    gain = target_lufs - lv
    y = M.as2d(x) * 10 ** (gain / 20)
    lim = 10 ** ((tp_limit - 0.5) / 20)
    for _ in range(3):
        z = D.ffilter(y, sr, f"alimiter=limit={lim:.4f}:attack=3:release=50:level=disabled:asc=1")
        tp = M.true_peak(z)
        if tp <= tp_limit:
            break
        lim *= 10 ** ((tp_limit - tp - 0.2) / 20)
    after = M.loudness(z, sr)
    return z, {"gain_db": round(gain, 2), "limiter_ceiling_dbfs": round(20 * np.log10(lim), 2), "lufs": after["lufs"], "true_peak": after["true_peak"]}


# ---------------------------------------------------------------- the chain
def plan(prof: dict, material: str | None = None) -> dict:
    cls, tool, strength = D.default_for(prof)
    if material:
        cls = material
        tool, strength = D.MATERIAL_DEFAULTS.get(material, (tool, strength))
        if tool not in ("none", *D.available()):
            tool, strength = "afftdn", "normal"
    return {"material": cls, "denoise": (tool, strength)}


def process(x: np.ndarray, sr: int, material: str | None = None, target_lufs: float = -16.0, turns: list[dict] | None = None,
            steps: set[str] | None = None, measure_speakers: bool = True) -> tuple[np.ndarray, dict]:
    """Run the adaptive chain. `steps` limits it to the named steps (default: all that the measurements call for).
    Returns (signal, report) where report = {"before", "after", "chain": [...], "material", "seconds"}."""
    t0 = time.perf_counter()
    want = (lambda s: steps is None or s in steps)
    x = M.as2d(np.asarray(x, dtype=np.float32))
    before = M.profile(x, sr, speakers=measure_speakers, turns=turns)
    pl = plan(before, material)
    chain: list[dict] = []

    def rec(name, applied, **kw):
        chain.append({"step": name, "applied": bool(applied), **kw})

    y = dc_highpass(x, sr) if want("dc_highpass") else x
    rec("dc_highpass", want("dc_highpass"), dc_before=before["dc_offset"], label="mean subtraction + 70 Hz high-pass")
    if want("channels"):
        y, info = fix_channels(y, sr)
        rec("channels", info.pop("applied"), **info)
    if want("declip") and before["clipped_runs"] >= 3:
        c0 = M.clipping(y)
        y = declip(y, sr)
        rec("declip", True, label="FFmpeg adeclip", clipped_before=c0, clipped_after=M.clipping(y))
    else:
        rec("declip", False, why=f"{before['clipped_runs']} clipped run(s)")
    if want("dehum") and before.get("hum_hz"):
        y = dehum(y, sr, before["hum_hz"])
        rec("dehum", True, hz=before["hum_hz"], prominence_db=before["hum_prominence_db"])
    else:
        rec("dehum", False, why=f"hum prominence {before.get('hum_prominence_db')} dB")
    tool, strength = pl["denoise"]
    if want("denoise") and tool != "none":
        y = D.apply(tool, y, sr, strength)
        rec("denoise", True, tool=tool, strength=strength, material=pl["material"], licence=D.TOOLS[tool]["licence"])
    else:
        rec("denoise", False, why=f"material class {pl['material']}: source already clean" if tool == "none" else "disabled")
    if want("dereverb"):
        rt = estimate_rt60(y, sr)
        if rt["rt60_s"] and rt["rt60_s"] > 0.5:
            c_in = clarity_c50(y, sr)
            y = wpe(y, sr)
            rec("dereverb", True, label="WPE, own single-channel numpy implementation", rt60_est_before=rt["rt60_s"],
                rt60_est_after=estimate_rt60(y, sr)["rt60_s"], clarity_before_db=c_in, clarity_after_db=clarity_c50(y, sr))
        else:
            rec("dereverb", False, rt60_est=rt["rt60_s"], why="no measurable reverberation tail (estimate <= 0.5 s or too few decays)")
    if want("breaths"):
        br = find_breaths(y, sr)
        if br:
            y = attenuate(y, sr, br)
        rec("breaths", bool(br), events=len(br), attenuation_db=BREATH_DB, first=br[:5],
            note="attenuated, never removed: timing and pauses unchanged")
    if want("deess"):
        sib = sibilance_ratio(y, sr)
        if sib > -6:
            y = D.ffilter(y, sr, "deesser=i=0.4:m=0.5:f=0.5:s=o")
            rec("deess", True, sibilance_before_db=sib, sibilance_after_db=sibilance_ratio(y, sr))
        else:
            rec("deess", False, sibilance_db=sib, why="95th-percentile 5-9 kHz/300-3400 Hz ratio <= -6 dB")
    if want("eq"):
        ep = eq_plan(y, sr, bandwidth_hz=before.get("bandwidth_hz"))
        if ep:
            y = D.ffilter(y, sr, eq_chain(ep))
        rec("eq", bool(ep), bands=ep, label="gentle tilt towards an LTASS-like curve")
    if want("speakers") and measure_speakers:
        sp = speaker_plan(y, sr, turns=turns)
        if sp["gains_db"]:
            y = apply_speaker_gains(y, sr, sp["labelled"], sp["gains_db"])
        rec("speakers", bool(sp["gains_db"]), method=sp["method"], levels=sp["levels"], gains_db=sp["gains_db"], spread_before_db=sp["spread_db"])
    if want("compress"):
        y = D.ffilter(y, sr, "acompressor=threshold=-24dB:ratio=2:attack=12:release=180:makeup=1")
        rec("compress", True, label="acompressor 2:1 @ -24 dB")
    if want("roomtone"):
        y, gaps = fill_gaps(y, sr)
        rec("roomtone", bool(gaps), gaps=len(gaps), seconds=round(sum(e - s for s, e in gaps), 3))
    if want("loudness"):
        y, info = to_loudness(y, sr, target_lufs)
        rec("loudness", True, target_lufs=target_lufs, tp_limit=TP_LIMIT, **info)
    after = M.profile(y, sr, speakers=measure_speakers, turns=turns)
    return y, {"before": before, "after": after, "chain": chain, "material": pl["material"], "target_lufs": target_lufs,
               "seconds": round(time.perf_counter() - t0, 2), "rtf": round((time.perf_counter() - t0) / max(1e-3, len(x) / sr), 3)}


SUMMARY_KEYS = ("lufs", "lra", "true_peak", "noise_floor_db", "noise_rel_lufs_db", "speech_band_snr_db", "clipped_samples", "dc_offset_db",
                "channel_imbalance_db", "speaker_spread_db", "digital_silence_frac")


def manifest(src: str | Path, out: str | Path, report: dict) -> dict:
    """AudioMasterManifest (nexus.contracts) for a processed file."""
    from .. import contracts
    pick = lambda p: {k: p.get(k) for k in SUMMARY_KEYS if k in p}
    return contracts.make("AudioMasterManifest", input=contracts.artifact(src, role="source", hash_it=False),
                          output=contracts.artifact(out, role="dialogue-master"), chain=report["chain"],
                          before=pick(report["before"]), after=pick(report["after"]), target_lufs=report["target_lufs"],
                          material=report["material"], rtf=report["rtf"],
                          human_listening="required: these are objective measurements only")


def process_file(src: str | Path, out: str | Path, material: str | None = None, target_lufs: float = -16.0,
                 start: float | None = None, duration: float | None = None, sr: int = 48000, **kw) -> dict:
    """Decode `src` (any FFmpeg-readable file), run the chain, write a 24-bit WAV and return its AudioMasterManifest."""
    x = M.decode(src, sr, 2, start, duration)
    y, rep = process(x, sr, material=material, target_lufs=target_lufs, **kw)
    M.write_wav(out, y, sr)
    return manifest(src, out, rep)
