"""GRW-1: shorts extractor, retention rubric + pass, hook variants, style profiles. Hand-built analyses, no models, no rendering."""
import re

from nexus import growth, shorts
from nexus import project as prj
from nexus.timeline import ops
from nexus.timeline.render import clip_duration, total_duration

FILLER = "So um yeah okay this is kind of the thing about it."
BODY = ("Solar panels convert sunlight into electricity with silicon cells. Heat reduces their output by about twenty percent. "
        "Cleaning dust off the glass restores a surprising share of the lost power. Installers tilt the frames to match the latitude. "
        "Batteries store the midday surplus for the evening peak. That is why a small battery changes the economics of a home system.")
HOOK = "Why do your solar panels lose 20 percent of their power every summer?"


def _sentences(t0: float, text: str, word_s: float = 0.32, gap: float = 0.06, pause: float = 0.5):
    segs, t = [], t0
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = []
        for w in sent.split():
            words.append({"start": round(t, 3), "end": round(t + word_s, 3), "word": w})
            t += word_s + gap
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": sent, "words": words})
        t += pause
    return segs, t


def _analysis(blocks, dur=None, size=(1920, 1080)):
    segs, t = [], 0.0
    for start, text in blocks:
        s, t = _sentences(start, text)
        segs += s
    dur = dur or t + 1.0
    return {"probe": {"duration": dur, "video": {"width": size[0], "height": size[1]}, "audio": {"channels": 2}},
            "scenes": [(0.0, dur)], "silences": [], "transcript": {"language": "en", "segments": segs}, "repetitions": [],
            "loudness": {"lufs": -16.0}}


def _all_words(an):
    return [w for s in an["transcript"]["segments"] for w in s["words"]]


# ---------------------------------------------------------------- shorts
def test_moments_whole_sentences_and_bounds():
    an = _analysis([(0.5, BODY + " " + BODY + " " + BODY)])
    ms = shorts.find_moments(an, min_s=12, max_s=30, top=4)
    assert ms
    words = _all_words(an)
    for m in ms:
        assert m["duration"] <= 30 + 1e-6 and m["tight_duration"] >= 12 - 1e-6
        assert not any(w["start"] < m["start"] < w["end"] or w["start"] < m["end"] < w["end"] for w in words)
        inside = [w for w in words if m["start"] <= w["start"] and w["end"] <= m["end"]]
        assert inside[0]["word"][0].isupper() and inside[-1]["word"].endswith((".", "!", "?"))
        assert set(m["components"]) == set(shorts.WEIGHTS) and 0 <= m["score"] <= 100
        assert 0 < len(m["title"].split()) <= 9
    for a in ms:  # ranked and non-overlapping
        assert all(not (a["start"] < b["end"] and b["start"] < a["end"]) for b in ms if b is not a)
    assert [m["score"] for m in ms] == sorted((m["score"] for m in ms), reverse=True)


def test_ranking_prefers_strong_hook_over_filler():
    an = _analysis([(0.5, FILLER + " " + BODY), (40.0, HOOK + " " + BODY)])
    ms = shorts.find_moments(an, min_s=15, max_s=40, top=2)
    assert ms[0]["text"].startswith("Why do your solar panels")
    assert not any(m["text"].startswith("So um") for m in ms)  # the filler-opened window loses to its own clean continuation
    only_filler = shorts.find_moments(_analysis([(0.5, FILLER + " " + BODY)]), min_s=15, max_s=40, top=1)
    assert ms[0]["score"] > only_filler[0]["score"]
    h_f, r_f = shorts.hook_score(FILLER)
    assert any("filler" in r for r in r_f) and h_f < shorts.hook_score(HOOK)[0]


