"""Account 2 integration of Agent B through the API and the job ledger: profiles with editorial fields, explainable
decisions with ids/confidence, manual overrides honoured by a re-direct, reference profiles (rights gate + structure
applied), visual-model install needs consent."""
import json
import subprocess

import pytest


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from fastapi.testclient import TestClient

    from nexus import api, workflows  # noqa: F401  (registers the job handlers)
    return TestClient(api.app)


SENTS = ["Mars has two small moons called Phobos and Deimos.", "The spacecraft reached orbit after a long cruise.",
         "Dust storms can cover the whole planet for weeks.", "Scientists mapped the volcanoes from orbit.",
         "The largest volcano is three times taller than Everest.", "Water once flowed across the surface.",
         "Today the planet is cold and dry.", "Future missions will look for signs of life."]


def _project(tmp_path):
    from nexus import project as prj
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=48", "-f", "lavfi",
                    "-i", "sine=f=220:d=48", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    p = prj.Project.create("director api", "edit")
    m = p.import_media(src)
    m["rights"] = {"basis": "owned", "holder": "test"}
    p.save()
    segs = []
    for i, s in enumerate(SENTS):
        t0 = 1.0 + i * 5.8
        ws = [{"start": round(t0 + k * 0.42, 2), "end": round(t0 + k * 0.42 + 0.35, 2), "word": w} for k, w in enumerate(s.split())]
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": s, "words": ws, "speaker": "narrator"})
    an = {"path": m["path"], "probe": {"duration": 48.0}, "duration": 48.0, "transcript": {"language": "en", "segments": segs},
          "scenes": [[0.0, 16.0], [16.0, 32.0], [32.0, 48.0]], "silences": [], "issues": []}
    (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(an))
    return p, m


def _run(job_id):
    from nexus import jobs
    j = jobs.claim("test-worker")
    assert j and j.id == job_id
    jobs.run_job(j)
    return jobs.get(job_id)


def test_profiles_expose_editorial_fields(client):
    profs = client.get("/api/director/profiles").json()
    real = {k: v for k, v in profs.items() if not v.get("alias_of")}
    assert len(real) >= 14
    sd = profs["scientific_documentary"]
    assert {"label", "hook_window_s", "shot_len_median", "pattern_interrupt_s", "cutaways_per_min", "title_style"} <= set(sd)


def test_direct_decisions_have_ids_and_a_drop_override_is_honoured(client, tmp_path):
    p, m = _project(tmp_path)
    r = client.post(f"/api/projects/{p.id}/direct", json={"profile": "scientific_documentary", "size": "320x180"}).json()
    assert _run(r["job"])["status"] == "done"
    plan = client.get(f"/api/projects/{p.id}/plan").json()
    kept = [d for d in plan["decisions"] if d["action"] == "keep" and d.get("kind") == "sentence" and d.get("id")]
    assert kept and all(d.get("confidence") is not None for d in kept)
    victim = next(d for d in kept if "volcano is three times" in (d.get("text") or "")) if any(
        "volcano is three times" in (d.get("text") or "") for d in kept) else kept[-1]
    r = client.post(f"/api/projects/{p.id}/direct", json={"profile": "scientific_documentary", "size": "320x180",
                                                          "overrides": {"drop": [victim["id"]]}}).json()
    assert _run(r["job"])["status"] == "done"
    plan2 = client.get(f"/api/projects/{p.id}/plan").json()
    kept2 = [d for d in plan2["decisions"] if d["action"] == "keep" and d.get("kind") == "sentence"]
    assert len(kept2) == len(kept) - 1 and victim["id"] not in {d.get("id") for d in kept2}
    assert victim["text"] not in {d.get("text") for d in kept2}


def test_reference_needs_rights_and_unknown_reference_is_refused(client, tmp_path):
    p, m = _project(tmp_path)
    assert client.post(f"/api/projects/{p.id}/reference", json={"media_id": m["id"], "rights_basis": "found-online"}).status_code == 422
    assert client.post(f"/api/projects/{p.id}/direct", json={"reference_profile_id": "ref_nope"}).status_code == 404
    r = client.post(f"/api/projects/{p.id}/reference", json={"media_id": m["id"], "rights_basis": "owned"}).json()
    job = _run(r["job"])
    assert job["status"] == "done", job
    refs = client.get(f"/api/projects/{p.id}/references").json()["references"]
    assert len(refs) == 1 and refs[0]["metrics"]["shot_length"]["n"] >= 1
    r = client.post(f"/api/projects/{p.id}/direct", json={"profile": "fast_social", "size": "320x180",
                                                          "reference_profile_id": refs[0]["id"]}).json()
    assert _run(r["job"])["status"] == "done"


def test_visual_install_requires_consent(client, monkeypatch):
    from nexus.media import visual
    monkeypatch.setattr(visual, "available", lambda: False)
    info = client.get("/api/visual/info").json()
    assert info["download_mb"] > 200 and "Apache-2.0" in info["licence"]
    assert client.post("/api/visual/install", json={}).status_code == 409
    assert client.post("/api/visual/install", json={"licence_accepted": True, "accepted_mb": 10}).status_code == 409
