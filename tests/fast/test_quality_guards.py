"""Guards added after acceptance test D exposed weak output: empty LLM answers, thin scripts, no stock images."""
import pytest

from nexus import assets, create, llm


def test_query_ladder_simplifies_descriptive_queries():
    ladder = assets.query_ladder("doctor giving BCG vaccine to child", ("BCG Vaccine Magic", "How vaccines train the immune system"))
    assert ladder[0] == "doctor BCG vaccine" and "vaccine" in ladder and len(ladder) <= 6   # sentence -> noun phrases -> single noun
    assert assets.query_ladder("solar panels")[0] == "solar panels"                         # short queries are tried as written
    assert assets.query_ladder("", ("wind turbine",)) == ["wind turbine", "turbine"]


def test_find_image_walks_the_ladder_and_reports_the_match(home, monkeypatch):
    asked = []

    def commons(q, n=5, min_width=1000):
        asked.append(q)
        return [{"id": "commons:1", "title": "Vaccine vial"}] if q == "vaccine" else []

    commons.__name__ = "search_commons"
    monkeypatch.setattr(assets, "search_commons", commons)
    monkeypatch.setattr(assets, "search_images", lambda q, **k: (_ for _ in ()).throw(assets.net.RemoteUnavailable("504")))
    monkeypatch.setattr(assets, "acquire", lambda c: {"id": c["id"], "path": "x.jpg", "license": "CC0"})
    monkeypatch.setattr(assets, "_usable_image", lambda p: True)
    row = assets.find_image("doctor giving BCG vaccine to child")
    assert row["matched_query"] == "vaccine" and asked == ["doctor BCG vaccine", "vaccine child", "vaccine"]


def test_chat_json_retries_empty_answers(monkeypatch):
    answers = iter(["{}", "not json", '{"scenes": [{"narration": "x"}]}'])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(answers))
    assert llm.chat_json("p", require="scenes") == {"scenes": [{"narration": "x"}]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "{}")
    with pytest.raises(ValueError, match="scenes"):
        llm.chat_json("p", require="scenes")
    assert llm.chat_json("p") == {}                                                         # without `require`, {} is valid JSON


def test_thin_scripts_are_rejected_and_feedback_reaches_the_writer(monkeypatch):
    res = {"text": "vaccines train immune system memory cells antibodies protect future infection " * 20, "grounded": True, "sources": []}
    thin = {"title": "T", "topic": "t", "scenes": [{"narration": "Vaccines train the immune system.", "on_screen_text": "a", "visual_query": "vaccine"}] * 2}
    rich = {"title": "T", "topic": "t", "scenes": [
        {"narration": "Did vaccines train your immune system before infection? They protect memory cells for the future, and antibodies follow.", "on_screen_text": "a", "visual_query": "vaccine"},
        {"narration": "Memory cells protect the immune system from future infection. Antibodies train quickly, so vaccines protect you again.", "on_screen_text": "b", "visual_query": "cells"}]}
    depth = next(c for c in create.review_script(thin, res, 2)["checks"] if c["check"] == "scene depth")
    assert not depth["ok"] and depth["severity"] == "major"
    assert next(c for c in create.review_script(rich, res, 2)["checks"] if c["check"] == "scene depth")["ok"]
    seen = []

    def writer(topic, res_, scenes, feedback="", **kw):
        seen.append(feedback)
        return thin if len(seen) == 1 else rich

    monkeypatch.setattr(create, "write_script", writer)
    script, review = create.script_with_review("t", res, 2)
    assert script is rich and review["attempts"] == 2
    assert seen[0] == "" and "scene depth" in seen[1] and "no repeated scenes" in seen[1]   # reviewer findings were passed back


def test_subtitle_cues_break_at_sentence_and_clause_boundaries():
    from nexus.timeline import subs
    text = ("Did you know vaccination can train your immune system to fight more than just the disease it's designed for? "
            "This is called trained immunity, a new concept in immunology.")
    cues = subs.cues_from_text(text, 0.0, 12.0)
    texts = [c["text"] for c in cues]
    assert " ".join(texts) == text and all(len(t) <= 42 for t in texts)
    assert not any("? " in t or ". " in t for t in texts)                     # no cue straddles two sentences
    assert any(t.endswith("designed for?") for t in texts) and texts[-1].endswith("immunology.")
    assert cues[0]["start"] == 0.0 and abs(cues[-1]["end"] - 12.0) < 0.01 and all(a["end"] <= b["start"] + 1e-6 for a, b in zip(cues, cues[1:]))
    assert [c["text"] for c in subs.cues_from_text("Short line.", 0, 2)] == ["Short line."]


def test_a_title_on_a_near_black_card_is_an_intended_still():
    from nexus import quality
    tl = {"clips": [{"id": "a", "type": "color", "duration": 3.0, "color": "0x0b1020", "text": [{"text": "Into the Cave"}]},
                    {"id": "b", "type": "color", "duration": 2.0, "color": "black"}]}
    spans = quality._intended_spans(tl, ("card",))
    assert spans == [(0.0, 3.0)]                     # the titled card is by design; a bare black clip is not


def _speechy(path, gain_change: bool):
    """Amplitude-modulated noise in 1.6 s 'phrases' with 0.5 s pauses; optionally the second half 14 dB quieter
    in the middle of a phrase (a real level jump)."""
    import subprocess
    expr = "1.5*sin(2*PI*5*t)*sin(2*PI*5*t)*lt(mod(t,2.1),1.6)"
    if gain_change:
        expr = f"({expr})*if(lt(t,5.2),1,0.2)"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=10:d=10",
                    "-f", "lavfi", "-i", f"anoisesrc=d=10:c=pink:a=0.5:r=48000:seed=7",
                    "-filter_complex", f"[1:a]volume='{expr}':eval=frame[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def test_pauses_between_phrases_are_not_level_jumps_but_a_gain_change_is(home, tmp_path):
    from nexus import quality
    def jumps(p):
        r = quality.score(p)
        c = next((x for x in r.get("failed", []) if x["id"] == "audio.level_jumps"), None)
        return c
    assert jumps(_speechy(tmp_path / "a.mp4", False)) is None
    assert jumps(_speechy(tmp_path / "b.mp4", True)) is not None
