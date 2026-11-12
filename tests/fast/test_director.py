"""DIR-1: word-level tightening, condensation to a target, director plan and deliverables (hand-built analyses, no models)."""
import pytest

from nexus import director, edit
from nexus.timeline.render import clip_duration, total_duration

WPS = 0.3   # seconds per word in the synthetic transcripts


def make_analysis(sentences, pauses=None, scores=None, lang="en", dur_pad=0.5, width=1280, height=720, word_gap=0.05):
    """sentences: list of strings. pauses: {(sent, word_index): seconds} extra pause before that word; default 0.5 s between sentences."""
    pauses = pauses or {}
    t, segs = 0.3, []
    for si, text in enumerate(sentences):
        words = []
        for wi, w in enumerate(text.split()):
            t += pauses.get((si, wi), 0.0)
            words.append({"start": round(t, 3), "end": round(t + WPS, 3), "word": w})
            t += WPS + word_gap
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words,
                     "score": (scores or {}).get(si, 1.0)})
        t += 0.5
    dur = round(t + dur_pad, 3)
    return {"probe": {"duration": dur, "video": {"width": width, "height": height, "fps": 30}, "audio": {"channels": 2}},
            "scenes": [(0.0, dur)], "silences": [], "black": [], "freeze": [], "quality": {"face_focus_x": None},
            "repetitions": [], "issues": [], "loudness": {"lufs": -20.0, "true_peak": -3.0},
            "transcript": {"language": lang, "segments": segs, "text": " ".join(sentences)}}


def all_words(an):
    return [w for s in an["transcript"]["segments"] for w in s["words"]]


def kept_words(an, prop):
    """Words fully inside a keep range; also asserts no keep boundary falls inside any word."""
    out = []
    for w in all_words(an):
        for k in prop["keep"]:
            assert not (w["start"] + 1e-6 < k["in"] < w["end"] - 1e-6), f"cut inside {w}"
            assert not (w["start"] + 1e-6 < k["out"] < w["end"] - 1e-6), f"cut inside {w}"
        if any(k["in"] - 1e-6 <= w["start"] and w["end"] <= k["out"] + 1e-6 for k in prop["keep"]):
            out.append(w["word"])
    return out


def test_tighten_words_removes_fillers_pauses_and_false_starts():
    an = make_analysis(["Solar panels um convert light into power.",
                        "You know, the the price fell ninety percent.",
                        "Storage is uh the next big step."],
                       pauses={(2, 4): 1.2})
    p = edit.tighten_words(an)
    words = kept_words(an, p)
    for f in ("um", "uh", "You", "know,"):
        assert f not in words
    assert words.count("the") == 2  # "the the" -> one, plus "the next"
    assert words == ["Solar", "panels", "convert", "light", "into", "power.", "the", "price", "fell", "ninety", "percent.",
                     "Storage", "is", "the", "next", "big", "step."]
    reasons = " ".join(r["reason"] for r in p["removed"])
    assert "filler 'um'" in reasons and "filler 'you know'" in reasons and "false start" in reasons and "pause 1.25" in reasons
    ins = [k["in"] for k in p["keep"]]
    assert ins == sorted(ins)
    assert all(b["in"] >= a["out"] - 1e-6 for a, b in zip(p["keep"], p["keep"][1:]))
    assert all(k["out"] - k["in"] >= 0.4 - 1e-6 for k in p["keep"])
    assert p["proposed_duration"] < an["probe"]["duration"]
    assert set(p) >= {"keep", "removed", "source_duration", "proposed_duration", "grade", "audio_fx", "status"}


def test_tighten_words_keeps_meaningful_you_know_and_other_languages():
    an = make_analysis(["Do you know the answer to this question?"])
    assert kept_words(an, edit.tighten_words(an)) == an["transcript"]["text"].split()
    de = make_analysis(["Das ist ähm eine gute Idee."], lang="de")
    assert "ähm" not in kept_words(de, edit.tighten_words(de))
    ar = make_analysis(["الطاقة يعني، مهمة جدا اه للمستقبل."], lang="ar")
    w = kept_words(ar, edit.tighten_words(ar))
    assert "يعني،" not in w and "اه" not in w and "مهمة" in w


def test_tighten_words_min_clip_length():
    # an isolated one-word sentence between long pauses would be a 0.3 s clip: it is widened to >= 0.4 s
    an = make_analysis(["Wind turbines spin all day long.", "Yes.", "They feed the grid at night too."], pauses={(1, 0): 1.0, (2, 0): 1.0})
    p = edit.tighten_words(an)
    assert all(k["out"] - k["in"] >= 0.4 - 1e-6 for k in p["keep"])
    assert "Yes." in kept_words(an, p)


def test_build_timeline_accepts_word_proposal():
    an = make_analysis(["Solar panels um convert light.", "Storage matters a lot today."])
    tl = edit.build_timeline("/tmp/x.mp4", an, edit.tighten_words(an))
    assert len([c for c in tl["clips"] if c["type"] == "video"]) >= 2
    assert tl["subtitles"] and all(q["end"] > q["start"] for q in tl["subtitles"])


SENTS = ["Solar power is now the cheapest electricity in history.",          # 0
         "Panels turn sunlight directly into electric current.",              # 1
         "Solar power is now the cheapest electricity ever built.",           # 2 near-duplicate of 0, lower score
         "Batteries store the midday surplus for the evening peak.",          # 3
         "Grid operators balance supply and demand every second.",            # 4
         "Wind farms complement solar during winter months.",                 # 5
         "Prices for batteries dropped by ninety percent in a decade.",       # 6
         "That is why the energy transition is accelerating now."]            # 7


