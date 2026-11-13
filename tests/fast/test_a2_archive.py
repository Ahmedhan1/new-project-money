"""Daily output archive (nexus/archive.py) against the mock destination: verified upload, the local final is deleted
ONLY after a checksum-verified external copy exists, failed/interrupted/corrupted uploads never lose the only copy,
crash recovery resumes without duplicates, and archived videos can be browsed and retrieved."""
import subprocess
from pathlib import Path

import pytest

from nexus import archive, db, diskman, project as prj


@pytest.fixture()
def dest(home, tmp_path):
    t = archive.MockTarget(tmp_path / "external", chunk=64 * 1024)
    archive.use_target(t)
    yield t
    archive.use_target(None)


def _render(p, name="daily_001.mp4", d=2):
    out = p.dir / "renders" / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={d}", "-f", "lavfi", "-i",
                    f"sine=f=440:d={d}:r=48000", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)],
                   check=True)
    out.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    (p.dir / "reports" / f"{out.stem}.qa.json").write_text('{"status": "PASSED_AUTOMATED_CHECKS"}', encoding="utf-8")
    return out


def test_archive_verifies_then_releases_local_copy_and_retrieves(dest):
    p = prj.Project.create("daily")
    out = _render(p)
    sha = archive.sha256_file(out)
    r = archive.archive_output(p.id, "renders/daily_001.mp4")
    assert r["verified"] and r["released"], r
    kinds = sorted(x["kind"] for x in r["records"])
    assert kinds == ["master", "project_file", "project_file", "subtitle", "thumbnail"]   # video, qa, manifest, srt, thumb
    assert not out.exists()                                                 # local final removed ...
    rec = archive.get(r["records"][0]["id"])
    assert rec["state"] == "released" and rec["verification"]["method"] == "provider_sha256"
    assert rec["release_verification"]["ok"]                                # ... after a fresh re-verification
    assert dest.find(sha)                                                   # ... and the external copy stays
    assert out.with_suffix(".srt").exists()                                 # small files stay local
    p2 = prj.Project(p.id)
    assert p2.data["archive"]["daily_001"]["state"] == "released" and p2.data["archive"]["daily_001"]["remote"]["artifact_id"]
    rows = archive.browse(p.id)
    assert len(rows) == 1 and rows[0]["state"] == "released" and not rows[0]["local_exists"] and rows[0]["thumbnail"]
    got = archive.retrieve(rec["id"])
    assert got == out and archive.sha256_file(out) == sha
    assert archive.get(rec["id"])["state"] == "verified"


def test_failed_upload_keeps_the_only_copy_and_retries(dest):
    p = prj.Project.create("fail")
    out = _render(p)
    dest.fail_store = True
    r = archive.archive_output(p.id, "renders/daily_001.mp4")
    assert not r["verified"] and not r["released"] and out.exists()
    rec = archive.get(r["records"][0]["id"])
    assert rec["state"] == "failed" and "unreachable" in rec["last_error"] and rec["next_retry"] > 0
    with pytest.raises(archive.ArchiveError, match="only a verified"):
        archive.release(rec["id"])
    assert out.exists()
    with pytest.raises(diskman.UnsafePath):                                 # the disk manager will not touch it either
        diskman.safe_delete(out)
    dest.fail_store = False
    res = archive.recover(now=rec["next_retry"] + 1)
    assert res["states"].get("verified", 0) >= 1
    assert archive.get(rec["id"])["state"] == "verified" and out.exists()   # recover never deletes


def test_interrupted_upload_resumes_without_duplicates(dest):
    p = prj.Project.create("resume")
    out = _render(p, d=4)
    rec = archive.enqueue(out, kind="master", project_id=p.id)
    dest.fail_after = out.stat().st_size // 2
    r1 = archive.upload(rec["id"])
    assert r1["state"] == "failed" and out.exists()
    part = dest.root / "transfers" / f"{rec['transfer_id']}.part"
    assert 0 < part.stat().st_size < out.stat().st_size                     # half the bytes are already there
    stores = dest.stores
    r2 = archive.recover(now=r1["next_retry"] + 1)
    assert archive.get(rec["id"])["state"] == "verified", r2
    assert dest.stores == stores                                            # resumed the same transfer, no new upload
    assert "resumed" in " ".join(e[1] for e in archive.get(rec["id"])["events"])
    assert len(dest._index()) == 1 and not part.exists()


def test_crash_after_upload_before_verify_is_recovered_without_second_entry(dest):
    p = prj.Project.create("crash")
    out = _render(p)
    rec = archive.enqueue(out, kind="master", project_id=p.id)
    dest.store(out, kind="master", name=rec["name"], project_id=p.id, transfer_id=rec["transfer_id"], mime="video/mp4", provenance={})
    rec["state"], rec["attempts"] = "uploading", 1                         # the process died here
    db.put_doc("a2_archive", rec, rec["id"])
    n = dest.stores
    archive.recover()
    r = archive.get(rec["id"])
    assert r["state"] == "verified" and dest.stores == n and len(dest._index()) == 1
    assert "already has these bytes" in " ".join(e[1] for e in r["events"])


