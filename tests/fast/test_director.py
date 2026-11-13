"""DIR-1: media understanding + director. Hand-built transcripts and FFmpeg lavfi media; no models, no network."""
import re
import subprocess

import pytest

from nexus import director
from nexus.media import analyze, ff
from nexus.media import understand as u
from nexus.timeline import render
from nexus.timeline.render import clip_duration, total_duration

SOLAR = ("Solar panels convert sunlight into electricity with silicon cells. Heat reduces their output by about twenty percent "
         "in summer. Cleaning dust off the glass restores a surprising share of the lost power. Installers tilt the frames to "
         "match the latitude of the roof. Batteries store the midday surplus for the evening peak. That is why a small battery "
         "changes the economics of a home system.")
BREAD = ("Sourdough bread rises because wild yeast and bacteria ferment the flour. The starter needs feeding with flour and "
         "water every single day. A hot oven and a covered pot give the loaf its crisp crust. Bakers score the dough so steam "
         "can escape in one place. Rye flour makes a denser and more sour loaf than wheat.")
PROMPTS = ("Thank you so much for joining us today. How are you doing today? What can you tell us about your training? "
           "Where did you do the cave training? How do you feel about the next mission?")
ANSWER = "We flew the jets from Houston all the way out to Mississippi and practised spacewalks in the big pool for six hours."


def _segments(t0: float, text: str, word_s: float = 0.3, gap: float = 0.05, pause: float = 0.6, speaker=None):
    segs, t = [], t0
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = []
        for w in sent.split():
            words.append({"start": round(t, 3), "end": round(t + word_s, 3), "word": w})
            t += word_s + gap
        seg = {"start": words[0]["start"], "end": words[-1]["end"], "text": sent, "words": words}
        if speaker:
            seg["speaker"] = speaker
        segs.append(seg)
        t += pause
    return segs, t


def _analysis(blocks, dur=None):
    segs, t = [], 0.0
    for start, text in blocks:
        s, t = _segments(max(start, t), text)
        segs += s
    dur = dur or t + 1.0
    return {"probe": {"duration": dur, "video": {"width": 1280, "height": 720}, "audio": {"channels": 2}},
            "scenes": [(0.0, dur)], "silences": [], "transcript": {"language": "en", "segments": segs}, "repetitions": [],
            "loudness": {"lufs": -16.0}}


def _words(an):
    return [w for s in an["transcript"]["segments"] for w in s["words"]]


@pytest.fixture(scope="module")
def talk_video(tmp_path_factory):
    """A 64 s talking-head stand-in with real audio: three visually distinct scenes so shots/scenes can be measured."""
    d = tmp_path_factory.mktemp("dir")
    out = d / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=22", "-f", "lavfi", "-i", "smptebars=s=320x180:r=25:d=21",
                    "-f", "lavfi", "-i", "rgbtestsrc=s=320x180:r=25:d=21", "-f", "lavfi", "-i", "sine=f=220:d=64:r=48000",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,trim=duration=64,format=yuv420p[v]",
                    "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)],
                   check=True)
    return str(out)


# ---------------------------------------------------------------- understanding
def test_hook_rejects_pleasantries_and_interviewer_prompts():
    an = _analysis([(0.5, PROMPTS + " " + ANSWER + " " + SOLAR)])
    sents = u.sentences(an)
    u.score_sentences(sents)
    by = {s["text"]: s for s in sents}
    assert by["How are you doing today?"]["hook"] <= 0.3
    assert not by["Thank you so much for joining us today."]["self_contained"]
    assert by["What can you tell us about your training?"]["prompt_like"]
    assert not by["What can you tell us about your training?"]["self_contained"]
    assert "first-hand account" in by[ANSWER]["hook_reasons"]
    roles = u.narrative_roles(sents, u.segment_topics(sents))
    assert sents[roles["hook"]]["text"] not in (PROMPTS.split("? ")[0] + "?",)
    assert not sents[roles["hook"]]["prompt_like"]


def test_rhetorical_question_in_a_monologue_stays_a_strong_hook():
    q = "Why do your solar panels lose twenty percent of their power every single summer?"
    an = _analysis([(0.5, q + " " + SOLAR + " " + BREAD)])
    sents = u.sentences(an)
    assert not u.dialogue_like(sents)
    u.score_sentences(sents)
    s0 = next(s for s in sents if s["text"] == q)
    assert not s0["prompt_like"] and s0["hook"] >= 0.7


