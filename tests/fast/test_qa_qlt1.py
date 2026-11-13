"""QLT-1 (independent QA, Account 3): series visuals from the location LOOK, reuse for repeated locations, off-bible
location repair, two-records-per-line parsing, scored reviewer rubric. No network, no models."""
import wave

from PIL import Image

from nexus import create, project as prj, series
from nexus.intelligence.quality import rubric

BIBLE = {"title": "The Ashveil Chronicles", "characters": [{"name": "Mira Vale"}, {"name": "Tobin"}],
         "locations": [{"name": "Hollow of Ashveil", "look": "misty pine valley with a ruined stone tower"},
                       {"name": "Glasswater Docks", "look": "rainy harbour, wooden piers, lanterns at night"},
                       {"name": "Mira's Forge", "look": "dark smithy, glowing coals and anvil"}]}


def test_visual_query_describes_the_place_not_its_fictional_name():
    q = series.visual_query_for(BIBLE, BIBLE["locations"][0], "Hollow of Ashveil")
    assert q == "misty pine valley ruined"
    assert "ashveil" not in q and "hollow" not in q
    q2 = series.visual_query_for(BIBLE, BIBLE["locations"][2])
    assert "mira" not in q2 and q2.startswith("dark smithy")          # character names never reach the image search
    assert series.visual_query_for(BIBLE, None, "fallback") == "fallback"


def test_location_lookup_is_tolerant_but_never_invents():
    assert series.location_for(BIBLE, "the Hollow of Ashveil at dawn")["name"] == "Hollow of Ashveil"
    assert series.location_for(BIBLE, "glasswater docks")["name"] == "Glasswater Docks"
    assert series.location_for(BIBLE, "Ashveil")["name"] == "Hollow of Ashveil"
    assert series.location_for(BIBLE, "Moon Palace") is None


def test_off_bible_locations_are_repaired_and_recorded():
    ep = {"scenes": [{"location": "Hollow of Ashveil", "narration": "Mira climbs the tower."},
                     {"location": "Old Harbour", "narration": "Lanterns sway over the wooden piers in the rain."},
                     {"location": "Moon Palace", "narration": "Silence."}]}
    rep = series.repair_locations(BIBLE, ep)
    assert [r["scene"] for r in rep] == [2, 3]
    assert ep["scenes"][1]["location"] == "Glasswater Docks" and ep["scenes"][1]["location_repaired_from"] == "Old Harbour"
    assert rep[0]["reason"] == "shared words with the bible location"
    assert ep["scenes"][2]["location"] == "Glasswater Docks" and rep[1]["reason"] == "previous scene's location"
    assert all(series.location_for(BIBLE, s["location"]) for s in ep["scenes"])


def test_next_episode_repairs_and_still_reports_the_writer_mistake(monkeypatch):
    b = {**BIBLE, "id": "s1", "logline": "x", "world_rules": [], "genre": "", "canon": [], "episodes": [],
         "characters": [{"name": "Mira", "role": "", "appearance": "", "trait": "", "goal": ""},
                        {"name": "Tobin", "role": "", "appearance": "", "trait": "", "goal": ""}]}
    ep = {"number": 1, "title": "T", "cliffhanger": "Tobin vanishes.", "events": [], "summary": "",
          "scenes": [{"location": "Hollow of Ashveil", "narration": "Mira and Tobin climb the ruined tower above the misty valley at dawn.", "on_screen_text": ""},
                     {"location": "Old Harbour", "narration": "Mira and Tobin wait under lanterns on the wooden piers while rain falls.", "on_screen_text": ""}]}
    monkeypatch.setattr(series.db, "get_doc", lambda *_: b)
    monkeypatch.setattr(series.db, "put_doc", lambda *a, **k: None)
    monkeypatch.setattr(series, "write_episode", lambda *a, **k: {**ep, "scenes": [dict(s) for s in ep["scenes"]]})
    _, out, review = series.next_episode("s1", rounds=1)
    assert out["scenes"][1]["location"] == "Glasswater Docks"
    by = {c["check"]: c for c in review["checks"]}
    assert by["off-bible locations repaired"]["ok"] and "Old Harbour" in by["off-bible locations repaired"]["detail"]
    assert by["writer used only bible locations"]["ok"] is False                 # the mistake stays visible


def _wav(path, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * seconds))
    return {"duration": seconds}


