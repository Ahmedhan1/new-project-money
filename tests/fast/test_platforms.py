"""PLAT-1: platform knowledge base, tune(), checklist(), metadata() (no models, no network)."""
import pytest

from nexus import platforms as plat, publish
from nexus.timeline import ops
from nexus.timeline.render import total_duration

ALL = ["youtube", "shorts", "tiktok", "instagram-reels", "instagram-feed", "facebook", "facebook-reels", "snapchat-spotlight", "linkedin", "x"]


def test_yaml_validates_and_covers_every_platform():
    assert plat.validate() == []
    assert set(ALL) <= set(plat.names())
    for p in ALL:
        sp = plat.spec(p)
        assert len(sp["signals"]) >= 3
        for s in sp["signals"]:
            assert s["source"] and s["read_on"] and s["confidence"] in plat.CONFIDENCE
            if isinstance(s["source"], dict):
                assert s["source"]["url"].startswith("https://")
            else:
                assert s["source"].startswith("unverified") and s["confidence"] == "editorial-rule-of-thumb"
        for k in ("top", "bottom", "left", "right"):
            assert 0 <= sp["safe_zone"][k] < 0.5
    stats = plat.source_stats()
    assert stats["sourced"] and stats["unverified"]


def test_publish_platforms_derived_from_yaml():
    assert set(ALL) | {"square"} == set(publish.PLATFORMS)
    assert publish.PLATFORMS["tiktok"]["size"] == (1080, 1920)
    assert publish.PLATFORMS["snapchat-spotlight"]["max_s"] == 60
    assert all(v.startswith("not implemented") for v in publish.AUTO_PUBLISH.values())


