"""Account 2 / agent A: CMX3600 EDL and OTIO JSON interchange. Round trips keep cuts, crossfades and speed; the
frame-accuracy test renders an EDL-imported timeline and decodes the exact frames at every cut boundary: each source
frame carries its own index in its pixel values."""
import json
import subprocess

import numpy as np
import pytest

from nexus.timeline import edl, ops, render

W, H, FPS = 160, 96, 30


def _indexed(path, cr):
    # frame N: luma 16 + 4*(N mod 50), Cb 32 + 48*floor(N/50) (0..149 frames), Cr identifies the source
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s={W}x{H}:r={FPS}:d=5,geq=lum='16+4*mod(N,50)':cb='32+48*floor(N/50)':cr={cr}",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return str(path)


def _frames(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv444p", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3, H, W)
    return a[:, :, H // 4: 3 * H // 4, W // 4: 3 * W // 4].mean(axis=(2, 3))


def _decode(f):
    """(source id by Cr, source frame index) of one decoded frame."""
    y, cb, cr = f
    return ("A" if cr < 128 else "B"), int(round((y - 16) / 4)) + 50 * int(round((cb - 32) / 48))


@pytest.fixture(scope="module")
def srcs(tmp_path_factory):
    d = tmp_path_factory.mktemp("edl")
    return {"A": _indexed(d / "a.mp4", 80), "B": _indexed(d / "b.mp4", 176)}


def _tl(srcs):
    a1 = ops.video_clip(srcs["A"], 0.5, 1.5)                                 # src frames 15..44
    b1 = ops.video_clip(srcs["B"], 1.0, 2.0, grade="warm")                    # 30..59 (cut)
    a2 = ops.video_clip(srcs["A"], 3.0, 4.0, transition="crossfade", xfade="wipeleft", transition_dur=0.2)
    b2 = ops.video_clip(srcs["B"], 0.2, 1.4, speed=2.0)                      # 6..41 at 2x -> 18 frames
    return {"version": 1, "width": W, "height": H, "fps": FPS, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": [a1, b1, a2, b2], "audio": [], "music": None, "subtitles": [],
            "burn_subtitles": False, "markers": []}


def _same_cuts(t1, t2):
    assert len(t1["clips"]) == len(t2["clips"])
    for a, b in zip(t1["clips"], t2["clips"]):
        assert a["src"] == b["src"] and a["in"] == pytest.approx(b["in"], abs=1e-6) and a["out"] == pytest.approx(b["out"], abs=1e-6)
        assert a.get("transition") == b.get("transition") and a.get("xfade") == b.get("xfade")
        assert float(a.get("transition_dur", 0)) == pytest.approx(float(b.get("transition_dur", 0)))
        assert float(a.get("speed", 1)) == pytest.approx(float(b.get("speed", 1)))
        assert a["id"] == b["id"] and a.get("grade") == b.get("grade")


def test_edl_text_is_cmx3600_and_round_trips(srcs):
    tl = _tl(srcs)
    text = edl.to_edl(tl, "test")
    lines = [ln for ln in text.splitlines() if ln[:3].isdigit()]
    assert lines[0].split()[3] == "C" and lines[0].split()[-2] == "01:00:00:00"
    assert lines[0].split()[4:6] == ["00:00:00:15", "00:00:01:15"]           # source in/out timecodes
    assert any(ln.split()[3] == "W001" and ln.split()[4] == "006" for ln in lines)  # wipeleft, 6 frames
    assert "M2" in text and "060.0" in text
    back = edl.from_edl(text, fps=FPS, base={"width": W, "height": H})
    _same_cuts(tl, back)
    assert render.total_duration(back) == pytest.approx(render.total_duration(tl), abs=1e-6)
    # without the NEXUS comments (another editor's EDL) the cuts still come back, given a reel map
    plain = "\n".join(ln for ln in text.splitlines() if not ln.startswith("*"))
    reels = {"A": srcs["A"], "B": srcs["B"]}
    names = {ln.split()[1] for ln in lines}
    rmap = {r: reels["A" if r.startswith("A") else "B"] for r in names}
    other = edl.from_edl(plain, fps=FPS, reels=rmap)
    assert [round(c["in"], 3) for c in other["clips"]] == [round(c["in"], 3) for c in tl["clips"]]
    assert other["clips"][2]["transition"] == "crossfade" and other["clips"][2]["xfade"] == "wipeleft"


def test_otio_json_round_trips_and_reads_in_opentimelineio_when_installed(srcs, tmp_path):
    tl = _tl(srcs)
    p = edl.write(tl, tmp_path / "t.otio")
    back = edl.read(p)
    _same_cuts(tl, back)
    doc = json.loads(p.read_text())
    tr = doc["tracks"]["children"][0]["children"]
    assert [x["OTIO_SCHEMA"] for x in tr] == ["Clip.2", "Clip.2", "Transition.1", "Clip.2", "Clip.2"]
    try:
        import opentimelineio as otio
    except ImportError:
        pytest.skip("opentimelineio not installed (optional)")
    t = otio.adapters.read_from_file(str(p))
    clips = list(t.find_clips())
    assert len(clips) == 4 and clips[0].source_range.start_time.value == 15
    assert t.duration().value == pytest.approx(round(render.total_duration(tl) * FPS))


def test_edl_import_renders_frame_accurate_cuts(home, srcs, tmp_path):
    tl = _tl(srcs)
    tl["clips"][1].pop("grade")                                                 # keep pixel values decodable
    back = edl.from_edl(edl.to_edl(tl), fps=FPS, base={"width": W, "height": H, "burn_subtitles": False})
    render.render(back, tmp_path / "o.mp4", tmp_path / "w", workers=1)
    f = _frames(tmp_path / "o.mp4")
    # programme frames: A 15..44 | B 30..53 | wipe 54..59 (B 54..59 -> A 90..95) | A 96..119 | B 6, 8, ... 40 (2x)
    assert len(f) == 30 + 30 + 30 - 6 + 18
    expect = {0: ("A", 15), 29: ("A", 44), 30: ("B", 30), 53: ("B", 53), 60: ("A", 96), 83: ("A", 119),
              84: ("B", 6), 85: ("B", 8), 101: ("B", 40)}
    for idx, want in expect.items():
        assert _decode(f[idx]) == want, (idx, _decode(f[idx]), want)
    # inside the wipe each frame is part outgoing B, part incoming A (left/right halves differ)
    assert _decode(f[52])[0] == "B"