def test_repeated_location_reuses_its_image_and_searches_once(home, monkeypatch, tmp_path):
    img = tmp_path / "valley.jpg"
    Image.new("RGB", (1280, 720), (90, 120, 90)).save(img)
    calls = []

    def find_image(q, *a, **k):
        calls.append(q)
        return {"id": f"img{len(calls)}", "path": str(img), "license": "CC0", "source": "commons", "attribution": "",
                "url": f"https://example.org/{len(calls)}", "title": q, "matched_query": q}

    monkeypatch.setattr(create.tts, "synthesize", lambda text, wav, *a: _wav(wav))
    monkeypatch.setattr(create.assets, "find_image", find_image)
    monkeypatch.setattr(create.music, "music_bed", lambda *a, **k: str(tmp_path / "m.wav"))
    p = prj.Project.create("qlt1", "series")
    script = {"title": "E1", "topic": "x", "scenes": [
        {"narration": "One.", "on_screen_text": "", "visual_query": "misty pine valley", "reuse_key": "location:Hollow"},
        {"narration": "Two.", "on_screen_text": "", "visual_query": "rainy harbour", "reuse_key": "location:Docks"},
        {"narration": "Three.", "on_screen_text": "", "visual_query": "misty pine valley", "reuse_key": "location:Hollow"}]}
    create.produce(p, script)
    assert calls == ["misty pine valley", "rainy harbour"]                        # third scene: no new search
    plan = sorted(p.data["visual_plan"], key=lambda x: x["scene"])
    assert plan[2]["reused_for"] == "location:Hollow" and plan[2]["source"] == plan[0]["source"]
    assert rubric.repeated_visuals(plan)["score"] == 1.0                          # intended reuse is not a duplicate


def test_rubric_scores_every_dimension_and_never_guesses_missing_evidence():
    script = {"scenes": [
        {"narration": "What if the Nile stopped flooding? For five thousand years it fed Egypt's farms every summer.", "on_screen_text": "The Nile"},
        {"narration": "Farmers along the Nile planted wheat in the black silt the river left behind each year.", "on_screen_text": ""},
        {"narration": "The Aswan dam ended the flood in 1970, and the silt now settles behind the dam instead of the farms.", "on_screen_text": ""},
        {"narration": "Today the farms need fertiliser, and the delta is slowly shrinking into the sea.", "on_screen_text": ""}]}
    plan = [{"scene": 1, "query": "nile", "kind": "licensed_photo", "source": "a"},
            {"scene": 2, "query": "silt farm", "kind": "licensed_photo", "source": "b"},
            {"scene": 3, "query": "aswan dam", "kind": "fact_graphic"},
            {"scene": 4, "query": "delta", "kind": "title_card_fallback"}]
    report = {"stages": [{"checks": [{"check": "loudness", "ok": True}, {"check": "clipping / true peak", "ok": True},
                                     {"check": "dead air", "ok": False, "severity": "minor"}]}]}
    r = rubric.score(script, plan, report)
    d = {k: v["score"] for k, v in r["dimensions"].items()}
    assert d["hook"] == 1.0 and d["scene_relevance"] == 0.7 and d["audio_balance"] == round((1 + 1 + 0.375) / 2.75, 3)
    assert d["repeated_visuals"] == 1.0 and 0.8 < d["story_arc"] <= 1.0
    assert r["verdict"] == "pass" and rubric.gate(r)["ok"]
    only_script = rubric.score(script)                                             # no render evidence yet
    assert only_script["unknown"] == ["audio_balance", "repeated_visuals", "scene_relevance"]
    assert only_script["verdict"] == "insufficient_evidence" and not rubric.gate(only_script)["ok"]


def test_rubric_flags_weak_hook_title_cards_and_repeats():
    script = {"scenes": [{"narration": "This video is going to be about a number of different things that happened over a very long period "
                                       "of time in many places around the world and why they matter", "on_screen_text": ""},
                         {"narration": "Rivers.", "on_screen_text": ""}]}
    plan = [{"query": "x", "kind": "title_card_fallback"}, {"query": "y", "kind": "licensed_photo", "source": "a"},
            {"query": "z", "kind": "licensed_photo", "source": "a"}]
    r = rubric.score(script, plan, {"stages": [{"checks": [{"check": "loudness", "ok": False, "severity": "major"}]}]})
    assert r["verdict"] == "needs_review"
    low = rubric.gate(r)["blockers"][0]
    assert "hook" in low and "repeated_visuals" in low and "audio_balance" in low
    assert rubric.PASS_OVERALL == 0.7 and rubric.FLOOR == 0.4                      # thresholds are fixed constants


def test_two_records_on_one_line_are_both_parsed():
    # Shape of the real Test F bible output (docs/results/F_series.json): the second LOCATION was swallowed.
    raw = ("TITLE: Sunken Currents\nLOCATION: Seabrook Bay | A sprawling estuary with fog-draped piers and rusting fishing nets "
           "LOCATION: Lunar Reef | glowing coral shelf under dark water\nVISUAL: teal and amber")
    f = series._fields(raw)
    assert f["LOCATION"] == ["Seabrook Bay | A sprawling estuary with fog-draped piers and rusting fishing nets",
                             "Lunar Reef | glowing coral shelf under dark water"]
    assert series._fields("SCENE: Dock | The CAPTAIN: shouts orders. | Go")["SCENE"] == ["Dock | The CAPTAIN: shouts orders. | Go"]
