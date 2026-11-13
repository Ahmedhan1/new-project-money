"""GRW-2: shorts factory v2 - selection filters, context preservation, hook variants, per-platform batches, honest scores
and calibration from real results. Hand-built transcripts (+ one lavfi render); no models, no network."""
import re
import subprocess

import pytest

from nexus import growth, shorts
from nexus.timeline import render
from nexus.timeline.render import total_duration

SOLAR = ("Why do your solar panels lose twenty percent of their power every summer? Heat reduces the output of every silicon cell "
         "on the roof. Cleaning dust off the glass restores a surprising share of the lost power. Installers tilt the frames to "
         "match the latitude of the roof. That is why a cool, clean panel produces more electricity.")
BREAD = ("Sourdough bread rises because wild yeast and bacteria ferment the flour slowly. The starter needs feeding with flour "
         "and water every single day. A hot oven and a covered pot give the loaf its crisp crust. Bakers score the dough so "
         "steam escapes in one place. Rye flour makes a denser and more sour loaf than wheat.")
DANGLING = ("That is exactly why it failed in the end and we had to start again. The second prototype used aluminium instead of "
            "steel. It weighed half as much and survived every test we ran in the lab. Engineers later copied the design for "
            "lighter bicycle frames.")


def _segments(t0, text, word_s=0.3, gap=0.05, pause=0.5, speaker=None):
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
    for start, text, *sp in blocks:
        s, t = _segments(max(start, t), text, speaker=sp[0] if sp else None)
        segs += s
    dur = dur or t + 1.0
    return {"probe": {"duration": dur, "video": {"width": 1920, "height": 1080}, "audio": {"channels": 2}},
            "scenes": [(0.0, dur)], "silences": [], "transcript": {"language": "en", "segments": segs}, "repetitions": []}


AN = _analysis([(0.5, SOLAR + " " + SOLAR.replace("solar", "rooftop")), (60.0, BREAD + " " + BREAD.replace("Sourdough", "Rye"))])


def test_select_filters_by_time_range_and_topic():
    early = shorts.select(AN, time_range=(0, 55), durations=(15, 25), top=5)
    assert early and all(m["start"] >= 0 and m["end"] <= 55 + 1e-6 for m in early)
    bread = shorts.select(AN, topic="sourdough starter flour", durations=(15, 25), top=5)
    assert bread and all(m["start"] >= 55 for m in bread)
    assert "bread" in " ".join(m["text"].lower() for m in bread) or "flour" in " ".join(m["text"].lower() for m in bread)


def test_speaker_filter_refuses_without_labels_and_works_with_them():
    sel = shorts.select(AN, speaker="guest", durations=(15,), top=3)
    assert not sel and any("speaker" in n.lower() for n in sel.notes)
    labelled = _analysis([(0.5, SOLAR, "host"), (30.0, BREAD + " " + BREAD, "guest")])
    got = shorts.select(labelled, speaker="guest", durations=(15,), top=3)
    assert got and all(m["start"] >= 29 for m in got)


def test_dangling_opening_is_extended_or_flagged_never_built_silently():
    an = _analysis([(0.5, "We tried a steel frame first and it cracked under load."), (5.0, DANGLING)])
    sel = shorts.select(an, durations=(10, 20), top=5, include_flagged=True)
    assert sel
    for m in sel:  # a clip may contain the dangling line only together with the sentence it refers to
        assert not m["text"].startswith("That is exactly why")
        if "That is exactly why" in m["text"]:
            assert m["text"].startswith("We tried a steel frame")


def test_variants_differ_in_their_first_seconds():
    items = shorts.factory("/dev/null.mp4", AN, n=1, platforms=("youtube_shorts",), variants=3)
    firsts = [repr(it["first_3s"]) for it in items]
    assert len(items) >= 2 and len(set(firsts)) == len(firsts)
    assert len({it["title"] for it in items}) == len(items)


def test_factory_batch_is_n_by_variants_by_platforms_without_overlap():
    plats = ("youtube_shorts", "tiktok", "instagram_reels")
    items = shorts.factory("/dev/null.mp4", AN, n=2, platforms=plats, variants=2, durations=(15, 25))
    ranks = {it["moment"]["rank"] for it in items}
    assert len(ranks) == 2 and {it["platform"] for it in items} == set(plats)
    per = {}
    for it in items:
        per.setdefault(it["moment"]["rank"], it["moment"])
    a, b = list(per.values())
    assert min(a["end"], b["end"]) - max(a["start"], b["start"]) <= shorts.OVERLAP_TOL + 1e-6
    for it in items:
        assert "virality" not in " ".join(it["scores"]) and "content_potential" in it["scores"]
        assert it["why"] and it["context_check"]["status"] in ("ok", "extended")
        assert (it["timeline"]["width"], it["timeline"]["height"]) == (1080, 1920)


