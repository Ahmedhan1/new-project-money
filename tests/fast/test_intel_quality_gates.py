"""Regression tests for the four defects found by inspecting Account 3 Demo 4 (2026-11-12): wrong-country historical
map, empty-box credits, silent title-card fallback, unsupported sensational hook."""
import shutil

import pytest

from nexus.intelligence.quality import scene_plan, typography, visual_context as vc
from nexus.timeline import fonts

CTX = vc.scene_context("The Aswan High Dam", "The dam created Lake Nasser, transforming the Aswan Governorate.", "Aswan Governorate")


def test_wrong_country_historical_map_is_refused():
    belgorod = {"title": "Belgorod governorate 1745", "description": "Map of Belgorod Governorate of the Russian Empire, 1745",
                "categories": "Maps of Belgorod Oblast, 1745 maps"}
    r = vc.check(belgorod, CTX)
    assert not r["ok"] and {f["check"] for f in r["findings"]} >= {"geo_mismatch", "historical_in_modern"}
    assert "RU" in r["findings"][0]["detail"] and "EG" in r["findings"][0]["detail"]


def test_right_place_passes_and_keyword_only_match_is_not_enough():
    assert vc.check({"title": "Aswan High Dam-1", "description": "The Aswan High Dam on the Nile, Egypt", "date": "2010"}, CTX)["ok"]
    r = vc.check({"title": "Concrete dam at sunset", "description": "A dam", "categories": "Dams"}, CTX)
    assert not r["ok"] and r["findings"][0]["check"] == "geo_unverified"


def test_old_map_allowed_only_for_historical_scenes():
    old = {"title": "Map of Egypt 1798", "description": "French map of Egypt", "categories": "Old maps of Egypt"}
    assert not vc.check(old, CTX)["ok"]
    hist = vc.scene_context("History of Egypt", "In 1798 French scholars mapped Egypt.", "old map of Egypt")
    assert vc.check(old, hist)["ok"]


def test_mixed_script_text_gets_a_font_per_run_and_no_boxes():
    line = '"Aswan High Dam-1" by شرف الدين, licensed CC BY-SA 3.0'
    rs = fonts.visual_runs(line)
    by_script = {s: f for _, f, s in rs}
    assert "Naskh" in by_script["arabic"] or "Arabic" in by_script["arabic"] or "tahoma" in by_script["arabic"].lower()
    assert all(fonts.covers(f, ch) for t, f, _ in rs for ch in t)          # every character is in the font that draws it
    arabic_run = next(t for t, _, s in rs if s == "arabic")
    assert "," not in arabic_run                                             # neutral punctuation stays on the Latin side
    for t in ("阿斯旺高坝于1970年建成", "असवान बाँध 1970 में पूरा हुआ", "Асуанская плотина", "Größe: 111 m"):
        assert fonts.missing(t) == []
    assert fonts.missing("glyph 𓂀") == ["𓂀"]


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
@pytest.mark.parametrize("text,lang", [("Der Assuan-Staudamm wurde 1970 fertiggestellt", "de"), ("Асуанская плотина построена в 1970 году", "ru"),
                                       ("阿斯旺高坝于1970年建成", "zh")])
def test_rendered_frame_reads_back(text, lang):
    if not typography.ocr_available(lang):
        pytest.skip(f"no tesseract data for {lang}")
    r = typography.render_check(text, lang)
    assert r["ok"] and r["recall"] >= 0.8, r


def test_title_card_fallback_blocks_approval_unless_approved():
    plan = [{"scene": i, "kind": k, "rejected": []} for i, k in enumerate(["licensed_photo", "title_card_fallback", "fact_graphic", "title_card_fallback"])]
    st = scene_plan.visual_stage(plan)
    assert not st["passed"] and "2, 4" in st["checks"][0]["detail"]
    assert scene_plan.visual_stage(plan, approved_fallback=True)["passed"]
    assert scene_plan.visual_stage(plan[:1] + plan[2:3])["passed"]
    assert scene_plan.key_fact("This vast area covers 62,726 square kilometres.")[0] == "62,726 square kilometres"
    assert scene_plan.key_fact("It is a symbol of progress.") is None


def test_typography_stage_fails_on_missing_glyphs():
    st = scene_plan.typography_stage({"clips": [{"text": [{"text": "Ancient 𓂀 symbol"}]}]}, ocr=False)
    assert not st["passed"] and st["checks"][0]["severity"] == "critical"


def test_create_review_rejects_sensational_framing():
    from nexus import create
    res = {"text": "The Aswan High Dam was built between 1960 and 1970 on the Nile.", "grounded": True, "sources": []}
    sc = {"title": "The Secret Dam", "topic": "Aswan", "scenes": [{"narration": "Did you know the Aswan High Dam has a secret? It was built between 1960 and 1970.",
                                                                   "on_screen_text": "x", "visual_query": "dam"}]}
    chk = {c["check"]: c for c in create.review_script(sc, res, 1)["checks"]}
    assert not chk["framing supported by research"]["ok"] and chk["framing supported by research"]["severity"] == "major"
