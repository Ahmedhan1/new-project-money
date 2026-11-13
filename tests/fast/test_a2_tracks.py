"""Account 2 / agent A: multi-track timeline (layers with z-order, opacity keyframes, per-layer effect stacks, audio and
text tracks) and opacity on main clips. Every assertion is on rendered frames/audio of small lavfi media."""
import subprocess

import numpy as np
import pytest

from nexus.timeline import ops, render, tracks

W, H = 320, 180


def _solid(path, colour, d=4.0, tone=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:r=30:d={d}"]
    if tone:
        cmd += ["-f", "lavfi", "-i", f"sine=f={tone}:d={d}:r=48000", "-shortest", "-c:a", "aac"]
    subprocess.run([*cmd, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def _gray(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: W * H], dtype=np.uint8).reshape(H, W).astype(float)


def _rgb(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: W * H * 3], dtype=np.uint8).reshape(H, W, 3).astype(float)


def _at(f, fx, fy, r=4):
    x, y = int(fx * W), int(fy * H)
    return float(f[y - r: y + r, x - r: x + r].mean())


def _tl(clips, **kw):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
            "markers": [], **kw}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("trk")
    return {"bright": _solid(d / "w.mp4", "0xE0E0E0"), "mid": _solid(d / "m.mp4", "0x808080"),
            "red": _solid(d / "r.mp4", "0xE02020"), "tone": _solid(d / "t.mp4", "0x101010", tone=440)}


def _base(d=3.0):
    return ops.color_clip(d, "0x202020")


def test_layers_draw_in_z_order_inside_their_boxes(home, media, tmp_path):
    tl = ops.add_track(_tl([_base()]), "video", "V2")
    tl = ops.add_track(tl, "video", "V3")
    tl = ops.add_item(tl, "V2", {"type": "video", "src": media["bright"], "in": 0, "out": 2.0, "x": 0, "y": 0, "w": 0.5, "h": 0.5},
                      start=0.5)
    tl = ops.add_item(tl, "V3", {"type": "video", "src": media["mid"], "in": 0, "out": 1.0, "x": 0.25, "y": 0.25, "w": 0.5,
                                 "h": 0.5}, start=1.0)
    assert tracks.validate(tl) == []
    st = render.render(tl, tmp_path / "o.mp4", tmp_path / "w", workers=1)
    assert abs(st["duration"] - 3.0) < 0.05 and st["segments"] == 3
    f0, f1 = _gray(tmp_path / "o.mp4", 0.2), _gray(tmp_path / "o.mp4", 1.5)
    dark, bright, mid = _at(f0, 0.1, 0.1), _at(f1, 0.1, 0.1), _at(f1, 0.4, 0.4)
    assert dark < 50 and bright > 180 and 100 < mid < 150          # V2 alone top-left, V3 drawn over V2 where they overlap
    assert _at(f1, 0.9, 0.9) < 50                                   # outside every box: the main storyline
    assert _at(f1, 0.6, 0.6) == pytest.approx(mid, abs=6)           # V3 over the base
    # z-order swap: V2 on top now shows bright where both overlap
    tl2 = ops.set_track(tl, "V2", z=5)
    render.render(tl2, tmp_path / "o2.mp4", tmp_path / "w", workers=1)
    assert _at(_gray(tmp_path / "o2.mp4", 1.5), 0.4, 0.4) > 180
    # after the layer items end the base shows again
    assert _at(_gray(tmp_path / "o.mp4", 2.8), 0.1, 0.1) < 50


def test_layer_opacity_keyframes_ramp_pixel_values(home, media, tmp_path):
    base = _base()
    tl = ops.add_track(_tl([base]), "video", "V2")
    tl = ops.add_item(tl, "V2", {"type": "video", "src": media["bright"], "in": 0, "out": 2.5}, start=0.5)
    tl = ops.set_keyframes(tl, tl["tracks"][0]["items"][0]["id"], "opacity", [[0, 0], [2, 1]])
    render.render(tl, tmp_path / "o.mp4", tmp_path / "w", workers=1)
    lo, hi = _at(_gray(tmp_path / "o.mp4", 0.2), 0.5, 0.5), _at(_gray(tmp_path / "o.mp4", 2.8), 0.5, 0.5)
    assert hi - lo > 120
    for t, a in ((1.0, 0.25), (2.0, 0.75)):                         # item-local 0.5 s and 1.5 s
        v = _at(_gray(tmp_path / "o.mp4", t), 0.5, 0.5)
        assert v == pytest.approx(lo + a * (hi - lo), abs=0.04 * (hi - lo) + 2), (t, v, lo, hi)
    # track opacity multiplies the item's: 0.5 at the end of the ramp
    tl2 = ops.set_track(tl, "V2", opacity=0.5)
    render.render(tl2, tmp_path / "o2.mp4", tmp_path / "w", workers=1)
    v = _at(_gray(tmp_path / "o2.mp4", 2.8), 0.5, 0.5)
    assert v == pytest.approx(lo + 0.5 * (hi - lo), abs=0.04 * (hi - lo) + 2)


