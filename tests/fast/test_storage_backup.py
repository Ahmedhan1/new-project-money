"""Acceptance tests D (storage recovery), E (provider failure) and H (zero-spend storage rules)."""
import json
import os
import shutil

import pytest

from nexus import backup, config, db, project as prj, storage


def _project(home, tmp_path, payload=b"original camera footage " * 4000):
    src = tmp_path / "clip.mp4"
    src.write_bytes(payload)
    p = prj.Project.create("Holiday film", "edit")
    m = p.import_media(src)
    tl = prj.new_timeline()
    tl["clips"].append({"id": "c1", "type": "video", "src": m["path"], "in": 0, "out": 5})
    narr = p.dir / "audio" / "en" / "scene00.wav"
    narr.parent.mkdir(parents=True)
    narr.write_bytes(b"RIFF" + os.urandom(2000))
    tl["audio"].append({"src": str(narr), "start": 0})
    p.save_timeline(tl, "first cut")
    (p.dir / "work" / "seg0.mp4").write_bytes(b"temporary render segment")   # must never be backed up
    (p.dir / "renders" / "final.mp4").write_bytes(b"export" * 100)
    return p, m


def _dest(name, root, **kw):
    return {"name": name, "provider": "local", "root": str(root), "enabled": True, **kw}


@pytest.mark.parametrize("kind", ["local", "rclone"])
def test_backend_contract(kind, tmp_path):
    if kind == "rclone" and not shutil.which("rclone"):
        pytest.skip("rclone not installed")
    b = storage.LocalBackend(tmp_path / "store") if kind == "local" else storage.RcloneBackend(str(tmp_path / "store"))
    f = tmp_path / "a.bin"
    f.write_bytes(os.urandom(70_000))
    assert b.size("x/a.bin") is None and not b.exists("x/a.bin") and b.list("x") == []
    b.put("x/a.bin", f)
    b.put("x/y/b.bin", f)
    assert b.size("x/a.bin") == 70_000 and b.sha256("x/a.bin") == storage.sha256_file(f)
    assert [o["key"] for o in b.list("x")] == ["x/a.bin", "x/y/b.bin"]
    b.get("x/a.bin", tmp_path / "back.bin")
    assert (tmp_path / "back.bin").read_bytes() == f.read_bytes()
    assert b.usage()["used_bytes"] == 140_000
    b.delete("x/a.bin")
    assert b.size("x/a.bin") is None
    with pytest.raises(storage.StorageError):
        b.get("x/a.bin", tmp_path / "nope")


def test_zero_spend_rules_for_storage(home):
    assert storage.eligibility("local")[0] and storage.eligibility("backblaze-b2")[0] and storage.eligibility("google-drive")[0]
    ok, why = storage.eligibility("cloudflare-r2")
    assert not ok and "payment method" in why                               # needs a card -> never enabled
    assert not storage.eligibility("some-new-cloud")[0]                      # unknown provider -> refused until verified
    assert not storage.eligibility("huggingface-private")[0]                 # not a general backup service
    with pytest.raises(storage.StorageBlocked):
        storage.open_destination({"name": "r2", "provider": "cloudflare-r2", "remote": "r2:bucket", "authorized": True})
    with pytest.raises(storage.StorageBlocked, match="authorized"):          # remote needs the user's explicit OK
        storage.open_destination({"name": "b2", "provider": "backblaze-b2", "remote": "b2:bucket"})
    assert storage.free_cap_bytes({"provider": "backblaze-b2"}) == 9_000_000_000   # stays under the 10 GB free tier
    assert storage.free_cap_bytes({"provider": "backblaze-b2", "limit_gb": 2}) == 2_000_000_000
    for p in storage.providers().values():                                   # every profile declares its cost facts
        assert {"free_gb", "payment_method_required", "billing_risk", "allowed_content"} <= set(p)


