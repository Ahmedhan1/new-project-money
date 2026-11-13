"""Agent D VFX processors on real synthetic videos: tracked masks move with the object, background replacement changes
the background but not the subject, procedural overlays have alpha and motion, depth effects / parallax (labelled
animated-still). No downloads: the matting and depth networks are replaced by tiny ONNX / NumPy stand-ins whose
behaviour is known, so the real pipelines (decode, model call, compositing, encode) are exercised end to end."""
import subprocess

import numpy as np
import pytest
from test_d2_util import moving_objects, texture, write_video

from nexus import contracts, vfx
from nexus.enhance import onnx_build as ob
from nexus.generate import classify, metrics
from nexus.media import ff
from nexus.vfx import depth, procedural, segment, track


def test_tracker_follows_the_object_and_mask_keyframes_move(home, tmp_path):
    frames, boxes = moving_objects(40, 144, 192)
    v = write_video(tmp_path / "m.mp4", frames)
    x, y, w, h = boxes[0]
    tr = track.track_box(v, (x / 192, y / 144, w / 192, h / 144))
    assert tr["frames"] == 40 and tr["lost"] == 0
    err = [abs(s["x"] * 192 - bx) + abs(s["y"] * 144 - by) for s, (bx, by, _, _) in zip(tr["samples"], boxes)]
    assert np.mean(err) < 4 and max(err) < 10, err
    kf = track.mask_keyframes(tr)
    assert set(kf) == {"x", "y", "w", "h"} and kf["x"][-1][1] > kf["x"][0][1] + 0.4  # moved ~117 px of 192 to the right
    assert all(len(k) == 3 and k[2] == "linear" for k in kf["x"]) and kf["x"][-1][0] == pytest.approx(39 / 25, abs=0.01)
    fk = track.follow_keyframes(tr, 108, 144, 192, 144)
    assert fk["x"][-1][1] > fk["x"][0][1]
    out = track.render_tracked_mask(v, tmp_path / "masked.mp4", tr, effect="pixelate", amount=1.0)
    got = metrics.read_frames(out["path"])
    assert len(got) == 40
    for i in (0, 39):  # the effect sits on the object in the first AND the last frame, background untouched
        bx, by, bw, bh = boxes[i]
        inside = np.abs(got[i, by + 4:by + bh - 4, bx + 4:bx + bw - 4].astype(int) - frames[i, by + 4:by + bh - 4, bx + 4:bx + bw - 4]).mean()
        far = np.abs(got[i, 100:140, 0:8].astype(int) - frames[i, 100:140, 0:8]).mean() if bx > 40 else \
            np.abs(got[i, 0:20, 150:190].astype(int) - frames[i, 0:20, 150:190]).mean()
        assert inside > 15 and far < 6, (i, inside, far)


def _fake_matte_onnx(path):
    """pixel_values [1,3,256,256] in 0..1 -> alphas = sigmoid(40*(R - B - 0.25)): 'subject' = red/yellow pixels."""
    w = np.array([[[[40.0]], [[0.0]], [[-40.0]]]], np.float32)
    b = np.array([-10.0], np.float32)
    m = ob.model([ob.node("Conv", ["pixel_values", "w", "b"], ["z"], kernel_shape=[1, 1]), ob.node("Sigmoid", ["z"], ["alphas"])],
                 [ob.tensor("w", w), ob.tensor("b", b)], [ob.value_info("pixel_values", [1, 3, 256, 256])],
                 [ob.value_info("alphas", [1, 1, 256, 256])])
    path.write_bytes(m)
    return path


def test_background_replacement_changes_background_not_subject(home, tmp_path, monkeypatch):
    n, H, W = 12, 144, 192
    bg = np.zeros((H, W, 3), np.uint8)
    bg[...] = (30, 90, 200)  # blue backdrop
    bg[:, :, 1] = (np.linspace(60, 140, W)[None, :]).astype(np.uint8)
    frames = []
    for i in range(n):
        f = bg.copy()
        f[40:110, 50 + 3 * i:110 + 3 * i] = (235, 60, 30)  # red 'person'
        frames.append(f)
    frames = np.stack(frames)
    v = write_video(tmp_path / "s.mp4", frames, crf=6)
    onnx = _fake_matte_onnx(tmp_path / "matte.onnx")
    monkeypatch.setattr(segment, "_need", lambda mid: onnx)
    segment._session.cache_clear()
    r = segment.replace_background(v, bg="#00B140", model_id="mediapipe-selfie", feather=1.0, smooth=0.0)
    out = metrics.read_frames(r["path"])
    assert out.shape == frames.shape
    for i in (0, n - 1):
        x0 = 50 + 3 * i
        subj = np.abs(out[i, 50:100, x0 + 8:x0 + 52].astype(int) - frames[i, 50:100, x0 + 8:x0 + 52]).mean()
        back = out[i, 5:30, 5:40].astype(int)
        assert subj < 8, subj
        assert np.abs(back - np.array([0, 177, 64])).mean() < 12 and np.abs(back - frames[i, 5:30, 5:40]).mean() > 60
    assert r["manifest"]["classification"] == "edited-source" and not contracts.validate(r["manifest"])
    cut = segment.cutout(v, tmp_path / "cut.mov", model_id="mediapipe-selfie", smooth=0.0)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt", "-of",
                            "csv=p=0", str(cut["path"])], capture_output=True, text=True).stdout.strip()
    assert probe == "rgba" and cut["frames"] == n


