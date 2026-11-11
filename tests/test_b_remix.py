"""Test B - several authorised videos -> understood, combined into a new narrative, sound design, multiple versions."""
import json

import fixtures
from nexus import project, qa, remix
from nexus.media import ff


def test_b_remix_multiple_videos(fx, results):
    srcs = [fixtures.make_talk(fx / "solar_talk.mp4", fixtures.SOLAR, pause_after=1, repeat=2),
            fixtures.make_talk(fx / "wind_talk.mp4", fixtures.WIND, voice="en_US-ljspeech-medium"),
            fixtures.make_talk(fx / "grid_talk.mp4", fixtures.GRID, pause_after=0)]
    before = [ff.probe(s)["size"] for s in srcs]
    p = project.Project.create("Test B - clean energy remix", "remix")
    lib = remix.build_library(p, srcs)
    assert len(lib) == 3 and all(a["transcript"]["segments"] for a in lib.values())
    versions = remix.make_versions(p, lib, "How solar, wind and smart grids work together to deliver clean power")
    plan = p.data["remix"]["plan"]
    rows = remix.compare(versions)
    out = {"plan": plan, "comparison": rows, "renders": {}}
    for name, tl in versions.items():
        stats, report = qa.render_with_qa(p, tl, name, lib)
        info = ff.probe(stats["output"])
        out["renders"][name] = {"status": report["status"], "duration": info["duration"], "video": info["video"],
                                "known_issues": report["known_issues"], "render_seconds": stats["elapsed"]}
        assert report["status"] == "PASSED_AUTOMATED_CHECKS", report["known_issues"]
        ff.run(["-ss", "9", "-i", stats["output"], "-frames:v", "1", results / f"B_{name}.png"])
    (results / "B_remix.json").write_text(json.dumps(out, indent=1))
    # a new narrative, not three files glued together: the story version uses every source and interleaves or regroups them
    a = next(r for r in rows if r["version"] == "A-story")
    assert a["sources_used"] == 3 and plan["opening"]
    # versions really differ: aspect ratio, length, grade, order
    assert len({r["size"] for r in rows}) >= 2 and len({r["grade"] for r in rows}) == 3
    assert len({tuple(r["order"]) for r in rows}) == 3
    assert versions["A-story"]["music"] and any(a_.get("role") == "sfx" for a_ in versions["A-story"]["audio"])
    # sources untouched, every version kept on its own branch
    assert [ff.probe(s)["size"] for s in srcs] == before
    assert set(versions) <= set(p.history())
