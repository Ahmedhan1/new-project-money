"""Agent C (Account 2): dialogue chain v2 on real generated audio - every assertion is a measurement of the output."""
import numpy as np
import pytest
from scipy.signal import fftconvolve, lfilter

from nexus import contracts
from nexus.audio import dialogue as G
from nexus.audio import measure as M

SR = 48000


def speechlike(seconds: float = 12.0, f0: float = 140.0, seed: int = 0, level: float = 0.15) -> np.ndarray:
    """Voiced syllables (harmonic buzz, 4 Hz on/off) in 2 s phrases separated by 1 s pauses."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    buzz = sum(np.sin(2 * np.pi * f0 * k * t + k) / k for k in range(1, 70))   # harmonics to ~10 kHz
    env = lfilter([0.01], [1, -0.99], ((np.sin(2 * np.pi * 4 * t) > 0) * ((t % 3) < 2.0)).astype(float))
    return (level * buzz * env / np.abs(buzz).max()).astype(np.float32)


def pink(n: int, db: float, seed: int = 1) -> np.ndarray:
    w = np.random.default_rng(seed).normal(0, 1, n)
    p = lfilter([0.049922035, -0.095993537, 0.050612699, -0.004408786], [1, -2.494956002, 2.017265875, -0.522189400], w)
    return (p / p.std() * 10 ** (db / 20)).astype(np.float32)


def test_wpe_dereverberation_raises_clarity_and_the_estimator_sees_the_tail():
    dry = speechlike(15) + pink(15 * SR, -75)
    rng = np.random.default_rng(3)
    tt = np.arange(int(1.2 * SR)) / SR
    ir = rng.normal(0, 1, len(tt)) * np.exp(-6.91 * tt / 0.8)        # RT60 0.8 s
    ir[0] = 8.0
    wet = fftconvolve(dry, ir)[: len(dry)]
    wet = (wet / np.abs(wet).max() * 0.3).astype(np.float32)
    rt_dry, rt_wet = G.estimate_rt60(dry, SR)["rt60_s"], G.estimate_rt60(wet, SR)["rt60_s"]
    assert rt_dry is not None and rt_wet is not None and rt_wet > 0.5 > rt_dry
    out = G.wpe(wet, SR)
    assert out.shape == M.as2d(wet).shape
    assert G.clarity_c50(out, SR) > G.clarity_c50(wet, SR) + 1.5      # late energy after offsets reduced (measured +1.8 dB)


def _piper_speech(tmp_path):
    from nexus import config
    from nexus.speech import tts
    if not (config.MODELS / "piper" / "en_US-joe-medium.onnx").exists():
        pytest.skip("no local Piper voice to make a real speech fixture")
    p = tmp_path / "sp.wav"
    tts.synthesize("The crew trained in a cave for six days. It was cold and very dark.", p, "en", voice="en_US-joe-medium",
                   master=False, cache=False)
    sp = M.mono(M.decode(p, SR, 1))
    return sp / np.abs(sp).max() * 0.15


def test_declip_removes_flat_tops():
    hot = np.clip(speechlike(6) * 8, -0.9, 0.9)
    before = M.clipping(hot)
    assert before["clipped_runs"] > 50
    assert M.clipping(G.declip(hot, SR))["clipped_samples"] < before["clipped_samples"] * 0.5


def test_declip_restores_clipped_speech(tmp_path):
    ref = _piper_speech(tmp_path) * 8                                   # real (synthetic-voice) speech, peaks at 1.2
    hot = np.clip(ref, -0.6, 0.6)
    assert M.clipping(hot)["clipped_samples"] > 500
    fixed = G.declip(hot, SR)[:, 0]
    assert M.clipping(fixed)["clipped_samples"] < 50
    assert np.mean((fixed - ref) ** 2) < 0.3 * np.mean((hot - ref) ** 2)  # measured: 10x lower error


def test_breaths_are_attenuated_not_removed():
    x = speechlike(12) + pink(12 * SR, -70)
    rng = np.random.default_rng(5)
    from scipy.signal import butter, sosfilt
    sos = butter(2, [1200, 6000], "band", fs=SR, output="sos")
    marks = []
    for s in (2.25, 5.25, 8.25):                                      # inside the 1 s pauses, after a phrase
        a, b = int(s * SR), int((s + 0.35) * SR)
        nz = sosfilt(sos, rng.normal(0, 1, b - a))
        win = np.hanning(b - a)
        x[a:b] += (nz / nz.std() * 10 ** (-38 / 20) * win).astype(np.float32)
        marks.append((s, s + 0.35))
    ev = G.find_breaths(x, SR)
    hits = [m for m in marks if any(e["start"] < m[1] and e["end"] > m[0] for e in ev)]
    assert len(hits) == 3
    y = G.attenuate(x, SR, ev)
    assert len(y) == len(x)                                          # timing unchanged
    a, b = int(marks[0][0] * SR), int(marks[0][1] * SR)
    drop = 20 * np.log10(np.sqrt(np.mean(x[a:b] ** 2)) / np.sqrt(np.mean(y[a:b, 0] ** 2)))
    assert 4.0 <= drop <= 7.0
    sa, sb = int(0.5 * SR), int(1.5 * SR)                             # speech itself untouched
    assert np.allclose(y[sa:sb, 0], x[sa:sb], atol=1e-6)


def test_room_tone_fills_digital_silence_at_the_source_floor():
    x = speechlike(10) + pink(10 * SR, -60)
    x[int(4.1 * SR):int(4.6 * SR)] = 0                               # an edit left 0.5 s of digital silence
    gaps = G.digital_silence_gaps(x, SR)
    assert len(gaps) == 1 and abs(gaps[0][1] - gaps[0][0] - 0.5) < 0.01
    y, filled = G.fill_gaps(x, SR)
    assert len(y) == len(x) and filled and not G.digital_silence_gaps(y, SR)
    seg = y[int(4.15 * SR):int(4.55 * SR), 0]
    lvl = 20 * np.log10(np.sqrt(np.mean(seg ** 2)))
    assert -64 <= lvl <= -56


def test_dead_channel_and_imbalance_are_fixed():
    m = speechlike(6)
    st = np.stack([m, np.zeros_like(m)], 1)
    y, info = G.fix_channels(st, SR)
    assert info["applied"] and M.channels(y, SR)["dead_channel"] is None
    y2, info2 = G.fix_channels(np.stack([m, m * 0.5], 1), SR)
    assert info2["applied"] and abs(M.channels(y2, SR)["channel_imbalance_db"]) < 0.1


def test_dehum_removes_60hz_hum():
    x = speechlike(10) + pink(10 * SR, -65)
    t = np.arange(len(x)) / SR
    x = x + (0.004 * (np.sin(2 * np.pi * 60 * t) + 0.6 * np.sin(2 * np.pi * 180 * t))).astype(np.float32)
    before = M.spectrum_facts(M.as2d(x), SR)
    assert before["hum_hz"] == 60.0
    after = M.spectrum_facts(G.dehum(x, SR, 60.0), SR)
    assert after["hum_hz"] is None and after["hum_prominence_db"] < before["hum_prominence_db"] - 8


def test_full_chain_meets_targets_and_writes_a_valid_manifest(home, tmp_path):
    x = speechlike(20) + pink(20 * SR, -48)
    x[int(11.2 * SR):int(11.5 * SR)] = 0
    st = np.stack([x, x * 0.7], 1)                                    # also a 3 dB channel imbalance
    src = M.write_wav(tmp_path / "in.wav", st, SR)
    out = tmp_path / "out.wav"
    man = G.process_file(src, out, target_lufs=-16.0, measure_speakers=False)
    assert contracts.validate(man) == []
    b, a = man["before"], man["after"]
    assert abs(a["lufs"] + 16.0) <= 0.5
    assert a["true_peak"] <= -1.4
    assert a["noise_rel_lufs_db"] < b["noise_rel_lufs_db"] - 2        # noise lower relative to the speech (afftdn fallback here)
    assert a["speech_band_snr_db"] > b["speech_band_snr_db"] + 2
    assert abs(a["channel_imbalance_db"]) < 0.3
    assert a["digital_silence_frac"] == 0
    steps = {c["step"]: c for c in man["chain"]}
    assert steps["denoise"]["applied"] and steps["roomtone"]["applied"] and steps["channels"]["applied"]
    assert man["human_listening"].startswith("required")
    assert abs(M.decode(out, SR, 2).shape[0] - len(st)) <= 2           # nothing was cut


@pytest.mark.parametrize("clean_src", [True])
def test_clean_studio_material_is_not_denoised(clean_src):
    x = speechlike(12) + pink(12 * SR, -80)
    prof = M.profile(M.as2d(x), SR, speakers=False)
    from nexus.audio import denoise as D
    assert D.classify(prof) == "studio"
    y, rep = G.process(x, SR, steps={"denoise", "loudness"}, measure_speakers=False)
    assert not {c["step"]: c for c in rep["chain"]}["denoise"]["applied"]
