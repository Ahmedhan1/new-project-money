"""Strategy Handoff: Account 3 -> Account 2 documents validate, the approved script reaches the renderer verbatim."""
from __future__ import annotations

import pytest

from nexus.intelligence import contracts as C
from nexus.intelligence import handoff, strategy

BRIEF = {"id": "brief_t", "claims": [
    {"id": "c1", "text": "The Aswan High Dam is 111 metres tall.", "source_url": "https://en.wikipedia.org/wiki/Aswan_Dam"},
    {"id": "c2", "text": "Lake Nasser is about 550 km long.", "source_url": "https://en.wikipedia.org/wiki/Lake_Nasser"}]}


def _script(hook="How tall is the dam that tamed the Nile?"):
    scenes = [{"purpose": "hook", "narration": "The Aswan High Dam is 111 metres tall. It holds back the Nile.",
               "visual": "Aerial view of the Aswan High Dam at sunset", "claim_ids": ["c1"]},
              {"purpose": "context", "narration": "Behind it, Lake Nasser stretches about 550 km. It reaches into Sudan.",
               "visual": "satellite image of Lake Nasser", "claim_ids": ["c2"]}]
    body = {"title": "The dam that tamed the Nile", "hook": hook, "scenes": scenes}
    return C.make("ContentScript", id="scr_t", title=body["title"], language="en", scenes=scenes, sources=[],
                  originality={**strategy.originality(body), "grounding": strategy.grounding(body, BRIEF),
                               "framing": strategy.framing(body, BRIEF), "hook": hook})


def test_handoff_documents_validate_and_keep_the_script_verbatim():
    sc = _script()
    h = handoff.build(sc, strategy.storyboard(sc), BRIEF)
    assert not C.validate(h) and h["approved"]
    assert h["editorial_plan"]["schema"] == "nexus.EditorialPlan/1" and h["render_job"]["schema"] == "nexus.RenderJobManifest/1"
    a2 = h["create_params"]["script"]
    assert [s["narration"] for s in a2["scenes"]] == [s["narration"] for s in sc["scenes"]]  # verbatim
    assert a2["scenes"][0]["visual_query"] == "Aswan High Dam" and a2["scenes"][1]["on_screen_text"].startswith("550")
    d = h["editorial_plan"]["decisions"]
    assert d[0]["claim_ids"] == ["c1"] and d[0]["sources"] == ["https://en.wikipedia.org/wiki/Aswan_Dam"] and d[1]["start"] == d[0]["end"]
    try:  # the shared package (integration branch): validate with Account 2's own validator
        from nexus.contracts import media
    except ImportError:
        media = None
    if media is not None and hasattr(media, "validate"):
        assert media.validate(h["editorial_plan"]) == [] and media.validate(h["render_job"]) == []


def test_unapproved_handoff_is_refused():
    sc = _script(hook="The dam has a secret they never told you")
    h = handoff.build(sc, strategy.storyboard(sc), BRIEF)
    assert not h["approved"] and not h["checks"]["framing"]
    with pytest.raises(ValueError):
        handoff.submit(h)


def test_create_video_uses_an_approved_script_verbatim(monkeypatch, tmp_path):
    from nexus import create
    seen = {}
    monkeypatch.setattr(create, "research", lambda topic, notes=None: {"text": "The Aswan High Dam is 111 metres tall. Lake Nasser is about 550 km long.", "sources": [], "grounded": True})
    monkeypatch.setattr(create, "script_with_review", lambda *a, **k: pytest.fail("approved script must not be rewritten"))

    class Stop(Exception):
        pass

    def stage(name, fn):
        if name == "assemble":
            raise Stop
        out = fn() if name != "project" else "prj_x"
        seen[name] = out
        return out
    sc = handoff.a2_script(_script())
    with pytest.raises(Stop):
        create.create_video("Aswan High Dam", script=sc, stage=stage)
    got = seen["script"]
    assert got["script"]["verbatim"] and [s["narration"] for s in got["script"]["scenes"]] == [s["narration"] for s in sc["scenes"]]
    assert got["pre"]["verbatim"] and got["pre"]["attempts"] == 0
