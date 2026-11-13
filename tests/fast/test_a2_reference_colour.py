"""Reference profile colour + transition measurements (added for Demo 4): a hard cut, a dissolve and a fade through
black are told apart on rendered video; colour stats separate warm, cool and monochrome pictures; the overrides map
them to a grade and a transition policy."""
import subprocess

import numpy as np

from nexus import reference


def _clip(path, colour, d=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=192x108:r=24:d={d}", "-vf",
                    f"colorchannelmixer={colour}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_transitions_hard_gradual_and_fade(tmp_path):
    a = _clip(tmp_path / "a.mp4", "rr=1:gg=0.3:bb=0.3")
    b = _clip(tmp_path / "b.mp4", "rr=0.3:gg=0.3:bb=1")
    hard, dis, fade = tmp_path / "hard.mp4", tmp_path / "dis.mp4", tmp_path / "fade.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-i", str(b), "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(hard)], check=True)
    for out, tr in ((dis, "dissolve"), (fade, "fadeblack")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-i", str(b), "-filter_complex",
                        f"[0:v][1:v]xfade=transition={tr}:duration=0.8:offset=1.2[v]", "-map", "[v]", "-c:v", "libx264",
                        "-preset", "ultrafast", str(out)], check=True)
    assert reference.transition_profile(hard, [2.0])["hard_cut"] == 1
    assert reference.transition_profile(dis, [1.6])["gradual"] == 1
    assert reference.transition_profile(fade, [1.6])["fade_through_black"] == 1


def test_colour_profile_and_grade_override():
    warm = np.zeros((4, 36, 64, 3), np.uint8)
    warm[..., 0], warm[..., 1], warm[..., 2] = 200, 120, 60
    cool = warm[..., ::-1].copy()
    grey = np.full((4, 36, 64, 3), 128, np.uint8)
    cw, cc, cg = (reference.colour_profile(x) for x in (warm, cool, grey))
    assert cw["warmth"] > 0.3 and cc["warmth"] < -0.3 and cg["monochrome_share"] == 1.0 and cg["saturation"] == 0.0
    base = {"name": "r", "metrics": {"cuts_per_min": 4, "colour": cw,
                                     "transitions": {"measured_cuts": 5, "hard_cut": 1, "gradual": 4, "fade_through_black": 0,
                                                     "hard_cut_share": 0.2, "gradual_share": 0.8, "fade_through_black_share": 0.0}}}
    o = reference.to_overrides(base)["profile_overrides"]
    assert o["grade"] == "warm" and o["transitions"]["crossfade_at_topics"] is True and o["transitions"]["xfade"] == "dissolve"
    base["metrics"]["colour"] = cg
    assert reference.to_overrides(base)["profile_overrides"]["grade"] == "bw"
