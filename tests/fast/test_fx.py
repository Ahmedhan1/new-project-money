"""FX-1 / PERF-1: the renderer really applies effects, transitions, captions, titles, PiP, overlays, colour correction,
stabilisation, roles and draft/parallel modes. Every assertion is on rendered output (ffprobe, pixels, audio), built
from FFmpeg lavfi sources. No models, no network."""
import subprocess

import numpy as np
import pytest

from nexus.media import ff
from nexus.timeline import ops, render

W, H = 320, 180


def _src(path, d=4.0, pattern="testsrc2", tone=440):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{pattern}{':' if '=' in pattern else '='}s={W}x{H}:r=30:d={d}",
                    "-f", "lavfi", "-i", f"sine=f={tone}:d={d}:r=48000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return str(path)


def _wav(path, d=4.0, tone=880, vol=0.5):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f={tone}:d={d}:r=48000",
                    "-af", f"volume={vol}", str(path)], check=True)
    return str(path)


def _frame(path, t, size=None):
    w, h = size or (W, H)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: w * h], dtype=np.uint8).reshape(h, w)


def _tl(clips, **kw):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
            "markers": [], **kw}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    return {"a": _src(d / "a.mp4"), "b": _src(d / "b.mp4", pattern="smptebars", tone=330),
            "gray": _src(d / "g.mp4", pattern="color=c=0x808080"), "dir": d}


def test_effects_render_and_letterbox_bars_are_black(home, media, tmp_path):
    c = ops.video_clip(media["a"], 0, 2.0, effects=[{"type": "punch_in", "start": 0.2, "end": 1.0, "scale": 1.2},
                                                     {"type": "vignette"}, {"type": "grain", "amount": 6},
                                                     {"type": "letterbox", "ratio": 2.39}, {"type": "flash", "at": 1.5}])
    st = render.render(_tl([c]), tmp_path / "o.mp4", tmp_path / "w", workers=1)
    assert abs(st["duration"] - 2.0) < 0.1
    f = _frame(tmp_path / "o.mp4", 0.6)
    bar = int((H - W / 2.39) / 2)
    assert f[: bar - 2].mean() < 20 and f[H - bar + 2:].mean() < 20      # letterbox bars
    assert f[bar + 5: H - bar - 5].mean() > 40                          # picture between them


def test_xfade_transition_shortens_programme_by_its_duration(home, media, tmp_path):
    a = ops.video_clip(media["a"], 0, 2.0)
    b = ops.video_clip(media["b"], 0, 2.0, transition="crossfade", xfade="wipeleft", transition_dur=0.5)
    tl = _tl([a, b])
    st = render.render(tl, tmp_path / "x.mp4", tmp_path / "w", workers=1)
    assert abs(st["duration"] - 3.5) < 0.1 and abs(render.total_duration(tl) - 3.5) < 1e-6


def test_parallel_matches_serial_and_reuses_cache(home, media, tmp_path):
    clips = [ops.video_clip(media["a"], k * 0.8, k * 0.8 + 0.8, grade=g) for k, g in enumerate(["none", "warm", "cool", "bw"])]
    s1 = render.render(_tl(clips), tmp_path / "s.mp4", tmp_path / "w1", workers=1)
    for p in (home / "cache" / "segments").glob("seg_*.mp4"):
        p.unlink()
    s2 = render.render(_tl(clips), tmp_path / "p.mp4", tmp_path / "w2", workers=3)
    assert s2["workers"] == 3 and s1["segments_from_cache"] == 0 and s2["segments_from_cache"] == 0
    assert abs(s1["duration"] - s2["duration"]) < 0.05
    pa, pb = ff.probe(tmp_path / "s.mp4"), ff.probe(tmp_path / "p.mp4")
    assert (pa["video"]["width"], pa["video"]["height"]) == (pb["video"]["width"], pb["video"]["height"])
    s3 = render.render(_tl(clips), tmp_path / "c.mp4", tmp_path / "w3", workers=2)
    assert s3["segments_from_cache"] == 4


