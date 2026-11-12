"""Noise reduction / dialogue enhancement: candidate tools, objective comparison metrics and the per-material default.

Candidates (all local, zero cost; licences read 2026-11-12):
  afftdn      FFmpeg FFT denoiser with noise tracking (LGPL-2.1+, part of FFmpeg)
  afwtdn      FFmpeg wavelet denoiser (LGPL-2.1+)
  anlmdn      FFmpeg non-local-means denoiser (LGPL-2.1+)
  arnndn      FFmpeg RNNoise (BSD-3-Clause) with GregorR's 'somnolent-hogwash' model (author: models are not creative
              works, not subject to copyright) - the existing clean_speech chain
  noisereduce spectral gating, Tim Sainburg's `noisereduce` 3.0.3 (MIT; PyPI wheel 22 KB, + joblib/tqdm). Optional:
              used only when importable, never installed automatically.
  DeepFilterNet  NOT used: code MIT/Apache-2.0, but the PyPI package needs PyTorch (CPU wheel 210 MB download,
              753 MB unpacked), pins numpy<2 (this venv: numpy 2.5 on Python 3.13), DeepFilterLib has no cp313 wheel
              (Rust build from source), and its weights download from GitHub (not an approved model host). Measured
              facts in docs/results/a2_denoise_compare.json.

Metrics (compare(), no human listening):
  noise_floor_reduction_db  input minus output noise floor (quietest 10 % of 50 ms blocks, measure.noise_floor)
  speech_band_change_db     change of the 300-3400 Hz level of the input's loudest 30 % blocks (speech kept ~0 dB)
  speech_lsd_db             log-spectral distance, 300-3400 Hz, on speech-active frames: vs the clean reference when
                            one exists (all bins); else vs the input on speech-DOMINANT bins only (>= 10 dB over the
                            input's noise spectrum), so removed noise is not counted as damage
  musical_noise_index       share of time-frequency bins in the residual pauses that are isolated peaks >= 12 dB over
                            their frame median and absent from the neighbouring frames ("birdies"/musical noise);
                            reported with the input's own value; a rise means artefacts
  residual_flatness         spectral flatness of the residual in pauses (white-ish = natural; a fall = tonal artefacts)
  si_sdr_db / si_sdr_gain_db  scale-invariant SDR vs the clean reference (synthetic mixtures only)
  rtf                       processing seconds / audio seconds on this shared 4-vCPU container
"""
from __future__ import annotations

import subprocess
import time

import numpy as np

from .. import costguard
from . import measure as M
from . import post

TOOLS = {
    "afftdn": {"licence": "LGPL-2.1+ (FFmpeg)", "install": "built into FFmpeg", "kind": "FFT spectral subtraction with noise tracking"},
    "afwtdn": {"licence": "LGPL-2.1+ (FFmpeg)", "install": "built into FFmpeg", "kind": "wavelet thresholding"},
    "anlmdn": {"licence": "LGPL-2.1+ (FFmpeg)", "install": "built into FFmpeg", "kind": "non-local means"},
    "arnndn": {"licence": "BSD-3-Clause (RNNoise); model: not copyrightable per its author", "install": "model file 298 KB",
               "kind": "recurrent neural network (speech vs noise), 48 kHz"},
    "noisereduce": {"licence": "MIT (noisereduce 3.0.3)", "install": "pip, 22 KB wheel + joblib/tqdm/cloudpickle (~3.5 MB)",
                    "kind": "spectral gating (stationary)"},
    "deepfilternet": {"licence": "MIT / Apache-2.0 (code)", "install": "needs torch: 210 MB wheel, 753 MB unpacked; numpy<2 pin; "
                      "no cp313 DeepFilterLib wheel; weights from GitHub", "kind": "deep filtering (neural)", "skipped": True},
}


def ffilter(x: np.ndarray, sr: int, af: str, out_sr: int | None = None) -> np.ndarray:
    """Run an FFmpeg audio filter chain on a float array (n, ch) through pipes."""
    costguard.authorize("ffmpeg", "audio-filter")
    x = M.as2d(np.asarray(x, dtype=np.float32))
    ch = x.shape[1]
    out_sr = out_sr or sr
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
                        "-af", af, "-ar", str(out_sr), "-ac", str(ch), "-f", "f32le", "-"],
                       input=np.ascontiguousarray(x).tobytes(), capture_output=True, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg filter failed ({af[:80]}): {p.stderr.decode(errors='replace')[-400:]}")
    y = np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, ch)
    n = int(round(len(x) * out_sr / sr))
    if len(y) < n:  # some filters drop a few trailing samples
        y = np.vstack([y, np.zeros((n - len(y), ch), np.float32)])
    return y[:n].copy()


