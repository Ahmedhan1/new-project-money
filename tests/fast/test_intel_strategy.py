"""Reference → original strategy → Account 2 job: originality and grounding gates, deterministic fallback."""
import pytest

from nexus.intelligence import contracts as C, strategy
from nexus.intelligence.model_router import router

BRIEF = {"id": "rb1", "claims": [{"id": "c1", "text": "Lithium-ion battery pack prices fell 90 percent between 2010 and 2023.", "source_url": "https://example.org/a"},
                                {"id": "c2", "text": "Home batteries store midday solar power for the evening peak.", "source_url": "https://example.org/b"},
                                {"id": "c3", "text": "Grid batteries respond to demand changes in milliseconds.", "source_url": "https://example.org/c"}]}
STYLE = {"id": "st1", "pacing": {"mean_shot_s": 3.2}, "hook_patterns": ["question"], "structure": ["hook", "stakes", "explain", "payoff"],
         "do_not_copy": ["Beast Games", "subscribe or this video gets deleted"]}


def test_originality_and_grounding_gates():
    good = {"title": "Why batteries change solar", "hook": "Why does sunshine at noon matter at 8 pm?",
            "scenes": [{"narration": "Battery prices fell 90 percent between 2010 and 2023.", "claim_ids": ["c1"]}]}
    assert strategy.originality(good, STYLE)["ok"] and strategy.grounding(good, BRIEF)["ok"]
    copied = {**good, "hook": "Subscribe or this video gets deleted!"}
    assert strategy.originality(copied, STYLE)["banned_phrases_used"] == ["subscribe or this video gets deleted"]
    ref = "the battery prices fell ninety percent in the last decade and nobody noticed it at all"
    lifted = {**good, "scenes": [{"narration": "Battery prices fell ninety percent in the last decade and nobody noticed it at all."}]}
    assert strategy.originality(lifted, STYLE, [ref])["max_6gram_overlap"] > 0.3 and not strategy.originality(lifted, STYLE, [ref])["ok"]
    invented = {**good, "scenes": [{"narration": "Prices fell 97 percent.", "claim_ids": ["c9"]}]}
    g = strategy.grounding(invented, BRIEF)
    assert not g["ok"] and g["unsupported_numbers"] == ["97"] and g["unknown_claim_ids"] == ["c9"]
    assert not strategy.grounding(good, None)["ok"]                                               # no research = unverified, never "ok"


def test_script_storyboard_and_production_job_without_any_ai_provider(home, monkeypatch):
    def none(*a, **k):
        raise router.NoProviderAvailable("scriptwriting", ["test: nothing available"])
    monkeypatch.setattr(router, "complete", none)
    monkeypatch.setattr("nexus.intelligence.model_router.complete", none)
    s = strategy.write_script("home solar batteries", BRIEF, STYLE, scenes=4)
    assert C.validate(s) == [] and s["originality"]["written_by"].startswith("fallback") and s["originality"]["ok"]
    assert s["originality"]["grounding"]["ok"] and len(s["scenes"]) == 4
    sb = strategy.storyboard(s, aspect="9:16", style=STYLE)
    assert C.validate(sb) == [] and len(sb["shots"]) >= len(s["scenes"]) and sb["style"]["mean_shot_s"] == 3.2
    p = strategy.production_job(s, sb, BRIEF, STYLE)
    assert p["preset"] == "commercial" and p["size"] == [720, 1280] and "https://example.org/a" in p["notes"]
    out = strategy.submit(s, sb, BRIEF, STYLE)
    from nexus import jobs
    j = jobs.get(out["job"])
    assert j["kind"] == "create-from-idea" and j["params"]["a3"]["script_id"] == s["id"]
    bad = {**s, "originality": {**s["originality"], "ok": False}}
    with pytest.raises(ValueError):
        strategy.submit(bad, sb, BRIEF, STYLE)


def test_router_script_is_schema_validated(home, monkeypatch):
    calls = []

    def fake(task, prompt, **kw):
        calls.append((task, kw["privacy"], kw["schema"]["required"]))
        return router.Result("", {"title": "T", "hook": "Why?", "scenes": [{"purpose": "hook", "narration": "Why does noon power matter at night?", "visual": "sun", "claim_ids": []},
                                                                           {"purpose": "explain", "narration": "Home batteries store midday solar power.", "visual": "battery", "claim_ids": ["c2"]},
                                                                           {"purpose": "payoff", "narration": "So the sun works after dark.", "visual": "house", "claim_ids": []}]},
                             "p", "m")
    monkeypatch.setattr("nexus.intelligence.model_router.complete", fake)
    s = strategy.write_script("batteries", BRIEF, STYLE, scenes=3, privacy="private")
    assert calls == [("scriptwriting", "private", ["title", "hook", "scenes"])] and s["originality"]["written_by"] == "p/m"



def test_clickbait_framing_blocks_production_unless_sourced(home):
    """Real run 2026-11-12: 'Did you know the Aswan High Dam has a secret?' passed originality + grounding."""
    s = {"title": "The Secret Dam", "hook": "Did you know the dam has a secret?", "scenes": [{"narration": "Built between 1960 and 1970."}]}
    f = strategy.framing(s, BRIEF)
    assert not f["ok"] and {h["where"] for h in f["unsupported_framing"]} == {"title", "hook"}
    sourced = {"claims": [{"id": "c1", "text": "The vault kept a secret archive until 1990."}]}
    assert strategy.framing({"title": "x", "hook": "The archive had a secret.", "scenes": []}, sourced)["ok"]
    assert strategy.framing({"title": "Why batteries change solar", "hook": "Why does noon matter at 8 pm?", "scenes": []}, BRIEF)["ok"]