def test_speaker_labels_switch_off_the_prompt_heuristic():
    # labels WITH roles: only the interviewer's lines are prompts; a guest's question is not
    segs, _ = _segments(0.5, "What can you tell us about your training?", speaker="guest")
    s2, _ = _segments(9.0, "Thanks for asking about the program.", speaker="interviewer")
    sents = u.sentences({"probe": {"duration": 20}, "transcript": {"segments": segs + s2}, "silences": []})
    u.score_sentences(sents)
    assert sents[0]["prompt_like"] is False and sents[0]["speaker"] == "guest" and sents[1]["prompt_like"] is True
    # labels WITHOUT roles (a panel S1/S2/S3): a question put to "you" is the moderator's prompt, a statement is not
    segs, _ = _segments(0.5, "What can you tell us about your training?", speaker="S1")
    s2, _ = _segments(5.0, "You learn to trust the people on your crew every single day.", speaker="S2")
    sents = u.sentences({"probe": {"duration": 20}, "transcript": {"segments": segs + s2}, "silences": []})
    u.score_sentences(sents)
    assert sents[0]["prompt_like"] is True and sents[1]["prompt_like"] is False


def test_topic_segmentation_splits_two_topics_and_search_finds_them():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    und = u.understand(an, measure=False)
    assert len(und["topics"]) >= 2
    first_bread = next(s for s in und["sentences"] if s["text"].startswith("Sourdough"))
    assert any(t["sentences"][0] == first_bread["i"] for t in und["topics"])
    hits = u.search(u.build_index({"src.mp4": und}), "starter flour feeding")
    assert hits and "starter" in hits[0]["text"].lower()


def test_fillers_and_retakes_are_detected():
    an = _analysis([(0.5, "So um the battery stores energy. The battery stores the energy for the evening. "
                          "The battery stores the energy for the evening peak.")])
    sents = u.sentences(an)
    assert any(f["text"].lower().strip(",.") == "um" for f in u.fillers(sents))
    assert u.retakes(sents)


def test_ffmpeg_scene_detector_finds_the_two_cuts(talk_video):
    scenes = analyze.detect_scenes_ffmpeg(talk_video)
    cuts = [a for a, _ in scenes[1:]]
    assert len(cuts) >= 2
    assert any(abs(c - 22) < 0.6 for c in cuts) and any(abs(c - 43) < 0.6 for c in cuts)


# ---------------------------------------------------------------- director
@pytest.mark.parametrize("target", [20.0, 35.0])
def test_plan_meets_target_and_never_cuts_inside_a_word(target):
    an = _analysis([(0.5, SOLAR + " " + BREAD + " " + SOLAR.replace("Solar", "Rooftop solar"))])
    p = director.plan({"talk.mp4": an}, {"profile": "documentary", "target_seconds": target})
    tl = director.build(p, size=(640, 360), music=False)
    assert abs(total_duration(tl) - target) <= max(p["tolerance"], 0.1 * target)
    words = _words(an)
    for c in tl["clips"]:
        if c["type"] != "video":
            continue
        for t in (c["in"], c["out"]):
            assert not any(w["start"] + 0.02 < t < w["end"] - 0.02 for w in words), f"cut at {t} is inside a word"


def test_edit_never_ends_on_a_sign_off():
    an = _analysis([(0.5, SOLAR + " " + BREAD + " No, thank you. Thanks so much for having me.")])
    p = director.plan({"talk.mp4": an}, {"profile": "documentary", "target_seconds": 30})
    last = [it for it in p["items"] if it["kind"] == "piece"][-1]
    assert "thank" not in " ".join(w["word"] for w in last["words"]).lower()
    assert any(d["kind"] == "pleasantry" for d in p["decisions"])


def test_every_dropped_span_has_a_reason_and_score():
    an = _analysis([(0.5, "Um so yeah. " + SOLAR + " " + BREAD)])
    p = director.plan({"talk.mp4": an}, {"profile": "interview_natural", "target_seconds": 25})
    dropped = [d for d in p["decisions"] if d["action"] != "keep"]
    assert dropped and all(d["reason"] and d["kind"] and d.get("score") is not None for d in dropped)
    assert {d["action"] for d in p["decisions"]} >= {"keep", "drop"}