def test_remote_backup_requires_encryption(home, tmp_path, monkeypatch):
    _project(home, tmp_path)
    storage.save_destinations([{"name": "b2", "provider": "backblaze-b2", "remote": str(tmp_path / "fake-remote"), "authorized": True, "enabled": True}])
    out = backup.run_backup()
    assert not out["ok"] and out["results"][0]["blocked"] and "NEXUS_BACKUP_PASSPHRASE" in out["results"][0]["error"]
    assert not (tmp_path / "fake-remote").exists()                           # nothing left the machine
    if shutil.which("rclone"):
        monkeypatch.setenv("NEXUS_BACKUP_PASSPHRASE", "correct horse battery staple")
        out = backup.run_backup()
        assert out["ok"] and out["results"][0]["encrypted"]
        blob_bytes = b"".join(f.read_bytes() for f in (tmp_path / "fake-remote" / "blobs").rglob("*") if f.is_file())
        assert b"original camera footage" not in blob_bytes
        assert b"Holiday film" not in (tmp_path / "fake-remote" / "snapshots").rglob("*.json").__next__().read_bytes()


def test_backup_verify_restore_and_reopen_project(home, tmp_path):
    """Acceptance D: back up, validate checksums, restore elsewhere, reopen, check media + timeline references."""
    p, m = _project(home, tmp_path)
    storage.save_destinations([_dest("disk2", tmp_path / "disk2", tiers=backup.TIERS)])
    out = backup.run_backup()
    r = out["results"][0]
    assert out["ok"] and out["complete"] and r["verify"]["ok"] and r["verify"]["checksums_compared"] == r["new_blobs"] > 0
    b = storage.open_destination(storage.destinations()[0])
    snap = backup.load_snapshot(b, r["snapshot"], None)
    paths = {f["path"] for f in snap["files"]}
    assert "nexus.db" in paths and any("/media/" in x for x in paths) and any("/timelines/" in x for x in paths)
    assert not any("/work/" in x for x in paths)                              # temporary render files are excluded
    assert backup.verify(b)["ok"] and backup.verify(b)["checksums_compared"] == backup.verify(b)["objects"]

    # incremental + deduplicated: nothing changed except (possibly) the database snapshot
    r2 = backup.run_backup()["results"][0]
    assert r2["ok"] and r2["new_blobs"] <= 1 and r2["files"] == r["files"]

    target = tmp_path / "restored-home"
    res = backup.restore(b, target)
    assert res["ok"] and res["files_restored"] == res["files_expected"] and res["paths_rewritten"] > 0
    assert res["references"]["missing"] == [] and res["references"]["projects"] == 1 and res["references"]["media_ok"] >= 3
    with pytest.raises(storage.StorageError, match="not empty"):             # restore never overwrites
        backup.restore(b, target)
    with pytest.raises(storage.StorageError, match="live NEXUS_HOME"):
        backup.restore(b, home)

    # originals untouched, and the restored copy opens as a working NEXUS_HOME with no reference to the old one
    assert open(m["path"], "rb").read().startswith(b"original camera footage")
    home.rename(tmp_path / "old-machine-is-gone")                             # nothing may still point at the old location
    db.reset_for_tests()
    config.set_home(target)
    q = prj.Project(p.id)
    tl = q.timeline()
    assert q.name == "Holiday film" and str(target) in tl["clips"][0]["src"]
    assert open(tl["clips"][0]["src"], "rb").read() == b"original camera footage " * 4000
    assert os.path.exists(tl["audio"][0]["src"]) and os.path.exists(q.data["media"][0]["path"])


def test_corruption_and_missing_objects_are_detected(home, tmp_path):
    _project(home, tmp_path)
    storage.save_destinations([_dest("disk2", tmp_path / "disk2")])
    assert backup.run_backup()["ok"]
    b = storage.open_destination(storage.destinations()[0])
    blobs = sorted((tmp_path / "disk2" / "blobs").rglob("*"), key=lambda f: f.stat().st_size if f.is_file() else 0)
    big = blobs[-1]
    data = bytearray(big.read_bytes())
    data[100] ^= 0xFF                                                         # silent bit rot, same size
    big.write_bytes(data)
    v = backup.verify(b)
    assert not v["ok"] and any("CHECKSUM MISMATCH" in x for x in v["problems"])
    res = backup.restore(b, tmp_path / "r1")
    assert not res["ok"] and any("checksum mismatch" in x for x in res["problems"])   # a corrupt file is never restored as good
    big.unlink()
    v = backup.verify(b)
    assert not v["ok"] and any("MISSING" in x for x in v["problems"])
    # the next backup notices the object is gone and uploads it again (self-healing)
    assert backup.run_backup()["ok"] and backup.verify(b)["ok"]