def test_compress_to_hits_target_drops_redundant_first_and_keeps_order():
    an = make_analysis(SENTS, scores={0: 3.0, 1: 1.5, 2: 2.5, 3: 2.0, 4: 1.0, 5: 1.2, 6: 2.2, 7: 1.8})
    full = sum(s["dur"] for s in edit.sentences(an))
    target = full * 0.75
    p = edit.compress_to(an, target)
    assert abs(p["proposed_duration"] - target) <= 0.1 * target
    st = {s["i"]: s for s in p["sentences"]}
    assert st[0]["kept"] and not st[2]["kept"] and "redundant" in st[2]["reason"]
    ins = [k["in"] for k in p["keep"]]
    assert ins == sorted(ins)
    # never inside a sentence: each sentence is fully kept or fully dropped
    for s in edit.sentences(an):
        inside = [w for w in s["words"] if any(k["in"] - 1e-6 <= w["start"] and w["end"] <= k["out"] + 1e-6 for k in p["keep"])]
        assert len(inside) in (0, len(s["words"])), s["text"]
    tl = edit.build_timeline("/tmp/x.mp4", an, p)
    assert abs(total_duration(tl) - p["proposed_duration"]) < 0.01


def test_compress_to_target_above_material_keeps_everything():
    an = make_analysis(SENTS[:3])
    p = edit.compress_to(an, 999)
    assert all(s["kept"] or "redundant" in s["reason"] for s in p["sentences"])


def long_analysis():
    topic_a = ["Solar panels convert sunlight into electricity on rooftops.", "Rooftop solar panels need sunlight and little maintenance.",
               "Cheap solar panels changed rooftop economics.", "Solar panels on rooftops pay back within years.",
               "Sunlight hitting panels creates electricity directly."]
    topic_b = ["Batteries store energy for the evening.", "Battery storage smooths evening demand peaks.",
               "Lithium batteries store energy cheaply now.", "Storage batteries release energy when demand rises.",
               "Battery storage costs keep falling quickly."]
    topic_c = ["Wind turbines spin offshore in strong winds.", "Offshore wind turbines are taller than towers.",
               "Strong offshore winds give turbines steady output.", "Wind turbines offshore need ships for repairs.",
               "Offshore wind farms power whole cities."]
    sents = topic_a + topic_b + topic_c
    return make_analysis(sents * 1, scores={i: 1.0 + (i % 4) * 0.3 for i in range(len(sents))}, word_gap=0.4)


def test_plan_chapters_cover_source_without_gaps_or_overlap():
    an = long_analysis()
    pl = director.plan({"/m/a.mp4": an}, "Clean energy in three ideas", platforms=("youtube", "tiktok"), chapter_s=25)
    ch = pl["chapters"]
    assert len(ch) == 3
    assert [c["sentences"][0] for c in ch] == [0, 5, 10]   # the three topic shifts
    assert ch[0]["start"] == 0.0 and abs(ch[-1]["end"] - an["probe"]["duration"]) < 1e-6
    for a, b in zip(ch, ch[1:]):
        assert abs(a["end"] - b["start"]) < 1e-6
    ids = [i for c in ch for i in c["sentences"]]
    assert ids == list(range(len(pl["sentences"])))
    assert all(0 <= c["importance"] <= 1 and c["name"] for c in ch)
    assert {d["platform"] for d in pl["deliverables"]} == {"youtube", "tiktok"}
    assert pl["master"]["duration_s"] > 0 and pl["summary"]


def test_plan_multi_source_orders_by_source():
    a, b = long_analysis(), make_analysis(SENTS)
    pl = director.plan({"/m/a.mp4": a, "/m/b.mp4": b}, "x", platforms=("youtube",))
    srcs = [c["src"] for c in pl["chapters"]]
    assert srcs == sorted(srcs, key=["/m/a.mp4", "/m/b.mp4"].index)
    for s in ("/m/a.mp4", "/m/b.mp4"):
        cs = [c for c in pl["chapters"] if c["src"] == s]
        assert cs[0]["start"] == 0.0
        assert all(abs(x["end"] - y["start"]) < 1e-6 for x, y in zip(cs, cs[1:]))


@pytest.fixture()
def no_media(monkeypatch):
    """Keep the test independent of music synthesis and SFX."""
    monkeypatch.setattr(director, "_sfx", lambda name, seed=0: None)


def test_build_deliverables_one_per_platform_with_platform_size(home, no_media):
    an = long_analysis()
    lib = {"/m/a.mp4": an}
    pl = director.plan(lib, "Clean energy", target_s=40, platforms=("youtube", "tiktok", "instagram-reels", "snapchat-spotlight", "x"))
    master = director.build_master(None, None, lib, pl, music_bed=False)
    assert master["subtitles"] and any(c.get("role") == "title_card" for c in master["clips"])
    assert [m for m in master["markers"] if m["label"].startswith("chapter:")][0]["t"] == 0.0
    dels = director.build_deliverables(None, None, lib, pl, master)
    plats = {tl["platform"]: tl for tl in dels.values()}
    assert set(plats) == {"youtube", "tiktok", "instagram-reels", "snapchat-spotlight", "x"}
    assert (plats["youtube"]["width"], plats["youtube"]["height"]) == (1920, 1080)
    for p in ("tiktok", "instagram-reels", "snapchat-spotlight"):
        tl = plats[p]
        assert (tl["width"], tl["height"]) == (1080, 1920)
        assert not any(c["type"] == "color" for c in tl["clips"])
        assert total_duration(tl) <= 60
    assert total_duration(plats["x"]) <= 140
    for tl in dels.values():
        assert all(clip_duration(c) > 0 for c in tl["clips"])
