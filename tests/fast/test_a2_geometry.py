"""Account 2 / agent A: perspective correction, rotation keyframes, extra split-screen layouts and the optical-flow
slow-motion profile, each checked on rendered frames."""
import subprocess

import numpy as np
import pytest

from nexus.fx import post
from nexus.timeline import ops, render

W, H = 320, 180


def _gradient(path, d=2.0):
    # luma = 16 + 200 * X / W (a horizontal ramp): an output pixel's value tells which source column it shows
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s={W}x{H}:r=30:d={d},geq=lum='16+200*X/{W}':cb=128:cr=128",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def _solid(path, colour, d=2.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:r=30:d={d}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def _gray(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: W * H], dtype=np.uint8).reshape(H, W).astype(float)


def _all(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).astype(float)


def _tl(clips, **kw):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": False,
            "markers": [], **kw}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("geo")
    return {"grad": _gradient(d / "g.mp4"), "red": _solid(d / "r.mp4", "0xC02020"), "blue": _solid(d / "b.mp4", "0x2020C0"),
            "white": _solid(d / "w.mp4", "0xE0E0E0"), "dir": d}


def test_perspective_maps_the_quad_onto_the_frame(home, media, tmp_path):
    # the area between 25 % and 75 % of the width fills the frame: the left edge shows source column W/4
    c = ops.video_clip(media["grad"], 0, 1.0, perspective={"tl": [0.25, 0], "tr": [0.75, 0], "bl": [0.25, 1], "br": [0.75, 1]})
    render.render(_tl([c]), tmp_path / "p.mp4", tmp_path / "w", workers=1)
    f = _gray(tmp_path / "p.mp4", 0.5)
    # (decoded as full-range grey: tv-range luma 16 + v maps to v * 255 / 219)
    assert f[90, 2:6].mean() == pytest.approx(200 * 0.25 * 255 / 219, abs=4)
    assert f[90, W - 6: W - 2].mean() == pytest.approx(200 * 0.75 * 255 / 219, abs=4)
    assert f[90, W // 2 - 2: W // 2 + 2].mean() == pytest.approx(200 * 0.5 * 255 / 219, abs=4)
    # a keystone (top narrower than bottom) makes the same column brighter at the top than at the bottom
    k = ops.video_clip(media["grad"], 0, 1.0, perspective={"tl": [0.2, 0], "tr": [0.8, 0], "bl": [0, 1], "br": [1, 1]})
    render.render(_tl([k]), tmp_path / "k.mp4", tmp_path / "w", workers=1)
    g = _gray(tmp_path / "k.mp4", 0.5)
    assert g[4, 10] > g[H - 4, 10] + 20


def test_rotation_keyframes_turn_the_picture_over_time(home, media, tmp_path):
    # a white frame rotated 0 -> 30 deg with black corners: corners stay white at t=0, turn black later
    c = ops.video_clip(media["white"], 0, 1.5, keyframes={"rotation": [[0, 0], [1, 30]]}, rotation_fill="black")
    render.render(_tl([c]), tmp_path / "r.mp4", tmp_path / "w", workers=1)
    f0, f1 = _gray(tmp_path / "r.mp4", 0.0), _gray(tmp_path / "r.mp4", 1.3)
    assert f0[2:8, 2:8].mean() > 180 and f1[2:8, 2:8].mean() < 40 and f1[85:95, 155:165].mean() > 180
    # the default fill zooms so that no corner ever shows
    z = ops.video_clip(media["white"], 0, 1.5, keyframes={"rotation": [[0, 0], [1, 30]]})
    render.render(_tl([z]), tmp_path / "z.mp4", tmp_path / "w", workers=1)
    assert _gray(tmp_path / "z.mp4", 1.3)[1:6, 1:6].mean() > 180


@pytest.mark.parametrize("layout,cells", [("main_left", [(0.3, 0.5, "red"), (0.85, 0.25, "blue"), (0.85, 0.75, "white")]),
                                          ("main_top", [(0.5, 0.3, "red"), (0.25, 0.85, "blue"), (0.75, 0.85, "white")]),
                                          ("thirds_h", [(0.16, 0.5, "red"), (0.5, 0.5, "blue"), (0.84, 0.5, "white")])])
def test_split_layouts_place_panels(home, media, tmp_path, layout, cells):
    c = ops.video_clip(media["red"], 0, 1.0, split={"srcs": [media["blue"], media["white"]], "layout": layout, "gap": 0})
    render.render(_tl([c]), tmp_path / "s.mp4", tmp_path / "w", workers=1)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(tmp_path / "s.mp4"), "-frames:v", "1", "-f",
                          "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw[: W * H * 3], dtype=np.uint8).reshape(H, W, 3).astype(float)
    for fx, fy, name in cells:
        r, g, b = f[int(fy * H) - 3: int(fy * H) + 3, int(fx * W) - 3: int(fx * W) + 3].reshape(-1, 3).mean(axis=0)
        got = "white" if min(r, g, b) > 180 else "red" if r > b + 60 else "blue" if b > r + 60 else "?"
        assert got == name, (layout, fx, fy, r, g, b)
    assert post.split_cells({"layout": "main_right", "srcs": [1, 2], "gap": 0}, W, H)[0][0] > 0


def test_optical_flow_slowmo_profile_synthesises_new_frames(home, tmp_path):
    src = tmp_path / "m.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=1.5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)], check=True)
    c = ops.video_clip(str(src), 0, 0.5, speed=0.5)
    render.render(_tl([c]), tmp_path / "n.mp4", tmp_path / "w", workers=1)
    render.render(_tl([c], slowmo="optical_flow"), tmp_path / "o.mp4", tmp_path / "w", workers=1)
    n, o = _all(tmp_path / "n.mp4"), _all(tmp_path / "o.mp4")
    assert len(n) == len(o) == 30

    def repeats(a):
        return sum(1 for i in range(1, len(a)) if np.abs(a[i] - a[i - 1]).mean() < 0.5)
    assert repeats(n) >= 12          # plain slow motion repeats every other frame
    assert repeats(o) <= 3           # optical flow interpolates in-between frames (only the clip end is held)
    assert render.segment_key(c, _tl([c])) != render.segment_key(c, _tl([c], slowmo="optical_flow"))