def test_repeated_takes_across_sources_collapse_to_one():
    take = "Grid operators now pay households for exported winter power during the coldest evening hours of the year."
    a = _analysis([(0.5, SOLAR + " " + take)])
    b = _analysis([(0.5, take + " " + BREAD)])
    p = director.plan({"a.mp4": a, "b.mp4": b}, {"profile": "documentary", "target_seconds": 120})
    kept_text = " ".join(" ".join(w["word"] for w in it.get("words") or []) for it in p["items"] if it["kind"] == "piece")
    assert kept_text.count("exported winter power") == 1
    assert any(d["kind"] in ("duplicate_take", "retake", "repetition") for d in p["decisions"])


def test_profiles_produce_measurably_different_edits():
    an = _analysis([(0.5, SOLAR + " " + BREAD + " " + SOLAR)])
    stats = {}
    for prof in ("short_fast", "documentary", "luxury_brand"):
        tl = director.build(director.plan({"talk.mp4": an}, {"profile": prof, "target_seconds": 30}), (640, 360), music=False)
        vids = [c for c in tl["clips"] if c["type"] == "video"]
        stats[prof] = (sum(clip_duration(c) for c in vids) / max(1, len(vids)), tl["grade"],
                       sum(1 for c in tl["clips"] if c.get("transition") == "crossfade"))
    assert len({s[1] for s in stats.values()}) >= 2
    assert stats["short_fast"][0] < stats["luxury_brand"][0]


def test_instructions_are_parsed_deterministically():
    ins = director.parse_instructions("Make it 90 seconds, faster, no music, keep the battery part and remove pauses")
    assert ins["target_seconds"] == 90 and ins["music"] is False and "battery" in ins["keep"]


def test_edl_cmx3600_is_well_formed():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    tl = director.build(director.plan({"talk.mp4": an}, {"profile": "documentary", "target_seconds": 25}), (640, 360), music=False)
    text = director.edl(tl, "cmx3600")
    lines = [ln for ln in text.splitlines() if re.match(r"^\d{3}\s", ln)]
    assert text.startswith("TITLE:") and "FCM: NON-DROP FRAME" in text
    numbers = [ln.split()[0] for ln in lines]
    assert sorted(set(numbers)) == numbers[:1] + sorted(set(numbers))[1:] and len(set(numbers)) == len(tl["clips"])
    tc = re.compile(r"^\d\d:\d\d:\d\d:\d\d$")
    for ln in lines:  # every event line carries four valid timecodes; dissolves are a 0-length C line + a D line
        f = ln.split()
        assert all(tc.match(x) for x in f[-4:]) and f[-4] <= f[-3] and f[-2] <= f[-1]
        assert f[3] in ("C", "D")
    rec_in = [ln.split()[-2] for ln in lines if ln.split()[3] == "D" or ln.split()[-2] != ln.split()[-1]]
    assert rec_in == sorted(rec_in)  # record timecodes advance monotonically


def test_built_timeline_renders_to_planned_duration(talk_video, tmp_path):
    an = _analysis([(0.5, SOLAR + " " + BREAD)], dur=64.0)  # transcript (49 s) inside the 64 s file
    an["path"] = talk_video
    out = director.direct([talk_video], {talk_video: an}, {"profile": "documentary", "target_seconds": 20}, size=(320, 180))
    tl = out["master"]
    tl["music"] = None
    stats = render.render(tl, tmp_path / "master.mp4", tmp_path / "w")
    assert abs(ff.duration(tmp_path / "master.mp4") - total_duration(tl)) < 0.3
    assert abs(stats["duration"] - 20) <= 3
    assert out["edl"]["cmx3600"].startswith("TITLE:")


def test_lower_thirds_show_names_never_machine_roles():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    for s in an["transcript"]["segments"]:
        s["speaker"] = "guest"
    an["speakers"] = {"available": True, "speakers": {"S1": {"role": "guest"}}}
    def lower_thirds(a):
        p = director.plan({"talk.mp4": a}, {"profile": "documentary", "target_seconds": 20})
        tl = director.build(p, size=(640, 360), music=False)
        return p, [x["text"] for c in tl["clips"] for x in c.get("text", []) if x.get("style") == "lower_third"]
    p, lt = lower_thirds(an)
    assert "guest" not in lt and not any(director.MACHINE_SPEAKER.match(t) for t in lt), lt
    assert any("no lower thirds" in n for n in p["notes"]) if "notes" in p else True
    from nexus.media import diarize
    an["speakers"] = diarize.set_name(an["speakers"], "S1", "Dr. Ada Example")
    p, lt = lower_thirds(an)
    assert "Dr. Ada Example" in lt and p["speaker_names"] == {"guest": "Dr. Ada Example"}
