"""REF-1: reference-style profile from a real rendered reference (cuts, text card ending, audio accents), applied as
structure to a different source through the director. No models."""
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageDraw
from test_director import BREAD, SOLAR, _analysis

from nexus import contracts, director, reference
from nexus.timeline.render import clip_duration


def _card(path, text, bg=(250, 250, 250)):
    im = Image.new("RGB", (320, 180), bg)
    d = ImageDraw.Draw(im)
    for k, line in enumerate(text.split("|")):
        d.text((24, 40 + 34 * k), line, fill=(0, 0, 0), font_size=24)
    im.save(path)


@pytest.fixture(scope="module")
def ref_video(tmp_path_factory):
    """14 s reference: 10 one-second colour shots (fast cuts), then a 4 s text end card. Audio: a beep at every cut."""
    d = tmp_path_factory.mktemp("ref")
    inputs, fc = [], []
    for k in range(10):
        p = d / f"c{k}.png"
        Image.new("RGB", (320, 180), ((37 * k) % 255, (91 * k + 60) % 255, (151 * k + 30) % 255)).save(p)
        inputs += ["-loop", "1", "-t", "1", "-i", str(p)]
    _card(d / "end.png", "SUBSCRIBE FOR MORE|NEW EPISODES WEEKLY|THANKS FOR WATCHING")
    inputs += ["-loop", "1", "-t", "4", "-i", str(d / "end.png")]
    for k in range(11):
        fc.append(f"[{k}:v]scale=320:180,setsar=1,fps=10,format=yuv420p[v{k}]")
    fc.append("".join(f"[v{k}]" for k in range(11)) + "concat=n=11:v=1:a=0[v]")
    beeps = "+".join(f"between(t,{k}.0,{k}.08)" for k in range(10))
    out = d / "ref.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-f", "lavfi", "-i",
                    f"aevalsrc='0.6*sin(2*PI*880*t)*({beeps})':s=16000:d=14", "-filter_complex", ";".join(fc),
                    "-map", "[v]", "-map", "11:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)],
                   check=True)
    an = {"probe": {"duration": 14.0, "audio": {"channels": 1}}, "scenes": [[k, k + 1] for k in range(10)] + [[10, 14]],
          "transcript": {"segments": [{"start": 0.3, "end": 1.8, "text": "Why does this work?",
                                       "words": [{"start": 0.3, "end": 0.6, "word": "Why"}, {"start": 0.65, "end": 0.9, "word": "does"},
                                                 {"start": 0.95, "end": 1.2, "word": "this"}, {"start": 1.25, "end": 1.8, "word": "work?"}]}]},
          "black": [], "faces": {"available": False}}
    return out, an


def test_text_heuristic_on_rendered_frames(tmp_path):
    _card(tmp_path / "t.png", "LINE ONE OF TEXT|LINE TWO OF TEXT")
    Image.new("RGB", (320, 180), (90, 140, 200)).save(tmp_path / "flat.png")
    g = lambda p: np.asarray(Image.open(p).convert("L").resize((256, 144)), dtype=np.float32)  # noqa: E731
    assert reference.text_bands(g(tmp_path / "t.png")) and not reference.text_bands(g(tmp_path / "flat.png"))


def test_refuses_without_lawful_basis(ref_video):
    with pytest.raises(reference.ReferenceRefused):
        reference.analyze(ref_video[0], ref_video[1], rights={"basis": "downloaded from somewhere"})


def test_profile_measures_structure_of_the_real_reference(ref_video):
    path, an = ref_video
    ref = reference.analyze(path, an, rights={"basis": "owned"})
    assert contracts.validate(ref) == []
    m = ref["metrics"]
    assert 38 <= m["cuts_per_min"] <= 47                         # 10 cuts in 14 s
    assert m["shot_length"]["median"] == pytest.approx(1.0, abs=0.05)
    assert m["first_3s"]["cuts"] == 2 and m["first_10s"]["cuts"] == 9 and m["first_3s"]["first_speech_s"] == pytest.approx(0.3, abs=0.01)
    assert m["ending"]["kind"] == "end_card" and m["ending"]["text_share"] >= 0.5
    assert m["on_screen_text_method"].startswith("edge-density")
    assert m["audio_accents_per_min"] >= 20                       # the beeps outside speech
    assert len(m["thumbnail_candidates"]) >= 2 and all(0 <= t["t"] <= 14 for t in m["thumbnail_candidates"])
    assert "no frames" in ref["rights"]["kept"]


def test_apply_reference_structure_to_a_different_source(ref_video):
    ref = reference.analyze(*ref_video, rights={"basis": "owned"})
    ov = reference.to_overrides(ref)
    assert ov["profile_overrides"]["shot_len"][1] <= 2.0 and ov["profile_overrides"]["ending"] == "end_card"
    talk = _analysis([(0.5, SOLAR + " " + BREAD)])
    base = director.plan({"talk.mp4": talk}, {"profile": "interview_storytelling", "target_seconds": 40})
    styled = reference.apply(ref, {"talk.mp4": talk}, {"profile": "interview_storytelling", "target_seconds": 40,
                                                       "end_text": "More next week"})

    def med(p):
        tl = director.build(p, (640, 360), music=False)
        xs = sorted(clip_duration(c) for c in tl["clips"] if c["type"] == "video")
        return xs[len(xs) // 2]
    assert med(styled) < 0.5 * med(base)
    assert styled["reference"]["applied"] and styled["items"][-1]["kind"] == "card"
    # the words come only from the user's own source
    used = {w["word"] for it in styled["items"] if it["kind"] == "piece" for w in it["words"]}
    own = {w["word"] for seg in talk["transcript"]["segments"] for w in seg["words"]}
    assert used and used <= own and "Why" not in used
