"""Daily output archive through ACCOUNT 1's real storage layer (nexus/cloud/storage): Google Drive adapter over Account
1's protocol-faithful fake Drive (real HTTP, real resumable sessions), and Account 1's LocalProvider.

Skipped on the Account 2 branch alone; runs once Account 1 is merged (and in the local three-account check). This is
the real Account 1 code path; only the Drive server is a fake. A live Drive upload needs the owner's OAuth tokens."""
import hashlib
import json
import subprocess

import pytest

storage = pytest.importorskip("nexus.cloud.storage", reason="Account 1 storage layer not on this branch")

from nexus import archive, project as prj, vault  # noqa: E402
from nexus.cloud.storage import artifacts, oauth  # noqa: E402
from nexus.cloud.storage.fake_drive import FakeGoogle  # noqa: E402

CLIENT = {"client_id": "test-client.apps.googleusercontent.com", "client_secret": "test-secret-EXAMPLE", "type": "limited_input"}


@pytest.fixture()
def drive(home):
    g = FakeGoogle(access_ttl=3600)
    g.add_client(CLIENT["client_id"], CLIENT["client_secret"])
    base = g.start()
    oauth.set_endpoints(oauth.fake_endpoints(base))
    oauth.reset_cache()
    vault.put(oauth.CLIENT_SECRET_NAME, json.dumps(CLIENT))
    start = oauth.device_start("drive_a")
    g.approve(start["user_code"], "owner@example.com")
    d = json.loads(vault.get("gdevice_drive_a"))
    d["next_poll"] = 0
    vault.put("gdevice_drive_a", json.dumps(d))
    assert oauth.device_poll("drive_a")["status"] == "connected"
    artifacts.save_rules({"authorized_remote": ["drive_a"], "order": ["drive_a", "local"]})
    archive.save_settings({"connection_id": "drive_a"})
    archive.use_target(None)
    yield g
    g.stop()
    oauth.set_endpoints(None)
    oauth.reset_cache()


def _render(p, name="daily_001.mp4", d=3):
    out = p.dir / "renders" / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={d}", "-f", "lavfi", "-i",
                    f"sine=f=440:d={d}:r=48000", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
    return out


def _drive_masters(g):
    return [f for f in g.files.values() if f["name"].endswith(".mp4")]


def test_daily_video_archived_to_drive_verified_released_and_retrieved(drive):
    p = prj.Project.create("daily")
    out = _render(p)
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    r = archive.archive_output(p.id, "renders/daily_001.mp4")
    assert r["verified"] and r["released"], r
    rec = archive.get(r["records"][0]["id"])
    assert rec["remote"]["connection_id"] == "drive_a" and rec["remote"]["path"].startswith("NEXUS/Masters/")
    assert rec["verification"]["method"] == "provider_sha256"           # Drive's own sha256Checksum
    assert not out.exists()
    files = _drive_masters(drive)
    assert len(files) == 1 and hashlib.sha256(files[0]["data"]).hexdigest() == sha
    m = artifacts.get(rec["remote"]["artifact_id"])
    assert m.retention == "archive" and m.kind == "master"              # Account 1 retention cleanup never touches it
    assert archive.retrieve(rec["id"]) == out and hashlib.sha256(out.read_bytes()).hexdigest() == sha


def test_cut_connection_resumes_the_same_drive_session_without_duplicates(drive):
    p = prj.Project.create("cut")
    out = _render(p, d=8)
    rec = archive.enqueue(out, kind="master", project_id=p.id)
    drive.drop_next_put_after = 300_000                                 # connection dies in the middle of the upload
    drive.fail_next_puts = 50
    r1 = archive.upload(rec["id"])
    assert r1["state"] == "failed" and out.exists()
    drive.fail_next_puts = 0
    drive.drop_next_put_after = None
    archive.recover(now=r1["next_retry"] + 1)
    r2 = archive.get(rec["id"])
    assert r2["state"] == "verified", r2.get("last_error")
    assert len(_drive_masters(drive)) == 1
    assert "resumed the interrupted transfer" in " ".join(e[1] for e in r2["events"])   # same Drive session continued


def test_remote_copy_damaged_after_archive_blocks_local_deletion(drive):
    p = prj.Project.create("damaged")
    out = _render(p)
    r = archive.archive_output(p.id, "renders/daily_001.mp4", release_local=False)
    assert r["verified"]
    f = _drive_masters(drive)[0]
    f["data"] = f["data"][:-10] + b"0123456789"
    f.pop("sha256Checksum", None)
    f.pop("md5Checksum", None)
    with pytest.raises(archive.ArchiveError, match="re-verification"):
        archive.release(r["records"][0]["id"])
    assert out.exists() and archive.get(r["records"][0]["id"])["state"] == "failed"


def test_account1_local_provider_on_the_same_disk_is_not_external(home):
    archive.use_target(None)
    archive.save_settings({"connection_id": "local"})
    p = prj.Project.create("local")
    out = _render(p)
    r = archive.archive_output(p.id, "renders/daily_001.mp4")
    assert r["verified"] and not r["released"] and "not external" in r["release_refused"]
    assert out.exists()


def test_configured_local_storage_root_same_and_separate_volume(home, tmp_path, monkeypatch):
    """Account 1's LocalProvider honours NEXUS_LOCAL_STORAGE; the archive uses that real root and its device."""
    ext_root = tmp_path / "usb_drive" / "nexus"
    monkeypatch.setenv("NEXUS_LOCAL_STORAGE", str(ext_root))
    archive.use_target(None)
    archive.save_settings({"connection_id": "local"})
    p = prj.Project.create("usb")
    out = _render(p)
    r = archive.archive_output(p.id, "renders/daily_001.mp4")
    assert r["verified"] and not r["released"] and "same disk" in r["release_refused"] and out.exists()
    assert any(ext_root in f.parents for f in ext_root.rglob("*.mp4"))               # the copy went to the configured root
    real = archive._device
    monkeypatch.setattr(archive, "_device", lambda q: 4242 if str(ext_root) in str(q) else real(q))  # a second volume
    rec = archive.release(r["records"][0]["id"])
    assert rec["state"] == "released" and not out.exists()
    assert archive.retrieve(rec["id"]) == out and out.exists()
