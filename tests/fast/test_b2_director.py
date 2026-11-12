"""DIR-4: director coherence across media types - word-level speaker turns (K-17), clean openings, stored analyses from an
older understanding layer, multi-speaker handling. Hand-built analyses, no models."""
from test_director import BREAD, SOLAR, _analysis, _segments

from nexus import director
from nexus.media import understand as und


def _k17():
    """The real failure from the interview benchmark: a Whisper segment boundary falls mid-sentence and diarisation labels
    the middle segment as the interviewer."""
    w = lambda t0, txt: [{"start": round(t0 + 0.3 * k, 2), "end": round(t0 + 0.3 * k + 0.25, 2), "word": x}  # noqa: E731
                         for k, x in enumerate(txt.split())]
    segs = [{"start": 0.0, "end": 2.2, "speaker": "interviewer", "text": "How long before you find out?",
             "words": w(0.0, "How long before you find out?")},
            {"start": 3.0, "end": 4.5, "speaker": "guest", "text": "Well I think it'll be quite some",
             "words": w(3.0, "Well I think it'll be quite some")},
            {"start": 5.1, "end": 8.0, "speaker": "interviewer", "text": "time before anyone finds out so I would",
             "words": w(5.1, "time before anyone finds out so I would")},
            {"start": 7.6, "end": 11.0, "speaker": "guest", "text": "suspect two years prior someone would find out.",
             "words": w(7.6, "suspect two years prior someone would find out.")},
            {"start": 12.0, "end": 14.0, "speaker": "interviewer", "text": "And what about training?",
             "words": w(12.0, "And what about training?")}]
    return {"probe": {"duration": 15.0}, "scenes": [(0.0, 15.0)], "transcript": {"language": "en", "segments": segs}}


def test_k17_mid_sentence_label_flip_is_one_guest_turn():
    sents = und.sentences(_k17())
    texts = [(s["speaker"], s["text"]) for s in sents]
    assert texts[0][0] == "interviewer" and texts[-1][0] == "interviewer"
    mid = sents[1]
    assert mid["speaker"] == "guest" and mid["text"].startswith("Well I think") and mid["text"].endswith("find out.")
    assert mid["speaker_repair"]["labels"]["interviewer"] > 0 and all(w["speaker"] == "guest" for w in mid["words"])
    # a real turn (capitalised question after a finished sentence) is still a turn
    assert len(sents) == 3
    assert len(und.sentences(_k17(), repair_turns=False)) >= 4


def test_stored_v1_understanding_is_recomputed_and_keeps_measured_shots():
    an = _k17()
    und.enrich(an)
    an["understanding"]["version"] = 1
    an["shots"][0]["measured"] = True
    an["shots"][0]["quality"] = 0.42
    u = und.ensure(an)
    assert u["version"] == und.VERSION and u["shots"][0]["quality"] == 0.42
    assert any("recomputed" in n for n in u["notes"])


def test_opening_never_starts_on_an_answer_word_or_conjunction():
    an = _analysis([(0.5, "Yes, solar panels convert sunlight into electricity with thin silicon cells on every roof. " + SOLAR)])
    p = director.plan({"talk.mp4": an}, {"profile": "interview_storytelling", "overrides": {"no_lift": True}})
    first = next(it for it in p["items"] if it["kind"] == "piece")
    assert first["words"][0]["word"] == "solar" and first["in"] > 0.5
    assert any(d["kind"] == "opening" and d["action"] == "drop" and "Yes," in d["reason"] for d in p["decisions"])


def test_multi_speaker_panel_keeps_answers_and_drops_host_prompts():
    segs, t = _segments(0.5, "Welcome back to the panel everyone. What changed for engineers this year?", speaker="host")
    s2, t = _segments(t, SOLAR, speaker="S1")
    s3, t = _segments(t, "And what do you think about bread?", speaker="host")
    s4, t = _segments(t, BREAD, speaker="S2")
    an = {"probe": {"duration": t + 1}, "scenes": [(0.0, t + 1)], "transcript": {"language": "en", "segments": segs + s2 + s3 + s4}}
    p = director.plan({"panel.mp4": an}, {"profile": "podcast_edit", "target_seconds": 60})
    spk = [it["speaker"] for it in p["items"] if it["kind"] == "piece"]
    assert {"S1", "S2"} <= set(spk)
    kept = " ".join(d["text"] for d in p["decisions"] if d["action"] == "keep" and d["kind"] == "sentence")
    assert "Welcome back" not in kept
