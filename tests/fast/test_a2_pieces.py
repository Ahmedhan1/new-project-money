"""Account 2 / agent A: the piece pipeline (timeline/pieces.py) against the legacy pipeline on the same timeline:
same length, same pictures (frame comparisons), same loudness; a warm render reuses everything; a caption-only edit
re-encodes only the pieces the changed cue touches; the output decodes without errors."""
import subprocess

import numpy as np
import pytest

from nexus.media import ff
from nexus.timeline import ops, pieces, render

W, H = 320, 180


def _src(path, pattern, tone, d=4.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{pattern}=s={W}x{H}:r=30:d={d}", "-f", "lavfi",
                    "-i", f"sine=f={tone}:d={d}:r=48000", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return str(path)


def _all(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).astype(float)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("pc")
    return {"a": _src(d / "a.mp4", "testsrc2", 440), "b": _src(d / "b.mp4", "smptebars", 330)}


def _tl(media):
    clips = [ops.video_clip(media["a"], 0, 1.5),
             ops.video_clip(media["b"], 0.5, 2.0, transition="crossfade", xfade="fade", transition_dur=0.4),
             ops.video_clip(media["a"], 2.0, 3.0),
             ops.video_clip(media["b"], 1.0, 2.2, transition="crossfade", xfade="wipeleft", transition_dur=0.3)]
    subs = [{"start": 0.2, "end": 1.0, "text": "first cue"}, {"start": 1.4, "end": 2.4, "text": "second cue"},
            {"start": 3.5, "end": 4.2, "text": "third cue"}]
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": subs, "burn_subtitles": True,
            "markers": [], "captions": {"style": "boxed"}, "overlays": [{"type": "progress_bar"}]}


def test_pieces_match_legacy_and_reuse_on_edits(home, media, tmp_path):
    tl = _tl(media)
    plan, frames = pieces.plan(tl)
    assert frames == round(render.total_duration(tl) * 30) and [p["kind"] for p in plan].count("xf") == 2
    new = render.render(tl, tmp_path / "n.mp4", tmp_path / "w", workers=2)
    old = render.render({**tl, "render_pipeline": "legacy"}, tmp_path / "o.mp4", tmp_path / "w", workers=2)
    assert new["pipeline"] == "pieces" and "pipeline" not in old
    pn = ff.probe(tmp_path / "n.mp4")
    assert int(pn["video"].get("nb_frames") or 0) in (0, frames)
    fn, fo = _all(tmp_path / "n.mp4"), _all(tmp_path / "o.mp4")
    assert len(fn) == len(fo) == frames
    diff = np.abs(fn - fo).mean(axis=(1, 2))
    assert diff.max() < 4.0, f"largest per-frame mean difference {diff.max():.2f} at frame {int(diff.argmax())}"
    assert abs(float(pn["audio"]["duration"]) - float(pn["video"]["duration"])) < 0.05
    assert abs(ff.loudness(tmp_path / "n.mp4")["lufs"] - ff.loudness(tmp_path / "o.mp4")["lufs"]) < 0.5
    assert ff.decode_check(tmp_path / "n.mp4") == []
    # warm: every segment, piece and the audio mix come from the cache
    warm = render.render(tl, tmp_path / "n2.mp4", tmp_path / "w", workers=2)
    assert warm["pieces_from_cache"] == warm["pieces"] and warm["audio_from_cache"]
    # caption-only edit: only the pieces under the edited cue are re-encoded, no segment, audio from cache
    tl2 = {**tl, "subtitles": [*tl["subtitles"][:2], {**tl["subtitles"][2], "text": "edited cue"}]}
    ed = render.render(tl2, tmp_path / "n3.mp4", tmp_path / "w", workers=2)
    assert ed["segments_from_cache"] == ed["segments"] and ed["audio_from_cache"]
    assert 1 <= ed["pieces"] - ed["pieces_from_cache"] <= 2
    f3 = _all(tmp_path / "n3.mp4")
    changed = np.where(np.abs(f3 - fn).mean(axis=(1, 2)) > 0.5)[0]
    assert len(changed) and changed.min() / 30 >= 3.5 - 0.1 and changed.max() / 30 <= 4.2 + 0.1
    # picture-only clip edit: one segment re-rendered, the mixed audio is still reused (same audio packets)
    tl3 = {**tl, "clips": [*tl["clips"][:2], {**tl["clips"][2], "grade": "bw"}, tl["clips"][3]]}
    g = render.render(tl3, tmp_path / "n4.mp4", tmp_path / "w", workers=2)
    assert g["segments"] - g["segments_from_cache"] == 1 and g["audio_from_cache"]
    assert 1 <= g["pieces"] - g["pieces_from_cache"] <= 3


def test_cut_only_timeline_without_burn_is_a_stream_copy_of_segments(home, media, tmp_path):
    clips = [ops.video_clip(media["a"], 0, 1.0), ops.video_clip(media["b"], 0, 1.0)]
    tl = {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
          "markers": []}
    st = render.render(tl, tmp_path / "c.mp4", tmp_path / "w", workers=1)
    assert st["pieces"] == 2 and abs(st["duration"] - 2.0) < 1e-6
    seg = render.render_segment(clips[0], tl, home / "cache" / "segments")[0]
    a, b = _all(seg)[:30], _all(tmp_path / "c.mp4")[:30]
    assert np.abs(a - b).max() == 0                      # bit-identical pictures: no re-encode