def test_build_short_exact_cuts_and_word_timing(home):
    an = _analysis([(2.0, HOOK + " " + BODY)])
    # widen two pauses so that jump cuts happen
    m = shorts.find_moments(an, min_s=10, max_s=40, top=1)[0]
    tl = shorts.build_short("/src/talk.mp4", an, m)
    sents = shorts.sentences(an)[m["sentences"][0]:m["sentences"][1] + 1]
    plan = shorts.clip_plan(sents, m["start"], m["end"])
    assert (tl["width"], tl["height"]) == (1080, 1920)
    extra = total_duration(tl) - sum(b - a for a, b in plan)      # the out added by shorts.add_out (pause, never a word)
    assert -1e-6 <= extra <= shorts.OUT_EXTEND_S + 1e-6 and tl["clips"][-1]["audio_fade_out"] > 0
    assert -1e-3 <= total_duration(tl) - m["tight_duration"] <= shorts.OUT_EXTEND_S + 1e-3   # + the out (shorts.add_out)
    assert tl["clips"][0]["in"] == m["start"] and m["end"] <= tl["clips"][-1]["out"] <= m["end"] + shorts.OUT_EXTEND_S + 1e-6
    assert len(tl["clips"]) == len(sents)  # 0.5 s pauses > 0.35 s: one jump cut per sentence boundary
    assert all(c["fit"] == "blur" for c in tl["clips"])
    kinds = [c["effects"][0]["type"] for c in tl["clips"]]
    assert kinds[:4] == ["zoom", "punch_in", "zoom", "punch_in"]
    total = total_duration(tl)
    ws = [w for q in tl["subtitles"] for w in q["words"]]
    assert len(ws) == sum(len(s["words"]) for s in sents)
    assert all(0 <= w["start"] <= w["end"] <= total + 1e-6 for w in ws)
    assert all(0 <= q["start"] < q["end"] <= total + 1e-6 for q in tl["subtitles"])
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(tl["subtitles"], tl["subtitles"][1:]))
    assert tl["captions"]["style"] == "pop"
    hook = tl["clips"][0]["text"][0]
    assert hook["start"] == 0 and hook["end"] <= 2.5 and len(hook["text"].split()) <= 6
    assert tl["overlays"][0]["type"] == "progress_bar"
    sfx = shorts.build_short("/src/talk.mp4", an, m, sfx=True)  # nexus.fx may not exist yet: must not fail
    assert all(a.get("role") == "sfx" for a in sfx["audio"])


def test_fallback_without_transcript():
    an = {"probe": {"duration": 90.0, "video": {"width": 1920, "height": 1080}, "audio": None},
          "scenes": [(0.0, 8.0), (8.0, 20.0), (20.0, 26.0), (26.0, 50.0), (50.0, 90.0)], "silences": [], "transcript": None}
    ms = shorts.find_moments(an, min_s=18, max_s=58, top=3)
    assert ms and all(m["basis"] == "scenes" and "no transcript" in m["note"] for m in ms)
    assert all(18 <= m["duration"] <= 58 for m in ms)
    tl = shorts.build_short("/src/x.mp4", an, ms[0])
    assert not tl["subtitles"] and tl["growth_notes"] and -1e-6 <= total_duration(tl) - ms[0]["duration"] <= shorts.OUT_EXTEND_S + 1e-6


def test_make_shorts_branches(home):
    an = _analysis([(0.5, HOOK + " " + BODY), (40.0, BODY), (75.0, BODY)])
    p = prj.Project.create("shorts test")
    rows = shorts.make_shorts(p, {"path": "/src/talk.mp4"}, an, n=2, min_s=12, max_s=30)
    assert [r["branch"] for r in rows] == ["short-1", "short-2"]
    for r in rows:
        tl = p.timeline(r["branch"])
        assert abs(total_duration(tl) - r["duration"]) < 1e-6 and r["title"]


# ---------------------------------------------------------------- retention
def _slow_static_tl():
    """Vertical timeline: 4 s of silence before the first word, then one unbroken 40 s shot, no captions."""
    segs, t = _sentences(4.0, BODY + " " + BODY, pause=0.4)
    an = {"probe": {"duration": 60.0}, "transcript": {"segments": segs}}
    tl = prj.new_timeline(1080, 1920)
    tl["clips"].append(ops.video_clip("/src/a.mp4", 0.0, round(t + 0.2, 3), fit="cover"))
    return tl, {"/src/a.mp4": an}


def _spoken(tl, ans):
    return [w["word"] for w in shorts.programme_words(tl, ans)]


