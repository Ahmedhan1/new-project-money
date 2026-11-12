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