def test_same_render_archived_twice_is_one_entry(dest):
    p = prj.Project.create("dup")
    _render(p)
    a = archive.archive_output(p.id, "renders/daily_001.mp4", release_local=False)
    n = dest.stores
    b = archive.archive_output(p.id, "renders/daily_001.mp4", release_local=False)
    assert a["records"][0]["id"] == b["records"][0]["id"]
    assert dest.stores - n <= 1                                             # only the re-written manifest (new timestamp)
    assert len([r for r in archive.records(p.id) if r["kind"] == "master"]) == 1


def test_corrupted_remote_copy_is_detected_and_reuploaded(dest):
    p = prj.Project.create("corrupt")
    out = _render(p)
    rec = archive.enqueue(out, kind="master", project_id=p.id)
    dest.corrupt_next = True
    r = archive.upload(rec["id"])
    assert r["state"] == "failed" and "verification failed" in r["last_error"] and out.exists()
    archive.recover(now=r["next_retry"] + 1)
    assert archive.get(rec["id"])["state"] == "verified"


def test_release_refused_when_not_external_or_local_changed(home, tmp_path):
    t = archive.MockTarget(tmp_path / "same", external=False)
    archive.use_target(t)
    try:
        p = prj.Project.create("same")
        out = _render(p)
        r = archive.archive_output(p.id, "renders/daily_001.mp4")
        assert r["verified"] and not r["released"] and "not external" in r["release_refused"] and out.exists()
        t._external = True
        out.write_bytes(out.read_bytes() + b"x")                            # edited after archiving
        with pytest.raises(archive.ArchiveError, match="differs"):
            archive.release(r["records"][0]["id"])
        assert out.exists()
    finally:
        archive.use_target(None)


@pytest.mark.parametrize("method", ["md5", "none"])
def test_verification_without_provider_sha256(home, tmp_path, method):
    t = archive.MockTarget(tmp_path / method, checksum=method)
    archive.use_target(t)
    try:
        p = prj.Project.create(method)
        out = _render(p)
        rec = archive.enqueue(out, kind="master", project_id=p.id)
        r = archive.upload(rec["id"])
        assert r["state"] == "verified"
        assert r["verification"]["method"] == {"md5": "provider_md5", "none": "download_sha256"}[method]
    finally:
        archive.use_target(None)


def test_retention_cannot_be_enabled_and_no_target_keeps_files(home):
    with pytest.raises(archive.ArchiveError, match="not available"):
        archive.save_settings({"retention": {"enabled": True}})
    assert archive.settings()["retention"]["enabled"] is False
    p = prj.Project.create("none")
    out = _render(p)
    try:
        import nexus.cloud.storage  # noqa: F401
        pytest.skip("Account 1 storage is present on this tree (covered by test_a2_cross_account)")
    except ImportError:
        pass
    with pytest.raises(archive.ArchiveUnavailable):
        archive.archive_output(p.id, "renders/daily_001.mp4")
    assert out.exists()
    assert all(r["state"] == "failed" for r in archive.records(p.id))
    assert archive.summary()["destination"]["name"] is None


def test_release_path_guard(dest, tmp_path):
    p = prj.Project.create("guard")
    elsewhere = tmp_path / "Documents" / "keep.mp4"
    elsewhere.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x36:r=10:d=1", str(elsewhere)], check=True)
    rec = archive.enqueue(elsewhere, kind="master", project_id=p.id)
    archive.upload(rec["id"])
    with pytest.raises(archive.ArchiveError, match="refusing"):
        archive.release(rec["id"])
    assert Path(elsewhere).exists()


def test_no_same_disk_override_exists_and_local_final_stays(home, tmp_path):
    """Regression (owner review on PR #7): a same-disk archive must never release the local final, with no setting to
    turn that off."""
    with pytest.raises(archive.ArchiveError, match="no same-disk override"):
        archive.save_settings({"allow_same_disk": True})
    assert "allow_same_disk" not in archive.settings()
    f = archive.config.HOME / "archive" / "settings.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"allow_same_disk": true}', encoding="utf-8")           # hand-edited settings file is ignored too
    assert "allow_same_disk" not in archive.settings()
    t = archive.MockTarget(tmp_path / "same", external=False)
    archive.use_target(t)
    try:
        p = prj.Project.create("override")
        out = _render(p)
        r = archive.archive_output(p.id, "renders/daily_001.mp4")
        assert r["verified"] and not r["released"] and out.exists()
        with pytest.raises(archive.ArchiveError, match="not external"):
            archive.release(r["records"][0]["id"])
        assert out.exists() and archive.get(r["records"][0]["id"])["state"] == "verified"
    finally:
        archive.use_target(None)


def test_local_root_external_compares_real_devices(home, tmp_path, monkeypatch):
    inside = archive.config.HOME / "storage" / "local"
    inside.mkdir(parents=True)
    assert archive._local_root_external("local", inside, None)[0] is False          # inside NEXUS_HOME
    outside = tmp_path / "other_folder_same_volume"
    outside.mkdir()
    final = archive.config.PROJECTS / "p" / "renders" / "x.mp4"
    ok, why = archive._local_root_external("local", outside, final)
    assert ok is False and "same disk" in why                                         # same volume: not external
    real = archive._device
    monkeypatch.setattr(archive, "_device", lambda p: 999 if str(outside) in str(p) else real(p))
    ok, why = archive._local_root_external("local", outside, final)
    assert ok is True and "another disk" in why                                        # separate volume
