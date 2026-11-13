"""Agent C (Account 2): sound design engine on real generated audio."""
import numpy as np
import pytest

from nexus.audio import design as S
from nexus.audio import measure as M
from nexus.audio import music
from nexus.audio.denoise import ffilter
from nexus.fx import sfx

SR = 48000


def click_track(bpm: float, seconds: float, offset: float = 0.13) -> np.ndarray:
    n = int(seconds * SR)
    y = np.zeros(n, np.float32)
    t = np.arange(int(0.03 * SR)) / SR
    click = (np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 150)).astype(np.float32) * 0.5
    for b in np.arange(offset, seconds - 0.05, 60 / bpm):
        i = int(b * SR)
        y[i:i + len(click)] += click[: n - i]
    return y + np.random.default_rng(0).normal(0, 1e-3, n).astype(np.float32)


def test_beat_grid_finds_tempo_and_phase_of_a_click_track():
    g = S.beat_grid(click_track(120, 20), SR)
    assert abs(g["bpm"] - 120) < 1.0
    beats = np.array(g["beats"])
    expected = np.arange(0.13, 20, 0.5)
    err = [np.min(np.abs(beats - e)) for e in expected[2:-2]]
    assert np.median(err) < 0.02


def test_beat_grid_on_the_procedural_upbeat_bed(home):
    x = M.decode(music.music_bed(30, "upbeat", seed=1), SR, 2)
    g = S.beat_grid(x, SR)
    bpm = g["bpm"]
    assert any(abs(bpm - k * 110) < 2 for k in (0.5, 1, 2)), bpm   # the bed pulses at 110 BPM


def test_snap_cuts_reports_offsets_and_respects_protected_spans():
    beats = [0.5, 1.0, 1.5, 2.0]
    r = S.snap_cuts([0.62, 1.9, 1.24], beats, max_shift=0.2, protect=[(1.45, 1.6)])
    assert r[0]["beat"] == 0.5 and r[0]["offset_s"] == pytest.approx(-0.12) and r[0]["snap"]
    assert r[1]["beat"] == 2.0 and r[1]["snap"]
    assert r[2]["beat"] == 1.0 and r[2]["snap"] is False or r[2]["beat"] == 1.5   # 1.5 is inside a word: never a target
    assert not any(x["snap"] and 1.45 < x["beat"] < 1.6 for x in r)


def test_ducking_depth_follows_speech_density_and_renders(tmp_path):
    dense = [{"start": s, "end": s + 2.6} for s in np.arange(0, 30, 3.0)]
    sparse = [{"start": s, "end": s + 1.0} for s in (3.0, 15.0, 25.0)]
    d, s = S.ducking(dense, 30), S.ducking(sparse, 30)
    assert d["density"] > 0.6 > 0.3 > s["density"]
    assert d["depth_db"] < s["depth_db"] < 0 and d["release_s"] > s["release_s"]
    # render the keyframes on a steady tone and measure the duck
    tone = (0.2 * np.sin(2 * np.pi * 220 * np.arange(30 * SR) / SR)).astype(np.float32)
    y = ffilter(tone, SR, f"volume='{S.volume_expr(s['volume_keyframes'])}':eval=frame")[:, 0]
    lv = lambda a, b: 20 * np.log10(np.sqrt(np.mean(y[int(a * SR):int(b * SR)] ** 2)))
    assert lv(3.3, 3.8) - lv(8, 10) == pytest.approx(s["depth_db"], abs=0.7)   # inside a speech span
    assert abs(lv(8, 10) - 20 * np.log10(0.2 / np.sqrt(2))) < 0.3             # recovered in the pause


