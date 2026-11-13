"""Account 1 production closure: deployment profile, readiness, single instance, graceful shutdown, request limits,
duplicate session cookies, trusted-proxy rate limiting, vault-aware backup/restore, offline vault-key backup, alerts.
No network, no external account. Every secret here is a throwaway test value.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

from nexus import backup, config, storage, vault  # noqa: E402
from nexus.cloud import audit, instance, lifecycle, production, security  # noqa: E402

TOKEN = "prod-owner-token-for-tests-0123456789abcdef"
FAKE_GOOGLE = "ya" + "29." + "A" * 28            # token-shaped, built at runtime so the repository scan stays clean
PROD_ENV = ("NEXUS_ENV", "NEXUS_API_TOKEN", "NEXUS_VAULT_KEY", "NEXUS_PUBLIC_ORIGIN", "NEXUS_TRUSTED_PROXIES",
            "NEXUS_BACKUP_PASSPHRASE", "NEXUS_BIND_HOST", "NEXUS_MAX_BODY_BYTES", "NEXUS_MAX_UPLOAD_BYTES")


@pytest.fixture()
def clean_env(home, monkeypatch):
    for k in PROD_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    security.reset_rate_limits()
    yield home
    security.reset_rate_limits()
    instance.release("api")


def _offsite_destination(tmp_path):
    storage.save_destinations([{"name": "usb", "provider": "local", "root": str(tmp_path / "offsite"), "enabled": True}])


def _production_env(monkeypatch, tmp_path, key: str | None = None):
    key = key or Fernet.generate_key().decode()
    monkeypatch.setenv("NEXUS_ENV", "production")
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    monkeypatch.setenv("NEXUS_VAULT_KEY", key)
    monkeypatch.setenv("NEXUS_PUBLIC_ORIGIN", "https://nexus.example.lan")
    monkeypatch.setenv("NEXUS_TRUSTED_PROXIES", "127.0.0.1")
    _offsite_destination(tmp_path)
    return key


def _probe_app() -> FastAPI:
    a = FastAPI()

    @a.get("/api/v1/_probe", dependencies=[Depends(security.require_owner)])
    def probe():
        return {"ok": True}

    @a.post("/api/v1/_probe", dependencies=[Depends(security.require_owner)])
    def probe_post():
        return {"ok": True}
    return a


# ---------------------------------------------------------------- cookie / CSRF discrepancy (Account 2 audit)
def test_duplicate_session_cookies_are_decided_by_signature_not_parser_order(clean_env, monkeypatch):
    """httpx 0.28 keeps a server-set cookie next to a manually set one; which one a server's parser keeps differs between
    versions. The decision must not depend on that: a valid cookie next to a forged one is the owner's own session
    (accepted in both orders), forged-only duplicates are refused, and CSRF still applies to every unsafe request."""
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    c = TestClient(_probe_app(), base_url="https://nexus.example.lan")
    good = security.make_session_value(TOKEN)
    forged = "s1.%d.%s" % (int(time.time()), "0" * 64)
    for header in (f"nexus_session={good}; nexus_session={forged}", f"nexus_session={forged}; nexus_session={good}"):
        assert c.get("/api/v1/_probe", headers={"cookie": header}).status_code == 200
        assert c.post("/api/v1/_probe", headers={"cookie": header}).status_code == 403                 # no CSRF header
        assert c.post("/api/v1/_probe", headers={"cookie": header, "x-nexus-csrf": "1",
                                                 "origin": "https://evil.example.com"}).status_code == 403
        assert c.post("/api/v1/_probe", headers={"cookie": header, "x-nexus-csrf": "1"}).status_code == 200
    assert c.get("/api/v1/_probe", headers={"cookie": f"nexus_session={forged}; nexus_session=s1.1.abc"}).status_code == 401
    expired = security.make_session_value(TOKEN, now=time.time() - security.SESSION_TTL_S - 5)
    assert c.get("/api/v1/_probe", headers={"cookie": f"nexus_session={expired}"}).status_code == 401
    assert c.get("/api/v1/_probe", headers={"cookie": f"nexus_session={security.make_session_value(TOKEN + 'x')}"}).status_code == 401


def test_session_cookie_attributes(clean_env, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    a = FastAPI()

    @a.post("/api/v1/session")
    def s(body: dict, request: security.Request, response: security.Response):
        return security.open_session(request, response, body.get("token", ""))
    r = TestClient(a, base_url="https://nexus.example.lan").post("/api/v1/session", json={"token": TOKEN})
    sc = r.headers["set-cookie"].lower()
    assert r.status_code == 200 and "httponly" in sc and "secure" in sc and "samesite=strict" in sc and "path=/" in sc
    assert f"max-age={security.SESSION_TTL_S}" in sc and TOKEN.lower() not in sc                      # never the token itself


# ---------------------------------------------------------------- rate limiting behind the owner's reverse proxy
def test_rate_limit_identity_behind_trusted_proxy(clean_env, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    monkeypatch.setenv("NEXUS_TRUSTED_PROXIES", "10.0.0.2")
    a = _probe_app()
    proxy = TestClient(a, client=("10.0.0.2", 5000), base_url="https://nexus.example.lan")
    bad = {"authorization": "Bearer wrong-guess"}
    for _ in range(security.MAX_FAILS):
        assert proxy.get("/api/v1/_probe", headers={**bad, "x-forwarded-for": "203.0.113.50"}).status_code == 401
    assert proxy.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}", "x-forwarded-for": "203.0.113.50"}).status_code == 429
    # the owner, arriving through the same proxy from another address, is not locked out by the attacker
    assert proxy.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}", "x-forwarded-for": "198.51.100.7"}).status_code == 200
    # an untrusted peer cannot dodge its own counter by inventing X-Forwarded-For values
    security.reset_rate_limits()
    direct = TestClient(a, client=("203.0.113.9", 1), base_url="https://nexus.example.lan")
    for i in range(security.MAX_FAILS):
        direct.get("/api/v1/_probe", headers={**bad, "x-forwarded-for": f"192.0.2.{i}"})
    assert direct.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}", "x-forwarded-for": "192.0.2.200"}).status_code == 429


# ---------------------------------------------------------------- production profile
def test_production_gates_fail_closed_and_name_fixes_without_values(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ENV", "production")
    monkeypatch.setenv("NEXUS_API_TOKEN", "short")
    vault.put("probe", "secret-value-should-never-appear-0123")          # creates NEXUS_HOME/secrets/vault.key
    bad = {c["check"] for c in production.failing("0.0.0.0")}
    assert {"owner_token", "vault_key", "vault_key_offdisk", "vault_readable", "public_origin", "bind_loopback",
            "trusted_proxies", "backup_offsite"} <= bad
    with pytest.raises(production.ProductionConfigError) as e:
        production.enforce("0.0.0.0")
    msg = str(e.value)
    assert "fix:" in msg and "short" not in msg and "secret-value" not in msg
    # production never falls back to loopback mode, even for a direct local client
    monkeypatch.delenv("NEXUS_API_TOKEN")
    r = TestClient(_probe_app(), headers={"host": "127.0.0.1:8765"}).get("/api/v1/_probe")
    assert r.status_code == 503


def test_production_gates_pass_with_complete_configuration(clean_env, monkeypatch, tmp_path):
    key = _production_env(monkeypatch, tmp_path)
    vault.put("probe", "x" * 20)
    assert not (clean_env / "secrets" / "vault.key").exists()           # env key in use: nothing written next to the data
    assert production.failing("127.0.0.1") == []
    production.enforce("127.0.0.1")
    monkeypatch.setenv("NEXUS_VAULT_KEY", Fernet.generate_key().decode())   # wrong key: secrets no longer decrypt
    assert [c["check"] for c in production.failing("127.0.0.1")] == ["vault_readable"]
    monkeypatch.setenv("NEXUS_VAULT_KEY", key)
    monkeypatch.setenv("NEXUS_PUBLIC_ORIGIN", "http://nexus.example.lan")
    assert [c["check"] for c in production.failing("127.0.0.1")] == ["public_origin"]
    storage.save_destinations([{"name": "drive", "provider": "gdrive", "enabled": True}])
    monkeypatch.setenv("NEXUS_PUBLIC_ORIGIN", "https://nexus.example.lan")
    assert [c["check"] for c in production.failing("127.0.0.1")] == ["backup_encryption"]


def _run(code: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)


def test_production_startup_refuses_then_serves_without_docs(clean_env, monkeypatch, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in PROD_ENV}
    env.update({"NEXUS_HOME": str(clean_env), "NEXUS_EMBEDDED_WORKER": "0", "NEXUS_ENV": "production"})
    r = _run("import nexus.api", env)
    assert r.returncode != 0 and "ProductionConfigError" in r.stderr and "owner_token" in r.stderr
    _offsite_destination(tmp_path)
    env.update({"NEXUS_API_TOKEN": TOKEN, "NEXUS_VAULT_KEY": Fernet.generate_key().decode(),
                "NEXUS_PUBLIC_ORIGIN": "https://nexus.example.lan", "NEXUS_TRUSTED_PROXIES": "127.0.0.1", "NEXUS_BIND_HOST": "127.0.0.1"})
    code = ("from fastapi.testclient import TestClient; import nexus.api as a, json\n"
            "with TestClient(a.app, base_url='https://nexus.example.lan') as c:\n"
            "  h = {'authorization': 'Bearer %s'}\n"
            "  print(json.dumps({'docs': c.get('/docs', headers=h).status_code, 'openapi': c.get('/openapi.json', headers=h).status_code,"
            " 'ready': c.get('/api/v1/health/ready').status_code, 'live': c.get('/api/v1/health/live').status_code,"
            " 'health': c.get('/api/v1/health', headers=h).json()['readiness']['ready'],"
            " 'loopback_no_token': c.get('/api/v1/events').status_code}))" % TOKEN)
    r = _run(code, env)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"docs": 404, "openapi": 404, "ready": 200, "live": 200, "health": True, "loopback_no_token": 401}
    assert TOKEN not in r.stdout + r.stderr


def test_single_instance_lock_refuses_a_second_api_process(clean_env, monkeypatch, tmp_path):
    assert instance.acquire("api") and instance.acquire("api")            # re-entrant in the holder
    env = {k: v for k, v in os.environ.items() if k not in PROD_ENV}
    env["NEXUS_HOME"] = str(clean_env)
    probe = "from nexus.cloud import instance; print(instance.available('api'), instance.acquire('api'))"
    assert _run(probe, env).stdout.split() == ["False", "False"]
    _production_env(monkeypatch, tmp_path)
    assert production.failing("127.0.0.1") == []                           # the holder itself passes single_instance
    env.update({k: os.environ[k] for k in PROD_ENV if k in os.environ})
    env.update({"NEXUS_EMBEDDED_WORKER": "0", "NEXUS_BIND_HOST": "127.0.0.1"})
    r = _run("import nexus.api", env)
    assert r.returncode != 0 and "single_instance" in r.stderr
    instance.release("api")
    assert _run(probe, {**env}).stdout.split() == ["True", "True"]         # released (and a dead holder frees it too)


def test_readiness_probe_reveals_only_the_verdict(clean_env, monkeypatch, tmp_path):
    from nexus import api
    c = TestClient(api.app, base_url="https://nexus.example.lan")
    r = c.get("/api/v1/health/ready")
    assert r.status_code == 200 and r.json() == {"ready": True}
    monkeypatch.setenv("NEXUS_ENV", "production")                         # configuration now incomplete for production
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    r = c.get("/api/v1/health/ready")
    assert r.status_code == 503 and r.json() == {"ready": False}
    h = c.get("/api/v1/health", headers={"authorization": f"Bearer {TOKEN}"}).json()["readiness"]
    assert h["ready"] is False and "vault_key" in h["failing_gates"] and h["mode"] == "production"


def test_api_docs_are_owner_only_in_development(clean_env):
    from nexus import api
    remote = TestClient(api.app, client=("203.0.113.7", 4000), base_url="https://nexus.example.lan")
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert remote.get(p).status_code == 401
    local = TestClient(api.app, headers={"host": "127.0.0.1:8765"})
    assert local.get("/openapi.json").status_code == 200


# ---------------------------------------------------------------- graceful shutdown and restart detection
def test_lifecycle_audits_start_stop_and_detects_unclean_restart(clean_env):
    from nexus import api
    from nexus.cloud import alerts, events
    from nexus.cloud.compute import poller
    with TestClient(api.app, headers={"host": "127.0.0.1:8765"}) as c:
        assert (config.RUN / lifecycle.MARKER).exists()
        assert c.get("/api/v1/health/live").json() == {"ok": True}
    assert not (config.RUN / lifecycle.MARKER).exists()                  # clean stop removes the marker
    assert alerts._stop.is_set() and not poller.running()
    ev = [e["event"] for e in audit.entries(50)]
    assert "service.start" in ev and "service.stop" in ev and "service.unclean_restart" not in ev
    (config.RUN / lifecycle.MARKER).write_text(json.dumps({"pid": 999999, "since": 1.0}))   # previous process was killed
    info = lifecycle.started()
    assert info["unclean_previous_stop"] is True
    assert "service.unclean_restart" in [e["event"] for e in audit.entries(50)]
    assert any("unclean" in n["title"] for n in events.notifications())
    lifecycle.stopping()


# ---------------------------------------------------------------- request and upload limits
def test_body_and_upload_limits(clean_env, monkeypatch):
    from nexus import api
    monkeypatch.setenv("NEXUS_MAX_BODY_BYTES", "1024")
    monkeypatch.setenv("NEXUS_MAX_UPLOAD_BYTES", "4096")
    c = TestClient(api.app, headers={"host": "127.0.0.1:8765"})
    assert c.post("/api/v1/notifications/read", json={"ids": ["x" * 2000]}).status_code == 413          # declared length

    def chunks():
        for _ in range(8):
            yield b'{"ids": ["' + b"y" * 300 + b'"]}'[:300]
    assert c.post("/api/v1/notifications/read", content=chunks(), headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/api/v1/notifications/read", json={"ids": []}).status_code == 200                    # normal request unaffected
    assert c.post("/api/upload", files={"file": ("big.mp4", b"\0" * 8000, "video/mp4")}).status_code == 413
    assert c.post("/api/upload", files={"file": ("tool.exe", b"MZ" * 10, "application/octet-stream")}).status_code == 415
    assert c.post("/api/upload", files={"file": ("../../evil.sh", b"#!", "text/plain")}).status_code == 415
    assert not list((config.CACHE / "uploads").glob("**/*.mp4")) if (config.CACHE / "uploads").exists() else True


# ---------------------------------------------------------------- backup / restore with the vault, offline key
def test_backup_carries_vault_ciphertext_never_the_key(clean_env, tmp_path, monkeypatch):
    from nexus import db
    db.query("SELECT 1")                                                     # a live installation always has its database
    vault.put("gdrive_a", "refresh-token-value-for-tests-0123456789", {"kind": "oauth"})
    assert (clean_env / "secrets" / "vault.key").exists()
    key = (clean_env / "secrets" / "vault.key").read_bytes().strip()
    storage.save_destinations([{"name": "offsite", "provider": "local", "root": str(tmp_path / "dest"), "enabled": True}])
    r = backup.run_backup()
    assert r["ok"] and r["complete"]
    b = storage.open_destination(storage.destinations()[0])
    m = backup.load_snapshot(b, backup.list_snapshots(b)[-1]["id"], None)
    paths = {f["path"] for f in m["files"]}
    assert "secrets/gdrive_a.secret" in paths and not any(p.endswith("vault.key") for p in paths)
    raw = b"".join(p.read_bytes() for p in (tmp_path / "dest").rglob("*") if p.is_file())
    assert b"refresh-token-value-for-tests" not in raw and key not in raw   # the copy alone can decrypt nothing
    assert backup.verify(b)["ok"]
    # restore without the key: files restored, vault not readable (key is offline)
    monkeypatch.delenv("NEXUS_VAULT_KEY", raising=False)
    r1 = backup.restore(b, tmp_path / "r1")
    assert r1["ok"], r1
    assert r1["vault"] == {"secrets": 1, "key_available": False, "decryptable": None, "undecryptable": None,
                                         "key_file_in_restore": False}
    # restore with the offline key: every credential decrypts; with a wrong key: reported, nothing overwritten
    monkeypatch.setenv("NEXUS_VAULT_KEY", key.decode())
    r2 = backup.restore(b, tmp_path / "r2")
    assert r2["vault"]["decryptable"] == 1 and r2["vault"]["undecryptable"] == 0
    assert backup.vault_check(tmp_path / "r2", Fernet.generate_key())["undecryptable"] == 1
    from nexus.storage import StorageError
    with pytest.raises(StorageError):
        backup.restore(b, tmp_path / "r2")                                   # never overwrites existing data
    with pytest.raises(StorageError):
        backup.restore(b, clean_env)                                          # never the live NEXUS_HOME


def test_offline_vault_key_export_and_verify(clean_env, tmp_path):
    vault.put("probe", "y" * 16)
    key = (clean_env / "secrets" / "vault.key").read_bytes().strip()
    fp = vault.key_fingerprint()
    assert fp.startswith("sha256:") and key.decode() not in fp
    with pytest.raises(ValueError):
        vault.export_key(clean_env / "keycopy")                             # next to the data
    with pytest.raises(ValueError):
        vault.export_key(ROOT / "keycopy")                                  # inside the repository
    storage.save_destinations([{"name": "usb-backups", "provider": "local", "root": str(tmp_path / "bk"), "enabled": True}])
    with pytest.raises(ValueError):
        vault.export_key(tmp_path / "bk" / "keycopy")                       # inside a backup destination
    out = vault.export_key(tmp_path / "usb" / "nexus-vault.key")
    assert out["fingerprint"] == fp and key.decode() not in json.dumps(out)
    if os.name != "nt":
        assert oct((tmp_path / "usb" / "nexus-vault.key").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        vault.export_key(tmp_path / "usb" / "nexus-vault.key")              # never overwrites
    assert vault.verify_key_file(tmp_path / "usb" / "nexus-vault.key")["matches_active_key"] is True
    (tmp_path / "other.key").write_bytes(Fernet.generate_key())
    assert vault.verify_key_file(tmp_path / "other.key")["matches_active_key"] is False
    assert "vault_key.export" in [e["event"] for e in audit.entries(20)]
    assert all(key.decode() not in json.dumps(e) for e in audit.entries(20))
    env = {**os.environ, "NEXUS_HOME": str(clean_env)}
    cli = subprocess.run([sys.executable, "-m", "nexus", "vault-key", "fingerprint"], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=60)
    assert cli.returncode == 0 and fp in cli.stdout and key.decode() not in cli.stdout


# ---------------------------------------------------------------- operational alerts
def test_alerts_for_backup_auth_and_disk(clean_env, monkeypatch):
    from nexus.cloud import alerts
    now = time.time()
    (config.HOME / "last_backup.json").write_text(json.dumps(
        {"ok": False, "complete": False, "ts": now, "warning": "NO destination holds this backup",
         "results": [{"destination": "drive_b", "ok": False, "error": "token " + FAKE_GOOGLE + " expired"}]}))
    for _ in range(alerts.AUTH_FAILS_HOUR):
        audit.record("auth.fail", ok=False, client="203.0.113.5", reason="bad_bearer")
    import shutil
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=1 << 20))
    fired = alerts.check(now=now)
    titles = " | ".join(a["title"] for a in fired)
    assert "backup failed" in titles and "failed sign-ins" in titles and "disk low" in titles
    assert all(FAKE_GOOGLE not in a["body"] for a in fired)
    monkeypatch.setattr(shutil, "disk_usage", real)
    (config.HOME / "last_backup.json").write_text(json.dumps({"ok": True, "complete": True, "ts": now, "results": []}))
    alerts.check(now=now + 10)
    keys = [a["key"] for a in alerts.active(now=now + 11)]
    assert not any(k.startswith(("backup:", "disk:")) for k in keys)      # resolved conditions leave the dashboard
    (config.HOME / "last_backup.json").write_text(json.dumps({"ok": True, "complete": True, "ts": now - 72 * 3600, "results": []}))
    assert any(a["key"] == "backup:stale" for a in alerts.check(now=now + 20))


def test_service_log_rotation(clean_env):
    from nexus import launcher
    log = config.LOGS / "api.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    for gen in range(launcher.LOG_KEEP + 2):
        log.write_bytes(b"x" * 2048 + str(gen).encode())
        assert launcher.rotate_log(log, max_bytes=1024, keep=launcher.LOG_KEEP)
    kept = sorted(p.name for p in log.parent.glob("api.log*"))
    assert kept == [f"api.log.{i}" for i in range(1, launcher.LOG_KEEP + 1)]
    assert (log.parent / "api.log.1").read_bytes().endswith(str(launcher.LOG_KEEP + 1).encode())   # newest generation first
    assert not launcher.rotate_log(log.parent / "api.log.1", max_bytes=10 << 20)


def test_job_manifest_with_a_credential_is_refused(clean_env):
    """Params go to remote workers (Kaggle kernels): a token-shaped value or a stored secret is refused on submit."""
    from nexus.cloud.compute import dispatcher as dsp
    from nexus.contracts import ProductionJobManifest
    vault.put("kaggle_primary", "stored-secret-value-0123456789", {"provider": "kaggle"})
    for bad in ({"note": "gh" + "p_" + "a" * 36}, {"note": "use stored-secret-value-0123456789 here"}):
        with pytest.raises(ValueError) as e:
            dsp.submit(ProductionJobManifest(task="probe", owner_account="account1", params=bad))
        assert "stored-secret-value" not in str(e.value) and "a" * 36 not in str(e.value)
    ok = dsp.submit(ProductionJobManifest(task="probe", owner_account="account1", params={"note": "plain"}))
    assert ok["created"] is True
