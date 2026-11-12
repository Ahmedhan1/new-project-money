"""K-23 + shorts openings: titles from complete thoughts, LLM titles validated against the clip, context line from the
question asked before an answer, no short starting on a pleasantry, distinct shorts. Hand-built analyses, LLM mocked."""
from test_director import BREAD, SOLAR, _segments

from nexus import shorts


def test_title_prefers_a_whole_sentence_then_a_clause():
    t = shorts.title_info("And so, um, batteries store the midday surplus. It was a long and winding conversation about many "
                          "things that happened over the years, because the grid operators changed every rule we had.")
    assert t["title"] == "Batteries store the midday surplus" and t["complete"] and t["source"] == "sentence"
    t2 = shorts.title_info("Well, when the long winter finally came to the northern valley towns, the grid operators changed "
                           "every rule.")
    assert t2["source"] == "clause" and t2["complete"] and len(t2["title"].split()) <= 9 and not t2["title"].lower().startswith("well")


def test_pleasantries_never_title_and_fallback_is_flagged():
    t = shorts.title_info("Thank you so much for having me. I have you loud and clear. It is a long time.")
    assert t["complete"] is False or "thank" not in t["title"].lower()
    long_ = shorts.title_info("So what we did when we arrived there around the middle of September with all of the equipment "
                              "and the instructors was train on rope techniques for six long days in the mountains")
    assert long_["source"] == "truncated" and not long_["complete"] and not long_["title"].lower().startswith("so ")


def test_llm_title_validated_against_the_clip(monkeypatch):
    from nexus import llm
    text = "Batteries store the midday surplus for the evening peak. That is why a small battery changes the economics."
    answers = iter([{"title": "Small batteries change home solar economics"}, {"title": "Tesla sells 90 batteries"}])
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: next(answers))
    ok = shorts.title_info(text, use_llm=True)
    assert ok["source"] == "llm" and ok["fallback"]
    bad = shorts.title_info(text, use_llm=True)       # invented brand and number: rejected, deterministic title kept
    assert bad["source"] != "llm"


def _qa():
    segs, t = _segments(0.5, "Thank you so much for joining us today.", speaker="interviewer")
    s2, t = _segments(t, "Hello, glad to be here with you.", speaker="guest")
    s3, t = _segments(t, "What did you learn about solar power this year?", speaker="interviewer")
    s4, t = _segments(t, SOLAR, speaker="guest")
    s5, t = _segments(t, "And how do you bake your bread at home?", speaker="interviewer")
    s6, t = _segments(t, BREAD, speaker="guest")
    return {"probe": {"duration": t + 1, "audio": {"channels": 1}}, "scenes": [(0.0, t + 1)],
            "transcript": {"language": "en", "segments": segs + s2 + s3 + s4 + s5 + s6}}


def test_context_line_and_no_pleasantry_openings():
    an = _qa()
    sel = shorts.select(an, top=6, durations=(15, 25))
    assert len(sel) >= 2
    sents = shorts.usable_sentences(shorts._norm(an))
    for m in sel:
        first = sents[m["sentences"][0]]
        assert "thank" not in first["text"].lower() and "glad to be here" not in first["text"].lower()
        prev = sents[m["sentences"][0] - 1]
        if not first.get("interviewer") and prev.get("interviewer") and prev["text"].endswith("?"):
            assert m["context_line"] and m["context_line"]["text"].endswith("?")
    # an answer that follows a question gets that question as its context line, drawn as a lower third
    k = next(i for i, x in enumerate(sents) if x["text"].startswith("Solar panels"))
    cl = shorts.context_line(sents, k)
    assert cl["text"] == "What did you learn about solar power this year?"
    assert shorts.context_line(sents, k + 1) is None  # mid-answer: the same speaker was already talking
    m = dict(sel[0], context_line=cl)
    tl = shorts.compose("/x.mp4", an, m, aspect="9:16")
    assert any(t["text"] == m["context_line"]["text"] for t in tl["clips"][0].get("text") or [])
    # distinct: no two shorts share more than the padding tolerance of source
    for a in sel:
        for b in sel:
            if a is not b:
                assert min(a["end"], b["end"]) - max(a["start"], b["start"]) <= shorts.OVERLAP_TOL + 1e-6
