"""FX-1 / PERF-1: effects, transitions, animated captions, SFX, per-scene cache and the parallel renderer.
Media are generated with FFmpeg lavfi sources; no models, no network."""
import re
import wave

import numpy as np
import pytest

from nexus import qa
from nexus.fx import captions, effects, sfx, transitions
from nexus.media import ff
from nexus.timeline import render, subs


def _src(path, dur=3.0, size="480x270", audio=True):
    args = ["-f", "lavfi", "-t", dur, "-i", f"testsrc2=s={size}:r=30"]
    if audio:
        args += ["-f", "lavfi", "-t", dur, "-i", "sine=f=220:r=48000,volume='0.6+0.4*sin(2*PI*3*t)':eval=frame"]
    ff.run([*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *(["-c:a", "aac", "-shortest"] if audio else []), path])
    return str(path)


def _tl(W=480, H=270, clips=None, **kw):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none", "target_lufs": -14.0,
            "clips": clips or [], "audio": [], "music": None, "subtitles": [], "burn_subtitles": True, "markers": [], **kw}


# ------------------------------------------------------------------ transitions
def test_transition_names_aliases_and_verification():
    assert transitions.resolve("whip_left") == "slideleft" and transitions.resolve("dip_black") == "fadeblack"
    assert transitions.resolve("crossfade") == "fade" and transitions.resolve("no-such-thing") == "fade"
    assert transitions.resolve("Slide-Up") == "slideup"
    assert all(v in transitions.NAMES for v in transitions.ALIASES.values())
    assert transitions.is_transition("dissolve") and not transitions.is_transition("cut") and not transitions.is_transition(None)
    ok = transitions.verify(["fade", "slideleft", "fadeblack", "dissolve", "slideup"])
    assert all(ok.values()), ok
    assert transitions.verify(["definitely_not_a_transition"]) == {"definitely_not_a_transition": False}


def test_timing_counts_named_transitions_like_crossfade():
    tl = _tl(clips=[{"type": "color", "duration": 2.0}, {"type": "color", "duration": 2.0, "transition": "whip_left", "transition_dur": 0.5},
                    {"type": "color", "duration": 2.0, "transition": "cut"}])
    assert render.total_duration(tl) == pytest.approx(5.5)
    assert render.clip_starts(tl) == pytest.approx([0.0, 1.5, 3.5])


# ------------------------------------------------------------------ effects
def test_effect_filters_and_unknown_types_are_ignored():
    c = {"type": "color", "duration": 3.0, "effects": [{"type": "punch_in", "start": 0.5, "end": 1.5, "scale": 1.2}, {"type": "vignette"},
                                                        {"type": "grain", "amount": "bad"}, {"type": "letterbox", "ratio": 2.39},
                                                        {"type": "flash", "at": 0.0}, {"type": "glitch", "start": 1, "end": 1.4},
                                                        {"type": "warp-drive"}, "junk", {"type": "blur", "start": 5, "end": 4}]}
    geo, look = effects.video_filters(c, 1280, 720, 30, 3.0)
    assert len(geo) == 1 and geo[0].startswith("perspective=") and "eval=frame" in geo[0]
    joined = ",".join(look)
    assert "vignette" in joined and "noise=c0s=8" in joined and "drawbox" in joined and "color=white" in joined and "chromashift" in joined
    assert "gblur" not in joined  # empty window
    assert effects.video_filters({"type": "color", "duration": 1, "effects": [{"type": "nope"}]}, 640, 360, 30, 1.0) == ([], [])


def test_speed_ramp_duration_and_audio_steps():
    c = {"type": "video", "src": "x.mp4", "in": 0.0, "out": 4.0, "effects": [{"type": "speed_ramp", "from": 1.0, "to": 0.5, "start": 1.0, "end": 2.0}]}
    # 1 s at 1x, ramp 1 -> 0.5 over 1 s of source (ln(2)/0.5 s), then 2 s at 0.5x (4 s)
    assert render.clip_duration(c) == pytest.approx(1.0 + np.log(2) / 0.5 + 4.0, abs=1e-6)
    g = effects.ramp_audio(c, "[0:a]", "[ar]")
    assert g.startswith("[0:a]asplit=") and g.endswith("[ar]") and "atempo=0.5" in g
    assert effects.ramp_setpts(c).startswith("setpts='if(lt(")
    assert render.clip_duration({"type": "video", "in": 0, "out": 4.0, "speed": 2.0}) == 2.0