def test_main_clip_opacity_keyframes_fade_to_black(home, media, tmp_path):
    c = ops.video_clip(media["bright"], 0, 2.0, keyframes={"opacity": [[0, 1], [2, 0]]})
    render.render(_tl([c]), tmp_path / "o.mp4", tmp_path / "w", workers=1)
    full = _at(_gray(tmp_path / "o.mp4", 0.0), 0.5, 0.5)
    half = _at(_gray(tmp_path / "o.mp4", 1.0), 0.5, 0.5)
    late = _at(_gray(tmp_path / "o.mp4", 1.8), 0.5, 0.5)
    assert full > 190 and late < 45
    assert half == pytest.approx((full + 16) / 2, abs=12)


def test_layer_effect_stack_and_text_track(home, media, tmp_path):
    tl = ops.add_track(_tl([_base()]), "video", "V2", grade="bw", effects=[{"type": "vignette"}])
    tl = ops.add_item(tl, "V2", {"type": "video", "src": media["red"], "in": 0, "out": 2.0, "x": 0.5, "y": 0, "w": 0.5, "h": 1},
                      start=0.5)
    tl = ops.add_track(tl, "text", "T1")
    tl = ops.add_item(tl, "T1", {"text": "LAYER TITLE", "style": "title", "end": 2.5, "anim": "fade"}, start=0.5)
    render.render(tl, tmp_path / "o.mp4", tmp_path / "w", workers=1)
    f = _rgb(tmp_path / "o.mp4", 1.5)
    box = f[20:160, 200:300]
    assert np.abs(box[..., 0] - box[..., 1]).mean() < 6              # red layer graded black-and-white by its track
    plain = render.render(_tl([_base()]), tmp_path / "p.mp4", tmp_path / "w", workers=1)
    assert plain["duration"] > 0
    diff = np.abs(_gray(tmp_path / "o.mp4", 1.5)[:, :150] - _gray(tmp_path / "p.mp4", 1.5)[:, :150])
    assert diff.max() > 80                                           # the title is drawn on the left half
    assert np.abs(_gray(tmp_path / "o.mp4", 0.2) - _gray(tmp_path / "p.mp4", 0.2)).max() < 12  # not before its start


def _rms(path, a, b):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(float)
    return float(np.sqrt((x[int(a * 8000): int(b * 8000)] ** 2).mean() + 1e-9))


def test_audio_track_item_window_and_mute(home, media, tmp_path):
    tl = ops.add_track(_tl([_base(3.0)]), "audio", "A2")
    tl = ops.add_item(tl, "A2", {"src": media["tone"], "in": 1.0, "out": 2.0}, start=1.0)
    render.render(tl, tmp_path / "o.mp4", tmp_path / "w", workers=1)
    before, during, after = _rms(tmp_path / "o.mp4", 0.1, 0.9), _rms(tmp_path / "o.mp4", 1.2, 1.8), _rms(tmp_path / "o.mp4", 2.2, 2.9)
    assert during > 50 * max(before, after, 1.0)
    render.render(ops.set_track(tl, "A2", mute=True), tmp_path / "m.mp4", tmp_path / "w", workers=1)
    assert _rms(tmp_path / "m.mp4", 1.2, 1.8) < 5


def test_track_ops_undo_redo_through_project_versions(home, media):
    from nexus import project
    p = project.Project.create("tracks")
    tl0 = _tl([_base()])
    p.save_timeline(tl0, "base")
    tl1 = ops.add_track(tl0, "video")
    tl1 = ops.add_item(tl1, "V2", {"type": "video", "src": media["mid"], "in": 0, "out": 1.0}, start=0.5)
    p.save_timeline(tl1, "layer")
    iid = tl1["tracks"][0]["items"][0]["id"]
    tl2 = ops.move_item(ops.add_track(tl1, "video"), iid, start=1.2, track_id="V3")
    p.save_timeline(tl2, "moved")
    assert p.timeline()["tracks"][1]["items"][0]["start"] == 1.2 and not p.timeline()["tracks"][0]["items"]
    assert p.undo()["tracks"][0]["items"][0]["start"] == 0.5
    assert "tracks" not in p.undo()
    assert p.redo()["tracks"][0]["items"][0]["id"] == iid
    with pytest.raises(ValueError):
        ops.move_item(ops.add_track(tl1, "audio"), iid, track_id="A2")
    over = ops.add_item(tl1, "V2", {"type": "video", "src": media["mid"], "in": 0, "out": 1.0}, start=1.0)
    assert any("overlap" in m for m in tracks.validate(over))
    assert ops.remove_item(tl1, iid)["tracks"][0]["items"] == []
    with pytest.raises(ValueError):
        ops.set_keyframes(tl1, iid, "nonsense", [[0, 1]])


def test_single_track_timeline_is_unchanged_by_the_tracks_feature(home, media, tmp_path):
    c = ops.video_clip(media["mid"], 0, 1.0)
    tl = _tl([c])
    assert tracks.plan(tl, W, H) == [] and tracks.with_audio(tl) is tl
    key = render.segment_key(c, tl)
    assert render.segment_key(c, {**tl, "tracks": []}) == key
