"""RPR-1: rights gate, transformation report, layouts, refusal of detection-evasion requests. No models, no network."""
import subprocess
import wave

import numpy as np
import pytest

from nexus import project as prj
from nexus import repurpose as rp
from nexus.media import ff
from nexus.timeline import ops


def _video(path, dur=20, color=None):
    src = f"color=c={color}:s=320x180:r=30:d={dur}" if color else f"testsrc2=s=320x180:r=30:d={dur}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-f", "lavfi", "-i", f"sine=f=330:r=48000:d={dur}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _fake_tts(monkeypatch, seconds=3.0):
    calls = []

    def synth(text, out, lang="en", voice=None, length_scale=1.0, **kw):
        n = int(48000 * seconds)
        t = np.arange(n) / 48000
        y = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(y.tobytes())
        calls.append(text)
        return {"path": str(out), "duration": seconds, "engine": "test-tone", "voice": "tone", "license": "test", "commercial": True, "lufs": -18.0}

    monkeypatch.setattr(rp.tts, "synthesize", synth)
    return calls


@pytest.fixture()
def proj(home, tmp_path):
    p = prj.Project.create("rp-test")
    m = p.import_media(_video(tmp_path / "talk.mp4"))
    return p, m


def _transcript(dur=20.0):
    segs = []
    lines = ["Solar panels turn sunlight into electricity on the roof.", "A typical home system makes 30 percent of its power at noon.",
             "Batteries store the extra energy for the evening hours.", "Inverters convert direct current for the household grid.",
             "Maintenance is mostly cleaning the panels twice a year.", "The payback period is often around eight years."]
    t = 0.5
    for i, line in enumerate(lines):
        words, wt = [], t
        for w in line.split():
            words.append({"start": round(wt, 2), "end": round(wt + 0.35, 2), "word": w})
            wt += 0.4
        segs.append({"start": t, "end": round(wt, 2), "text": line, "words": words, "avg_logprob": -0.2})
        t = round(wt + 0.6, 2)
    return {"language": "en", "duration": dur, "text": " ".join(lines), "segments": segs}


# ---------------------------------------------------------------- rights gate
def test_rights_gate_blocks_undeclared_and_passes_declared(proj):
    p, m = proj
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(m["path"], 0, 5)]
    rv = rp.rights_review(p, tl)
    assert rv["stage"] == "1-rights" and not rv["passed"]
    bad = [c for c in rv["checks"] if not c["ok"]]
    assert bad and bad[0]["severity"] == "critical" and bad[0]["fix"] == "declare-rights"
    with pytest.raises(rp.RepurposeRefused, match="rights review"):
        rp.assert_exportable(p, tl)
    with pytest.raises(ValueError):
        rp.declare_rights(p, m["id"], "found-it-online")
    d = rp.declare_rights(p, m["id"], "owned", evidence="filmed by me, 2026-10-01")
    assert d["basis"] == "owned" and d["holder"] == "the user" and d["declared_at"]
    stored = prj.Project(p.id).data["media"][0]["rights"]  # persisted on the project
    assert stored["basis"] == "owned" and stored["evidence"].startswith("filmed")
    assert rp.rights_review(p, tl)["passed"]
    assert rp.assert_exportable(p, tl)["passed"]
    assert rp.credits_block(p, tl) == ["talk.mp4 - (c) the user - own footage - filmed by me, 2026-10-01"]


def test_rights_gate_unknown_media_and_licence_evidence(proj, tmp_path):
    p, m = proj
    other = _video(tmp_path / "not_imported.mp4", 3)
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(str(other), 0, 2)]
    assert not rp.rights_review(p, tl)["passed"]
    rp.declare_rights(p, m["id"], "licensed", holder="Stock Co")
    tl["clips"] = [ops.video_clip(m["path"], 0, 2)]
    rv = rp.rights_review(p, tl)
    assert not rv["passed"] and any(c["check"].startswith("licence evidence") and c["severity"] == "major" for c in rv["checks"])
    rp.declare_rights(p, m["id"], "licensed", evidence="licence #A-123", holder="Stock Co")
    assert rp.rights_review(p, tl)["passed"]
    assert prj.Project(p.id).data["media"][0]["rights"]["previous"]["holder"] == "Stock Co"


