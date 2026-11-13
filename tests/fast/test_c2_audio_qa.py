"""Agent C (Account 2): audio QA on real rendered outputs (renderer + FFmpeg), known delays and known faults."""
import subprocess

import numpy as np
import pytest

from nexus import project as prj
from nexus.audio import measure as M
from nexus.audio import qa as AQ
from nexus.qa_continuity import _cut_times, continuity_review
from nexus.timeline import ops, render

SR = 48000


def _src(path):
    """8 s grey video with non-periodic audio (amplitude-modulated pink noise), so cross-correlation is unambiguous."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x606060:s=320x180:r=30:d=8",
                    "-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:a=0.3:r=48000:seed=4,apulsator=hz=1.3:amount=0.8",
                    "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return str(path)


@pytest.fixture()
def rendered(home, tmp_path):
    src = _src(tmp_path / "src.mp4")
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(src, 0.5, 3.0), ops.video_clip(src, 4.0, 6.5), ops.video_clip(src, 1.0, 3.2)]
    out = tmp_path / "out.mp4"
    render.render(tl, out, tmp_path / "w")
    return tl, out, tmp_path


def test_first_clip_is_in_sync_and_every_clip_is_measurable(rendered):
    tl, out, _ = rendered
    rows = AQ.sync_offsets(out, tl, _cut_times(tl))
    assert len(rows) == 3
    for r in rows:
        assert r["corr"] > 0.5 and r["distinct"] > 0.05
    assert abs(rows[0]["offset_ms"]) <= 1000 / 30        # within one frame


# Regression guard: this check found AAC priming/padding drift at hard cuts (+5, +44, +82 ms); fixed in the renderer
# (93f851b: audio decoded sample-exact at the concat, video still stream-copied). Every clip must stay within one frame.
def test_every_rendered_clip_is_in_sync_with_its_source(rendered):
    tl, out, _ = rendered
    rows = AQ.sync_offsets(out, tl, _cut_times(tl))
    assert all(abs(r["offset_ms"]) <= 1000 / 30 for r in rows)


def test_a_known_audio_delay_is_measured(rendered):
    tl, out, tmp = rendered
    late = tmp / "late.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-af", "adelay=150:all=1", "-c:v", "copy", "-c:a", "aac", str(late)], check=True)
    base = AQ.sync_offsets(out, tl, _cut_times(tl))
    rows = AQ.sync_offsets(late, tl, _cut_times(tl))
    for r0, r in zip(base, rows):
        assert r["offset_ms"] - r0["offset_ms"] == pytest.approx(150, abs=2)    # known delay measured within 2 ms
    rv = AQ.audio_review(late, tl, _cut_times(tl))
    bad = [c for c in rv["checks"] if c["fix"] == "resync-audio"]
    assert len(bad) == 3 and not rv["passed"]                                 # > 125 ms late: major
    assert all(c["severity"] == "major" for c in bad)


def test_continuity_review_includes_the_audio_checks(rendered):
    tl, out, _ = rendered
    rv = continuity_review(out, tl)
    audio = [c for c in rv["checks"] if c.get("group") == "audio"]
    assert any(c["check"].startswith("audio/video sync") for c in audio)
    assert any(c["check"] == "true peak" for c in audio) and "sync" in rv["audio"]


def test_gaps_jumps_clipping_and_deliverable_loudness(tmp_path):
    rng = np.random.default_rng(0)
    t = np.arange(12 * SR) / SR
    env = ((t % 2.0) < 1.4).astype(float)
    x = rng.normal(0, 0.05, len(t)) * env + rng.normal(0, 1e-3, len(t))
    x[int(6 * SR):] *= 10 ** (9 / 20)                 # the second voice is 9 dB louder
    x[int(9.5 * SR):int(10.0 * SR)] = 0               # a 0.5 s hole
    j = AQ.level_jumps(x.astype(np.float32), SR)
    assert any(abs(r["t"] - 6.0) < 0.2 and r["jump_db"] == pytest.approx(9, abs=1.5) for r in j)
    g = AQ.gaps(x.astype(np.float32), SR)
    assert any(abs(s - 9.5) < 0.05 for s, e in g["digital_silence"])
    hot = np.clip(x * 30, -1, 1)
    assert M.clipping(hot)["clipped_runs"] > 0
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    M.write_wav(a, np.stack([x, x], 1) * 0.5, SR)
    M.write_wav(b, np.stack([x, x], 1) * 0.5 * 10 ** (3 / 20), SR)
    lc = AQ.loudness_consistency([a, b], tol_lu=1.0)
    assert lc["spread_lu"] == pytest.approx(3.0, abs=0.1) and not lc["ok"]
