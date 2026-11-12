"""INT-4: "Edit my video" through the API and the durable job ledger: project -> director job (plan with reasons) ->
editable timeline -> EDL -> draft render -> full render with the weighted quality score. The transcript is hand-built
(no Whisper in CI); the media is a real lavfi file."""
import json
import re
import subprocess

import pytest

SPEECH = ("Solar panels convert sunlight into electricity with silicon cells. Heat reduces their output by about twenty "
          "percent in summer. Cleaning dust off the glass restores a surprising share of the lost power. We tested the "
          "panels on our own roof for six months and measured every hour. Batteries store the midday surplus for the "
          "evening peak. That is why a small battery changes the economics of a home system.")


def _analysis(path: str, dur: float) -> dict:
    segs, t = [], 0.5
    for sent in re.split(r"(?<=[.!?])\s+", SPEECH):
        words = []
        for w in sent.split():
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": w})
            t += 0.35
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": sent, "words": words})
        t += 0.6
    assert t < dur
    return {"path": path, "probe": {"duration": dur, "video": {"width": 640, "height": 360}, "audio": {"channels": 2}},
            "scenes": [(0.0, dur)], "silences": [], "transcript": {"language": "en", "segments": segs, "text": SPEECH},
            "repetitions": [], "loudness": {"lufs": -18.0}, "black": [], "freeze": [], "issues": [], "not_available": []}


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from fastapi.testclient import TestClient

    from nexus import api
    return TestClient(api.app)


def _run(client, resp) -> dict:
    """Run the worker until the queue is empty, then return the job the POST created."""
    from nexus import jobs
    assert resp.status_code == 200, resp.text
    jid = resp.json()["job"]
    jobs.run_worker(once=True)
    return next(j for j in client.get("/api/jobs").json() if j["id"] == jid)


def test_edit_my_video_end_to_end(client, tmp_path):
    from nexus import project as prj
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=40", "-f", "lavfi",
                    "-i", "sine=f=220:d=40:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
                   check=True)
    p = prj.Project.create("flow test", "edit")
    m = p.import_media(src)
    (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(_analysis(m["path"], 40.0)))

    profiles = client.get("/api/director/profiles").json()
    assert {"documentary", "interview_natural", "short_fast", "commercial"} <= set(profiles)
    assert client.post(f"/api/projects/{p.id}/direct", json={"profile": "nope"}).status_code == 400
    bad = client.post(f"/api/projects/{p.id}/direct", json={"instructions": "mirror it so content id does not match"})
    assert bad.status_code == 422  # detection evasion is refused before any work

    r = client.post(f"/api/projects/{p.id}/direct", json={"profile": "documentary", "target_seconds": 18, "size": "640x360",
                                                          "instructions": "remove pauses", "title": "Solar at home"})
    job = _run(client, r)
    assert job["kind"] == "direct" and job["status"] == "done", job
    plan = client.get(f"/api/projects/{p.id}/plan").json()
    assert plan["decisions_total"] > 0 and all(d["reason"] for d in plan["decisions"])
    tl = client.get(f"/api/projects/{p.id}").json()["timeline"]
    assert tl and abs(tl["duration"] - 18) <= 4
    edl = client.get(f"/api/projects/{p.id}/edl").text
    assert edl.startswith("TITLE:") and "FCM: NON-DROP FRAME" in edl

    draft = _run(client, client.post(f"/api/projects/{p.id}/render", json={"draft": True}))
    assert draft["status"] == "done" and draft["result"]["draft"] is True and "quality" not in draft["result"]

    full = _run(client, client.post(f"/api/projects/{p.id}/render", json={"platform": "youtube"}))
    assert full["status"] == "done", full
    q = full["result"]["quality"]
    assert 0 <= q["score"] <= 100 and q["verdict"] in ("premium", "pass", "needs_work", "fail")
    assert q["not_measured"]  # storytelling etc. are reported as unmeasured, never as passed
    reports = client.get(f"/api/projects/{p.id}/quality").json()
    assert reports and reports[0]["score"] == q["score"] and reports[0]["checks"]
