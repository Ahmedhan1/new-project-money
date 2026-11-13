"""Objective audio measurements on decoded PCM (numpy), shared by the benchmark, the dialogue chain and audio QA.

Everything here is a measurement, not a judgement of how something sounds: no person listens in this pipeline, so
every report built on these numbers must still say that human listening is required.

    decode(path, sr, channels, start, duration) -> float32 array (n, ch)
    loudness(x, sr)        -> {"lufs", "lra", "true_peak"}   ITU-R BS.1770-4 / EBU Tech 3342 in numpy
                              (cross-checked against FFmpeg's ebur128 in tests/fast/test_c2_audio_measure.py)
    profile(x, sr)         -> every metric of the audio benchmark (see PROFILE_KEYS)

Definitions (all levels dBFS, blocks of 50 ms on the channel mean unless stated):
  noise_floor_db        power mean of the quietest 10 % of blocks (digital-silence blocks < -100 dBFS excluded and
                        reported separately as digital_silence_s)
  speech_band_snr_db    in the 300-3400 Hz band: median of the loudest 30 % of blocks minus the power mean of the
                        quietest 10 %. An estimate (no reference signal), comparable between versions of one file.
  clipped_samples       samples at >= 0.999 FS, plus samples in flat-top runs (>= 3 equal consecutive samples at the
                        channel's own peak, when that peak is > 0.5 FS: clipping that happened before a gain change)
  dc_offset             mean per channel (linear) and the worst channel in dBFS
  channel_imbalance_db  RMS(L) - RMS(R) over active blocks; `dead_channel` and `dual_mono` flags
  noise_rel_lufs_db     noise floor minus integrated loudness (comparable before/after a gain change)
  bandwidth_hz          highest frequency where the loud-block average spectrum (31-bin smoothed) is within 60 dB of
                        its 100-4000 Hz maximum (1948 optical track ~5.7 kHz, modern AAC dialogue 8-16 kHz)
  speaker_spread_db     see speaker_levels(): silence-separated segments clustered by voice (the diariser's WeSpeaker
                        embeddings when installed, else its MFCC fallback); spread = loudest minus quietest cluster's
                        median speech level
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from .. import costguard

BLOCK_S = 0.05
FS_CLIP = 0.999


# ---------------------------------------------------------------- decoding
def decode(path: str | Path, sr: int = 48000, channels: int = 2, start: float | None = None,
           duration: float | None = None) -> np.ndarray:
    """Decode the first audio stream to float32 PCM, shape (n, channels). Empty array if there is no audio."""
    costguard.authorize("ffmpeg", "audio-measure")
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{float(duration):.3f}"]
    cmd += ["-vn", "-map", "0:a:0", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=3600)
    if p.returncode != 0:
        return np.zeros((0, channels), dtype=np.float32)
    return np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, channels).copy()


def write_wav(path: str | Path, x: np.ndarray, sr: int) -> Path:
    """Float PCM (n,) or (n, ch) -> 24-bit WAV via FFmpeg (no soundfile dependency)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(x, dtype=np.float32)
    ch = 1 if x.ndim == 1 else x.shape[1]
    costguard.authorize("ffmpeg", "audio-write")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
                    "-c:a", "pcm_s24le", str(path)], input=np.ascontiguousarray(x).tobytes(), check=True, timeout=3600)
    return path


def as2d(x: np.ndarray) -> np.ndarray:
    return x[:, None] if x.ndim == 1 else x


def mono(x: np.ndarray) -> np.ndarray:
    return x if x.ndim == 1 else x.mean(axis=1)