def test_one_factory_short_renders_vertical(home, tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=58", "-f", "lavfi",
                    "-i", "sine=f=220:d=58:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    an = _analysis([(0.5, SOLAR + " " + SOLAR.replace("solar", "rooftop"))], dur=58.0)
    it = shorts.factory(str(src), an, n=1, platforms=("youtube_shorts",), variants=1, durations=(12, 18))[0]
    tl = it["timeline"]
    tl["width"], tl["height"], tl["music"] = 360, 640, None
    st = render.render(tl, tmp_path / "short.mp4", tmp_path / "w")
    assert abs(st["duration"] - total_duration(tl)) < 0.3 and 8 <= st["duration"] <= 30


def test_calibrate_refuses_small_samples_and_learns_from_enough():
    few = [{"features": {"hook": 0.9, "pace": 0.5}, "retention": 0.6}] * 5
    assert growth.calibrate(few)["ok"] is False
    recs = []
    for k in range(40):
        hook, pace = (k % 10) / 10, ((k * 7) % 10) / 10
        recs.append({"features": {"hook": hook, "pace": pace}, "retention": 0.2 + 0.6 * hook + 0.05 * pace})
    fit = growth.calibrate(recs)
    assert fit["ok"] and fit["samples"] == 40 and fit["weights"]["hook"] > fit["weights"]["pace"] and fit["r2"] > 0.9


@pytest.mark.parametrize("platform", ["youtube_shorts", "tiktok", "snapchat_spotlight"])
def test_platform_text_respects_limits_and_uses_only_spoken_words(platform):
    t = growth.platform_text(SOLAR * 20, title="Why do your solar panels lose power " * 6, platform=platform)
    lim = {**growth.TEXT_LIMITS["default"], **growth.TEXT_LIMITS[platform]}
    assert len(t["title"]) <= lim["title_max"] and len(t["description"]) <= lim["description_max"]
    spoken = set(re.findall(r"\w+", SOLAR.lower()))
    joined = re.sub(r"\W+", "", SOLAR.lower())
    assert all(h[1:].lower() in spoken or h[1:].lower() in joined for h in t["hashtags"])   # entities join their words
    assert len(t["hashtags"]) <= growth.HASHTAGS_EDITORIAL


def test_hashtags_are_entities_and_repeated_words_never_fillers():
    text = ("Basically we always go to the cave. Absolutely, sometimes the cave in New Mexico has microbes, and the "
            "microbes have gotten tougher. NASA funds it.")
    tags = growth.hashtags(text, 60)
    assert tags[:2] == ["#NewMexico", "#NASA"] and "#Cave" in tags and "#Microbes" in tags
    assert not {"#Basically", "#Absolutely", "#Sometimes", "#Gotten", "#Always"} & set(tags)
    assert growth.platform_text(text, platform="youtube_shorts")["hashtags"] == tags[:growth.HASHTAGS_EDITORIAL]


def test_a_short_ends_with_an_out_not_on_the_last_frame_of_the_last_word():
    """Acceptance batch: shorts 1, 3 and 5 ended with sound running to the last frame (editing.ending)."""
    from nexus import shorts
    words = [{"start": 10.0 + 0.3 * i, "end": 10.25 + 0.3 * i, "word": w} for i, w in enumerate("we flew home today.".split())]
    nxt = [{"start": 12.0, "end": 12.3, "word": "Next"}]
    an = {"probe": {"duration": 60.0}, "transcript": {"segments": [{"start": 10, "end": 11.2, "words": words}, {"start": 12, "end": 12.3, "words": nxt}]}}
    tl = {"clips": [{"type": "video", "src": "s.mp4", "in": 9.9, "out": 11.15}]}
    shorts.add_out(tl, an, "s.mp4")
    c = tl["clips"][0]
    assert abs(c["out"] - 11.55) < 1e-6 and c["audio_fade_out"] == 0.3        # +0.4 s of pause, sound fades
    tl2 = {"clips": [{"type": "video", "src": "s.mp4", "in": 9.9, "out": 11.8}]}
    shorts.add_out(tl2, an, "s.mp4")
    assert tl2["clips"][0]["out"] <= 12.0 - 0.08 + 1e-6                        # never into the next word