def test_without_model_background_removal_asks_for_consent(home, tmp_path):
    from nexus.generate import downloads
    v = write_video(tmp_path / "x.mp4", moving_objects(4)[0])
    with pytest.raises(downloads.ConsentRequired):
        segment.replace_background(v)
    em = vfx.effect_manifest()
    assert not contracts.validate(em)
    rb = next(e for e in em["effects"] if e["type"] == "vfx.replace_background")
    assert not rb["available"] and rb["needs_download_gb"]["modnet"] > 0


def _rgba(path):
    info = ff.probe(path)["video"]
    w, h = info["width"], info["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 4)


def test_procedural_particles_leaks_and_flares_have_alpha_and_motion(home, tmp_path):
    for kind in ("dust", "snow", "embers", "bokeh"):
        r = procedural.particles(tmp_path / f"{kind}.mov", kind=kind, size=(320, 180), seconds=1.0, fps=12, density=0.6)
        a = _rgba(r["path"])
        assert a.shape == (12, 180, 320, 4) and r["manifest"]["classification"] == "procedural"
        cover = (a[..., 3] > 10).mean()
        assert 0.0002 < cover < 0.6, (kind, cover)
        moved = (np.abs(a[0, ..., 3].astype(int) - a[-1, ..., 3]) > 10).mean()
        assert moved > 0.3 * cover, (kind, moved, cover)  # particles move between the first and last frame
    snow = _rgba(tmp_path / "snow.mov")[..., 3].astype(float)
    assert snow.shape[0] == 12
    lk = _rgba(procedural.light_leak(tmp_path / "leak.mov", size=(160, 90), seconds=1.0, fps=12)["path"])
    assert lk[..., 3].mean() > 5 and lk[..., 0].mean() > lk[..., 2].mean()  # warm palette
    fl = _rgba(procedural.lens_flare(tmp_path / "flare.mov", size=(160, 90), seconds=1.0, fps=12, path=((0.2, 0.3), (0.7, 0.3)))["path"])
    x_first = np.unravel_index(np.argmax(fl[0, ..., 3] * (fl[0, ..., :3].sum(-1) > 700)), (90, 160))[1]
    x_last = np.unravel_index(np.argmax(fl[-1, ..., 3] * (fl[-1, ..., :3].sum(-1) > 700)), (90, 160))[1]
    assert x_last > x_first + 40, (x_first, x_last)
    base = write_video(tmp_path / "base.mp4", np.stack([texture(90, 160, 2)] * 12), fps=12)
    o = procedural.overlay(base, tmp_path / "leak.mov", tmp_path / "o.mp4", mode="screen")
    assert metrics.read_frames(o["path"]).mean() > metrics.read_frames(base).mean() + 3
    o2 = procedural.overlay(base, tmp_path / "snow.mov", tmp_path / "o2.mp4", mode="over")
    assert len(metrics.read_frames(o2["path"])) == 12


def test_depth_of_field_and_parallax_is_an_animated_still(home, tmp_path, monkeypatch):
    from PIL import Image
    img = texture(180, 320, 4)
    Image.fromarray(img).save(tmp_path / "still.png")
    grad = np.tile(np.linspace(0, 1, 320, dtype=np.float32)[None, :], (180, 1))  # right = near, left = far
    monkeypatch.setattr(depth, "depth", lambda frame, size=518: grad if frame.shape[:2] == (180, 320) else
                        np.tile(np.linspace(0, 1, frame.shape[1], dtype=np.float32)[None, :], (frame.shape[0], 1)))
    r = depth.depth_of_field(tmp_path / "still.png", tmp_path / "dof.png", focus=1.0, max_blur=6)
    import cv2

    def sharp(a):
        return cv2.Laplacian(cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
    assert sharp(r["image"][:, 300:]) > 0.8 * sharp(img[:, 300:]) and sharp(r["image"][:, :60]) < 0.35 * sharp(img[:, :60])
    p = depth.parallax(tmp_path / "still.png", tmp_path / "par.mp4", seconds=1.0, amount=0.05, size=(320, 180), fps=24)
    fr = metrics.read_frames(p["path"])
    assert fr.shape == (24, 180, 320, 3) and p["manifest"]["classification"] == "animated-still"
    near_shift = np.abs(fr[0, :, 250:310].astype(int) - fr[-1, :, 250:310]).mean()
    far_shift = np.abs(fr[0, :, 10:70].astype(int) - fr[-1, :, 10:70]).mean()
    assert near_shift > far_shift  # near layers move more: depth-based, not a flat pan
    forged = dict(p["manifest"], classification="generated-video")
    with pytest.raises(classify.ClassificationError):
        classify.guard(forged)


def test_vfx_apply_tracked_mask_on_a_timeline_clip(home, tmp_path):
    frames, boxes = moving_objects(30, 144, 192)
    v = write_video(tmp_path / "m.mp4", frames)
    x, y, w, h = boxes[5]
    c = {"id": "c", "type": "video", "src": str(v), "in": 0.2, "out": 1.0, "label": "x"}
    n = vfx.apply(c, "tracked_mask", box=[x / 192, y / 144, w / 192, h / 144], effect="blur", amount=1.0)
    assert n["in"] == 0.0 and n["out"] == 0.8 and n["enhanced_from"][0]["op"] == "vfx.tracked_mask"
    assert abs(ff.probe(n["src"])["duration"] - 0.8) < 0.1