# ---------------------------------------------------------------- BS.1770 loudness
def _biquads(sr: int):
    # K-weighting for any sample rate (shelf + RLB high-pass, analog prototypes of BS.1770 at 48 kHz)
    G, Q, fc = 3.99984385397, 0.7071752369554193, 1681.9744509555319
    A = 10 ** (G / 40)
    w0 = 2 * np.pi * fc / sr
    al = np.sin(w0) / (2 * Q)
    c = np.cos(w0)
    b1 = [A * ((A + 1) + (A - 1) * c + 2 * np.sqrt(A) * al), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - 2 * np.sqrt(A) * al)]
    a1 = [(A + 1) - (A - 1) * c + 2 * np.sqrt(A) * al, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - 2 * np.sqrt(A) * al]
    Q2, fc2 = 0.5003270373253953, 38.13547087613982
    w0 = 2 * np.pi * fc2 / sr
    al = np.sin(w0) / (2 * Q2)
    a2 = [1 + al, -2 * np.cos(w0), 1 - al]
    b2 = [1.0, -2.0, 1.0]
    return (np.array(b1) / a1[0], np.array(a1) / a1[0]), (np.array(b2), np.array(a2) / a2[0])


def k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import lfilter
    (b1, a1), (b2, a2) = _biquads(sr)
    return lfilter(b2, a2, lfilter(b1, a1, as2d(x).astype(np.float64), axis=0), axis=0)


def _block_ms(y: np.ndarray, sr: int, win: float, step: float) -> np.ndarray:
    """Sum over channels of mean square per block (BS.1770 z_i, all channel weights 1)."""
    w, s = int(win * sr), int(step * sr)
    if len(y) < w:
        return np.zeros(0)
    c = np.cumsum(np.vstack([np.zeros((1, y.shape[1])), y ** 2]), axis=0)
    idx = np.arange(0, len(y) - w + 1, s)
    return ((c[idx + w] - c[idx]) / w).sum(axis=1)


def _lk(z):
    return -0.691 + 10 * np.log10(np.maximum(z, 1e-20))


def integrated(z: np.ndarray) -> float:
    if not len(z):
        return -99.0
    zz = z[_lk(z) > -70]
    if not len(zz):
        return -99.0
    rel = _lk(zz.mean()) - 10
    zz = zz[_lk(zz) > rel]
    return float(_lk(zz.mean())) if len(zz) else -99.0


def true_peak(x: np.ndarray, chunk: int = 1 << 20) -> float:
    """4x oversampled peak (dBTP), chunked with overlap so a long file never needs 4x its memory."""
    from scipy.signal import resample_poly
    x = as2d(x)
    pk, ov = 0.0, 64
    for a in range(0, len(x), chunk):
        seg = x[max(0, a - ov): a + chunk + ov].astype(np.float64)
        if len(seg) < 8:
            continue
        pk = max(pk, float(np.abs(resample_poly(seg, 4, 1, axis=0)).max()))
    return round(20 * np.log10(pk + 1e-12), 2)


def loudness(x: np.ndarray, sr: int) -> dict:
    x = as2d(x)
    if len(x) < int(0.4 * sr):
        return {"lufs": -99.0, "lra": 0.0, "true_peak": -99.0}
    y = k_weight(x, sr)
    lufs = integrated(_block_ms(y, sr, 0.4, 0.1))
    st = _block_ms(y, sr, 3.0, 0.1)
    lra = 0.0
    if len(st):
        s = st[_lk(st) > -70]
        if len(s):
            s = s[_lk(s) > _lk(s.mean()) - 20]
            if len(s) > 1:
                lv = _lk(s)
                lra = float(np.percentile(lv, 95) - np.percentile(lv, 10))
    return {"lufs": round(lufs, 2), "lra": round(lra, 2), "true_peak": true_peak(x)}


# ---------------------------------------------------------------- block statistics
def block_db(x: np.ndarray, sr: int, block: float = BLOCK_S) -> np.ndarray:
    m = mono(x).astype(np.float64)
    w = max(1, int(block * sr))
    k = len(m) // w
    if k == 0:
        return np.zeros(0)
    return 10 * np.log10((m[: k * w].reshape(k, w) ** 2).mean(1) + 1e-20)


def _pmean_db(db: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(10 ** (db / 10)))) if len(db) else -120.0


