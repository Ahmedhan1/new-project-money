"""Market research V2: credibility, cross-source, recency, geography, language demand, coverage, uncertainty."""
from __future__ import annotations

import json
from pathlib import Path

from nexus.intelligence.market import assess_v2 as A
from nexus.intelligence.quality import visual_context

R = Path(__file__).resolve().parents[2] / "docs/results/account3"


def test_real_report_is_indicative_with_an_honest_interval():
    a = A.assess(json.loads((R / "market_report_jwst_en.json").read_text(encoding="utf-8")))
    assert a["schema"] == "nexus.MarketAssessment/2"
    assert a["credibility"]["by_tier"].get("primary", 0) > 0 and 0 < a["credibility"]["value"] <= 1
    u = a["uncertainty"]
    assert u["low"] <= u["score"] <= u["high"] and set(u["unknown_factors"]) == {"freshness", "competition"}
    assert a["competition"]["value"] is None and a["saturation"]["value"] is None  # never guessed
    assert a["verdict"].startswith("indicative only") and a["geography"]["countries"] == {"GF": 2}
    assert a["recency"]["dated_publications"] > 0  # NASA items carry publication dates; Wikipedia retrieval dates do not count


def test_cross_source_counts_publishers_not_pages():
    brief = {"sources": [{"id": "a", "url": "https://en.wikipedia.org/wiki/X"}, {"id": "b", "url": "https://en.wikipedia.org/wiki/Y"},
                         {"id": "c", "url": "https://www.nasa.gov/x"}],
             "claims": [{"text": "one", "supported_by": ["a", "b"]}, {"text": "two", "supported_by": ["a", "c"]}]}
    cs = A.cross_source(brief)
    assert cs["confirmed"] == 1 and cs["value"] == 0.5
    assert A.tier("https://www.nasa.gov/x") == "primary" and A.tier("https://random.blog/x") == "unknown"


def test_place_resolver_does_not_match_longer_names_by_prefix():
    # regression: "Kourou" (French Guiana) was resolved to Guinea via the subdivision "Kouroussa"
    assert visual_context.places("launched from Kourou")["Kourou"] == ["GF"]
    assert "GN" not in {c for v in visual_context.places("Kourou launch site").values() for c in v}


def test_generic_words_never_identify_a_place():
    # regression from the Demo 10 research: "Upper Egypt" resolved to Ghana/Gambia/Guyana/South Sudan via "Upper ...",
    # "New Valley" (Egypt) to Colombia/Honduras/Italy via "Valle"
    assert visual_context.places("the southernmost governorate in Upper Egypt") == {"Upper Egypt": ["EG"]}
    assert visual_context.places("the New Valley Governorate") == {"New Valley": ["EG"]}
    assert visual_context.places("Belgorodskaya oblast") == {"Belgorodskaya": ["RU"]}  # inflected forms still resolve
