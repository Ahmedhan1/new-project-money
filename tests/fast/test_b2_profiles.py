"""DIR-3: 14 creative profiles (editorial abstractions) consumed by the director; manual overrides by decision id;
explainable EditorialPlan (reason + confidence per decision). Hand-built analyses, no models."""
from test_director import BREAD, SOLAR, _analysis

from nexus import contracts, director
from nexus.timeline.render import clip_duration

FOURTEEN = {"cinematic_documentary", "premium_brand_commercial", "high_energy_challenge", "educational_explainer",
            "scientific_documentary", "interview_storytelling", "podcast_edit", "luxury_advertising", "travel_vlog",
            "investigative", "emotional_storytelling", "fast_social", "news_analysis", "product_demo"}
FIELDS = ("hook_window_s", "shot_len", "shot_len_median", "cutaways_per_min", "captions", "music", "pattern_interrupt_s",
          "chapters", "title_style", "grade", "transitions", "ending", "end_structure")


def test_fourteen_profiles_with_every_editorial_parameter_and_aliases():
    base = director.profiles(include_aliases=False)
    assert set(base) == FOURTEEN
    for name, p in base.items():
        missing = [f for f in FIELDS if p.get(f) is None]
        assert not missing, (name, missing)
        assert len(p["music"]["energy"]) == 3 and p["shot_len"][0] <= p["shot_len_median"] <= p["shot_len"][1]
    allp = director.profiles()
    for old in ("documentary", "interview_natural", "short_fast", "commercial", "luxury_brand", "news"):
        assert allp[old]["alias_of"] in FOURTEEN
    # abstractions only: no profile names a real creator, channel or brand in its label
    assert all("mrbeast" not in p["label"].lower() for p in base.values())


def _stats(profile, an, **brief):
    p = director.plan({"talk.mp4": an}, {"profile": profile, "target_seconds": 60, **brief})
    tl = director.build(p, (640, 360), music=False)
    vids = [c for c in tl["clips"] if c["type"] == "video"]
    lens = sorted(clip_duration(c) for c in vids)
    return p, tl, {"median": lens[len(lens) // 2], "max": lens[-1], "n": len(vids),
                   "changes": sum(1 for c in vids if c.get("pip") or any(e["type"] == "punch_in" for e in c.get("effects") or []))}


def test_profiles_change_pacing_interrupts_and_look():
    an = _analysis([(0.5, SOLAR + " " + BREAD + " " + SOLAR.replace("Solar", "Rooftop"))])
    stats = {k: _stats(k, an)[2] for k in ("high_energy_challenge", "scientific_documentary", "podcast_edit")}
    assert stats["high_energy_challenge"]["median"] < stats["scientific_documentary"]["median"]
    assert stats["high_energy_challenge"]["n"] > stats["podcast_edit"]["n"]
    # pattern interrupt: no visible shot runs longer than the profile's interval (+ one word of slack)
    pi = director.get_profile("high_energy_challenge")["pattern_interrupt_s"]
    assert stats["high_energy_challenge"]["max"] <= pi + 0.8, stats
    looks = {k: _stats(k, an)[1]["grade"] for k in ("investigative", "travel_vlog", "news_analysis")}
    assert looks == {"investigative": "bleach_bypass", "travel_vlog": "vivid", "news_analysis": "none"}


def test_hook_window_moves_or_shortens_the_title_card():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    p = director.plan({"talk.mp4": an}, {"profile": "luxury_advertising", "target_seconds": 40, "title": "Sun and Bread",
                                         "overrides": {}})
    rep = [d for d in p["decisions"] if d["kind"] == "opening" and "hook_start_s" in d]
    assert rep and rep[0]["hook_start_s"] <= director.get_profile("luxury_advertising")["hook_window_s"]


def test_cta_end_card_only_from_the_brief():
    an = _analysis([(0.5, SOLAR)])
    p = director.plan({"t.mp4": an}, {"profile": "podcast_edit", "end_text": "Full episode on our channel"})
    assert p["items"][-1]["kind"] == "card" and p["items"][-1]["text"] == "Full episode on our channel"
    p2 = director.plan({"t.mp4": an}, {"profile": "podcast_edit"})
    assert p2["items"][-1]["kind"] == "piece"  # nothing invented


def test_manual_overrides_keep_drop_and_opening_by_id():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    p = director.plan({"talk.mp4": an}, {"profile": "educational_explainer", "target_seconds": 20})
    kept = [d for d in p["decisions"] if d["action"] == "keep" and d["kind"] == "sentence"]
    dropped = [d for d in p["decisions"] if d["action"] == "drop" and d.get("sentence_id")]
    assert kept and dropped and all(0 <= d["confidence"] <= 1 for d in p["decisions"])
    k_id, d_id = dropped[0]["sentence_id"], kept[0]["sentence_id"]
    p2 = director.plan({"talk.mp4": an}, {"profile": "educational_explainer", "target_seconds": 20,
                                          "overrides": {"keep": [k_id], "drop": [d_id]}})
    kept2 = {d["sentence_id"] for d in p2["decisions"] if d["action"] == "keep" and d["kind"] == "sentence"}
    assert k_id in kept2 and d_id not in kept2
    man = [d for d in p2["decisions"] if d["kind"] == "manual"]
    assert man and man[0]["confidence"] == 1.0 and "removed by a person" in man[0]["reason"]
    # force a specific opening line, then forbid lifting
    last = kept[-1]["sentence_id"]
    p3 = director.plan({"talk.mp4": an}, {"profile": "educational_explainer", "overrides": {"opening": last}})
    first = next(it for it in p3["items"] if it["kind"] == "piece")
    assert abs(first["words"][0]["start"] - kept[-1]["start"]) < 1.0 or p3["summary"]["cold_open"]
    p4 = director.plan({"talk.mp4": an}, {"profile": "educational_explainer", "overrides": {"no_lift": True}})
    assert not p4["summary"]["cold_open"]


def test_editorial_plan_contract_and_chapter_merge():
    an = _analysis([(0.5, SOLAR + " " + BREAD)])
    p = director.plan({"talk.mp4": an}, {"profile": "news_analysis", "instructions": "keep the bread part"})
    doc = director.editorial_plan(p)
    assert contracts.validate(doc) == [] and doc["schema"] == "nexus.EditorialPlan/1"
    assert all({"id", "action", "reason", "confidence"} <= set(d) for d in doc["decisions"])
    short = director._chapters([{"kind": "piece", "topic": k, "in": 0.0, "out": 5.0} for k in range(4)], {}, min_s=12.0)
    assert len(short) <= 2