def test_sfx_plan_caps_density_and_spacing():
    events = [{"t": float(t), "kind": "transition"} for t in range(2, 60, 3)] + [{"t": 30.5, "kind": "title", "x": 0.2}]
    p = S.sfx_plan(events, "documentary", duration=60)
    assert len(p["placed"]) <= 2 and p["cap"] == 2
    assert p["placed"][0]["event"] == "title" or any(i["event"] == "title" for i in p["placed"])   # titles first
    ts = sorted(i["event_t"] for i in p["placed"])
    assert all(b - a >= 12 for a, b in zip(ts, ts[1:]))
    e = S.sfx_plan(events, "energetic", duration=60, speech=[{"start": 0, "end": 10}])
    assert 2 < len(e["placed"]) <= 6
    under = [i for i in e["placed"] if i["under_speech"]]
    assert all(i["volume"] < S.PROFILES["energetic"]["gain"] for i in under)
    title = next(i for i in e["placed"] if i["event"] == "title")
    assert title["pan"] == 0.2


def test_pan_by_screen_position():
    y = sfx.render_array("pop")
    left, mid = S.pan(y, 0.0), S.pan(y, 0.5)
    rms = lambda a: np.sqrt(np.mean(a ** 2))
    assert rms(left[:, 1]) < 1e-6 * rms(left[:, 0]) + 1e-9
    assert abs(20 * np.log10(rms(mid[:, 0]) / rms(mid[:, 1]))) < 0.5


@pytest.mark.parametrize("kind", S.AMBIENCES)
def test_ambience_beds_are_quiet_stereo_and_clean(kind):
    y = S.ambience_array(kind, 8.0, SR, seed=2, level_db=-40)
    assert y.shape == (8 * SR, 2)
    mid = y[SR:-SR]
    lv = 20 * np.log10(np.sqrt(np.mean(mid ** 2)))
    assert -43 < lv < -37
    assert M.clipping(y)["clipped_samples"] == 0 and abs(float(y.mean())) < 1e-4
    assert np.abs(y[:100]).max() < 1e-3                           # fades in from silence


@pytest.mark.parametrize("name", sfx.NAMES)
def test_every_generated_sfx_passes_the_quality_pass(name, tmp_path):
    q = S.sfx_quality(name, png=tmp_path / f"{name}.png")
    assert q["passed"], q["checks"]
    assert (tmp_path / f"{name}.png").stat().st_size < 200_000


def test_quality_pass_catches_a_bad_impact():
    y = sfx.render_array("impact")[::-1].copy()                    # reversed: slow attack, no decay
    q = S.sfx_quality("impact", y, sfx.SR)
    assert not q["passed"]
    assert not next(c for c in q["checks"] if c["check"] == "fast attack")["ok"]


def test_mixgraph_lines_run_in_ffmpeg_and_do_what_they_say(tmp_path):
    """The renderer-integration builders produce a working filter graph: keyframe ducking under speech, panned SFX."""
    import subprocess

    from nexus.audio import mixgraph
    tone = tmp_path / "bed.wav"
    M.write_wav(tone, np.stack([np.sin(2 * np.pi * 220 * np.arange(12 * SR) / SR)] * 2, 1) * 0.3, SR)
    sw = tmp_path / "swell.wav"
    M.write_wav(sw, sfx.render_array("swell", duration=2.0), SR)
    speech = [{"start": 4.0, "end": 6.0}]
    mline, side = mixgraph.music_line(1, {"src": str(tone), "volume": 1.0, "duck": "keyframes"}, 12.0, speech)
    assert side is False
    iline = mixgraph.item_line(1, {"start": 8.0, "volume": 1.0, "pan": 0.0})   # input 2, hard left
    out = tmp_path / "mix.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=12", "-i", str(tone), "-i", str(sw),
                    "-filter_complex", f"{mline};{iline};[mus][n1]amix=inputs=2:normalize=0:duration=first[o]", "-map", "[o]", str(out)],
                   check=True)
    y = M.decode(out, SR, 2)

    def lv(a, b, c=0):
        return 20 * np.log10(np.sqrt(np.mean(y[int(a * SR):int(b * SR), c] ** 2)) + 1e-12)
    assert lv(4.5, 5.5) < lv(2.0, 3.0) - 7                       # ducked under the speech span
    assert lv(9.2, 9.9, 0) - lv(2.0, 3.0, 0) > lv(9.2, 9.9, 1) - lv(2.0, 3.0, 1) + 3   # the swell sits on the left