def test_one_destination_down_does_not_lose_data(home, tmp_path):
    """Acceptance E: a backup destination is unavailable; the others still work and the failure is loud."""
    _project(home, tmp_path)
    storage.save_destinations([_dest("usb-drive", tmp_path / "unplugged" / "drive" / "nexus"), _dest("disk2", tmp_path / "disk2")])
    out = backup.run_backup()
    by = {r["destination"]: r for r in out["results"]}
    assert out["ok"] and not out["complete"] and out["copies"] == 1
    assert not by["usb-drive"]["ok"] and "not reachable" in by["usb-drive"]["error"]
    assert by["disk2"]["ok"] and by["disk2"]["verify"]["ok"]
    assert "FAILED on usb-drive" in out["warning"]
    assert json.loads((home / "last_backup.json").read_text())["warning"] == out["warning"]
    drill = backup.restore_drill()
    assert not drill["ok"] and {r["destination"]: r["ok"] for r in drill["results"]} == {"usb-drive": False, "disk2": True}
    # the drive comes back: it catches up on the next run, nothing was lost meanwhile
    (tmp_path / "unplugged" / "drive").mkdir(parents=True)
    out = backup.run_backup()
    assert out["complete"] and out["copies"] == 2 and backup.restore_drill()["ok"]
    # every destination down: reported as a failure, never as success
    storage.save_destinations([_dest("usb-drive", tmp_path / "gone" / "x" / "y")])
    out = backup.run_backup()
    assert not out["ok"] and "only copy" in out["warning"]


def test_free_allowance_cap_is_never_exceeded(home, tmp_path):
    """Acceptance H: when the free allowance is full, lower-priority data is skipped and reported - never billed, never silent."""
    _project(home, tmp_path, payload=os.urandom(300_000))
    storage.save_destinations([_dest("tiny", tmp_path / "tiny", limit_gb=0.0002, tiers=backup.TIERS)])   # 200 kB "free tier"
    r = backup.run_backup()["results"][0]
    assert r["ok"] and r["uploaded_bytes"] <= 200_000
    skipped = {s["tier"] for s in r["skipped_over_cap"]}
    assert "originals" in skipped and "state" not in skipped and "metadata" not in skipped   # priority order respected
    assert storage.LocalBackend(tmp_path / "tiny").usage()["used_bytes"] <= 200_000 + 4096


def test_encrypted_backup_roundtrip_and_wrong_passphrase(home, tmp_path, monkeypatch):
    p, m = _project(home, tmp_path)
    storage.save_destinations([_dest("disk2", tmp_path / "disk2", encrypt=True)])
    monkeypatch.setenv("NEXUS_BACKUP_PASSPHRASE", "open sesame")
    assert backup.run_backup()["results"][0]["encrypted"]
    b = storage.open_destination(storage.destinations()[0])
    stored = b"".join(f.read_bytes() for f in (tmp_path / "disk2").rglob("*") if f.is_file())
    assert b"original camera footage" not in stored and b"Holiday film" not in stored
    with pytest.raises(storage.StorageError, match="wrong backup passphrase"):
        backup.restore(b, tmp_path / "r-bad", pw="guess")
    with pytest.raises(storage.StorageError, match="encrypted"):
        backup.restore(b, tmp_path / "r-none", pw=None)
    res = backup.restore(b, tmp_path / "r-ok", pw="open sesame")
    assert res["ok"] and (tmp_path / "r-ok" / "projects" / p.id / "media").exists()
    assert backup.restore_drill()["ok"]


def test_prune_keeps_recent_snapshots_restorable(home, tmp_path):
    p, _ = _project(home, tmp_path)
    storage.save_destinations([_dest("disk2", tmp_path / "disk2")])
    for i in range(3):
        (p.dir / "reports" / f"note{i}.json").write_text(json.dumps({"i": i}))
        assert backup.run_backup()["ok"]
        import time
        time.sleep(1.1)
    b = storage.open_destination(storage.destinations()[0])
    out = backup.prune(b, keep=1)
    assert out["snapshots_removed"] == 2 and len(backup.list_snapshots(b)) == 1
    assert backup.verify(b)["ok"] and backup.restore(b, tmp_path / "after-prune")["ok"]
