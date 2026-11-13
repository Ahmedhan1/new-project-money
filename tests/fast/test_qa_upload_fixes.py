"""QA-D4: an upload that does not fit, or a disk that fills during an upload, must leave no truncated media and no
orphan project, and answer HTTP 507. Each test fails on the pre-fix code (HTTP 500 + orphan project)."""
import errno
import shutil
from collections import namedtuple

import pytest


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    from nexus import api
    from nexus.cloud import security
    security.reset_rate_limits()
    return TestClient(api.app, base_url="http://127.0.0.1")


def _projects():
    from nexus import project as prj
    return {p["id"] for p in prj.Project.list()}


def test_qa_d4_upload_refused_with_507_when_it_cannot_fit(client, monkeypatch):
    from nexus import api
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(api.shutil, "disk_usage", lambda p: U(10 ** 9, 10 ** 9 - 10 ** 6, 10 ** 6))
    before = _projects()
    r = client.post("/api/upload", files={"file": ("clip.mp4", b"\x00" * 4096, "video/mp4")})
    assert r.status_code == 507 and "free" in r.json()["detail"]
    assert _projects() == before


def test_qa_d4_disk_full_during_import_leaves_nothing(client, monkeypatch):
    from nexus import config
    from nexus import project as prj
    real_copy = shutil.copy2

    def full(src, dst, *a, **k):
        with open(dst, "wb") as f:
            f.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(prj.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    monkeypatch.setattr(prj.shutil, "copy2", full)
    before = _projects()
    r = client.post("/api/upload", files={"file": ("clip.mp4", b"\x00" * 4096, "video/mp4")})
    assert r.status_code == 507
    assert _projects() == before                                   # no orphan project
    left = [p for p in config.PROJECTS.rglob("*") if p.is_file()] if config.PROJECTS.exists() else []
    assert not left, left                                          # no truncated media or .part
    assert not any((config.HOME / "tmp" / "uploads").rglob("*.mp4"))
    monkeypatch.setattr(prj.shutil, "copy2", real_copy)


def test_qa_d4_import_media_is_atomic(home, tmp_path, monkeypatch):
    from nexus import project as prj
    p = prj.Project.create("atomic")
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 1000)

    def boom(s, d, *a, **k):
        with open(d, "wb") as f:
            f.write(b"x" * 10)
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(prj.shutil, "copy2", boom)
    with pytest.raises(OSError):
        p.import_media(src)
    assert not any((p.dir / "media").iterdir()) and p.data["media"] == []


def test_upload_still_works(client):
    r = client.post("/api/upload", files={"file": ("ok.mp4", b"\x00" * 2048, "video/mp4")})
    assert r.status_code == 200 and r.json()["media"]["name"] == "ok.mp4"