# ------------------------------------------------------------------ captions
def test_pop_captions_without_word_timings(tmp_path):
    cues = [{"start": 1.0, "end": 3.0, "text": "make every word count today"}]
    p = captions.write_ass(cues, tmp_path / "c.ass", 1080, 1920, "en", {"style": "pop", "highlight": "#FF0000", "uppercase": True,
                                                                         "words_per_cue": 2})
    s = p.read_text(encoding="utf-8")
    ev = [ln for ln in s.splitlines() if ln.startswith("Dialogue:")]
    assert len(ev) == 5  # one event per word, groups of 2 (+ the orphan joined to the last group)
    assert all("{\\1c&H0000FF&}" in e for e in ev)  # #FF0000 -> ASS &HBBGGRR&
    assert sum("\\t(" in e for e in ev) == 2  # pop animation once per group
    assert "MAKE" in s and ",-1\n" in s  # uppercase; Encoding -1 = libass auto base direction (RTL)
    m = re.search(r"Style: Default,[^,]+,(\d+),.*,(\d+),(\d+),(\d+),-1", s)
    size, ml, mr, mv = map(int, m.groups())
    assert mv >= 1920 * 0.15 and ml >= 1080 * 0.08 and size > 60  # safe margins on 9:16


def test_karaoke_uses_word_timings_and_arabic_is_not_uppercased(tmp_path):
    cue = {"start": 0.0, "end": 2.0, "text": "مرحبا بكم جميعا",
           "words": [{"start": 0.2, "end": 0.6, "word": "مرحبا"}, {"start": 0.7, "end": 1.2, "word": "بكم"}, {"start": 1.3, "end": 1.9, "word": "جميعا"}]}
    s = captions.write_ass([cue], tmp_path / "k.ass", 1280, 720, "ar", {"style": "karaoke", "uppercase": True}).read_text(encoding="utf-8")
    assert "Noto Naskh Arabic" in s and "مرحبا" in s
    ks = [int(x) for x in re.findall(r"\\k(\d+)", s)]
    assert ks[0] == 20 and sum(ks) == pytest.approx(190, abs=2)
    wt = captions.word_times({"start": 0, "end": 3, "text": "a bbbb"})
    assert wt[0][1] == pytest.approx(3 * 2 / 7) and wt[-1][1] == pytest.approx(3)


def test_no_caption_options_reproduces_legacy_ass(tmp_path):
    cues = [{"start": 0.5, "end": 2.0, "text": "Hello there"}]
    a = captions.write_ass(cues, tmp_path / "a.ass", 1280, 720, "en", None).read_text(encoding="utf-8")
    b = subs.write_ass(cues, tmp_path / "b.ass", 1280, 720, "en").read_text(encoding="utf-8")
    assert a == b


# ------------------------------------------------------------------ sfx
def test_sfx_are_shaped_normalised_and_cached(home):
    for name in sfx.NAMES:
        p = sfx.make(name, seed=3)
        with wave.open(str(p)) as w:
            assert w.getnchannels() == 2 and w.getframerate() == 48000
            y = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32767
        peak = 20 * np.log10(np.abs(y).max())
        assert -3.3 < peak < -2.7, (name, peak)
        assert np.abs(y[:4]).max() < 0.01 and np.abs(y[-4:]).max() < 0.01, name  # no click at either end
        assert sfx.make(name, seed=3) == p
    with pytest.raises(ValueError):
        sfx.make("kazoo")


# ------------------------------------------------------------------ renderer
def test_changed_effect_invalidates_only_that_clip(home, tmp_path):
    clips = [{"id": f"c{i}", "type": "color", "duration": 1.0, "color": col} for i, col in enumerate(["0x203050", "0x503020", "0x205030"])]
    tl = _tl(320, 180, clips)
    keys = [render.segment_key(c, tl) for c in clips]
    s1 = render.render(tl, tmp_path / "a.mp4", tmp_path / "w")
    assert s1["segments_from_cache"] == 0 and s1["workers"] >= 1 and s1["draft"] is False
    tl2 = {**tl, "clips": [dict(c) for c in clips]}
    tl2["clips"][1]["effects"] = [{"type": "vignette"}]
    keys2 = [render.segment_key(c, tl2) for c in tl2["clips"]]
    assert [a == b for a, b in zip(keys, keys2)] == [True, False, True]
    s2 = render.render(tl2, tmp_path / "b.mp4", tmp_path / "w")
    assert s2["segments_from_cache"] == 2
    # pip source, overlays-free caption edits and transitions never touch the clip's own key
    tl3 = {**tl2, "subtitles": [{"start": 0.2, "end": 1.5, "text": "only captions changed"}]}
    s3 = render.render(tl3, tmp_path / "c.mp4", tmp_path / "w")
    assert s3["segments_from_cache"] == 3 and s3["pieces"] == 3 and s3["audio_from_cache"]
    assert render.segment_key({**clips[0], "transition": "crossfade", "xfade": "slideup"}, tl) == keys[0]