def chain(tool: str, noise_floor_db: float = -50.0, strength: str = "normal") -> str | None:
    """FFmpeg filter text for an FFmpeg-based tool (None for non-FFmpeg tools)."""
    # afftdn's nf is a per-band level, about 20 dB above the 50 ms block noise floor (measured on interview/walktalk/ad:
    # nf at the block floor removes ~1 dB, nf = floor + 20 removes ~10 dB)
    nf = int(np.clip(round(noise_floor_db + 20), -80, -20))
    nr = {"light": 8, "normal": 12, "strong": 20}[strength]
    if tool == "afftdn":
        return f"afftdn=nr={nr}:nf={nf}:tn=1"
    if tool == "afwtdn":
        return {"light": "afwtdn=sigma=0.005:percent=70", "normal": "afwtdn=sigma=0.01:percent=85",
                "strong": "afwtdn=sigma=0.02:percent=95"}[strength]
    if tool == "anlmdn":
        return {"light": "anlmdn=s=0.01", "normal": "anlmdn=s=0.1", "strong": "anlmdn=s=1"}[strength]
    if tool == "arnndn":
        p = post.model_path()
        if not p.exists():
            return None
        mix = {"light": 0.7, "normal": 0.9, "strong": 1.0}[strength]
        return f"arnndn=m='{post._esc(p)}':mix={mix}"
    return None


def noisereduce_available() -> bool:
    try:
        import noisereduce  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def apply(tool: str, x: np.ndarray, sr: int, strength: str = "normal", noise_floor_db: float | None = None) -> np.ndarray:
    x = M.as2d(np.asarray(x, dtype=np.float32))
    if tool == "none":
        return x.copy()
    if noise_floor_db is None:
        noise_floor_db = M.noise_floor(M.block_db(x, sr))[0]
    if tool == "noisereduce":
        import noisereduce as nr
        prop = {"light": 0.6, "normal": 0.8, "strong": 1.0}[strength]
        # noise profile from the quietest blocks (without one, noisereduce profiles the whole clip, speech included,
        # and pulls the speech down by 4-7 dB - measured on the interview excerpt)
        db = M.block_db(x, sr)
        w = int(M.BLOCK_S * sr)
        q = np.where(db <= np.percentile(db, 10))[0]
        noise = np.concatenate([x[b * w:(b + 1) * w] for b in q]) if len(q) >= 4 else x
        y = nr.reduce_noise(y=x.T, sr=sr, y_noise=noise.T, stationary=True, prop_decrease=prop, n_jobs=1)
        return np.asarray(y, dtype=np.float32).reshape(x.shape[1], -1).T[: len(x)].copy()
    af = chain(tool, noise_floor_db, strength)
    if af is None:
        raise RuntimeError(f"{tool} is not available on this machine")
    return ffilter(x, sr, af)


