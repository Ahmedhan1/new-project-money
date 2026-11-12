"""Language readiness, terminology memory and the native-review workflow."""
from __future__ import annotations

import pytest

from nexus.intelligence.language import readiness, review, terminology
from nexus.intelligence.language.translation_qa import check_line


def test_reviewer_terminology_outranks_machine_and_feeds_translation_qa(home):
    terminology.add("High Dam", "de", "Hochdamm", origin="machine")
    assert "saved" in terminology.add("High Dam", "de", "Hochdamm von Assuan", origin="glossary")
    ids = review.submit("de", [{"source": "The High Dam is 111 m high.", "target": "Der Hochdamm ist 111 m hoch."}])
    review.record(ids[0], "correct", "native-de-1", correction="Der Assuan-Staudamm ist 111 m hoch.",
                  terms={"High Dam": "Assuan-Staudamm"})
    assert terminology.lookup("High Dam", "de") == "Assuan-Staudamm"
    assert "refused" in terminology.add("High Dam", "de", "Hochbehinderung", origin="machine")  # MT cannot overwrite a reviewer
    g = terminology.glossary("de", "The High Dam is tall.")
    f = check_line("The High Dam is tall.", "Der Hochdamm ist hoch.", "de", glossary=g)
    assert any(x["check"] == "glossary_term_missing" for x in f["findings"])
    assert review.final_text(ids[0]) == "Der Assuan-Staudamm ist 111 m hoch."


def test_review_validates_input(home):
    iid = review.submit("ar-Gulf", [{"source": "x", "target": "y"}])[0]
    with pytest.raises(ValueError):
        review.record(iid, "correct", "rev")  # correction text required
    with pytest.raises(ValueError):
        review.record(iid, "approve", " ")  # reviewer label required
    with pytest.raises(ValueError):
        review.record(iid, "maybe", "rev")


def test_readiness_needs_native_review_before_publishable(home):
    r = readiness.assess("de", ocr=False)
    assert r["level"] == "draft_needs_native_review" and r["dimensions"]["human_reviewed"]["state"] == "unavailable"
    assert r["dimensions"]["lip_sync"]["state"] == "unavailable" and 0 < r["score"] < 100
    ids = review.submit("de", [{"source": f"s{i}", "target": f"t{i}"} for i in range(readiness.REVIEW_MIN_LINES)])
    for i in ids:
        review.record(i, "approve", "native-de-1")
    r2 = readiness.assess("de", ocr=False)
    assert r2["dimensions"]["human_reviewed"]["state"] == "verified" and r2["score"] > r["score"]
    # without an OCR read-back the subtitles are only "available" -> still not publishable: no claim without evidence
    assert r2["level"] == "draft_needs_native_review"


def test_readiness_is_honest_about_gulf_and_failed_voices(home):
    g = readiness.assess("ar-Gulf", ocr=False)
    assert g["dimensions"]["translation"]["state"] == "partial" and "MSA" in g["dimensions"]["translation"]["evidence"]
    assert g["dimensions"]["commercial_dubbing_licensed"]["state"] == "unavailable"
    zh = readiness.assess("zh", ocr=False)
    assert zh["dimensions"]["tts"]["state"] == "partial"  # the cleared voice failed to render in the real run
    assert any("Gulf" in b or "MSA" in b for b in g["blockers"])


def test_language_api_review_and_terms(home):
    from fastapi.testclient import TestClient
    from nexus import api
    c = TestClient(api.app)
    ids = c.post("/api/a3/language/review", json={"lang": "hi", "lines": [{"source": "dam", "target": "बांध"}]}).json()["ids"]
    assert c.post(f"/api/a3/language/review/{ids[0]}", json={"verdict": "approve", "reviewer": "native-hi-1",
                                                               "terms": {"High Dam": "हाई डैम"}}).json()["status"] == "approved"
    assert c.get("/api/a3/language/terms", params={"lang": "hi"}).json()["glossary"] == {"High Dam": "हाई डैम"}
    r = c.post("/api/a3/language/check", json={"source": ["The High Dam is tall."], "target": ["बांध ऊँचा है।"], "lang": "hi"}).json()
    assert r["terminology_applied"] == ["High Dam"]
    rd = c.get("/api/a3/language/readiness", params={"lang": "hi", "ocr": False}).json()
    assert rd["review"]["approved"] == 1 and set(rd["dimensions"]) == set(readiness.WEIGHTS)
    assert c.post(f"/api/a3/language/review/{ids[0]}", json={"verdict": "nope", "reviewer": "x"}).status_code == 400