def noise_floor(db: np.ndarray) -> tuple[float, float]:
    """(noise floor dBFS, digital-silence fraction): power mean of the quietest 10 % of non-silent blocks."""
    live = db[db > -100]
    if not len(live):
        return -120.0, 1.0
    q = live[live <= np.percentile(live, 10)]
    return round(_pmean_db(q), 2), round(1 - len(live) / len(db), 4)


def bandpass(x: np.ndarray, sr: int, lo: float = 300.0, hi: float = 3400.0) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    sos = butter(4, [lo, min(hi, sr / 2 * 0.95)], btype="band", fs=sr, output="sos")
    return sosfiltfilt(sos, mono(x).astype(np.float64))


def speech_band(x: np.ndarray, sr: int) -> dict:
    """{"snr_db", "speech_db", "noise_db"} in the 300-3400 Hz band."""
    db = block_db(bandpass(x, sr), sr)
    live = db[db > -100]
    if len(live) < 20:
        return {"snr_db": 0.0, "speech_db": -120.0, "noise_db": -120.0}
    noise = _pmean_db(live[live <= np.percentile(live, 10)])
    speech = float(np.median(live[live >= np.percentile(live, 70)]))
    return {"snr_db": round(speech - noise, 2), "speech_db": round(speech, 2), "noise_db": round(noise, 2)}


def clipping(x: np.ndarray) -> dict:
    x = as2d(x)
    hard = int((np.abs(x) >= FS_CLIP).sum())
    flat, runs = 0, 0
    for c in range(x.shape[1]):
        a = np.abs(x[:, c])
        pk = float(a.max()) if len(a) else 0.0
        if pk <= 0.5:
            continue
        at = a >= pk - 1e-6
        # runs of >= 3 consecutive samples at the channel's own peak
        d = np.diff(np.concatenate([[0], at.astype(np.int8), [0]]))
        s, e = np.where(d == 1)[0], np.where(d == -1)[0]
        ln = e - s
        long = ln >= 3
        runs += int(long.sum())
        flat += int(ln[long].sum())
    return {"clipped_samples": max(hard, flat), "clipped_runs": runs, "hard_clipped_samples": hard}


def channels(x: np.ndarray, sr: int) -> dict:
    x = as2d(x)
    dc = [round(float(x[:, c].mean()), 6) for c in range(x.shape[1])]
    out = {"dc_offset": dc, "dc_offset_db": round(20 * np.log10(max(abs(v) for v in dc) + 1e-12), 1)}
    if x.shape[1] < 2:
        return out | {"channel_imbalance_db": 0.0, "dead_channel": None, "dual_mono": True, "correlation": 1.0}
    db = block_db(x, sr)
    act = db > (np.percentile(db[db > -100], 50) if (db > -100).any() else -100)
    w = int(BLOCK_S * sr)
    k = len(db)
    sel = np.repeat(act, w)[: k * w]
    L, R = x[: k * w, 0][sel].astype(np.float64), x[: k * w, 1][sel].astype(np.float64)
    rl, rr = (10 * np.log10(np.mean(v ** 2) + 1e-20) if len(v) else -200.0 for v in (L, R))
    corr = float(np.corrcoef(L, R)[0, 1]) if len(L) > 10 and L.std() > 0 and R.std() > 0 else 0.0
    dead = None
    if rl < -70 < rr - 20:
        dead = "L"
    elif rr < -70 < rl - 20:
        dead = "R"
    return out | {"channel_imbalance_db": round(rl - rr, 2), "dead_channel": dead, "dual_mono": corr > 0.999,
                  "correlation": round(corr, 4)}


