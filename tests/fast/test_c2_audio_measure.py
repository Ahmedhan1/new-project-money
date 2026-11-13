"""Agent C (Account 2): audio measurements on real generated audio (FFmpeg lavfi), checked against FFmpeg's own
EBU R128 meter and against known signal properties."""
import re
import subprocess

import numpy as np
import pytest

from nexus.audio import measure as M

SR = 48000


def _lavfi(tmp_path, expr: str, name: str, d: float = 8.0, ch: int = 2):
    out = tmp_path / f"{name}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", expr, "-t", str(d), "-ar", str(SR), "-ac", str(ch),
                    "-c:a", "pcm_f32le", str(out)], check=True)
    return out


def _ebur128(path):
    err = subprocess.run(["ffmpeg", "-nostdin", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1))
    lra = float(re.search(r"LRA:\s+(-?[\d.]+) LU", tail).group(1))
    tp = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail).group(1))
    return i, lra, tp


@pytest.fixture(scope="module")
def speechlike(tmp_path_factory):
    """Speech-like test signal: a 150 Hz buzz with formant-ish harmonics, syllable-rate (4 Hz) on/off envelope with
    pauses, over pink noise at a known level."""
    d = tmp_path_factory.mktemp("m")
    n = 12 * SR
    t = np.arange(n) / SR
    rng = np.random.default_rng(1)
    buzz = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 25))
    env = (np.sin(2 * np.pi * 4 * t) > 0).astype(float) * ((t % 3) < 2.0)  # 2 s talk, 1 s pause
    from scipy.signal import lfilter
    env = lfilter([0.01], [1, -0.99], env)
    sp = 0.15 * buzz * env / np.abs(buzz).max()
    pink = lfilter([0.049922035, -0.095993537, 0.050612699, -0.004408786], [1, -2.494956002, 2.017265875, -0.522189400],
                   rng.normal(0, 1, n))
    pink *= 10 ** (-50 / 20) / pink.std()
    x = np.stack([sp + pink, sp + pink], 1).astype(np.float32)
    return x, d


def test_loudness_matches_ffmpeg_ebur128(tmp_path):
    p = _lavfi(tmp_path, "sine=f=440:sample_rate=48000,volume=0.3,apulsator=hz=0.2", "tone", d=12)
    x = M.decode(p, SR, 2)
    mine = M.loudness(x, SR)
    i, lra, tp = _ebur128(p)
    assert abs(mine["lufs"] - i) <= 0.3
    assert abs(mine["lra"] - lra) <= 1.0
    assert abs(mine["true_peak"] - tp) <= 0.3


def test_noise_floor_and_speech_band_snr(speechlike):
    x, _ = speechlike
    db = M.block_db(x, SR)
    nf, dsil = M.noise_floor(db)
    assert -53 <= nf <= -47          # the pink noise was put at -50 dBFS RMS
    assert dsil == 0
    sb = M.speech_band(x, SR)
    assert sb["snr_db"] > 20          # speech is far above the noise in the band
    quiet = x * 0 + x[:, :1] * 0      # digital silence
    assert M.noise_floor(M.block_db(quiet, SR))[1] == 1.0


def test_clipping_dc_and_channel_faults_are_detected(speechlike):
    x, _ = speechlike
    assert M.clipping(x)["clipped_samples"] == 0
    hot = np.clip(x * 12, -1, 1)
    c = M.clipping(hot)
    assert c["clipped_samples"] > 1000 and c["clipped_runs"] > 50
    # clipping that happened before a gain change (flat tops at 0.8 FS) is still found
    soft = np.clip(x * 12, -1, 1) * 0.8
    assert M.clipping(soft)["clipped_runs"] > 50
    ch = M.channels(x + np.array([0.01, 0.0], dtype=np.float32), SR)
    assert ch["dc_offset"][0] == pytest.approx(0.01, abs=1e-3) and ch["dc_offset_db"] > -45
    dead = x.copy()
    dead[:, 1] = 0
    assert M.channels(dead, SR)["dead_channel"] == "R"
    assert M.channels(x, SR)["dual_mono"] is True
    quiet_r = x * np.array([1.0, 0.5], dtype=np.float32)
    assert M.channels(quiet_r, SR)["channel_imbalance_db"] == pytest.approx(6.02, abs=0.1)


def test_hum_is_found_in_the_quiet_parts(speechlike):
    x, _ = speechlike
    t = np.arange(len(x)) / SR
    hum = (0.003 * (np.sin(2 * np.pi * 60 * t) + 0.5 * np.sin(2 * np.pi * 180 * t)))[:, None].astype(np.float32)
    f = M.spectrum_facts(x + hum, SR)
    assert f["hum_hz"] == 60.0 and f["hum_prominence_db"] > 10
    assert M.spectrum_facts(x, SR)["hum_hz"] is None


def test_speaker_spread_measures_a_quiet_second_voice():
    from scipy.signal import lfilter
    n = 18 * SR
    t = np.arange(n) / SR
    env = lfilter([0.01], [1, -0.99], ((np.sin(2 * np.pi * 4 * t) > 0) * ((t % 3) < 2.0)).astype(float))
    a = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 25))
    b = sum(np.sin(2 * np.pi * 75 * k * t) / k for k in range(1, 25)) * 10 ** (-8 / 20)   # second voice, 8 dB quieter
    m = 0.15 * np.where(t < 9, a, b) / np.abs(a).max() * env + np.random.default_rng(0).normal(0, 1e-3, n)
    segs = M.active_segments(m, SR, min_len=0.5)
    assert len(segs) == 6
    turns = [{**s, "speaker": "A" if s["start"] < 9 else "B"} for s in segs]
    r = M.speaker_levels(m, SR, turns=turns)
    assert r["method"] == "diarisation turns"
    assert 7 <= r["spread_db"] <= 9
    auto = M.speaker_levels(m, SR, segments=segs, neural=False)    # unlabelled: MFCC voice clustering finds both
    assert "2 cluster" in auto["method"] and 7 <= auto["spread_db"] <= 9
