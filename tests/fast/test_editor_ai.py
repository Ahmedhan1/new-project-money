"""Editor brain: a cut represents the whole video at the requested length; finishing adds transitions only at real jumps."""
from nexus import editor_ai


def _an(dur, shots, segments=()):
    return {"probe": {"duration": dur, "audio": True, "video": {"width": 1280, "height": 720}},
            "transcript": {"segments": list(segments)}, "shots": shots, "scenes": [(s["start"], s["end"]) for s in shots],
            "moments": [], "understanding": {}}


def test_no_speech_cut_spans_beginning_middle_and_end():
    shots = [{"start": i * 20.0, "end": (i + 1) * 20.0, "motion": 0.01 + (i % 5) * 0.01, "sharpness": 100} for i in range(36)]
    an = _an(720.0, shots)  # 12 minutes, equal-looking shots: the old code returned the first 5 minutes
    pick = editor_ai.choose(an, 300, use_llm=False)
    keep = pick["keep"]
    total = sum(r["out"] - r["in"] for r in keep)
    assert abs(total - 300) <= 9, total
    assert keep[0]["in"] < 90 and keep[-1]["out"] > 630, (keep[0], keep[-1])   # from the start AND the end
    assert any(240 < r["in"] < 480 for r in keep)                                 # and the middle
    assert all(a["out"] <= b["in"] for a, b in zip(keep, keep[1:]))


def test_speech_cut_without_llm_uses_strong_lines_across_the_video():
    segs = [{"start": i * 6.0, "end": i * 6.0 + 5.0, "text": "word " * (3 + (i * 7) % 11)} for i in range(100)]
    an = _an(600.0, [{"start": 0.0, "end": 600.0, "speech_share": 0.8}], segs)
    keep = editor_ai.choose(an, 120, use_llm=False)["keep"]
    total = sum(r["out"] - r["in"] for r in keep)
    assert abs(total - 120) <= 6 and keep[0]["in"] < 120 and keep[-1]["out"] > 480


def test_fit_sentences_respects_length_and_keeps_the_hook():
    segs = [{"start": i * 6.0, "end": i * 6.0 + 5.0, "text": "a b c d e f"} for i in range(60)]
    an = _an(360.0, [{"start": 0.0, "end": 360.0}], segs)
    keep = editor_ai._fit_sentences(an, [40, 3, 20], 60)
    total = sum(r["out"] - r["in"] for r in keep)
    assert 50 <= total <= 70 and any(3 in r["segs"] for r in keep)


def test_polish_transitions_only_at_scene_jumps_and_framing_on_jump_cuts():
    clip = lambda a, b: {"id": f"c{a}", "src": "/v.mp4", "in": a, "out": b}  # noqa: E731
    tl = {"clips": [clip(0, 8), clip(9, 15), clip(200, 210), clip(400, 401)], "width": 1280, "height": 720}
    out = editor_ai.polish(tl, "documentary", speech=True)
    c = out["clips"]
    assert "transition" not in c[1]                          # 1 s gap inside one shot: no transition...
    assert {"type": "zoom", "from": 1.08, "to": 1.08} in c[1]["effects"]   # ...but a reframe hides the jump cut
    assert c[2]["transition"] == "crossfade" and c[2]["xfade"] in ("fade", "fadeblack")
    assert "transition" not in c[3]                          # too short to dissolve into
    assert out["grade"] == "documentary" and c[0]["fade_in"] >= 0.4 and c[-1]["fade_out"] >= 0.8
    assert any(e["type"] == "zoom" and e["to"] > 1.0 for e in c[0]["effects"])  # 8 s shot: Ken Burns push-in
