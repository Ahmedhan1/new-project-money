"""Creator intelligence V2: 14 measurable dimensions, weighted blends, conflicts surfaced (no media needed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.intelligence.creators import style_v2 as S

PROFILES = json.loads((Path(__file__).resolve().parents[2] / "docs/results/account3/creators_profiles.json").read_text(encoding="utf-8"))["profiles"]


def test_fourteen_dimensions_from_a_real_profile_without_media():
    d = S.dimensions(PROFILES[0])
    assert set(S.DIMENSIONS) <= set(d["dimensions"]) and len(S.DIMENSIONS) == 14
    assert d["dimensions"]["cuts_per_min"]["value"] == PROFILES[0]["measured"]["cuts_per_min"]
    # frame dimensions are null with a reason, never guessed
    assert d["dimensions"]["on_screen_text_share"]["value"] is None and "no media" in d["frame_note"]
    assert d["coverage"].endswith("/14") and int(d["coverage"].split("/")[0]) >= 10


def _fake(ref, cpm, wpm):
    dims = {k: {"value": None} for k in S.DIMENSIONS}
    dims["cuts_per_min"] = {"value": cpm}
    dims["wpm_speaking"] = {"value": wpm}
    dims["colour"] = {"value": {"saturation": 0.4, "brightness": 0.5}}
    return {"reference": ref, "dimensions": dims}


def test_weighted_blend_and_conflict_detection():
    b = S.combine([_fake("a", 10, 150), _fake("b", 14, 160)], [3, 1])
    assert b["dimensions"]["cuts_per_min"]["value"] == 11.0 and not b["conflicts"] and b["verdict"] == "coherent"
    assert b["production_targets"]["mean_shot_s"] == round(60 / 11, 2)
    c = S.combine([_fake("doc", 2, 150), _fake("challenge", 40, 160)])
    assert c["conflicts"] == ["cuts_per_min"] and "incoherent" in c["verdict"]
    assert c["confidence"] < b["confidence"] or c["dimensions"]["cuts_per_min"]["agreement"] == 0
    with pytest.raises(ValueError):
        S.combine([_fake("a", 1, 1)], [0])


def test_distance_orders_similar_references_closer():
    a, b, c = _fake("a", 10, 150), _fake("b", 11, 155), _fake("c", 30, 90)
    assert S.distance(a, b)["mean"] < S.distance(a, c)["mean"]