# ---------------------------------------------------------------- transformation report
def _hand_built(src):
    tl = prj.new_timeline(320, 180)
    card = ops.color_clip(2.0, text=[{"text": "Title", "style": "title"}])
    a = ops.video_clip(src, 0.0, 10.0)                                 # 2..12  original audio
    f = ops.image_clip("/x/freeze.png", 4.0, derived_from=src, composited=True)  # 12..16 freeze with PiP
    b = ops.video_clip(src, 20.0, 30.0, mute=True)                     # 16..26 muted under narration
    tl["clips"] = [card, a, f, b]
    tl["audio"] = [{"src": "/x/n1.wav", "start": 2.0, "duration": 3.0, "role": "narration"},
                   {"src": "/x/n2.wav", "start": 12.0, "duration": 4.0, "role": "narration"},
                   {"src": "/x/n3.wav", "start": 16.0, "duration": 10.0, "role": "narration"},
                   {"src": "/x/whoosh.wav", "start": 1.9, "duration": 0.5, "role": "sfx"}]
    tl["repurpose"] = {"layout": "test", "sources": {src: {"media_id": "m0", "name": "talk.mp4", "duration": 60.0}}, "used": []}
    return tl


def test_transformation_numbers_exact():
    r = rp.transformation_report(_hand_built("/src/talk.mp4"))
    assert r["programme_s"] == 26.0
    assert r["third_party_picture_s"] == 24.0 and r["source_moving_picture_s"] == 20.0 and r["source_stills_s"] == 4.0
    assert r["original_audio_kept_s"] == 10.0 and r["original_audio_replaced_s"] == 10.0
    assert r["new_narration_s"] == 17.0 and r["commentary_share"] == round(17 / 26, 4)
    assert r["new_graphics_s"] == 6.0  # title card 2 s + composited freeze 4 s
    assert r["source_clips"] == 2 and r["longest_source_stretch_s"] == 10.0
    assert r["longest_unmodified_stretch_s"] == 7.0  # 5..12: source with its own audio, no narration, no graphics
    assert r["source_used_s"]["/src/talk.mp4"] == 20.0 and r["max_source_share"] == round(20 / 60, 4)
    assert r["commentary"]["passes"] is True
    assert "not a legal test" in r["text"] and "case by case" in r["disclaimer"]


def test_commentary_thresholds_enforced(proj):
    p, m = proj
    rp.declare_rights(p, m["id"], "commentary", holder="Original Channel", evidence="criticism of the claims at 0:05")
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(m["path"], 0, 18.0)]  # long, unmodified, 90 % of the source, no commentary
    rv = rp.rights_review(p, tl)
    failed = {c["check"] for c in rv["checks"] if not c["ok"]}
    assert not rv["passed"]
    assert {"commentary: new narration share", "commentary: share of the source used"} <= failed
    with pytest.raises(rp.RepurposeRefused):
        rp.assert_exportable(p, tl)
    tl2 = _hand_built(m["path"])
    tl2["clips"][1] = ops.video_clip(m["path"], 0.0, 40.0)  # 2..42 with only the 2..5 narration on top
    tl2["audio"] = tl2["audio"][:1]
    tl2["repurpose"]["sources"][m["path"]]["duration"] = 200.0
    r2 = rp.transformation_report(tl2)
    assert r2["longest_unmodified_stretch_s"] == 37.0 and not r2["commentary"]["stretch_ok"]
    assert any(c["check"] == "commentary thresholds are editorial" for c in rv["checks"])


# ---------------------------------------------------------------- refusal path
@pytest.mark.parametrize("text", ["mirror the clip and pitch shift it so Content ID does not catch it",
                                  "download from youtube and reupload someone else's video",
                                  "crop to avoid detection", "how do I bypass the reused content check"])
def test_refuses_detection_evasion(text):
    with pytest.raises(rp.RepurposeRefused, match="will not do this"):
        rp.check_request(text)


def test_refusal_in_layouts_and_review(proj, monkeypatch):
    p, m = proj
    _fake_tts(monkeypatch)
    rp.check_request("summarise my own lecture with new narration and section titles")  # legitimate: no exception
    with pytest.raises(rp.RepurposeRefused):
        rp.commentary_timeline(p, m["id"], {"probe": ff.probe(m["path"])}, [{"at": 5, "text": "Look here."}], mirror=True)
    with pytest.raises(rp.RepurposeRefused):
        rp.recap_timeline(p, "https://www.youtube.com/watch?v=abc", {}, use_llm=False)
    rp.declare_rights(p, m["id"], "owned")
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(m["path"], 0, 5, hflip=True)]
    rv = rp.rights_review(p, tl)
    assert not rv["passed"] and any(c["check"] == "no detection-evasion transforms" and not c["ok"] for c in rv["checks"])