def test_retention_report_catches_problems_and_pass_fixes_them():
    tl, ans = _slow_static_tl()
    rep = growth.retention_report(tl, ans, "shorts")
    assert rep["stage"] == "retention" and 0 <= rep["score"] <= 100
    bad = {c["check"]: c for c in rep["checks"] if not c["ok"]}
    assert "hook within 1.5s" in bad and bad["hook within 1.5s"]["fix"] == "trim_lead"
    assert "longest static stretch" in bad and "caption coverage of speech" in bad
    assert all(c["fix"] for c in bad.values() if c["check"] not in ("ending",))
    assert {"trim_lead", "add_punch_ins", "captions_from_words", "add_progress_bar"} <= set(rep["auto_fixes"])
    fixed = growth.apply_retention_pass(tl, rep)
    assert tl["clips"][0]["in"] == 0.0  # input untouched
    rep2 = growth.retention_report(fixed, ans, "shorts")
    ok2 = {c["check"]: c["ok"] for c in rep2["checks"]}
    assert ok2["hook within 1.5s"] and ok2["longest static stretch"] and ok2["caption coverage of speech"] and ok2["progress indicator"]
    assert rep2["score"] > rep["score"]
    assert _spoken(fixed, ans) == _spoken(tl, ans)  # nothing spoken was removed
    assert fixed["retention_pass"]["applied"]
    ws = [w for q in fixed["subtitles"] for w in q["words"]]
    assert all(0 <= w["start"] <= w["end"] <= total_duration(fixed) + 1e-6 for w in ws)


def test_retention_pass_tightens_inner_pause_without_losing_words():
    s1, t = _sentences(0.2, BODY[:120] + ".")
    s2, t2 = _sentences(t + 3.0, BODY)  # a 3 s pause in the middle
    an = {"probe": {"duration": 80.0}, "transcript": {"segments": s1 + s2}}
    ans = {"/src/b.mp4": an}
    tl = prj.new_timeline(1080, 1920)
    tl["clips"].append(ops.video_clip("/src/b.mp4", 0.0, round(t2, 3)))
    rep = growth.retention_report(tl, ans)
    assert "tighten_pauses" in rep["fixes"]
    fixed = growth.apply_retention_pass(tl, rep)
    assert total_duration(fixed) < total_duration(tl) - 2.5
    assert _spoken(fixed, ans) == _spoken(tl, ans)
    assert next(c for c in growth.retention_report(fixed, ans)["checks"] if c["check"] == "dead air")["ok"]


def test_landscape_long_form_norms_and_table():
    norms = growth.platform_norms()
    assert set(norms) == {"shorts", "tiktok", "reels", "youtube", "linkedin"}
    norms["shorts"]["static_max"] = 999  # a copy
    assert growth.PLATFORM_NORMS["shorts"]["static_max"] == 6.0
    tl = prj.new_timeline(1920, 1080)
    tl["clips"].append(ops.color_clip(30.0, fade_in=1.0))
    rep = growth.retention_report(tl, None, "shorts")
    assert not next(c for c in rep["checks"] if c["check"] == "vertical 9:16")["ok"]
    assert not next(c for c in rep["checks"] if c["check"].startswith("picture in the first"))["ok"]


# ---------------------------------------------------------------- hooks
def test_hook_variants_supported_and_deduplicated():
    text = (HOOK + " " + BODY + " Many owners think panels do not work in winter, which is actually wrong. "
            "A battery can save money on the evening bill.")
    vs = growth.hook_variants(text, n=6)
    assert 3 <= len(vs) <= 6
    norm = [frozenset(re.findall(r"\w+", v["title"].lower())) for v in vs]
    assert len(set(norm)) == len(norm)
    assert len({v["pattern"] for v in vs}) >= 3
    for v in vs:
        assert v["pattern"] in growth.PATTERNS and v["rule"] and 0 <= v["score"] <= 100
        if v["supported_by"]:
            assert v["supported_by"] in text
        assert all(n in text for n in re.findall(r"\d+", v["title"]))
    assert any(v["pattern"] == "number" and v["supported_by"] and "20" in v["supported_by"] for v in vs)
    topic_only = growth.hook_variants("home batteries", n=6)
    assert any(v["verify"] for v in topic_only) and all("home batteries" in v["title"].lower() for v in topic_only)


# ---------------------------------------------------------------- style
def _known_tl():
    tl = prj.new_timeline(1080, 1920)
    for k, d in enumerate((2.0, 4.0, 6.0)):
        tl["clips"].append(ops.video_clip(f"/src/{k}.mp4", 0.0, d))
    words = [{"start": round(1.0 + 0.3 * i, 3), "end": round(1.0 + 0.3 * i + 0.25, 3), "word": f"w{i}"} for i in range(30)]
    tl["subtitles"] = [{"start": ws[0]["start"], "end": ws[-1]["end"], "text": " ".join(w["word"] for w in ws), "words": ws}
                       for ws in (words[i:i + 3] for i in range(0, 30, 3))]
    tl["captions"] = {"style": "karaoke"}
    return tl