def test_draft_mode_is_half_size_and_cached_separately(home, media, tmp_path):
    c = ops.video_clip(media["a"], 0, 1.0)
    full = render.segment_key(c, _tl([c]))
    st = render.render(_tl([c]), tmp_path / "d.mp4", tmp_path / "w", draft=True)
    v = ff.probe(tmp_path / "d.mp4")["video"]
    assert (v["width"], v["height"]) == (W // 2, H // 2) and st["draft"]
    assert render.segment_key(c, _tl([c]), draft=True) != full


@pytest.mark.parametrize("style", ["classic", "pop", "boxed"])
def test_caption_styles_draw_text_in_the_bottom_band(home, media, tmp_path, style):
    c = ops.video_clip(media["gray"], 0, 2.0)
    words = [{"start": 0.2, "end": 0.7, "word": "Hello"}, {"start": 0.7, "end": 1.3, "word": "captions"}]
    tl = _tl([c], subtitles=[{"start": 0.2, "end": 1.8, "text": "Hello captions", "words": words}],
             captions={"style": style, "position": "bottom"})
    render.render(tl, tmp_path / f"{style}.mp4", tmp_path / "w")
    with_cap = _frame(tmp_path / f"{style}.mp4", 1.0)
    tl2 = {**tl, "burn_subtitles": False}
    render.render(tl2, tmp_path / "none.mp4", tmp_path / "w2")
    without = _frame(tmp_path / "none.mp4", 1.0)
    band = slice(int(H * 0.6), H)
    assert np.abs(with_cap[band].astype(int) - without[band].astype(int)).mean() > 2.0
    assert np.abs(with_cap[: int(H * 0.4)].astype(int) - without[: int(H * 0.4)].astype(int)).mean() < 1.0


def test_pip_titles_and_colour_correction_change_the_right_pixels(home, media, tmp_path):
    base = ops.video_clip(media["gray"], 0, 2.0)
    pip = ops.video_clip(media["gray"], 0, 2.0, pip={"src": media["a"], "x": 0.65, "y": 0.05, "w": 0.3})
    ttl = ops.video_clip(media["gray"], 0, 2.0, titles=[{"text": "Lower third", "style": "lower_third", "start": 0.1,
                                                          "end": 1.9, "anim": "slide"}])
    bright = ops.video_clip(media["gray"], 0, 2.0, correct={"exposure": 1.0})
    out = {}
    for name, c in (("base", base), ("pip", pip), ("title", ttl), ("bright", bright)):
        render.render(_tl([c]), tmp_path / f"{name}.mp4", tmp_path / f"w{name}")
        out[name] = _frame(tmp_path / f"{name}.mp4", 1.0).astype(int)
    corner = (slice(int(H * 0.08), int(H * 0.25)), slice(int(W * 0.7), int(W * 0.9)))
    assert np.abs(out["pip"][corner] - out["base"][corner]).mean() > 10
    assert np.abs(out["pip"][H // 2:, : W // 2] - out["base"][H // 2:, : W // 2]).mean() < 2
    assert np.abs(out["title"] - out["base"]).mean() > 0.5
    assert out["bright"].mean() > out["base"].mean() + 15


def test_stabilize_and_rotation_render(home, media, tmp_path):
    c = ops.video_clip(media["a"], 0, 1.5, stabilize={"smoothing": 8}, rotate=3)
    st = render.render(_tl([c]), tmp_path / "st.mp4", tmp_path / "w")
    assert abs(st["duration"] - 1.5) < 0.1


def test_progress_bar_overlay_grows(home, media, tmp_path):
    c = ops.video_clip(media["gray"], 0, 3.0)
    tl = _tl([c], overlays=[{"type": "progress_bar", "color": "#FFFFFF", "height": 12, "position": "bottom"}])
    render.render(tl, tmp_path / "pb.mp4", tmp_path / "w")
    early, late = _frame(tmp_path / "pb.mp4", 0.3), _frame(tmp_path / "pb.mp4", 2.7)
    row = slice(H - 3, H)
    assert (late[row] > 200).mean() > (early[row] > 200).mean() + 0.5


def test_sfx_does_not_key_the_music_ducking(home, media, tmp_path):
    tl = _tl([ops.color_clip(3.0)], music={"src": _wav(media["dir"] / "m.wav", 3.0, 220, 0.3), "volume": 0.5, "duck": True},
             audio=[{"src": _wav(media["dir"] / "s.wav", 0.5, 2000, 0.6), "start": 1.0, "role": "sfx"},
                    {"src": _wav(media["dir"] / "n.wav", 0.5, 600, 0.6), "start": 2.0, "role": "narration"}])
    fc, _ = render._mix_graph(tl, 2, True, 3.0)
    sp = next(line for line in fc if line.endswith("[sp]"))
    assert "[n1]" in sp and "[n0]" not in sp                 # narration keys the ducking, the sfx does not
    assert any("[n0]" in line and "[pre]" in line for line in fc)  # sfx is still mixed into the programme
    st = render.render(tl, tmp_path / "mix.mp4", tmp_path / "w")
    assert abs(st["duration"] - 3.0) < 0.1


def test_clip_past_the_end_of_its_source_fails_loudly(home, media, tmp_path):
    c = ops.video_clip(media["a"], 10.0, 11.0)
    with pytest.raises(ff.FFError, match="past the end"):
        render.render(_tl([c]), tmp_path / "bad.mp4", tmp_path / "w")


def test_legacy_timeline_renders_unchanged_keys(home, media, tmp_path):
    c = ops.video_clip(media["a"], 0, 1.0)
    tl = _tl([c])
    k1 = render.segment_key(c, tl)
    assert render.segment_key({**c, "effects": [{"type": "vignette"}]}, tl) != k1
    assert render.segment_key({**c, "label": "x", "notes": {"a": 1}}, tl) == k1


# ---------------------------------------------------------------- FX-2: builders wired into the renderer
def _gray_frame(path, t, w=W, h=H):
    return _frame(path, t, (w, h)).astype(int)


def _band_level(path, t0, dur, lo, hi):
    err = subprocess.run(["ffmpeg", "-ss", f"{t0:.2f}", "-t", f"{dur:.2f}", "-i", str(path), "-af",
                          f"highpass=f={lo},lowpass=f={hi},volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(err.split("mean_volume:")[1].split("dB")[0])


def test_freeze_frame_holds_the_picture_and_silences_audio(home, media, tmp_path):
    c = ops.video_clip(media["a"], 0, 2.0, freeze={"at": 0.5, "duration": 1.0})
    assert abs(render.clip_duration(c) - 3.0) < 1e-6
    st = render.render(_tl([c]), tmp_path / "fz.mp4", tmp_path / "w")
    assert abs(st["duration"] - 3.0) < 0.1
    f1, f2 = _gray_frame(tmp_path / "fz.mp4", 0.7), _gray_frame(tmp_path / "fz.mp4", 1.3)
    assert np.abs(f1 - f2).mean() < 1.0                          # held frame
    assert _band_level(tmp_path / "fz.mp4", 0.8, 0.5, 100, 2000) < -50  # silence under the freeze


def test_split_screen_shows_both_sources(home, media, tmp_path):
    c = ops.video_clip(media["gray"], 0, 1.5, split={"srcs": [media["b"]], "layout": "h", "gap": 0})
    render.render(_tl([c]), tmp_path / "sp.mp4", tmp_path / "w")
    f = _gray_frame(tmp_path / "sp.mp4", 0.7)
    left, right = f[:, : W // 2 - 4], f[:, W // 2 + 4:]
    assert left.std() < 6 and right.std() > 20                  # flat grey panel | colour bars panel


def test_chroma_key_replaces_the_green_screen(home, media, tmp_path):
    green = _src(media["dir"] / "green.mp4", d=2.0, pattern="color=c=0x00FF00")
    red = media["dir"] / "red.png"
    from PIL import Image
    Image.new("RGB", (W, H), (220, 20, 20)).save(red)
    c = ops.video_clip(green, 0, 1.0, chroma_key={"color": "#00FF00", "similarity": 0.2, "bg": str(red)})
    render.render(_tl([c]), tmp_path / "ck.mp4", tmp_path / "w")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(tmp_path / "ck.mp4"), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    px = np.frombuffer(raw[: W * H * 3], dtype=np.uint8).reshape(H, W, 3).astype(int)
    assert px[..., 0].mean() > 150 and px[..., 1].mean() < 90    # red background where the green was


def test_mask_blacks_out_only_outside_its_region(home, media, tmp_path):
    c = ops.video_clip(media["gray"], 0, 1.0, mask={"shape": "rect", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "feather": 0,
                                                     "effect": "black", "region": "outside"})
    render.render(_tl([c]), tmp_path / "mk.mp4", tmp_path / "w")
    f = _gray_frame(tmp_path / "mk.mp4", 0.5)
    assert f[:10, :10].mean() < 25 and f[H // 2 - 5: H // 2 + 5, W // 2 - 5: W // 2 + 5].mean() > 100


def test_credits_roll_moves_up(home, media, tmp_path):
    c = ops.color_clip(3.0, credits={"title": "Credits", "lines": ["Footage: NASA", "Edit: NEXUS", "Music: procedural"]})
    render.render(_tl([c]), tmp_path / "cr.mp4", tmp_path / "w")

    def text_rows(t):
        f = _gray_frame(tmp_path / "cr.mp4", t)
        rows = np.where((f > 150).sum(1) > 3)[0]
        return rows.mean() if len(rows) else None
    a, b = text_rows(1.0), text_rows(2.0)
    assert a is not None and b is not None and b < a - 10       # the text block moved up


def test_keyframed_pan_and_volume(home, media, tmp_path):
    from PIL import Image
    grad = media["dir"] / "grad.png"
    Image.fromarray(np.tile(np.linspace(0, 255, 640).astype("uint8"), (180, 1))).convert("RGB").save(grad)
    wide = media["dir"] / "wide.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(grad), "-f", "lavfi", "-i", "sine=f=500:d=2:r=48000",
                    "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(wide)], check=True)
    c = ops.video_clip(str(wide), 0, 2.0, keyframes={"x": [[0, 0.0], [2.0, 1.0, "ease"]], "volume": [[0, 0.05], [1.5, 1.0]]})
    tl = _tl([c])
    tl["width"], tl["height"] = 180, 320                          # vertical crop of a wide gradient
    render.render(tl, tmp_path / "kf.mp4", tmp_path / "w")
    early, late = _frame(tmp_path / "kf.mp4", 0.1, (180, 320)), _frame(tmp_path / "kf.mp4", 1.9, (180, 320))
    assert late.mean() > early.mean() + 60                       # the crop window travelled from the dark to the bright end
    assert _band_level(tmp_path / "kf.mp4", 1.6, 0.3, 300, 800) > _band_level(tmp_path / "kf.mp4", 0.05, 0.3, 300, 800) + 15


def test_j_cut_brings_the_next_clip_audio_under_the_previous_picture(home, media, tmp_path):
    lo = _src(media["dir"] / "jc_lo.mp4", d=4.0, tone=300)
    hi = _src(media["dir"] / "jc_hi.mp4", d=4.0, pattern="smptebars", tone=2000)
    a = ops.video_clip(lo, 0, 2.0)
    b = ops.video_clip(hi, 1.0, 3.0, audio_lead=0.8)             # b's 2 kHz tone is heard 0.8 s before its picture

    def band(path, t0):
        err = subprocess.run(["ffmpeg", "-ss", f"{t0:.2f}", "-t", "0.4", "-i", str(path), "-af",
                              "bandpass=f=2000:width_type=h:w=150,bandpass=f=2000:width_type=h:w=150,volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        return float(err.split("mean_volume:")[1].split("dB")[0])
    st = render.render(_tl([a, b]), tmp_path / "jc.mp4", tmp_path / "w")
    assert abs(st["duration"] - 4.0) < 0.1                       # picture timing unchanged
    assert band(tmp_path / "jc.mp4", 1.5) > band(tmp_path / "jc.mp4", 0.3) + 15
    st2 = render.render(_tl([ops.video_clip(lo, 0, 2.0), ops.video_clip(hi, 1.0, 3.0)]), tmp_path / "hard.mp4", tmp_path / "w2")
    assert band(tmp_path / "hard.mp4", 1.5) < band(tmp_path / "jc.mp4", 1.5) - 15   # without the J-cut: no 2 kHz there
