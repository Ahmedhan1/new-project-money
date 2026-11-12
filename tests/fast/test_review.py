"""REV-1: review API - speaker names/roles/merge/reassign, translated line approval, cutaway rejection."""
import json
import subprocess

import pytest


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from fastapi.testclient import TestClient

    from nexus import api
    return TestClient(api.app)


def _project(tmp_path):
    from nexus import project as prj
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=12", "-f", "lavfi",
                    "-i", "sine=f=220:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    p = prj.Project.create("review", "edit")
    m = p.import_media(src)
    w = lambda t0, txt: [{"start": t0 + i * 0.4, "end": t0 + i * 0.4 + 0.3, "word": x} for i, x in enumerate(txt.split())]  # noqa: E731
    segs = [{"start": 0.0, "end": 2.0, "text": "What did you find there?", "words": w(0.0, "What did you find there?"), "speaker": "interviewer"},
            {"start": 3.0, "end": 9.0, "text": "We found sand in a flat area of the cave.", "words": w(3.0, "We found sand in a flat area of the cave."), "speaker": "guest"}]
    an = {"probe": {"duration": 12.0}, "transcript": {"language": "en", "segments": segs},
          "speakers": {"available": True, "n_speakers": 2, "segment_labels": ["S1", "S2"],
                       "turns": [{"start": 0.0, "end": 2.0, "speaker": "S1"}, {"start": 3.0, "end": 9.0, "speaker": "S2"}],
                       "speakers": {"S1": {"role": "interviewer", "talk_share": 0.2}, "S2": {"role": "guest", "talk_share": 0.8}}}}
    (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(an))
    tl = {"version": 1, "width": 320, "height": 180, "fps": 25, "lang": "en", "grade": "none", "audio": [], "music": None, "markers": [],
          "clips": [{"id": "c1", "type": "video", "src": m["path"], "in": 0.0, "out": 2.5},
                    {"id": "c2", "type": "video", "src": m["path"], "in": 3.0, "out": 9.5,
                     "pip": {"src": "x.mp4", "in": 0, "full": True, "start": 1, "end": 3,
                             "provenance": {"kind": "footage", "source": "NASA Image and Video Library", "title": "Cave", "id": "nasa:1"}}}],
          "credits": ["NASA (JSC) - Cave"], "subtitles": [{"start": 0.0, "end": 2.0, "text": "Was habt ihr gefunden?"},
                                                         {"start": 3.0, "end": 9.0, "text": "Wir fanden Sand in der Wohnung."}]}
    p.save_timeline(tl, "test", "main")
    p.save_timeline({**tl, "lang": "de"}, "de", "de")
    return p, m


def test_speaker_name_gives_a_lower_third_and_roles_never_appear(client, tmp_path):
    p, m = _project(tmp_path)
    rows = client.get(f"/api/projects/{p.id}/review/speakers").json()
    assert {r["speaker"]: r["role"] for r in rows} == {"S1": "interviewer", "S2": "guest"}
    assert client.post(f"/api/projects/{p.id}/review/names", json={"branch": "main"}).json()["added"] == []   # no names yet
    r = client.post(f"/api/projects/{p.id}/review/speakers", json={"media": m["id"], "op": "name", "speaker": "S2", "value": "Dr. Ada Example"})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/projects/{p.id}/review/names", json={"branch": "main"}).json()["added"] == ["Dr. Ada Example"]
    from nexus import project as prj
    tl = prj.Project(p.id).timeline("main")
    lts = [(c["id"], t["text"]) for c in tl["clips"] for t in c.get("text", []) if t.get("style") == "lower_third"]
    assert lts == [("c2", "Dr. Ada Example")]
    bad = client.post(f"/api/projects/{p.id}/review/speakers", json={"media": m["id"], "op": "rename", "speaker": "S2", "value": "boss"})
    assert bad.status_code == 400


def test_reassigning_a_turn_relabels_its_segments_and_merge_keeps_one_voice(client, tmp_path):
    p, m = _project(tmp_path)
    assert client.post(f"/api/projects/{p.id}/review/speakers", json={"media": m["id"], "op": "reassign", "speaker": "S2", "value": "0"}).status_code == 200
    an = json.loads((p.dir / "reports" / f"analysis_{m['id']}.json").read_text())
    assert an["transcript"]["segments"][0]["speaker"] == "guest" and an["speakers"]["edits"][-1]["op"] == "reassign"
    assert an["speakers"]["auto"]["segment_labels"] == ["S1", "S2"]                     # the automatic result is kept
    assert client.post(f"/api/projects/{p.id}/review/speakers", json={"media": m["id"], "op": "merge", "speaker": "S2", "value": "S1"}).status_code == 200
    assert [r["speaker"] for r in client.get(f"/api/projects/{p.id}/review/speakers").json()] == ["S2"]


def test_line_review_edits_text_and_counts_toward_native_review(client, tmp_path):
    from nexus import publish
    p, _ = _project(tmp_path)
    r = client.post(f"/api/projects/{p.id}/review/lines", json={"branch": "de", "index": 1, "text": "Wir fanden Sand auf einer flachen Fläche.", "approved": True})
    assert r.json() == {"approved": 1, "total": 2, "native_review_done": False, "sidecars": []}
    st = client.get(f"/api/projects/{p.id}/review/lines", params={"branch": "de"}).json()
    assert st["lines"][1]["machine_text"] == "Wir fanden Sand in der Wohnung." and st["lines"][1]["edited"]
    assert client.post(f"/api/projects/{p.id}/review/lines", json={"branch": "de", "index": 5, "approved": True}).status_code == 400
    assert client.post(f"/api/projects/{p.id}/review/lines", json={"branch": "de", "index": 0, "approved": True}).json()["native_review_done"]
    from nexus import project as prj
    assert publish._reviewed(prj.Project(p.id), "de") and not publish._reviewed(prj.Project(p.id), "fr")
    client.post(f"/api/projects/{p.id}/op", json={"op": "undo", "branch": "de"})          # undo works on review edits
    assert not publish._reviewed(prj.Project(p.id), "de")


def test_rejected_cutaway_is_removed_with_its_credit_and_remembered(client, tmp_path):
    p, _ = _project(tmp_path)
    assert [c["clip"] for c in client.get(f"/api/projects/{p.id}/review/cutaways").json()] == ["c2"]
    r = client.post(f"/api/projects/{p.id}/review/cutaways/reject", json={"clip": "c2", "why": "not the same cave"})
    assert r.json() == {"rejected": "c2", "remaining": 0}
    from nexus import project as prj
    tl = prj.Project(p.id).timeline("main")
    assert "pip" not in tl["clips"][1] and tl["credits"] == [] and tl["broll_rejected"][0]["why"] == "not the same cave"
    assert client.post(f"/api/projects/{p.id}/review/cutaways/reject", json={"clip": "c2"}).status_code == 400
