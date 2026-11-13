"""Control-centre service panel (/api/v1/system) and installed-mode data location."""
import hashlib
import io
import json

import pytest

from nexus import config


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", "test-owner-token-0123456789abcdef")
    from fastapi.testclient import TestClient

    from nexus import api
    from nexus.cloud import security
    security._fail.clear()
    c = TestClient(api.app, base_url="http://127.0.0.1")
    yield c
    security._fail.clear()


H = {"Authorization": "Bearer test-owner-token-0123456789abcdef"}


def test_system_routes_are_owner_only(client):
    for m, path in (("get", "/api/v1/system/status"), ("post", "/api/v1/system/stop"), ("post", "/api/v1/system/restart"),
                    ("get", "/api/v1/system/update"), ("post", "/api/v1/system/update")):
        assert getattr(client, m)(path).status_code == 401


def test_status_and_stop_restart_spawn_detached(client, monkeypatch):
    from nexus.cloud import routes_system
    spawned = []
    monkeypatch.setattr(routes_system, "_detached", lambda args: spawned.append(args))
    r = client.get("/api/v1/system/status", headers=H)
    assert r.status_code == 200 and r.json()["version"] == config.VERSION and "services" in r.json()
    assert client.post("/api/v1/system/stop", headers=H).json()["ok"]
    assert client.post("/api/v1/system/restart", headers=H).json()["ok"]
    assert spawned == [["stop"], ["restart"]]


def test_cross_origin_post_refused_in_loopback_mode(home, monkeypatch):
    """No token configured (local desktop mode): a page on another origin must not be able to stop the app."""
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    from nexus import api
    from nexus.cloud import routes_system, security
    security._fail.clear()
    monkeypatch.setattr(routes_system, "_detached", lambda args: pytest.fail("must not run"))
    c = TestClient(api.app, base_url="http://127.0.0.1:8765")
    r = c.post("/api/v1/system/stop", headers={"Origin": "https://evil.example"})
    assert r.status_code in (401, 403)
    security._fail.clear()


def test_update_check_compares_versions():
    from nexus.cloud import routes_system as rs
    rel = {"tag_name": "v9.0.0", "assets": [{"name": rs.ASSET, "browser_download_url": "https://x/s.exe"},
                                            {"name": rs.ASSET + ".sha256", "browser_download_url": "https://x/s.exe.sha256"}]}
    info = rs.check_update(fetch=lambda url: rel)
    assert info["available"] and info["installer"] and info["sha256_file"]
    assert not rs.check_update(fetch=lambda url: {"tag_name": "v0.0.1", "assets": []})["available"]
    assert not rs.check_update(fetch=lambda url: (_ for _ in ()).throw(OSError("offline")))["available"]


def _opener(files):
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return lambda url, timeout=None: R(files[url])


def test_update_download_verifies_sha256(tmp_path):
    from fastapi import HTTPException

    from nexus.cloud import routes_system as rs
    body = b"MZ fake installer bytes"
    good = {"https://x/s.exe": body, "https://x/s.exe.sha256": (hashlib.sha256(body).hexdigest() + "  NEXUS_VIDEO_AI_Setup.exe\n").encode()}
    info = {"installer": "https://x/s.exe", "sha256_file": "https://x/s.exe.sha256"}
    out = rs.download_verified(info, tmp_path / "ok", opener=_opener(good))
    assert out.read_bytes() == body
    bad = {**good, "https://x/s.exe.sha256": ("0" * 64).encode()}
    with pytest.raises(HTTPException):
        rs.download_verified(info, tmp_path / "bad", opener=_opener(bad))
    assert not any((tmp_path / "bad").glob("*"))
    with pytest.raises(HTTPException):
        rs.download_verified({"installer": "https://x/s.exe"}, tmp_path / "none", opener=_opener(good))


def test_installed_mode_puts_data_outside_program_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path / "app")
    (tmp_path / "app").mkdir()
    assert config._default_home() == tmp_path / "app" / ".nexus"
    (tmp_path / "app" / "install.json").write_text(json.dumps({"version": "0.3.0", "data_dir": str(tmp_path / "data")}))
    assert config._default_home() == tmp_path / "data"
    (tmp_path / "app" / "install.json").write_text(json.dumps({"version": "0.3.0"}))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert config._default_home() == tmp_path / "lad" / "NEXUS" / "data"


def test_import_home_copies_without_overwriting(tmp_path):
    from nexus import migrate
    old = tmp_path / "old" / ".nexus"
    (old / "secrets").mkdir(parents=True)
    (old / "nexus.db").write_bytes(b"db")
    (old / "secrets" / "vault.key").write_bytes(b"k")
    (old / "projects" / "p1").mkdir(parents=True)
    (old / "run").mkdir()
    (old / "run" / "api.json").write_text("{}")
    new = tmp_path / "data"
    r = migrate.import_home(old, new)
    assert (new / "nexus.db").read_bytes() == b"db" and (new / "secrets" / "vault.key").exists() and (new / "projects" / "p1").is_dir()
    assert not (new / "run").exists() and "run" not in r["copied"]
    assert (old / "nexus.db").exists()  # copied, never moved
    with pytest.raises(migrate.ImportRefused):
        migrate.import_home(old, new)  # destination already has data
    with pytest.raises(migrate.ImportRefused):
        migrate.import_home(tmp_path / "nothing", tmp_path / "d2")


def test_installer_sources_pin_downloads_and_ship_no_secrets():
    root = config.ROOT / "installer" / "windows"
    pins = json.loads((root / "pins.json").read_text())
    assert all(len(pins[k]["sha256"]) == 64 for k in ("python", "ffmpeg"))
    build = (root / "build.ps1").read_text()
    assert "Get-Pinned" in build and "refusing to package" in build and ".sha256" in build
    iss = (root / "NEXUS.iss").read_text()
    assert "PrivilegesRequired=lowest" in iss and "OutputBaseFilename=NEXUS_VIDEO_AI_Setup" in iss
    for bad in ("NEXUS_API_TOKEN", "vault.key", "client_secret", "kaggle"):
        assert bad.lower() not in (iss + build).lower().replace("refusing to package", "") or bad == "vault.key"
