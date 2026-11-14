"""Big uploads: chunks through the gateway's per-request limit, joined, then reduced to 1080p when worthwhile."""
import subprocess

from fastapi.testclient import TestClient

from nexus.media import optimize


def test_plan_keeps_small_files_and_shrinks_big_ones():
    small = {"duration": 60, "video": {"codec": "h264", "width": 1280, "height": 720}}
    assert optimize.plan(small, 50 * 2 ** 20)["reencode"] is False
    big = {"duration": 60, "video": {"codec": "hevc", "width": 3840, "height": 2160}}
    p = optimize.plan(big, 900 * 2 ** 20)
    assert p["reencode"] and p["scale"] and any("1080p" in w for w in p["why"])
    vertical = {"duration": 60, "video": {"codec": "h264", "width": 2160, "height": 3840}}
    assert optimize.plan(vertical, 900 * 2 ** 20)["scale"]
    audio = {"duration": 60, "video": None}
    assert optimize.plan(audio, 900 * 2 ** 20)["reencode"] is False


def test_chunked_upload_joins_pieces_and_rejects_gaps(home, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from nexus import api, jobs
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "2",
                    "-c:v", "libx264", str(src)], check=True)
    data = src.read_bytes()
    pieces = [data[i:i + 4000] for i in range(0, len(data), 4000)]
    c = TestClient(api.app)
    uid = "0123456789abcdef0123"
    for i, b in enumerate(pieces[:-1]):
        r = c.put(f"/api/upload/chunk?upload_id={uid}&index={i}&total={len(pieces)}&size={len(data)}", content=b)
        assert r.status_code == 200, r.text
    r = c.post("/api/upload/finish", json={"upload_id": uid, "name": "clip.mp4", "total": len(pieces), "size": len(data)})
    assert r.status_code == 409 and "missing" in r.text
    i = len(pieces) - 1
    c.put(f"/api/upload/chunk?upload_id={uid}&index={i}&total={len(pieces)}&size={len(data)}", content=pieces[-1])
    r = c.post("/api/upload/finish", json={"upload_id": uid, "name": "clip.mp4", "total": len(pieces), "size": len(data)})
    assert r.status_code == 200, r.text
    j = jobs.get(r.json()["job"])
    assert j["kind"] == "media-prepare" and j["status"] == "queued"
    assert c.put("/api/upload/chunk?upload_id=../etc&index=0&total=1&size=1", content=b"x").status_code == 400