def available(tools=("afftdn", "afwtdn", "anlmdn", "arnndn", "noisereduce")) -> list[str]:
    out = []
    for t in tools:
        if t == "noisereduce":
            if noisereduce_available():
                out.append(t)
        elif t == "arnndn":
            if post.model_path().exists():
                out.append(t)
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------- metrics
def _stft_db(m: np.ndarray, sr: int, n_fft: int = 1024, hop: int = 512) -> tuple[np.ndarray, np.ndarray]:
    if len(m) < n_fft:
        return np.zeros((0, n_fft // 2 + 1)), np.fft.rfftfreq(n_fft, 1 / sr)
    win = np.hanning(n_fft)
    idx = np.arange(0, len(m) - n_fft, hop)
    fr = np.lib.stride_tricks.sliding_window_view(m.astype(np.float64), n_fft)[idx] * win
    return 10 * np.log10(np.abs(np.fft.rfft(fr, axis=1)) ** 2 + 1e-14), np.fft.rfftfreq(n_fft, 1 / sr)


def _frame_masks(ref: np.ndarray, sr: int, n_fft: int = 1024, hop: int = 512):
    """Speech-active and pause frames from a reference signal's frame levels."""
    S, f = _stft_db(ref, sr, n_fft, hop)
    if not len(S):
        return np.zeros(0, bool), np.zeros(0, bool)
    lv = 10 * np.log10(np.mean(10 ** (S / 10), axis=1) + 1e-14)
    active = lv >= np.percentile(lv, 70)
    pause = lv <= np.percentile(lv, 15)
    return active, pause


def musical_noise_index(S: np.ndarray, f: np.ndarray, frames: np.ndarray) -> float:
    band = (f > 200) & (f < 8000)
    if frames.sum() < 3:
        return 0.0
    X = S[:, band]
    med = np.median(X, axis=1, keepdims=True)
    peak = X > med + 12
    iso = peak.copy()
    iso[1:] &= ~peak[:-1]
    iso[:-1] &= ~peak[1:]
    return round(float(iso[frames].mean()), 5)


def _flatness(S: np.ndarray, f: np.ndarray, frames: np.ndarray) -> float:
    band = (f > 200) & (f < 8000)
    if frames.sum() < 1:
        return 0.0
    P = 10 ** (S[frames][:, band] / 10)
    return round(float(np.mean(np.exp(np.mean(np.log(P + 1e-20), axis=1)) / (np.mean(P, axis=1) + 1e-20))), 4)


def lsd(A: np.ndarray, B: np.ndarray, f: np.ndarray, frames: np.ndarray, pause: np.ndarray | None = None,
        margin_db: float = 10.0) -> float:
    """Log-spectral distance (dB RMS) between A (reference) and B on `frames`, 300-3400 Hz. With `pause` frames the
    comparison is limited to speech-dominant bins (A at least margin_db above A's own noise spectrum in the pauses), so
    removing noise between harmonics is not counted as speech damage."""
    band = (f >= 300) & (f <= 3400)
    if frames.sum() < 1:
        return 0.0
    a, b = A[frames][:, band], B[frames][:, band]
    if pause is not None and pause.sum() >= 3:
        noise = 10 * np.log10(np.mean(10 ** (A[pause][:, band] / 10), axis=0) + 1e-14)
        dom = a > noise[None, :] + margin_db
        if dom.sum() < 50:
            return 0.0
        return round(float(np.sqrt(np.mean((a - b)[dom] ** 2))), 2)
    return round(float(np.mean(np.sqrt(np.mean((a - b) ** 2, axis=1)))), 2)


def si_sdr(ref: np.ndarray, est: np.ndarray) -> float:
    ref, est = ref - ref.mean(), est - est.mean()
    a = np.dot(est, ref) / (np.dot(ref, ref) + 1e-12)
    t = a * ref
    return round(float(10 * np.log10(np.dot(t, t) / (np.dot(est - t, est - t) + 1e-12) + 1e-12)), 2)


def align(ref: np.ndarray, est: np.ndarray, max_lag: int = 2400) -> np.ndarray:
    """Shift `est` to the lag (within +-max_lag samples) that best matches `ref` (filters such as anlmdn add latency)."""
    n = min(len(ref), len(est), 48000 * 20)
    r, e = ref[:n], est[:n]
    F = np.fft.rfft(r, 2 * n) * np.conj(np.fft.rfft(e, 2 * n))
    cc = np.fft.irfft(F, 2 * n)
    cc = np.concatenate([cc[-max_lag:], cc[: max_lag + 1]])
    lag = int(np.argmax(cc)) - max_lag
    out = np.roll(est, lag)
    if lag > 0:
        out[:lag] = 0
    elif lag < 0:
        out[lag:] = 0
    return out


def compare(x: np.ndarray, y: np.ndarray, sr: int, clean: np.ndarray | None = None) -> dict:
    """Objective before/after metrics for one processed signal (see module docstring)."""
    xm, ym = M.mono(M.as2d(x)).astype(np.float64), M.mono(M.as2d(y)).astype(np.float64)
    ym = align(xm, ym)
    ref = M.mono(M.as2d(clean)).astype(np.float64) if clean is not None else xm
    Sx, f = _stft_db(xm, sr)
    Sy, _ = _stft_db(ym, sr)
    Sr, _ = _stft_db(ref, sr)
    n = min(len(Sx), len(Sy), len(Sr))
    Sx, Sy, Sr = Sx[:n], Sy[:n], Sr[:n]
    active, _ = _frame_masks(ref, sr)
    _, pause = _frame_masks(xm, sr)   # pauses: where the INPUT is quietest (the noise lives there)
    active, pause = active[:n], pause[:n]
    nx, _ = M.noise_floor(M.block_db(xm, sr))
    ny, _ = M.noise_floor(M.block_db(ym, sr))
    bx, by = M.block_db(M.bandpass(xm, sr), sr), M.block_db(M.bandpass(ym, sr), sr)
    loud = bx >= np.percentile(bx, 70)
    k = min(len(bx), len(by))
    sb = float(10 * np.log10(np.mean(10 ** (by[:k][loud[:k]] / 10)) / np.mean(10 ** (bx[:k][loud[:k]] / 10))))
    out = {"noise_floor_in_db": nx, "noise_floor_out_db": ny, "noise_floor_reduction_db": round(nx - ny, 2),
           "speech_band_change_db": round(sb, 2), "speech_lsd_db": lsd(Sr, Sy, f, active) if clean is not None else lsd(Sx, Sy, f, active, pause),
           "musical_noise_index": musical_noise_index(Sy, f, pause), "musical_noise_index_in": musical_noise_index(Sx, f, pause),
           "residual_flatness": _flatness(Sy, f, pause), "residual_flatness_in": _flatness(Sx, f, pause)}
    if clean is not None:
        m = min(len(ref), len(ym), len(xm))
        out["si_sdr_db"] = si_sdr(ref[:m], ym[:m])
        out["si_sdr_in_db"] = si_sdr(ref[:m], xm[:m])
        out["si_sdr_gain_db"] = round(out["si_sdr_db"] - out["si_sdr_in_db"], 2)
    return out


def run_tool(tool: str, x: np.ndarray, sr: int, strength: str = "normal", clean=None) -> dict:
    t = time.perf_counter()
    y = apply(tool, x, sr, strength)
    el = time.perf_counter() - t
    return {"tool": tool, "strength": strength, "rtf": round(el / (len(x) / sr), 4), **compare(x, y, sr, clean)}, y


# ---------------------------------------------------------------- default per material
# Chosen from docs/results/a2_denoise_compare.json (see choose()). "none" when the source is already clean:
# processing a clean recording only adds risk.
MATERIAL_DEFAULTS = {
    "studio": ("none", "normal"),
    "interview": ("arnndn", "light"),
    "panel": ("arnndn", "light"),
    "podcast": ("arnndn", "light"),
    "outdoor": ("arnndn", "normal"),
    "documentary": ("afftdn", "light"),
    "archival": ("afftdn", "normal"),
}

# acceptance limits for a tool on a material (do not over-process)
MAX_SPEECH_CHANGE_DB = 1.5
MAX_LSD_DB = 6.0
MAX_MNI_RISE = 0.01


def acceptable(r: dict) -> bool:
    return (abs(r["speech_band_change_db"]) <= MAX_SPEECH_CHANGE_DB and r["speech_lsd_db"] <= MAX_LSD_DB
            and r["musical_noise_index"] - r["musical_noise_index_in"] <= MAX_MNI_RISE)


def choose(rows: list[dict], min_gain_db: float = 3.0) -> dict:
    """From compare() rows of one material: the acceptable row with the largest noise-floor reduction; ties within
    1 dB go to the gentler one (lower LSD). 'none' when no tool gains min_gain_db acceptably."""
    ok = [r for r in rows if acceptable(r) and r["noise_floor_reduction_db"] >= min_gain_db]
    if not ok:
        return {"tool": "none", "strength": "-", "why": f"no tool reduced the noise floor by >= {min_gain_db} dB within the speech limits"}
    best = max(r["noise_floor_reduction_db"] for r in ok)
    near = [r for r in ok if r["noise_floor_reduction_db"] >= best - 1.0]
    pick = min(near, key=lambda r: (r["speech_lsd_db"], r["rtf"]))
    return {"tool": pick["tool"], "strength": pick["strength"],
            "why": f"{pick['noise_floor_reduction_db']} dB floor reduction, speech band {pick['speech_band_change_db']:+} dB, "
                   f"LSD {pick['speech_lsd_db']} dB, MNI {pick['musical_noise_index']} (in {pick['musical_noise_index_in']})"}


def classify(prof: dict) -> str:
    """Material class from a measure.profile(): drives the default denoiser."""
    nf, snr = prof.get("noise_floor_db", -60), prof.get("speech_band_snr_db", 40)
    if prof.get("bandwidth_hz", 20000) < 7000 and (prof.get("hum_hz") or prof.get("hiss_share_db", -40) > -25 or nf > -48):
        return "archival"
    if nf <= -55 and snr >= 35:
        return "studio"
    if snr < 24 or prof.get("lra", 0) > 8:
        return "outdoor"
    return "interview"


def default_for(prof: dict) -> tuple[str, str, str]:
    """(material class, tool, strength); falls back to afftdn when the chosen tool is not installed."""
    cls = classify(prof)
    tool, strength = MATERIAL_DEFAULTS.get(cls, ("afftdn", "light"))
    if tool not in ("none", *available()):
        tool, strength = "afftdn", "normal"
    return cls, tool, strength