def test_style_profile_numbers_on_known_timeline():
    p = growth.style_profile(None, _known_tl())
    assert p["duration"] == 12.0 and p["shots"] == 3
    assert p["cuts_per_min"] == 10.0 and p["mean_shot_s"] == 4.0 and p["median_shot_s"] == 4.0
    assert p["words"] == 30 and p["wpm"] == 150.0
    assert p["time_to_first_speech"] == 1.0 and p["caption_style"] == "karaoke" and p["caption_words_per_cue"] == 3
    assert p["first_5s"]["cuts"] == 1 and p["first_5s"]["speech_start"] == 1.0
    assert not any(isinstance(v, str) and "w1" in v for v in p.values())  # structure only, no content


def test_style_profile_from_analysis_and_apply_profile():
    an = _analysis([(0.5, HOOK + " " + BODY)])
    an["scenes"] = [(0.0, 10.0), (10.0, 20.0), (20.0, an["probe"]["duration"])]
    prof = growth.style_profile(an)
    assert prof["source"] == "analysis" and prof["shots"] == 3 and prof["time_to_first_speech"] == 0.5
    assert 0 < prof["speech_share"] < 1 and prof["loudness_lufs"] == -16.0
    tl = prj.new_timeline(1080, 1920)
    tl["clips"].append(ops.video_clip("/src/a.mp4", 0.0, 24.0))
    tl["subtitles"] = [{"start": 0.0, "end": 1.0, "text": "a b c"}]
    target = {"mean_shot_s": 2.0, "effects_per_min": 20, "caption_style": "pop", "caption_words_per_cue": 2.4, "source": "analysis"}
    out = growth.apply_profile(tl, target)
    c = out["clips"][0]
    assert (c["in"], c["out"]) == (0.0, 24.0) and clip_duration(c) == 24.0  # nothing cut
    assert len(c["effects"]) >= 6 and out["captions"]["style"] == "pop" and out["captions"]["words_per_cue"] == 2
    assert growth.style_profile(None, out)["effects_per_min"] > growth.style_profile(None, tl)["effects_per_min"]


SOLAR = [(0.0, "Solar panels turn sunlight directly into electricity."),
         (4.0, "In the last 10 years the price of solar power fell by almost 90%."),
         (10.56, "That makes it the cheapest source of new electricity in most countries."),
         (15.54, "That makes it the cheapest source of new electricity in most countries."),
         (19.72, "The main challenge is storage, because the sun does not shine at night."),
         (24.28, "Batteries are solving that problem faster than anyone expected.")]


def test_repeated_take_is_cut_out_of_shorts():
    """Same structure as the solar_talk fixture: segment 2 is a retake of segment 3; only the later take may be used."""
    an = _analysis(SOLAR, dur=28.4)
    an["repetitions"] = [{"earlier": 2, "later": 3, "text": SOLAR[2][1]}]
    for m in shorts.find_moments(an, min_s=8, max_s=28, top=3):
        assert m["text"].count("cheapest source") <= 1
        tl = shorts.build_short("/src/solar.mp4", an, m)
        spoken = " ".join(w["word"] for w in shorts.programme_words(tl, {"/src/solar.mp4": an}))
        assert spoken.count("cheapest") <= 1
        retake = an["transcript"]["segments"][2]
        assert all(c["out"] <= retake["start"] or c["in"] >= retake["end"] for c in tl["clips"])
    text = " ".join(t for _, t in SOLAR)
    vs = growth.hook_variants(text, n=6)
    assert all("solar panels" in v["title"].lower() for v in vs if v["pattern"] != "question" or v["source"] == "template")
    num = next(v for v in vs if v["pattern"] == "number")
    assert "90%" in num["title"] and "90%" in num["supported_by"]


def test_punch_ins_fix_mean_shot_length_without_stacking():
    tl = prj.new_timeline(1080, 1920)
    tl["clips"].append(ops.video_clip("/src/a.mp4", 0.0, 4.2, effects=[{"type": "punch_in", "start": 0.0, "end": 4.2, "scale": 1.12}]))
    tl["clips"].append(ops.video_clip("/src/a.mp4", 9.0, 13.1))
    rep = growth.retention_report(tl, None)
    assert not next(c for c in rep["checks"] if c["check"] == "average shot length")["ok"]
    out = growth.apply_retention_pass(tl, rep)
    assert next(c for c in growth.retention_report(out, None)["checks"] if c["check"] == "average shot length")["ok"]
    p0 = [e for e in out["clips"][0]["effects"] if e["type"] == "punch_in"]
    assert len(p0) == 1 and p0[0]["end"] < 4.2  # the running punch-in was released, not stacked