def spectrum_facts(x: np.ndarray, sr: int, db: np.ndarray | None = None) -> dict:
    """Noise character from the quietest blocks: spectral flatness, hum (50/60 Hz harmonics), hiss (>6 kHz share)
    and the signal's 99 % energy bandwidth over the whole file."""
    m = mono(x).astype(np.float64)
    db = block_db(m, sr) if db is None else db
    w = int(BLOCK_S * sr)
    if len(db) < 20:
        return {}
    n_fft = 16384
    span = int(np.ceil(n_fft / w))
    q = (db <= np.percentile(db[db > -100], 15) + 3) & (db > -100) if (db > -100).any() else np.zeros(len(db), bool)
    # an FFT window must lie entirely inside quiet blocks, or speech leaks into the "noise" spectrum
    run = np.convolve(q.astype(int), np.ones(span, int), "full")[span - 1:len(q) + span - 1]
    quiet = np.where(run >= span)[0]
    if len(quiet) < 3:
        quiet = np.where(q)[0]
    win = np.hanning(n_fft)
    f = np.fft.rfftfreq(n_fft, 1 / sr)

    def avg_spec(blocks):
        acc, cnt = np.zeros(len(f)), 0
        for b in blocks[:: max(1, len(blocks) // 200)]:
            seg = m[b * w: b * w + n_fft]
            if len(seg) < n_fft:
                continue
            acc += np.abs(np.fft.rfft(seg * win)) ** 2
            cnt += 1
        return acc / max(cnt, 1)
    qs = avg_spec(quiet)
    band = (f > 100) & (f < min(8000, sr / 2))
    flat = float(np.exp(np.mean(np.log(qs[band] + 1e-20))) / (np.mean(qs[band]) + 1e-20)) if qs.any() else 0.0
    hum = {}
    for base in (50.0, 60.0):
        prom = []
        for h in range(1, 5):
            i = int(np.argmin(np.abs(f - base * h)))
            nb = np.r_[qs[max(0, i - 30): max(0, i - 4)], qs[i + 5: i + 31]]
            prom.append(10 * np.log10(qs[max(0, i - 1): i + 2].max() / (np.median(nb) + 1e-20) + 1e-20))
        hum[base] = float(0.5 * max(prom[0], 0) + 0.5 * max(max(prom[1:]), 0))  # fundamental must be present
    hz = max(hum, key=hum.get)
    tot = qs[(f > 100)].sum() + 1e-20
    hiss = float(10 * np.log10(qs[f > 6000].sum() / tot + 1e-20)) if sr > 12000 else -99.0
    loud = np.arange(len(db))[db >= np.percentile(db, 70)]
    sp = 10 * np.log10(avg_spec(loud) + 1e-20)
    sm = np.convolve(sp, np.ones(31) / 31, "same")
    ref = sm[(f > 100) & (f < 4000)].max()
    above = np.where((sm > ref - 60) & (f < sr / 2 * 0.98))[0]
    bw = float(f[above.max()]) if len(above) else 0.0
    return {"noise_flatness": round(flat, 3), "hum_hz": hz if hum[hz] > 10 else None, "hum_prominence_db": round(hum[hz], 1),
            "hiss_share_db": round(hiss, 1), "bandwidth_hz": round(bw)}


# ---------------------------------------------------------------- speech segments and speaker levels
def active_segments(x: np.ndarray, sr: int, min_len: float = 1.0, merge_gap: float = 0.3) -> list[dict]:
    """Silence-separated active regions: 50 ms blocks more than 10 dB over the noise floor (and above the median - 15 dB),
    gaps < merge_gap merged, regions shorter than min_len dropped."""
    db = block_db(x, sr)
    if not len(db):
        return []
    nf, _ = noise_floor(db)
    live = db[db > -100]
    thr = max(nf + 10, (np.median(live) - 15) if len(live) else -60)
    act = db > thr
    segs, i, n = [], 0, len(act)
    while i < n:
        if not act[i]:
            i += 1
            continue
        j = i
        while j < n and act[j]:
            j += 1
        segs.append([i, j])
        i = j
    merged = []
    for s in segs:
        if merged and (s[0] - merged[-1][1]) * BLOCK_S < merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [{"start": round(a * BLOCK_S, 3), "end": round(b * BLOCK_S, 3)} for a, b in merged if (b - a) * BLOCK_S >= min_len]


def speaker_levels(x: np.ndarray, sr: int, segments: list[dict] | None = None, turns: list[dict] | None = None,
                   max_speakers: int = 4, neural: bool | None = None) -> dict:
    """Per-speaker speech level. With diarisation `turns` ({start,end,speaker}) those labels are used; otherwise the
    silence-separated segments are clustered by voice (MFCC statistics, diarize's numpy fallback). Returns
    {"method", "speakers": {label: {"db", "seconds"}}, "spread_db", "segment_level_p10_p90_db"}."""
    from ..media import diarize as dz
    from . import post
    m = mono(x).astype(np.float64)
    if turns:
        method = "diarisation turns"
        labelled = turns
    else:
        segs = segments if segments is not None else active_segments(m, sr)
        labelled, method = [], "silence-separated segments, one cluster (too few to compare voices)"
        if len(segs) >= 4:
            x16 = m if sr == dz.SR else _resample(m, sr, dz.SR)
            use_nn = (dz.ensure_emb_model() is not None) if neural else (neural is None and dz.emb_model_path().exists())
            E, keep = dz.embed_neural(x16.astype(np.float32), segs) if use_nn else dz.embed(x16.astype(np.float32), segs)
            if len(E) >= 4:
                lab, sil, k = dz._cluster(E, max_speakers)
                labelled = [{**segs[i], "speaker": f"V{int(lab[j]) + 1}"} for j, i in enumerate(keep)]
                how = "WeSpeaker ResNet34 embeddings" if use_nn else "MFCC statistics"
                method = f"silence-separated segments clustered by voice ({how}, {k} cluster(s), silhouette {sil:.2f})"
        if not labelled:
            labelled = [{**s, "speaker": "V1"} for s in segs]
    lv = post.speaker_levels(m, sr, labelled)
    seg_db = []
    for s in labelled:
        d = block_db(m[int(s["start"] * sr): int(s["end"] * sr)], sr)
        if len(d) >= 10:
            seg_db.append(float(np.median(d[d > np.percentile(d, 90) - 25])))
    spread = round(max(v["db"] for v in lv.values()) - min(v["db"] for v in lv.values()), 2) if len(lv) > 1 else 0.0
    p = (round(float(np.percentile(seg_db, 90) - np.percentile(seg_db, 10)), 2) if len(seg_db) >= 3 else 0.0)
    return {"method": method, "speakers": lv, "spread_db": spread, "segment_level_p10_p90_db": p, "segments": len(labelled),
            "labelled": labelled}


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    from math import gcd

    from scipy.signal import resample_poly
    g = gcd(sr_in, sr_out)
    return resample_poly(x, sr_out // g, sr_in // g, axis=0)


PROFILE_KEYS = ("lufs", "lra", "true_peak", "noise_floor_db", "noise_rel_lufs_db", "digital_silence_frac", "speech_band_snr_db", "clipped_samples",
                "dc_offset_db", "channel_imbalance_db", "speaker_spread_db")


def profile(x: np.ndarray, sr: int, speakers: bool = True, turns: list[dict] | None = None, neural: bool | None = None) -> dict:
    """All benchmark metrics for one signal."""
    x = as2d(x)
    db = block_db(x, sr)
    nf, dsil = noise_floor(db)
    out = {"duration": round(len(x) / sr, 3), "sample_rate": sr, "channels": x.shape[1], **loudness(x, sr),
           "noise_floor_db": nf, "digital_silence_frac": dsil}
    out["noise_rel_lufs_db"] = round(nf - out["lufs"], 2) if out["lufs"] > -70 else None
    sb = speech_band(x, sr)
    out |= {"speech_band_snr_db": sb["snr_db"], "speech_band_speech_db": sb["speech_db"], "speech_band_noise_db": sb["noise_db"]}
    out |= clipping(x) | channels(x, sr) | spectrum_facts(x, sr, db)
    if speakers:
        sp = speaker_levels(x, sr, turns=turns, neural=neural)
        out |= {"speaker_spread_db": sp["spread_db"], "speaker_levels": sp["speakers"], "speaker_method": sp["method"],
                "segment_level_p10_p90_db": sp["segment_level_p10_p90_db"], "speech_segments": sp["segments"]}
    return out
