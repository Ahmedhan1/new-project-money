"""PLAT-1: platform knowledge base + per-destination tuning + rendered-file checks. No network, no models."""
import copy
import datetime as dt
import subprocess

import pytest

from nexus import platforms
from nexus.timeline import ops, render
from nexus.timeline.render import total_duration

KINDS = {"documented", "observed", "experimental", "unverified"}


def test_all_destinations_load_and_every_value_has_provenance():
    assert set(platforms.CANONICAL) <= set(platforms.names())
    for n in platforms.names():
        prof = platforms.get(n)
        assert prof["fields"], n
        for f, x in prof["fields"].items():
            assert x.get("kind") in KINDS, (n, f)
            if x.get("value") is None:
                continue
            assert x.get("source") and x.get("read_on"), (n, f)
            if x["kind"] == "documented":
                assert str(x["source"]).startswith("https://"), (n, f)
            if x["kind"] == "experimental":  # NEXUS's own choices are never dressed up as platform data
                assert str(x["source"]).startswith("editorial"), (n, f)


def test_facebook_reels_merges_its_sub_profile_and_aliases_resolve():
    fr, fb = platforms.get("facebook_reels"), platforms.get("facebook")
    assert fr["v"]["aspect_recommended"] != fb["v"]["aspect_recommended"] or fr["display"] != fb["display"]
    assert platforms.get("shorts")["name"] == "youtube_shorts"
    with pytest.raises(KeyError):
        platforms.get("myspace")


def _master(n_sent=30, dur=None):
    clips, subs, t = [], [], 0.0
    for k in range(n_sent):
        d = 4.0
        clips.append(ops.video_clip("/dev/null.mp4", k * 4.0, k * 4.0 + d, focus=[0.3, 0.5] if k % 2 else [0.5, 0.5]))
        subs.append({"start": t + 0.2, "end": t + 3.8, "text": f"Sentence number {k} ends here."})
        t += d
    return {"version": 1, "width": 1920, "height": 1080, "fps": 30, "lang": "en", "grade": "none", "clips": clips, "audio": [],
            "music": None, "subtitles": subs, "markers": [], "target_lufs": -16.0}


def test_tune_vertical_reframes_keeps_focus_moves_captions_and_never_mutates():
    master = _master()
    snap = copy.deepcopy(master)
    tl, notes = platforms.tune(master, "tiktok")
    assert master == snap                                   # input untouched
    assert (tl["width"], tl["height"]) == (1080, 1920)
    vids = [c for c in tl["clips"] if c["type"] == "video"]
    assert any(c["fit"] == "cover" and c["focus"] == [0.3, 0.5] for c in vids)  # measured focus kept: subject-aware crop
    assert any(c["fit"] == "blur" for c in vids)                                # unknown subject: no blind centre crop
    z = platforms.value("tiktok", "safe_zone")
    assert tl["captions"]["safe_bottom"] == z["bottom"]
    assert all("[" in n and "]" in n for n in notes)         # every change cites the field (and its kind) behind it
    assert tl["platform"] == "tiktok"


def test_over_long_master_is_cut_after_a_whole_sentence_under_the_limit():
    tl, notes = platforms.tune(_master(n_sent=30), "snapchat_spotlight")   # 120 s master, 60 s limit
    limit = platforms.value("snapchat_spotlight", "duration_max_s")
    assert total_duration(tl) <= limit + 0.01
    last = tl["subtitles"][-1]
    assert last["text"].endswith(".") and last["end"] <= total_duration(tl) + 1e-6
    assert any("limit" in n for n in notes)


def test_deliverables_share_renders_and_split_only_what_needs_a_shorter_cut():
    names = ["youtube_shorts", "tiktok", "instagram_reels", "youtube", "snapchat_spotlight"]
    short = platforms.deliverables(names, duration=45)
    vertical = next(g for g in short if g["height"] == 1920)
    assert set(vertical["platforms"]) == {"youtube_shorts", "tiktok", "instagram_reels", "snapchat_spotlight"}
    assert len(short) == 2                                  # one vertical render + one 16:9 render for five destinations
    longer = platforms.deliverables(names, duration=120)    # Snapchat's 60 s limit needs its own trimmed cut
    snap = next(g for g in longer if "snapchat_spotlight" in g["platforms"])
    assert snap["platforms"] == ["snapchat_spotlight"] and snap["trim_to_s"] == 60


def test_safe_zone_in_pixels():
    z = platforms.safe_zone("instagram_reels", 1080, 1920)
    assert z["bottom"] == round(platforms.value("instagram_reels", "safe_zone")["bottom"] * 1920)
    assert platforms.safe_zone("youtube", 1920, 1080) == {"top": 0, "bottom": 0, "left": 0, "right": 0}


def test_stale_profiles_with_a_shifted_clock():
    assert platforms.stale(90, today=dt.date(2026, 11, 13)) == []
    old = platforms.stale(90, today=dt.date(2027, 6, 1))
    assert {o["platform"] for o in old} == set(platforms.CANONICAL)


def test_check_passes_a_compliant_render_and_fails_wrong_aspect_and_length(home, tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3", "-f", "lavfi",
                    "-i", "sine=d=3:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    tl = {**_master(1), "clips": [ops.video_clip(str(src), 0, 2.5)], "subtitles": [], "width": 640, "height": 360}
    vt, _ = platforms.tune(tl, "tiktok")
    vt["width"], vt["height"] = 360, 640                    # keep the test fast: same 9:16 aspect, smaller frame
    render.render(vt, tmp_path / "v.mp4", tmp_path / "w")
    good = platforms.check(tmp_path / "v.mp4", "tiktok")
    assert next(c for c in good["checks"] if c["id"] == "aspect_recommended")["ok"]
    assert next(c for c in good["checks"] if c["id"] == "duration_max")["ok"]
    wide = platforms.check(src, "tiktok")
    assert not next(c for c in wide["checks"] if c["id"] == "aspect_recommended")["ok"]
    long = platforms.check({"duration": 600.0, "video": {"width": 1080, "height": 1920, "codec": "h264", "fps": 30}}, "snapchat_spotlight")
    assert not long["ok"] and not next(c for c in long["checks"] if c["id"] == "duration_max")["ok"]


def test_metrics_import_normalises_a_manual_export(tmp_path):
    f = tmp_path / "yt.csv"
    f.write_text("Video title,Views,Average view duration,Duration\nA,1200,0:30,60\nB,800,0:45,90\nC,,,\n")
    rows = platforms.import_metrics(f, "youtube")
    assert len(rows) == 2 and rows[0]["views"] == 1200 and abs(rows[0]["retention"] - 0.5) < 1e-6
    g = tmp_path / "tt.json"
    g.write_text('[{"title": "x", "plays": "2,000", "completion rate": "35%"}]')
    r = platforms.import_metrics(g, "tiktok")
    assert r[0]["views"] == 2000 and abs(r[0]["retention"] - 0.35) < 1e-6
