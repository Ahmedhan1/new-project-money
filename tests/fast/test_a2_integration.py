"""Lead integration fixes found while driving the Cinematic Workspace (Account 2)."""
from nexus import edit, project, qa
from nexus.timeline import ops, render


def test_draft_expectation_uses_draft_geometry():
    tl = project.new_timeline(1280, 720)
    tl["clips"] = [ops.color_clip(2)]
    full, draft = qa.expectation(tl), qa.expectation(tl, draft=True)
    assert (full["width"], full["height"]) == (1280, 720)
    assert (draft["width"], draft["height"]) == render._size(tl, True)[:2] and draft["width"] < 1280


def test_retimed_captions_never_run_past_the_programme():
    src = "/media/talk.mp4"
    words = [{"start": 0.1 + i * 0.3, "end": 0.35 + i * 0.3, "word": f"w{i}"} for i in range(6)] + [{"start": 4.85, "end": 4.95, "word": "end"}]
    an = {src: {"transcript": {"segments": [{"start": 0, "end": 5, "text": "", "words": words}]}}}
    tl = project.new_timeline(640, 360)
    tl["clips"] = [ops.video_clip(src, 0, 2.0), ops.video_clip(src, 4.8, 5.0)]  # last clip is 0.2 s: its cue would be stretched
    edit.retime_subtitles(tl, an)
    total = render.total_duration(tl)
    assert tl["subtitles"] and all(c["end"] <= total + 1e-6 for c in tl["subtitles"])


def _tone(path, freq=440, dur=6.0):
    from nexus.media import ff
    ff.run(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}:sample_rate=48000", "-ac", "2", str(path)])
    return str(path)


def _channel_rms(path):
    import json
    import subprocess
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "astats=measure_overall=0:measure_perchannel=RMS_level",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return [float(line.rsplit(":", 1)[1]) for line in out.splitlines() if "RMS level dB" in line][:2] or json.loads("[]")


def test_renderer_pans_an_sfx_item_and_ducks_music_with_keyframes(home, tmp_path):
    """Agent C's mix requests wired into the renderer: `pan` moves an item to one side; `duck: "keyframes"` lowers the
    music where captions say someone speaks (no sidechain), and the music comes back up in the pause."""
    from nexus.timeline import render as R
    tl = project.new_timeline(320, 180)
    tl["clips"] = [ops.color_clip(6.0)]
    tl["target_lufs"] = -20
    # 1) panned item, no music: left channel much louder than right
    tl["audio"] = [{"src": _tone(tmp_path / "sfx.wav", 1000), "start": 0, "role": "sfx", "pan": 0.0}]
    out = tmp_path / "pan.mp4"
    R.render(tl, out, tmp_path / "w1")
    left, right = _channel_rms(out)
    assert left - right > 20, (left, right)
    # 2) keyframe duck: speech cue 0-2.5 s, pause after it; the music is quieter under the cue than in the pause
    tl["audio"] = []
    tl["subtitles"] = [{"start": 0.0, "end": 2.5, "text": "someone is talking here"}]
    tl["burn_subtitles"] = False
    tl["music"] = {"src": _tone(tmp_path / "bed.wav", 220), "volume": 0.5, "duck": "keyframes"}
    out2 = tmp_path / "duck.mp4"
    R.render(tl, out2, tmp_path / "w2")
    from nexus.media import ff
    a = tmp_path / "under.wav"
    b = tmp_path / "pause.wav"
    ff.run(["-ss", "1.2", "-t", "0.8", "-i", str(out2), str(a)])
    ff.run(["-ss", "4.2", "-t", "0.8", "-i", str(out2), str(b)])
    under, pause = _channel_rms(a)[0], _channel_rms(b)[0]
    assert pause - under > 4, (under, pause)
