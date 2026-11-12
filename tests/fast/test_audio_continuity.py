"""AUD-1: voice mastering, TTS cache, scene-to-scene continuity QA. Generated media only; no models, no network."""
import subprocess
import wave

from nexus import project as prj
from nexus.audio import master
from nexus.media import ff
from nexus.qa_continuity import continuity_review
from nexus.speech import tts
from nexus.timeline import ops, render


def _burst(path, vol):
    """Silence, a tone, a noise burst and trailing silence: a stand-in for a spoken line."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono:d=0.4",
                    "-f", "lavfi", "-i", f"sine=f=220:r=22050:d=1.5,volume={vol}", "-f", "lavfi", "-i", "anoisesrc=r=22050:d=0.6:a=0.3:c=pink",
                    "-filter_complex", f"[2]volume={vol}[n];[0][1][n]concat=n=3:v=0:a=1,apad=pad_dur=0.5", "-ar", "22050", str(path)], check=True)
    return path


def test_master_voice_matches_quiet_and_loud(home, tmp_path):
    q, lo = _burst(tmp_path / "q.wav", 0.12), _burst(tmp_path / "l.wav", 6)
    rq = master.master_voice(q, tmp_path / "qm.wav")
    rl = master.master_voice(lo, tmp_path / "lm.wav")
    assert rq["before"]["lufs"] < -35 and rl["before"]["lufs"] > -12  # really different inputs
    assert abs(rq["after"]["lufs"] - rl["after"]["lufs"]) <= 1.0
    assert abs(rq["after"]["lufs"] - (-18.0)) <= 1.0
    assert rq["after"]["true_peak"] < -1.0 and rl["after"]["true_peak"] < -1.0
    assert rq["trimmed_s"] > 0.5  # 0.4 s lead + 0.5 s tail cut to the fixed pads
    with wave.open(rq["path"], "rb") as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
    lm = master.level_match([rq["path"], rl["path"]])
    assert lm["ok"] and lm["spread_lu"] <= 1.0
    raw = master.level_match([q, lo])
    assert not raw["ok"] and raw["spread_lu"] > 20
    master.master_voice(tmp_path / "q.wav", tmp_path / "q.wav")  # in place works
    assert abs(master.measure(tmp_path / "q.wav")["lufs"] + 18) <= 1.0


def test_room_tone_is_quiet_and_cached(home):
    p = master.room_tone(2.0, seed=4)
    assert master.room_tone(2.0, seed=4) == p and ff.duration(p) == 2.0
    assert -66 < master.measure(p)["lufs"] < -55


def test_tts_cache_avoids_second_engine_call(home, tmp_path, monkeypatch):
    calls = []

    def fake_piper(text, out, voice, length_scale):
        calls.append(text)
        _burst(out, 0.5)

    monkeypatch.setattr(tts, "_piper", fake_piper)
    r1 = tts.synthesize("Hello there, this is a test line.", tmp_path / "a.wav")
    r2 = tts.synthesize("Hello there, this is a test line.", tmp_path / "b.wav")
    assert len(calls) == 1 and not r1["cached"] and r2["cached"]
    assert r2["path"] == str(tmp_path / "b.wav") and (tmp_path / "b.wav").read_bytes() == (tmp_path / "a.wav").read_bytes()
    assert set(r1) >= {"path", "duration", "engine", "voice", "license", "commercial", "lufs"}
    assert r1["engine"] == "piper" and abs(r1["lufs"] + 18) <= 1.0
    tts.synthesize("Hello there, this is a test line.", tmp_path / "c.wav", length_scale=1.2)  # different key
    r4 = tts.synthesize("Another line.", tmp_path / "d.wav", master=False)
    assert len(calls) == 3 and r4["lufs"] is None


def _src(path):
    """0-4 s dark grey, 4-8 s bright; a steady tone throughout."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=320x180:r=30:d=4",
                    "-f", "lavfi", "-i", "color=c=0xE0E0E0:s=320x180:r=30:d=4", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=8,volume=0.5",
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0,noise=alls=6:allf=t[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return str(path)


def _tl(clips):
    tl = prj.new_timeline(320, 180)
    tl["clips"] = clips
    tl["burn_subtitles"] = False
    return tl


def test_continuity_flags_level_and_brightness_jump(home, tmp_path):
    src = _src(tmp_path / "src.mp4")
    # a jump cut inside one take (source gap 0.7 s): +12 dB and dark -> bright
    bad = _tl([ops.video_clip(src, 0.5, 3.5), ops.video_clip(src, 4.2, 7.2, volume=4.0)])
    render.render(bad, tmp_path / "bad.mp4", tmp_path / "w")
    rv = continuity_review(tmp_path / "bad.mp4", bad)
    assert rv["stage"] == "3-scene-continuity" and not rv["passed"]
    failed = {c["fix"]: c for c in rv["checks"] if not c["ok"]}
    assert "level-match-cut" in failed and failed["level-match-cut"]["jump_db"] > 9
    assert "match-grade" in failed and abs(failed["match-grade"]["t"] - 3.0) < 0.05

    # with scene data saying 0-4 s and 4-8 s are different shots, the same picture jump is information, not a defect
    rs = continuity_review(tmp_path / "bad.mp4", bad, {src: {"scenes": [(0.0, 4.0), (4.0, 8.0)]}})
    assert not any(c["fix"] == "match-grade" for c in rs["checks"] if not c["ok"])
    assert any("expected jumps" in c["check"] for c in rs["checks"])

    clean = _tl([ops.video_clip(src, 0.5, 2.0), ops.video_clip(src, 2.0, 3.5)])
    render.render(clean, tmp_path / "clean.mp4", tmp_path / "w")
    rc = continuity_review(tmp_path / "clean.mp4", clean)
    assert rc["passed"], [c for c in rc["checks"] if not c["ok"]]


def test_continuity_flash_frame_and_narration_cut(home, tmp_path):
    src = _src(tmp_path / "src.mp4")
    other = _src(tmp_path / "other.mp4")
    tl = _tl([ops.video_clip(src, 0.5, 2.0), ops.video_clip(src, 2.0, 2.05), ops.video_clip(other, 0.5, 2.0)])
    render.render(tl, tmp_path / "f.mp4", tmp_path / "w")
    rv = continuity_review(tmp_path / "f.mp4", tl)
    assert any(c["fix"] == "remove-flash-frame" for c in rv["checks"] if not c["ok"])
    tl2 = _tl([ops.color_clip(1.0), ops.video_clip(other, 0.5, 2.0)])
    render.render(tl2, tmp_path / "n.mp4", tmp_path / "w")
    tl2["audio"] = [{"src": "/x/n.wav", "start": 0.2, "duration": 2.0, "role": "narration"}]  # timeline-only check
    r2 = continuity_review(tmp_path / "n.mp4", tl2)
    assert any(c["fix"] == "extend-scene" for c in r2["checks"] if not c["ok"])
    tl2["audio"][0]["spans_cuts"] = True
    assert not any(c["fix"] == "extend-scene" for c in continuity_review(tmp_path / "n.mp4", tl2)["checks"] if not c["ok"])
