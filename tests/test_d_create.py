"""Test D - a text idea -> research, script, storyboard, visuals, narration, music, assembly, render, validation."""
import json
from pathlib import Path

from nexus import create
from nexus.media import ff


def test_d_create_from_idea(results):
    r = create.create_video("How vaccines train the immune system", scenes=4, name="Test D - vaccines")
    rep = r["report"]
    pre = next(s for s in rep["stages"] if s["stage"] == "1-pre-production")
    tech = next(s for s in rep["stages"] if s["stage"] == "5-technical")
    info = ff.probe(r["output"])
    stock = [c for s in rep["stages"] if s["stage"] == "2-assets" for c in s["checks"] if "CC " in c["detail"] or "public" in c["detail"].lower()]
    (results / "D_create.json").write_text(json.dumps({
        "research_sources": r["research"], "script": r["script"], "script_attempts": pre.get("attempts"),
        "pre_production": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in pre["checks"]],
        "licensed_images_used": len(stock), "credits": rep.get("credits"), "status": rep["status"], "known_issues": rep["known_issues"],
        "output": {"duration": info["duration"], "video": info["video"], "render_seconds": r["render"]["elapsed"]},
        "technical": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in tech["checks"]]}, indent=1))
    # researched from real sources, with URLs
    assert any(s.get("url") for s in r["research"]), "research must cite real sources"
    assert len(r["script"]["scenes"]) == 4 and all(len(s["narration"]) > 30 for s in r["script"]["scenes"])
    assert r["storyboard"] and Path(r["storyboard"]).exists()
    assert tech["passed"], [c for c in tech["checks"] if not c["ok"]]
    assert info["duration"] > 25 and info["audio"]
    # every visual has a provenance record (licensed stock or generated)
    assert next(s for s in rep["stages"] if s["stage"] == "2-assets")["passed"]
    import shutil
    shutil.copy(r["storyboard"], results / "D_storyboard.png")
    for i, ts in enumerate((1.0, 8.0, 20.0)):
        ff.run(["-ss", ts, "-i", r["output"], "-frames:v", "1", "-vf", "scale=640:-2", results / f"D_frame{i}.png"])  # evidence frames stay small in Git
