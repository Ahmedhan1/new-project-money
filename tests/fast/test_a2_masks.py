"""Account 2 / agent A: moving / tracked masks. The mask's box follows keyframes (or a face track); the effect is
checked on rendered pixels at two times, where the box is in different places."""
import subprocess

import numpy as np
import pytest

from nexus.fx import post
from nexus.timeline import ops, render

W, H = 320, 180


def _src(path, d=3.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"nullsrc=s={W}x{H}:r=30:d={d},geq=lum='if(mod(floor(X/4)+floor(Y/4),2),210,40)':cb=128:cr=128",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def _gray(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: W * H], dtype=np.uint8).reshape(H, W).astype(float)


def _tl(clips):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": False,
            "markers": []}


def _reg(f, cx, cy, r=12):
    x, y = int(cx * W), int(cy * H)
    return f[y - r: y + r, x - r: x + r]


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    return _src(tmp_path_factory.mktemp("mm") / "s.mp4")


@pytest.fixture()
def plain(home, src, tmp_path):
    render.render(_tl([ops.video_clip(src, 0, 2.0)]), tmp_path / "p.mp4", tmp_path / "w", workers=1)
    return tmp_path / "p.mp4"


def test_kf_value_matches_kf_expr_curve():
    pts = [[0, 0.1], [1, 0.5, "ease"], [2, 0.9]]
    assert post.kf_value(pts, -1) == 0.1 and post.kf_value(pts, 5) == 0.9
    assert post.kf_value(pts, 0.5) == pytest.approx(0.3)
    assert post.kf_value(pts, 1.5) == pytest.approx(0.7)                  # ease: smoothstep at the midpoint = 0.5


@pytest.mark.parametrize("effect", ["dim", "blur", "pixelate"])
def test_moving_mask_effect_follows_keyframes(home, src, plain, tmp_path, effect):
    m = {"shape": "rect", "x": 0.05, "y": 0.3, "w": 0.3, "h": 0.4, "feather": 0.0, "effect": effect, "region": "inside",
         "amount": 1.0, "keyframes": {"x": [[0, 0.05], [2, 0.65]]}}
    render.render(_tl([ops.video_clip(src, 0, 2.0, mask=m)]), tmp_path / "m.mp4", tmp_path / "w", workers=1)
    for t, inside, outside in ((0.25, 0.275, 0.80), (1.75, 0.725, 0.25)):
        fm, fp = _gray(tmp_path / "m.mp4", t), _gray(plain, t)
        mi, pi = _reg(fm, inside, 0.5), _reg(fp, inside, 0.5)
        mo, po = _reg(fm, outside, 0.5), _reg(fp, outside, 0.5)
        assert np.abs(mo - po).mean() < 4, (t, "outside the moving box must be untouched")
        if effect == "dim":
            assert mi.mean() < pi.mean() - 25, (t, mi.mean(), pi.mean())
        else:
            # blur / pixelate destroy fine detail inside the box: local gradients collapse
            def grad(a):
                return np.abs(np.diff(a, axis=1)).mean()
            assert grad(mi) < 0.6 * grad(pi), (t, effect, grad(mi), grad(pi))


def test_mask_from_face_track_follows_the_subject(home, src, plain, tmp_path):
    # a "face" moving left -> right in a source of the same size as the output (no crop): box centre must follow it
    samples = [{"t": t / 10, "x": 0.2 + 0.3 * t / 10, "y": 0.5, "w": 0.12, "h": 0.2} for t in range(0, 21)]
    c = ops.video_clip(src, 0, 2.0)
    m = post.mask_from_track(samples, c, W, H, W, H, pad=1.5, effect="dim", amount=1.0)
    assert m and m["keyframes"]["x"][0] == [0.0, pytest.approx(0.2 - 0.09, abs=1e-3)]
    assert post.mask_at(post.masks({"mask": m})[0], 2.0)["x"] == pytest.approx(0.8 - 0.09, abs=0.02)
    c["mask"] = {**m, "feather": 0.0}
    render.render(_tl([c]), tmp_path / "f.mp4", tmp_path / "w", workers=1)
    for t, at in ((0.2, 0.26), (1.8, 0.74)):
        assert _reg(_gray(tmp_path / "f.mp4", t), at, 0.5, 6).mean() < _reg(_gray(plain, t), at, 0.5, 6).mean() - 25
    # a cover crop shifts the coordinates: a 2x wider source centred keeps the subject at the same output position
    m2 = post.mask_from_track([{"t": 0, "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.2}], c, W, H, 2 * W, H)
    assert m2["x"] + m2["w"] / 2 == pytest.approx(0.5, abs=1e-3)
    assert post.mask_from_track([{"t": 50, "x": 0.5, "y": 0.5}], c, W, H, W, H) is None