# ---------------------------------------------------------------- layouts
def test_commentary_layout_meets_thresholds_and_renders(proj, monkeypatch, tmp_path):
    from nexus.timeline import render
    p, m = proj
    calls = _fake_tts(monkeypatch, 3.0)
    rp.declare_rights(p, m["id"], "commentary", holder="Original Channel")
    an = {"probe": ff.probe(m["path"]), "transcript": None}
    tl = rp.commentary_timeline(p, m["id"], an, [{"at": 5.0, "text": "Notice the claim here."}, {"at": 12.0, "text": "That number is not sourced."}])
    assert len(calls) == 4  # intro, two comments, outro
    kinds = [c["type"] for c in tl["clips"]]
    assert kinds == ["color", "video", "image", "video", "image", "color"]
    assert all(c.get("derived_from") == m["path"] and c.get("composited") for c in tl["clips"] if c["type"] == "image")
    assert len(tl["repurpose"]["used"]) == 2 and tl["subtitles"]
    rv = rp.rights_review(p, tl, {m["path"]: an})
    assert rv["passed"], rv["checks"]
    rep = rv["report"]
    assert rep["commentary"]["passes"] and rep["commentary_share"] >= 0.5 and rep["max_source_share"] <= 0.5
    st = render.render(tl, tmp_path / "c.mp4", tmp_path / "w")
    assert abs(st["duration"] - render.total_duration(tl)) < 0.3


def test_recap_layout_new_narration(proj, monkeypatch):
    p, m = proj
    _fake_tts(monkeypatch, 2.5)
    rp.declare_rights(p, m["id"], "owned")
    an = {"probe": ff.probe(m["path"]), "transcript": _transcript()}
    tl = rp.recap_timeline(p, m["id"], an, target_s=30, use_llm=False, music=False)
    vids = [c for c in tl["clips"] if c["type"] == "video"]
    assert vids and all(c.get("mute") for c in vids)
    assert tl["repurpose"]["planner"] == "deterministic fallback"
    ov = tl["repurpose"]["narration_overlap"]
    assert ov["ok"] and ov["max"] <= rp.RECAP_MAX_OVERLAP
    assert all(a["spans_cuts"] for a in tl["audio"][1:])
    assert any(c.get("text") and c["text"][0]["style"] == "kinetic" for c in tl["clips"])
    assert tl["subtitles"] and rp.rights_review(p, tl)["passed"]
    rep = rp.transformation_report(tl, {m["path"]: an})
    assert rep["original_audio_kept_s"] == 0.0 and rep["original_audio_replaced_s"] > 0
    assert rp.narration_overlap(an["transcript"]["segments"][0]["text"], an["transcript"]["text"]) == 1.0


def test_compilation_layout(proj, monkeypatch, tmp_path):
    p, m = proj
    _fake_tts(monkeypatch, 2.0)
    m2 = p.import_media(_video(tmp_path / "clip_b.mp4", 8, color="0x335577"))
    rp.declare_rights(p, m["id"], "owned")
    rp.declare_rights(p, m2["id"], "public-domain", evidence="archive.org item 123", holder="NASA")
    tl = rp.compilation_timeline(p, [{"media": m["id"], "in": 1, "out": 4, "label": "Rooftop"},
                                     {"media": m2["path"], "in": 0, "out": 3, "label": "Night sky", "note": "This one is public domain."}],
                                 "Two views of energy")
    cards = [c["text"][0]["text"] for c in tl["clips"] if c["type"] == "color"]
    assert cards[1].startswith("#1") and cards[2].startswith("#2") and cards[-1] == "Credits"
    assert len(tl["repurpose"]["sources"]) == 2 and len(tl["repurpose"]["used"]) == 2
    assert len(tl["audio"]) == 3  # intro, one item note, outro
    lines = rp.credits_block(p, tl)
    assert any("NASA" in line and "public domain" in line for line in lines)
    assert rp.rights_review(p, tl)["passed"]
