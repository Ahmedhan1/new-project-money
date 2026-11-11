"""Test A - upload one video: analyse, transcribe, propose, editable timeline, effects+captions, audio, export, validate."""
import json

import fixtures
from nexus import edit, project, qa
from nexus.media import analyze, ff
from nexus.timeline import ops


def test_a_edit_uploaded_video(fx, results):
    src = fixtures.make_talk(fx / "solar_talk.mp4", fixtures.SOLAR, pause_after=1, repeat=2)
    p = project.Project.create("Test A - solar talk", "edit")
    media = p.import_media(src)
    an = analyze.analyze(media["path"])
    # understood the source
    assert an["transcript"]["language"] == "en" and len(an["transcript"]["segments"]) >= 5
    assert len(an["scenes"]) >= 4, "should find the slide changes"
    assert any(b - a > 2.0 for a, b in an["silences"]), "should find the long pause"
    assert an["repetitions"], "should find the repeated take"
    assert any("quiet" in i for i in an["issues"])
    # proposal first - nothing rendered or modified yet
    prop = edit.propose(an, "tighten")
    assert prop["status"] == "awaiting approval" and prop["proposed_duration"] < prop["source_duration"] - 4
    tl = edit.build_timeline(media["path"], an, prop, title="Why Solar Won")
    p.save_timeline(tl, "accepted proposal")
    # the timeline is editable: per-scene override + undo/redo
    tl2 = ops.set_props(tl, tl["clips"][1]["id"], text=[{"text": "Solar basics", "style": "lower_third", "start": 0.3, "end": 3.0}])
    p.save_timeline(tl2, "lower third on first scene")
    assert p.undo() == tl and p.redo() == tl2
    stats, report = qa.render_with_qa(p, tl2, "edit_v1", {media["path"]: an})
    out = ff.probe(stats["output"])
    assert report["status"] == "PASSED_AUTOMATED_CHECKS", report["known_issues"]
    assert out["duration"] < an["probe"]["duration"] - 3
    assert tl2["subtitles"] and stats["sidecars"]["srt"]
    after = ff.loudness(stats["output"])
    assert abs(after["lufs"] + 14) < 1.5 and an["loudness"]["lufs"] < -22
    # originals untouched
    assert ff.probe(media["path"])["duration"] == an["probe"]["duration"]
    ff.run(["-ss", "4", "-i", stats["output"], "-frames:v", "1", results / "A_frame.png"])
    (results / "A_edit.json").write_text(json.dumps({
        "source": {"duration": an["probe"]["duration"], "lufs": an["loudness"]["lufs"], "issues": an["issues"],
                   "scenes": len(an["scenes"]), "transcript": an["transcript"]["text"]},
        "proposal": {k: prop[k] for k in ("actions", "removed", "proposed_duration")},
        "output": {"duration": out["duration"], "lufs": after["lufs"], "true_peak": after["true_peak"],
                   "video": out["video"], "render_seconds": stats["elapsed"]},
        "qa_status": report["status"], "known_issues": report["known_issues"], "corrections": report["corrections"],
        "checks": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for s in report["stages"] for c in s["checks"]],
    }, indent=1))
