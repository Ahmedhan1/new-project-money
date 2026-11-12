"""CreatorReferenceIntelligence: measured pacing, contract validity, no causation claims. No network, no models."""
import json
import subprocess

import pytest

from nexus.intelligence import contracts as C
from nexus.intelligence.creators import (DISCLAIMER, RightsError, analyze_reference, compare, library, metadata,
                                         style_from, to_account2_reference_style)
from nexus.intelligence.creators import measure as M

COLORS = ["red", "blue", "green", "yellow", "white", "black", "magenta", "cyan", "orange", "purple", "gray", "navy"]


def make_video(path, seg_s: float, total: float = 12.0):
    n = int(total / seg_s)
    args = ["ffmpeg", "-v", "error", "-y"]
    for k in range(n):
        args += ["-f", "lavfi", "-i", f"color=c={COLORS[k % len(COLORS)]}:s=160x90:r=10:d={seg_s}"]
    args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={total}"]
    flt = "".join(f"[{k}:v]" for k in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    args += ["-filter_complex", flt, "-map", "[v]", "-map", f"{n}:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(args, check=True)
    return path


@pytest.fixture(scope="module")
def vids(tmp_path_factory):
    d = tmp_path_factory.mktemp("refs")
    return {"fast": make_video(d / "fast.mp4", 1.0), "slow": make_video(d / "slow.mp4", 4.0)}


def fake_transcript(onset: float, words: list[str], step: float = 0.4) -> dict:
    ws = [{"start": onset + i * step, "end": onset + i * step + 0.3, "word": w} for i, w in enumerate(words)]
    return {"language": "en", "segments": [{"start": ws[0]["start"], "end": ws[-1]["end"], "text": " ".join(words), "words": ws}]}


def test_measured_pacing_differs(home, vids):
    fast = analyze_reference(vids["fast"], rights="owned", transcribe=False, meta={"format": "short_form_story"})
    slow = analyze_reference(vids["slow"], rights="owned", transcribe=False, meta={"format": "premium_ad"})
    assert C.validate(fast) == [] and C.validate(slow) == []
    assert fast["measured"]["cuts_per_min"] > 2 * slow["measured"]["cuts_per_min"] > 0
    assert fast["measured"]["median_shot_s"] < slow["measured"]["median_shot_s"]
    assert fast["measured"]["opening"]["first_3s"]["cuts"] >= 2 and slow["measured"]["opening"]["first_3s"]["cuts"] == 0
    assert len(fast["measured"]["pacing_curve"]) == 10
    assert fast["editing"]["tempo"]["kind"] == "interpreted" and fast["editing"]["tempo"]["based_on"]
    assert "measured" in fast["provenance"] and "interpreted" in fast["provenance"]
    assert fast["source"]["sha256"] and fast["source"]["rights"] == "owned"


def test_rights_required(home, vids):
    with pytest.raises(RightsError):
        analyze_reference(vids["fast"], rights="downloaded-from-youtube", transcribe=False)


def test_url_is_metadata_only_without_key(home, monkeypatch):
    monkeypatch.delenv("NEXUS_KEY_YOUTUBE", raising=False)
    p = analyze_reference("https://www.youtube.com/watch?v=dQw4w9WgXcQ", rights="",
                          meta={"title": "Owner typed title", "views": 123, "creator": "someone"})
    assert C.validate(p) == []
    assert p["source"]["downloaded"] is False and p["measured"] is None
    assert p["packaging"]["views"] == {"value": 123, "source": "owner_entered"}
    assert p["source"]["youtube_api"]["status"] in ("no_key", "blocked")


def test_youtube_api_parse():
    payload = {"items": [{"id": "abcdefghijk", "snippet": {"title": "T", "publishedAt": "2024-01-01T00:00:00Z", "channelTitle": "C"},
                          "contentDetails": {"duration": "PT1H2M3S"}, "statistics": {"viewCount": "10"}}]}
    pk = metadata.parse_videos_list(payload)
    assert pk["duration_s"]["value"] == 3723 and pk["views"]["value"] == 10 and "likes" not in pk
    assert metadata.youtube_id("https://youtu.be/abcdefghijk") == "abcdefghijk"


def test_compare_and_style(home, vids):
    a = analyze_reference(vids["fast"], rights="owned", meta={"format": "short_form_story", "title": "Fast ref"},
                          transcript=fake_transcript(0.2, "why would anyone lose a million dollars in 10 seconds".split()))
    b = analyze_reference(vids["slow"], rights="public-domain", meta={"format": "premium_ad", "creator": "Agency X"},
                          transcript=fake_transcript(6.0, "the city wakes up slowly".split()))
    assert a["hook"]["type"]["value"] == "question" and a["measured"]["opening"]["first_15s"]["numbers"] >= 1
    rep = compare([a, b])
    assert rep["disclaimer"] == DISCLAIMER and "not proof of causation" in rep["disclaimer"]
    assert rep["strongest_opening"]["text"].startswith(a["id"])
    text = json.dumps(rep).lower()
    for bad in ("causes ", "caused by", "guarantee", "will go viral", "proves that"):
        assert bad not in text
    for sec in ("shared_patterns", "differences", "adaptable_techniques", "nexus_should_differently"):
        for st in rep[sec]:
            assert "cites" in st
    assert all(st["cites"] for st in rep["differences"] + rep["adaptable_techniques"])
    assert rep["original_angle"]["cites"]
    style = style_from([a, b], name="test style")
    assert C.validate(style) == []
    assert any("Fast ref" in x for x in style["do_not_copy"]) and any("Agency X" in x for x in style["do_not_copy"])
    a2 = to_account2_reference_style(style, rights="public-domain")
    assert set(a2) >= {"name", "source", "metrics", "rights"}


def test_from_parts_pure():
    m = M.from_parts(duration=60, scenes=[(0, 30), (30, 60)], silences=[(0, 6)], transcript=None)
    assert m["cuts_per_min"] == 1.0 and m["audio"]["silence_share"] == 0.1 and m["speech"]["available"] is False


def test_library_sources():
    assert library.check() == []
    ids = {a["id"] for a in library.archetypes()}
    assert {"challenge_high_stakes", "arabic_edu_explainer", "science_documentary", "travel_vlog", "interview_podcast",
            "investigative", "product_review", "premium_ad", "short_form_story"} <= ids


def test_caption_mismatch_flagged():
    tr = fake_transcript(1.0, ["word"] * 200, step=1.0)  # captions run to ~200 s on a 60 s video
    m = M.from_parts(duration=60, scenes=[(0, 60)], transcript=tr)
    assert m["speech"]["caption_mismatch"] and m["speech"]["speech_share"] <= 1.0