def test_full_render_with_effects_captions_overlays_pip_passes_technical_review(home, tmp_path):
    src = _src(tmp_path / "s.mp4", 3.0)
    pip = _src(tmp_path / "p.mp4", 3.0, "160x90", audio=False)
    clips = [
        {"id": "a", "type": "video", "src": src, "in": 0.0, "out": 2.5,
         "effects": [{"type": "punch_in", "start": 0.3, "end": 1.5}, {"type": "flash", "at": 0.0}, {"type": "vignette"}, {"type": "glitch", "start": 1.6, "end": 2.0}]},
        {"id": "b", "type": "video", "src": src, "in": 0.5, "out": 3.0, "transition": "crossfade", "xfade": "whip_left", "transition_dur": 0.4,
         "effects": [{"type": "zoom", "from": 1.0, "to": 1.2}, {"type": "grain"}, {"type": "speed_ramp", "from": 1.0, "to": 0.6, "start": 0.5, "end": 1.5}],
         "pip": {"src": pip, "x": 0.65, "y": 0.08, "w": 0.3, "shape": "circle"}},
        {"id": "c", "type": "color", "duration": 2.0, "transition": "crossfade", "xfade": "dip_black", "transition_dur": 0.5,
         "effects": [{"type": "letterbox"}, {"type": "shake", "start": 0.2, "end": 1.2}], "pip": {"src": pip, "shape": "rect", "x": 0.05, "y": 0.5}},
    ]
    tl = _tl(480, 270, clips, captions={"style": "pop", "highlight": "#FFD400", "uppercase": True},
             overlays=[{"type": "progress_bar", "color": "#3EC5FF", "height": 6}, {"type": "watermark", "text": "NEXUS"}])
    total = render.total_duration(tl)
    tl["subtitles"] = subs.cues_from_text("cut the noise and keep the story moving fast", 0.2, total - 0.2, 24)
    tl["audio"] = [{"src": str(sfx.make("whoosh")), "start": 2.0, "role": "sfx", "volume": 0.4}]
    progress = []
    st = render.render(tl, tmp_path / "out.mp4", tmp_path / "w", on_progress=progress.append, workers=2)
    assert progress[-1] == 1.0 and progress == sorted(progress)
    assert st["workers"] == 2 and st["pieces"] == 5
    rv = qa.technical_review(st["output"], qa.expectation(tl))
    assert rv["passed"], [c for c in rv["checks"] if not c["ok"]]
    # warm re-render: everything comes from cache and the final pass only stream-copies
    st2 = render.render(tl, tmp_path / "out2.mp4", tmp_path / "w")
    assert st2["segments_from_cache"] == 3 and st2["pieces_from_cache"] == st2["pieces"] and st2["audio_from_cache"]


def test_draft_render_is_smaller_and_uses_its_own_cache(home, tmp_path):
    from nexus import config
    tl = _tl(640, 360, [{"id": "a", "type": "color", "duration": 1.0}, {"id": "b", "type": "color", "duration": 1.0, "color": "0x502020",
                                                                      "transition": "crossfade", "xfade": "slideup", "transition_dur": 0.3}])
    st = render.render(tl, tmp_path / "d.mp4", tmp_path / "w", draft=True)
    v = ff.probe(st["output"])["video"]
    assert st["draft"] and (v["width"], v["height"]) == (320, 180)
    assert list((config.CACHE / "segments_draft").glob("part_*")) and not list((config.CACHE / "segments").glob("part_*"))


def test_hardware_encoder_detection_falls_back_to_libx264(home, monkeypatch):
    found = render.hw_encoders(refresh=True)
    assert set(found) == set(render.HW_CANDIDATES) and all(isinstance(v, bool) for v in found.values())
    monkeypatch.setenv("NEXUS_HWENC", "0")
    assert render.encoder()["name"] == "libx264"
    monkeypatch.setenv("NEXUS_HWENC", "1")
    monkeypatch.setattr(render, "_hw_cache", {k: False for k in render.HW_CANDIDATES})
    assert render.encoder()["name"] == "libx264"
    monkeypatch.setattr(render, "_hw_cache", {**{k: False for k in render.HW_CANDIDATES}, "h264_nvenc": True})
    assert render.encoder()["name"] == "h264_nvenc"
