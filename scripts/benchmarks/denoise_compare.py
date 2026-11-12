"""Noise reduction comparison (Account 2, Agent C) -> docs/results/a2_denoise_compare.json

    python scripts/benchmarks/denoise_compare.py [--real-s 30] [--start 60]

Part A, synthetic with a clean reference: Piper speech (CC0 voices en_US-joe-medium, en_US-ljspeech-medium) mixed with
three generated noise types: room (pink, ~22 dB SNR), outdoor (wind gusts + traffic rumble + broadband, ~10 dB SNR),
archival (band-limited 4.5 kHz speech + hiss + 60 Hz hum with harmonics + crackle, ~15 dB SNR). Metrics include
SI-SDR gain and log-spectral distance against the clean speech.
Part B, real benchmark media, no reference: 30 s excerpts of interview, panel, podcast, walktalk, documentary, ad.
Every tool x strength (light/normal/strong). Then denoise.choose() picks the default per material.
All numbers measured on a shared, noisy 4-vCPU container. Nobody listened: human listening is still required.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BENCH = Path("/home/user/new-project-money/.nexus/benchmarks")
SR = 48000
REAL = ["interview", "panel", "podcast", "walktalk", "documentary", "ad"]
LINES = ["The crew trained in a cave for six days.", "It was cold and very dark, so we used our helmet lamps.",
         "Every morning we checked the ropes, the radios and the water.", "Nobody knew how long the storm would last.",
         "Then the signal came back, clear and strong."]


def clean_speech(tmp: Path, voice: str) -> np.ndarray:
    from nexus.audio import measure as M
    from nexus.speech import tts
    parts = [np.zeros(int(0.5 * SR), np.float32)]
    for i, t in enumerate(LINES):
        p = tmp / f"{voice}_{i}.wav"
        tts.synthesize(t, p, "en", voice=voice, master=False, cache=False)
        parts += [M.mono(M.decode(p, SR, 1)), np.zeros(int(0.7 * SR), np.float32)]
    x = np.concatenate(parts)
    return (x / (np.abs(x).max() + 1e-9) * 0.5).astype(np.float32)


def noises(n: int, rng) -> dict:
    from scipy.signal import butter, lfilter, sosfilt
    t = np.arange(n) / SR
    w = rng.normal(0, 1, n)
    pink = lfilter([0.049922035, -0.095993537, 0.050612699, -0.004408786], [1, -2.494956002, 2.017265875, -0.522189400], w)
    brown = np.cumsum(rng.normal(0, 1, n))
    brown = sosfilt(butter(2, 20, "high", fs=SR, output="sos"), brown)
    gust = 0.4 + 0.6 * np.clip(np.convolve(rng.normal(0, 1, n // 4800 + 2), np.ones(3) / 3, "same"), 0, None).repeat(4800)[:n]
    wind = sosfilt(butter(2, [40, 400], "band", fs=SR, output="sos"), brown) * gust
    rumble = sosfilt(butter(2, 120, "low", fs=SR, output="sos"), rng.normal(0, 1, n)) * (1 + np.sin(2 * np.pi * t / 7.0))
    outdoor = wind / wind.std() + 0.6 * rumble / rumble.std() + 0.25 * pink / pink.std()
    hiss = sosfilt(butter(2, 2500, "high", fs=SR, output="sos"), w)
    hum = sum(a * np.sin(2 * np.pi * 60 * k * t) for k, a in ((1, 1.0), (2, 0.4), (3, 0.6), (5, 0.25)))
    crackle = np.zeros(n)
    idx = rng.choice(n, size=int(n / SR * 6), replace=False)
    crackle[idx] = rng.normal(0, 1, len(idx)) * 8
    archival = hiss / hiss.std() + 0.5 * hum / hum.std() + crackle
    return {"room": pink / pink.std(), "outdoor": outdoor / outdoor.std(), "archival": archival / archival.std()}


SNR = {"room": 22.0, "outdoor": 10.0, "archival": 15.0}


def mixtures(tmp: Path) -> dict:
    from scipy.signal import butter, sosfiltfilt
    out = {}
    rng = np.random.default_rng(7)
    for voice in ("en_US-joe-medium", "en_US-ljspeech-medium"):
        try:
            s = clean_speech(tmp, voice)
        except Exception as e:  # noqa: BLE001
            print("no voice", voice, e)
            continue
        nz = noises(len(s), rng)
        act = np.abs(s) > 0.01
        sp_rms = np.sqrt(np.mean(s[act] ** 2))
        for kind, nv in nz.items():
            ref = s
            if kind == "archival":   # optical track: speech itself is band-limited
                ref = sosfiltfilt(butter(4, [120, 4500], "band", fs=SR, output="sos"), s).astype(np.float32)
            g = sp_rms / 10 ** (SNR[kind] / 20)
            mix = ref + (nv * g).astype(np.float32)
            out[f"{kind}/{voice.split('-')[0].split('_')[1]}"] = (np.stack([mix, mix], 1).astype(np.float32), ref)
    return out


def main() -> int:
    from nexus.audio import denoise as D
    from nexus.audio import measure as M
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-s", type=float, default=30.0)
    ap.add_argument("--start", type=float, default=60.0)
    ap.add_argument("--tmp", default="/tmp/a2-c/denoise")
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "a2_denoise_compare.json"))
    a = ap.parse_args()
    tmp = Path(a.tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    tools = D.available()
    strengths = ("light", "normal", "strong")
    doc = {"what": "noise reduction comparison: objective metrics, no human listening", "date": time.strftime("%Y-%m-%d"),
           "machine": "shared 4-vCPU container, no GPU (RTF noisy)", "tools": D.TOOLS, "available": tools,
           "metrics": "nexus/audio/denoise.py docstring", "limits": {"max_speech_change_db": D.MAX_SPEECH_CHANGE_DB,
           "max_lsd_db": D.MAX_LSD_DB, "max_musical_noise_rise": D.MAX_MNI_RISE}, "synthetic": {}, "real": {}, "choice": {}}
    for name, (x, ref) in mixtures(tmp).items():
        rows = []
        for t in tools:
            for s in strengths:
                r, _ = D.run_tool(t, x, SR, s, clean=ref)
                rows.append(r)
                print(name, t, s, r["si_sdr_gain_db"], r["noise_floor_reduction_db"], r["speech_lsd_db"], r["rtf"], flush=True)
        doc["synthetic"][name] = {"snr_db": SNR[name.split("/")[0]], "rows": rows}
    for i in REAL:
        src = BENCH / i / "source.mp4"
        if not src.exists():
            continue
        x = M.decode(src, SR, 2, a.start, a.real_s)
        prof = M.profile(x, SR, speakers=False)
        rows = []
        for t in tools:
            for s in strengths:
                r, _ = D.run_tool(t, x, SR, s)
                rows.append(r)
                print(i, t, s, r["noise_floor_reduction_db"], r["speech_band_change_db"], r["speech_lsd_db"], r["rtf"], flush=True)
        cls = D.classify(prof)
        doc["real"][i] = {"excerpt": [a.start, a.real_s], "class": cls,
                          "profile": {k: prof.get(k) for k in ("noise_floor_db", "speech_band_snr_db", "hum_hz", "hiss_share_db",
                                                               "bandwidth_hz", "lra", "noise_flatness")},
                          "rows": rows, "choice": D.choose(rows)}
    # synthetic choice per noise kind (pooled over voices: worst case of the two)
    for kind in SNR:
        pooled = {}
        for name, v in doc["synthetic"].items():
            if name.startswith(kind + "/"):
                for r in v["rows"]:
                    k = (r["tool"], r["strength"])
                    pooled.setdefault(k, []).append(r)
        doc["choice"][f"synthetic/{kind}"] = sorted(
            ({"tool": k[0], "strength": k[1], "si_sdr_gain_db": min(r["si_sdr_gain_db"] for r in v),
              "speech_lsd_db": max(r["speech_lsd_db"] for r in v), "noise_floor_reduction_db": min(r["noise_floor_reduction_db"] for r in v)}
             for k, v in pooled.items()), key=lambda r: -r["si_sdr_gain_db"])[:5]
    for i, v in doc["real"].items():
        doc["choice"][f"real/{i}"] = v["choice"] | {"class": v["class"]}
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
