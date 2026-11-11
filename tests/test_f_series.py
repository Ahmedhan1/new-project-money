"""Test F - short film plan, multi-scene story, recurring characters, series structure, continuity-aware next episode."""
import json

from nexus import series
from nexus.media import ff


def test_f_film_and_series(results):
    film = series.film_plan("A lighthouse keeper discovers the light has been guiding something other than ships", scenes=5)
    assert len(film["scenes"]) >= 4 and film["characters"] and len(film["shot_list"]) >= 4
    b = series.create_series("A young engineer and a retired sea captain restore a solar-powered research boat and uncover why the "
                             "coastal town's fish are disappearing", episodes=3)
    assert len(b["characters"]) >= 2 and len(b["season"]) >= 2 and b["world_rules"]
    b, ep1, rev1 = series.next_episode(b["id"])
    out1 = series.produce_episode(b, ep1, rev1)
    b, ep2, rev2 = series.next_episode(b["id"])
    out2 = series.produce_episode(b, ep2, rev2)
    names = [c["name"].split()[0] for c in b["characters"]]
    t1 = " ".join(s["narration"] for s in ep1["scenes"])
    t2 = " ".join(s["narration"] for s in ep2["scenes"])
    (results / "F_series.json").write_text(json.dumps({
        "film_plan": film, "bible": {k: b[k] for k in ("title", "genre", "logline", "world_rules", "characters", "locations", "style", "season", "canon")},
        "episode_1": ep1, "episode_2": ep2,
        "continuity_ep2": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in rev2["checks"]], "continuity_attempts": rev2["attempts"],
        "renders": {"ep1": {"status": out1["report"]["status"], "duration": out1["render"]["duration"], "issues": out1["report"]["known_issues"]},
                    "ep2": {"status": out2["report"]["status"], "duration": out2["render"]["duration"], "issues": out2["report"]["known_issues"]}}}, indent=1))
    # recurring characters across episodes
    recurring = [n for n in names if n in t1 and n in t2]
    assert len(recurring) >= 2, (names, t1[:200], t2[:200])
    # episode 2 is continuity-aware: it was checked against episode 1's ending and the canon, and has a recap
    assert any(c["check"] == "picks up previous cliffhanger" for c in rev2["checks"])
    assert len(b["canon"]) >= 2 and b["episodes"][1]["number"] == 2
    # same production identity (look/sound) for both episodes
    assert out1["style"] == out2["style"]
    for o in (out1, out2):
        tech = next(s for s in o["report"]["stages"] if s["stage"] == "5-technical")
        assert tech["passed"], [c for c in tech["checks"] if not c["ok"]]
    cont = series.continue_project(out2["project"])
    assert cont["style"]["music_seed"] == b["style"]["music_seed"] and cont["last_narration"]
    ff.run(["-ss", "6", "-i", out2["output"], "-frames:v", "1", results / "F_ep2_frame.png"])