def landscape_tl(durs=(5.0, 5.0, 5.0), title=True, scores=None):
    tl = {"version": 1, "width": 1920, "height": 1080, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none", "target_lufs": -16.0,
          "clips": [], "audio": [], "music": None, "subtitles": [], "burn_subtitles": True, "markers": [],
          "overlays": [{"type": "watermark", "text": "x"}], "captions": {"style": "classic", "position": "bottom"}}
    if title:
        tl["clips"].append(ops.color_clip(3.0, text=[{"text": "My Talk", "style": "title"}], role="title_card", fade_in=0.5))
    t = 0.0
    for i, d in enumerate(durs):
        tl["clips"].append(ops.video_clip("/m/a.mp4", t, t + d, score=(scores or {}).get(i, 1.0), fade_in=0.4 if i == 0 else 0))
        t += d
    tl["clips"][-1]["fade_out"] = 0.5
    start = 3.0 if title else 0.0
    tl["subtitles"] = [{"start": start + k * 2.0 + 0.1, "end": start + k * 2.0 + 1.8, "text": f"cue {k}"} for k in range(int(sum(durs) // 2))]
    return tl


AN = {"/m/a.mp4": {"probe": {"duration": 400, "video": {"width": 1920, "height": 1080}, "audio": {}}, "quality": {"face_focus_x": None}}}


@pytest.mark.parametrize("p", ["tiktok", "instagram-reels", "shorts"])
def test_tune_short_form(p):
    tl, notes = plat.tune(landscape_tl(), p, AN)
    sp = plat.spec(p)
    assert (tl["width"], tl["height"]) == (1080, 1920)
    assert not any(c["type"] == "color" for c in tl["clips"])           # title card removed
    assert tl["clips"][0]["text"][0]["text"] == "My Talk"                 # ...and moved onto the first shot
    assert not tl["clips"][0].get("fade_in") and not tl["clips"][-1].get("fade_out")
    assert all(c["fit"] == "blur" for c in tl["clips"])                  # no face: full picture on blurred fill
    assert tl["subtitles"][0]["start"] < 1.0                             # captions rippled with the card removal
    assert tl["target_lufs"] == sp["loudness_lufs"]
    box = plat._caption_box(tl["captions"], 1080, 1920)
    assert plat._in_safe(box, sp["safe_zone"])
    assert any(o["type"] == "progress_bar" for o in tl["overlays"]) == bool(sp["progress_bar"])
    assert not any(o["type"] == "watermark" for o in tl["overlays"])
    assert all("signal" in n and "change" in n for n in notes)
    assert plat.checklist(tl, p)["passed"]


def test_tune_youtube_keeps_frame_and_shortens_card():
    tl, notes = plat.tune(landscape_tl(), "youtube", AN)
    assert (tl["width"], tl["height"]) == (1920, 1080)
    assert tl["clips"][0]["type"] == "color" and tl["clips"][0]["duration"] == 2.5
    assert tl["captions"]["position"] == "bottom" and tl["captions"]["style"] == "classic"
    assert tl["clips"][-1]["fade_out"] == 0.5
    assert any("end screen" in n["change"] for n in notes)


def test_tune_face_focus_crops_instead_of_blur():
    an = {"/m/a.mp4": {**AN["/m/a.mp4"], "quality": {"face_focus_x": 0.3}}}
    tl, _ = plat.tune(landscape_tl(), "tiktok", an)
    v = [c for c in tl["clips"] if c["type"] == "video"]
    assert all(c["fit"] == "cover" and c["focus"][0] == 0.3 for c in v)


def test_tune_trims_over_long_short_by_least_important_clip():
    tl0 = landscape_tl(durs=(20, 20, 20, 15), title=False, scores={0: 3, 1: 0.5, 2: 2, 3: 2})   # 75 s for a 60 s platform
    tl, notes = plat.tune(tl0, "snapchat-spotlight", AN)
    assert total_duration(tl) <= 60
    assert [c["score"] for c in tl["clips"]] == [3, 2, 2]               # the 0.5 clip went
    assert any("least important" in n["change"] for n in notes)
    assert all(q["end"] <= total_duration(tl) + 1e-6 for q in tl["subtitles"])
    assert plat.checklist(tl, "snapchat-spotlight")["passed"]


def test_tune_flags_when_cut_would_lose_meaning():
    tl, notes = plat.tune(landscape_tl(durs=(60, 60, 60, 60), title=False), "snapchat-spotlight", AN)
    assert "over_max_duration" in tl["platform_flags"]
    assert any(n["change"].startswith("FLAG") for n in notes)
    assert total_duration(tl) == 240


def test_checklist_flags_four_minute_video_for_snapchat():
    tl, _ = plat.tune(landscape_tl(durs=(60, 60, 60, 60), title=False), "snapchat-spotlight", AN)
    ck = plat.checklist(tl, "snapchat-spotlight")
    assert ck["stage"] == "platform-snapchat-spotlight" and not ck["passed"]
    bad = {c["check"] for c in ck["checks"] if not c["ok"]}
    assert "max duration" in bad
    assert set(ck["checks"][0]) >= {"check", "ok", "detail", "severity", "fix"}


def test_checklist_flags_untuned_vertical_problems():
    tl = landscape_tl()
    tl["width"], tl["height"] = 1080, 1920
    ck = plat.checklist(tl, "tiktok")
    bad = {c["check"] for c in ck["checks"] if not c["ok"]}
    assert {"first-second visual", "no watermark overlay", "loudness target"} <= bad


def test_metadata_within_limits_with_credits():
    class P:
        name = "proj"
        data = {"credits": ["Photo: NASA (public domain)"], "script": {"topic": "word " * 3000}}
    tl = landscape_tl()
    for p in ALL:
        sp = plat.spec(p)
        md = plat.metadata(P(), tl, p, title="A very long title " * 20)
        assert len(md["title"]) <= sp["title_max"]
        assert len(md["description"]) <= sp["desc_max"]
        assert len(md["hashtags"]) <= sp["hashtags"]
        if sp["desc_max"] >= 500:
            assert "NASA" in md["description"]


def test_youtube_chapters_in_description():
    tl = landscape_tl()
    tl["markers"] = [{"t": 0.0, "label": "chapter: Intro"}, {"t": 30.0, "label": "chapter: Cost"}, {"t": 75.0, "label": "chapter: Storage"}]
    md = plat.metadata(None, tl, "youtube", title="T", description="About energy.")
    assert "0:00 Intro" in md["description"] and "1:15 Storage" in md["description"]


def test_pillarboxed_vertical_puts_captions_under_picture_when_renderer_supports_margin(monkeypatch):
    monkeypatch.setattr(plat, "renderer_margin_v", lambda: True)
    tl, _ = plat.tune(landscape_tl(), "tiktok", AN)
    assert tl["captions"]["position"] == "bottom" and tl["captions"]["margin_v"] == round(plat.spec("tiktok")["safe_zone"]["bottom"] + 0.03, 3)
    assert plat.checklist(tl, "tiktok")["passed"]
    monkeypatch.setattr(plat, "renderer_margin_v", lambda: False)
    tl, _ = plat.tune(landscape_tl(), "tiktok", AN)
    assert tl["captions"]["position"] == "middle" and "margin_v" not in tl["captions"]
