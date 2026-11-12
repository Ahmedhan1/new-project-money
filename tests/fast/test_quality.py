"""QSC-1: the weighted quality score catches deliberately broken files for the RIGHT reason, ranks a clean file above
every broken one, and never calls a merely-completed render premium. lavfi media only; no models, no network."""
import subprocess

import pytest

from nexus import quality

W, H, FPS = 320, 180, 30
TARGET = {"width": W, "height": H, "fps": FPS, "lufs": -16.0}
TONE = "sine=f=300:d={d}:r=48000,volume=0.6,aformat=channel_layouts=stereo"


def _make(path, vf: str, af: str, d: float = 6.0):
    """vf/af are filter chains applied to testsrc2 / a tone (both `d` seconds)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}:d={d}",
                    "-f", "lavfi", "-i", TONE.format(d=d), "-filter_complex", f"[0:v]{vf},format=yuv420p[v];[1:a]{af}[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                    "-c:a", "pcm_s16le" if "clip" in str(path) else "aac", "-b:a", "192k", "-t", str(d),
                    str(path).replace(".mp4", ".mov" if "clip" in str(path) else ".mp4")], check=True)
    return str(path).replace(".mp4", ".mov" if "clip" in str(path) else ".mp4")


LEVEL = "loudnorm=I=-16:TP=-2:LRA=7"
SCENARIOS = {
    "good": ("null", LEVEL),
    "black": ("drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,2,4.8)'", LEVEL),
    "frozen": ("trim=0:1.5,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=4.5", LEVEL),
    "quiet": ("null", "volume=0.01"),
    "clip": ("null", "volume=40,alimiter=limit=1:level=disabled,volume=1.5"),
    "silence": ("null", f"{LEVEL},volume=enable='between(t,0.8,8.0)':volume=0", 9.0),  # 7.2 s: past the 6 s fail bar
}


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("q")
    return {k: _make(d / f"{k}.mp4", *spec) for k, spec in SCENARIOS.items()}


@pytest.fixture(scope="module")
def reports(files):
    return {k: quality.score(p, target=TARGET) for k, p in files.items()}


def _failed(rep):
    return {c["id"] for c in rep["failed"]}


@pytest.mark.parametrize("name, check, verdicts", [
    ("black", "visual.black_frames", ("fail",)),
    ("frozen", "visual.frozen_frames", ("needs_work", "fail")),
    ("quiet", "audio.loudness", ("needs_work", "fail")),
    ("clip", "audio.clipping", ("fail",)),
    ("silence", "editing.dead_air", ("needs_work", "fail")),
])
def test_each_defect_is_caught_for_the_right_reason(reports, name, check, verdicts):
    rep = reports[name]
    assert check in _failed(rep), (name, sorted(_failed(rep)))
    assert rep["verdict"] in verdicts, (name, rep["verdict"], rep["verdict_reasons"])
    c = next(c for c in rep["failed"] if c["id"] == check)
    assert c["why"] and c.get("fix")


def test_clean_file_outscores_every_broken_one(reports):
    good = reports["good"]
    assert good["verdict"] in ("pass", "premium") and not any(c["severity"] == "critical" for c in good["failed"])
    for name, rep in reports.items():
        if name != "good":
            assert good["score"] > rep["score"], (name, good["score"], rep["score"])


def test_unmeasurable_criteria_are_listed_not_passed(reports):
    nm = {n["id"] for n in reports["good"]["not_measured"]}
    assert {"localization.translation_accuracy", "audio.voice_naturalness", "editing.storytelling"} <= nm
    checked = {c["id"] for c in reports["good"]["checks"]}
    assert not (nm & checked)


def test_completed_render_with_a_failed_major_check_is_never_premium():
    checks = [{"id": "editing.flash_cut", "ok": False, "severity": "major", "level": "fail", "hard": False}]
    cats = {k: {"score": 99.0} for k in quality.CATEGORIES}
    assert quality.verdict_for(99.0, checks, cats, [])[0] != "premium"
    crit_nm = [{"id": "editing.storytelling", "critical": True}]
    assert quality.verdict_for(99.0, [], cats, crit_nm)[0] != "premium"
    assert quality.verdict_for(99.0, [], cats, [])[0] == "premium"


def test_subtitle_overlap_and_range_use_the_timeline(files):
    tl = {"width": W, "height": H, "fps": FPS, "lang": "en", "clips": [{"id": "c1", "type": "color", "duration": 6.0}],
          "subtitles": [{"start": 0.5, "end": 2.5, "text": "First line here"}, {"start": 2.0, "end": 3.0, "text": "Overlapping"},
                        {"start": 7.0, "end": 8.0, "text": "After the end"}], "burn_subtitles": False}
    rep = quality.score(files["good"], tl=tl, target=TARGET)
    assert {"editing.subtitle_overlap", "editing.subtitle_range"} <= _failed(rep)


def test_fixes_map_a_loudness_failure_to_a_timeline_change(reports):
    tl = {"width": W, "height": H, "fps": FPS, "target_lufs": -14.0, "clips": [], "subtitles": []}
    ops = quality.fixes(reports["quiet"], tl)
    assert any(o.get("supported") is not False and "lufs" in str(o).lower() for o in ops), ops
    new, applied, _ = quality.apply_ops(tl, ops)
    assert applied and new is not tl
