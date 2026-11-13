"""Account 2 / agent A: an interrupted render (a real render subprocess killed with SIGKILL mid-way) resumes, reusing
the segments it had finished, and reports a RenderExecutionResult-shaped checkpoint."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus import contracts
from nexus.media import ff
from nexus.timeline import ops, render, resume

W, H = 320, 180
ROOT = Path(__file__).resolve().parents[2]
CHILD = ("import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[5]); from nexus import config; "
         "config.set_home(sys.argv[1]); config.ensure_dirs(); from nexus.timeline import render; "
         "render.render(json.load(open(sys.argv[2])), Path(sys.argv[3]), Path(sys.argv[4]), workers=1)")


def _src(path, d=12.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d={d}", "-f", "lavfi",
                    "-i", f"sine=f=440:d={d}:r=48000", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(path)], check=True)
    return str(path)


def test_killed_render_resumes_from_finished_segments(home, tmp_path):
    src = _src(tmp_path / "s.mp4")
    clips = [ops.video_clip(src, i, i + 1.5, effects=[{"type": "grain", "amount": 8}], grade="cinematic") for i in range(8)]
    tl = {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
          "markers": []}
    tlp = tmp_path / "tl.json"
    tlp.write_text(json.dumps(tl))
    out, work = tmp_path / "out" / "prog.mp4", tmp_path / "work"
    seg_dir = home / "cache" / "segments"
    p = subprocess.Popen([sys.executable, "-c", CHILD, str(home), str(tlp), str(out), str(work), str(ROOT)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    while time.time() - t0 < 120 and p.poll() is None:
        c0 = resume.load(work, out)  # wait for the CHECKPOINT (a segment file is renamed into place just before it is recorded)
        if c0 and len(c0.get("segments_done") or []) >= 3:
            break
        time.sleep(0.02)
    killed = p.poll() is None
    p.kill()
    p.wait()
    assert killed, f"render finished before it could be interrupted: {p.stderr.read().decode()[-500:]}"
    assert not out.exists()                                   # no truncated programme at the output path
    cp = resume.load(work, out)
    assert cp and cp["stage"] == "segments" and len(cp["segments_done"]) >= 3
    finished = {s.stem for s in seg_dir.glob("seg_*.mp4") if ".tmp" not in s.name}
    assert set(cp["segments_done"]) <= finished

    st = render.render(tl, out, work, workers=1)
    assert st["resumed"] and st["checkpoint"]["attempt"] == 2
    assert st["segments_from_cache"] >= len(cp["segments_done"])
    assert st["checkpoint"]["reused"]["segments"] == st["segments_from_cache"]
    assert st["checkpoint"]["previous"]["segments_done"] == cp["segments_done"]
    assert st["checkpoint"]["stage"] == "done" and len(st["checkpoint"]["segments_done"]) == 8
    pr = ff.probe(out)
    assert abs(float(pr["video"]["duration"]) - 12.0) < 0.1 and pr["audio"] is not None
    # a contract-valid result for Account 1's scheduler, and a retryable failure that carries the checkpoint
    ok = resume.execution_result("req_1", stats=st)
    assert contracts.validate(ok) == [] and ok["checkpoint"]["resumed"]
    bad = resume.execution_result("req_2", error=ff.FFError("ffmpeg timed out after 10s"), checkpoint=cp)
    assert contracts.validate(bad) == [] and bad["error"]["retryable"] and bad["checkpoint"]["segments_done"]
    # a completed render is not "resumed" again
    assert not render.render(tl, out, work, workers=1)["resumed"]


def test_stale_tmp_files_are_cleaned_but_fresh_ones_kept(tmp_path):
    old, new = tmp_path / "seg_a.123.tmp.mp4", tmp_path / "seg_b.456.tmp.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    import os
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert resume.clean_stale_tmp(tmp_path) == 1 and not old.exists() and new.exists()


@pytest.mark.parametrize("changed", [False, True])
def test_checkpoint_of_another_timeline_is_not_resumed(tmp_path, changed):
    tl = {"clips": [1]}
    a = resume.Checkpoint(tmp_path, tmp_path / "o.mp4", tl, False)
    a.done("segments", "seg_x", False)
    b = resume.Checkpoint(tmp_path, tmp_path / "o.mp4", {"clips": [2]} if changed else tl, False)
    assert b.data["resumed"] is (not changed)
