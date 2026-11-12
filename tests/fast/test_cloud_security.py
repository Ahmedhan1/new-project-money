"""Account 1 security: owner auth, CSRF, headers, worker tokens, audit, /api/v1/health + /security/*, and recovery
(vault restart, broken key fails closed, migration 2 on a pre-cloud database, redaction).

Evidence: `python tests/fast/test_cloud_security.py` runs the recovery checks in a scratch NEXUS_HOME and writes
docs/results/account1/security_recovery.json.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nexus import config, db, vault  # noqa: E402
from nexus.cloud import audit, security  # noqa: E402

TOKEN = "owner-token-for-tests-0123456789"
REMOTE = ("203.0.113.7", 40000)
LOCAL_HOST = {"host": "127.0.0.1:8765"}


@pytest.fixture()
def app(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_VAULT_KEY", raising=False)
    monkeypatch.delenv("NEXUS_PUBLIC_ORIGIN", raising=False)
    security.reset_rate_limits()
    import nexus.api  # noqa: F401 - /api/v1/health reports its CLOUD_ROUTERS / CLOUD_MISSING
    from nexus.cloud import routes_core, routes_security
    a = FastAPI()
    a.include_router(routes_core.router)
    a.include_router(routes_security.router)

    @a.post("/api/v1/_probe", dependencies=[Depends(security.require_owner)])
    def probe():
        return {"ok": True}

    @a.get("/api/v1/_probe", dependencies=[Depends(security.require_owner)])
    def probe_get():
        return {"ok": True}

    @a.get("/studio")
    def studio():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<html><body>studio</body></html>")

    @a.post("/api/v1/_session")
    def session(body: dict, request: security.Request, response: security.Response):
        return security.open_session(request, response, body.get("token", ""))

    security.install(a)
    security.install(a)  # idempotent
    yield a
    security.reset_rate_limits()


def local(a):
    return TestClient(a, headers=LOCAL_HOST)


def remote(a, **kw):
    return TestClient(a, client=REMOTE, base_url="https://nexus.example.org", **kw)


# ---------------------------------------------------------------- loopback mode (no NEXUS_API_TOKEN)
def test_loopback_allowed_without_token(app):
    c = local(app)
    assert c.get("/api/v1/_probe").status_code == 200
    assert c.post("/api/v1/_probe").status_code == 200
    # TestClient's own host name is treated as loopback only for the TestClient address
    assert TestClient(app).get("/api/v1/_probe").status_code == 200


def test_remote_denied_without_token(app):
    assert remote(app).get("/api/v1/_probe").status_code == 401


def test_proxied_loopback_is_remote(app):
    """Codespaces port forwarding / tunnels connect from 127.0.0.1: forwarding headers make the request remote."""
    c = local(app)
    assert c.get("/api/v1/_probe", headers={"x-forwarded-for": "198.51.100.1"}).status_code == 401
    assert c.get("/api/v1/_probe", headers={"forwarded": "for=198.51.100.1"}).status_code == 401


def test_dns_rebinding_host_refused(app):
    c = TestClient(app, client=("127.0.0.1", 5000), base_url="http://evil.example.com:8765")
    assert c.get("/api/v1/_probe").status_code == 401
    c2 = TestClient(app, client=("127.0.0.1", 5000), base_url="http://localhost:8765")
    assert c2.get("/api/v1/_probe").status_code == 200


def test_loopback_cross_origin_post_refused(app):
    c = local(app)
    assert c.post("/api/v1/_probe", headers={"origin": "https://evil.example.com"}).status_code == 403
    assert c.post("/api/v1/_probe", headers={"origin": "http://127.0.0.1:8765"}).status_code == 200
    assert c.post("/api/v1/_probe", headers={"origin": "null"}).status_code == 403
    # safe methods are not CSRF-relevant
    assert c.get("/api/v1/_probe", headers={"origin": "https://evil.example.com"}).status_code == 200


# ---------------------------------------------------------------- token mode
def test_bearer_ok_and_wrong(app, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    c = remote(app)
    assert c.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.post("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get("/api/v1/_probe", headers={"authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/v1/_probe").status_code == 401
    # with a token configured, loopback is no longer a free pass
    assert local(app).get("/api/v1/_probe").status_code == 401


def test_cookie_session_and_csrf(app, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    c = remote(app)
    r = c.post("/api/v1/_session", json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["mode"] == "token"
    cookie = r.cookies.get("nexus_session") or c.cookies.get("nexus_session")
    set_cookie = r.headers["set-cookie"].lower()
    assert cookie and TOKEN not in cookie and cookie.startswith("s1.")
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "secure" in set_cookie
    assert c.get("/api/v1/_probe").status_code == 200
    # state-changing request by cookie: CSRF header required, foreign Origin refused
    assert c.post("/api/v1/_probe").status_code == 403
    assert c.post("/api/v1/_probe", headers={"x-nexus-csrf": "1"}).status_code == 200
    assert c.post("/api/v1/_probe", headers={"x-nexus-csrf": "1", "origin": "https://evil.example.com"}).status_code == 403
    assert c.post("/api/v1/_probe", headers={"x-nexus-csrf": "1", "origin": "https://nexus.example.org"}).status_code == 200
    # forged / expired cookies fail
    c.cookies.set("nexus_session", "s1.123.deadbeef")
    assert c.get("/api/v1/_probe").status_code == 401
    old = security.make_session_value(TOKEN, now=time.time() - security.SESSION_TTL_S - 10)
    assert not security.session_valid(old, TOKEN)
    # rotating the owner token ends every session
    good = security.make_session_value(TOKEN)
    assert security.session_valid(good, TOKEN) and not security.session_valid(good, TOKEN + "x")
    # wrong token at the session endpoint
    assert c.post("/api/v1/_session", json={"token": "wrong"}).status_code == 401


def test_proxy_origin_via_forwarded_host(app, monkeypatch):
    """Codespaces: Host is rewritten, the original host arrives in X-Forwarded-Host; Studio's same-origin POST works."""
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    c = TestClient(app, client=("127.0.0.1", 5000), base_url="http://localhost:8765")
    c.cookies.set("nexus_session", security.make_session_value(TOKEN))
    h = {"x-forwarded-host": "x-8765.app.github.dev", "x-forwarded-proto": "https", "x-nexus-csrf": "1",
         "origin": "https://x-8765.app.github.dev"}
    assert c.post("/api/v1/_probe", headers=h).status_code == 200
    assert c.post("/api/v1/_probe", headers={**h, "origin": "https://evil.example.com"}).status_code == 403


def test_rate_limit_after_failures(app, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    c = remote(app)
    for _ in range(security.MAX_FAILS):
        assert c.get("/api/v1/_probe", headers={"authorization": "Bearer guess-QWERTY"}).status_code == 401
    # locked out - even the right token waits out the window (brute force cannot race the counter)
    assert c.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 429
    assert c.post("/api/v1/_session", json={"token": TOKEN}).status_code == 429
    # another client is unaffected
    other = TestClient(app, client=("198.51.100.9", 1), base_url="https://nexus.example.org")
    assert other.get("/api/v1/_probe", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200
    ev = [e["event"] for e in audit.entries(100)]
    assert ev.count("auth.fail") == security.MAX_FAILS and "auth.locked" in ev
    assert all(TOKEN not in json.dumps(e) and "QWERTY" not in json.dumps(e) for e in audit.entries(100))


def test_security_headers(app):
    c = local(app)
    r = c.get("/studio")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "connect-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    api = c.get("/api/v1/_probe")
    assert api.headers["content-security-policy"].startswith("default-src 'none'")
    assert api.headers["cache-control"] == "no-store"
    assert "strict-transport-security" in remote(app).get("/api/v1/health/live").headers


# ---------------------------------------------------------------- worker tokens
def test_worker_token_hashing(app):
    tok = security.issue_worker_token("colab-1")
    assert tok.startswith("nxw_") and len(tok) > 40
    stored = vault.get("worker-token.colab-1")
    assert stored != tok and len(stored) == 64 and tok not in (config.HOME / "secrets" / "worker-token.colab-1.secret").read_text()
    assert security.verify_worker_token("colab-1", tok)
    assert not security.verify_worker_token("colab-1", tok + "x")
    assert not security.verify_worker_token("colab-2", tok)
    assert not security.verify_worker_token("../etc", tok)
    tok2 = security.issue_worker_token("colab-1")  # rotate
    assert not security.verify_worker_token("colab-1", tok) and security.verify_worker_token("colab-1", tok2)
    assert security.worker_tokens()[0]["worker_id"] == "colab-1"
    with pytest.raises(ValueError):
        security.issue_worker_token("Bad/ID")

    a = FastAPI()

    @a.get("/w")
    def w(wid: str = Depends(security.require_worker)):
        return {"worker": wid}

    c = TestClient(a, client=REMOTE)
    assert c.get("/w", headers={"x-nexus-worker": "colab-1", "authorization": f"Bearer {tok2}"}).json() == {"worker": "colab-1"}
    assert c.get("/w", headers={"x-nexus-worker": "colab-1", "authorization": f"Bearer {tok}"}).status_code == 401
    assert security.revoke_worker_token("colab-1")
    assert not security.verify_worker_token("colab-1", tok2)
    events = [e["event"] for e in audit.entries(50)]
    assert {"worker_token.issued", "worker_token.revoked", "worker.auth_fail"} <= set(events)
    assert tok2 not in json.dumps(audit.entries(50))


# ---------------------------------------------------------------- /api/v1/security + /health
def test_secrets_endpoint_never_returns_values(app):
    value = "ya29." + "A1b2C3d4" * 6
    vault.put("drive_a.refresh", value, {"kind": "oauth", "identity": "owner@example.com", "connection_id": "drive_a",
                                          "access_token": value, "note": value})
    security.issue_worker_token("gpu-box")
    r = local(app).get("/api/v1/security/secrets")
    assert r.status_code == 200
    body = r.text
    assert value not in body and "ya29." not in body
    names = {s["name"]: s for s in r.json()["secrets"]}
    assert names["drive_a.refresh"]["meta"]["identity"] == "owner@example.com"
    assert "access_token" not in names["drive_a.refresh"]["meta"] and "note" not in names["drive_a.refresh"]["meta"]
    assert names["drive_a.refresh"]["readable"] is True
    assert vault.get("worker-token.gpu-box") not in body
    assert remote(app).get("/api/v1/security/secrets").status_code == 401


def test_health_endpoint_shape(app):
    r = local(app).get("/api/v1/health")
    assert r.status_code == 200
    h = r.json()
    assert set(h) >= {"ok", "version", "routers", "db", "vault", "disk", "auth"}
    assert h["version"] == config.VERSION and h["db"] == {"ok": True, "schema": len(db.MIGRATIONS)}
    assert "routes_core" in h["routers"]["mounted"] and "routes_security" in h["routers"]["mounted"]
    assert h["vault"]["ok"] is True and h["auth"]["mode"] == "loopback"
    assert isinstance(h["disk"]["free_bytes"], int)
    assert remote(app).get("/api/v1/health").status_code == 401
    assert remote(app).get("/api/v1/health/live").json() == {"ok": True}


def test_audit_and_scan_endpoints(app):
    c = local(app)
    r = c.post("/api/v1/security/scan", json={})
    assert r.status_code == 200 and set(r.json()) == {"clean", "hits", "history"}
    assert c.post("/api/v1/security/scan", json={"extra": 1}).status_code == 422
    entries = c.get("/api/v1/security/audit?event=scan.run").json()["entries"]
    assert entries and entries[0]["event"] == "scan.run"


def test_audit_strips_secrets(app):
    secret = "GOCSPX-" + "Zz9" * 9
    vault.put("drive_b.client", secret)
    audit.record("connection.disconnect", ok=True, connection_id="drive_b", refresh_token=secret,
                 message=f"revoked {secret}", nested={"Authorization": "Bearer x", "ok": [secret]})
    audit.record("made.up.event", detail="x")
    e = audit.entries(5)
    dumped = json.dumps(e)
    assert secret not in dumped and "GOCSPX-" not in dumped
    assert e[1]["detail"]["refresh_token"] == "[removed]" and e[1]["detail"]["nested"]["Authorization"] == "[removed]"
    assert e[0]["event"] == "other" and e[0]["detail"]["event"] == "made.up.event"
    db.execute("UPDATE security_audit SET at = 1")
    assert audit.prune() >= 2 and audit.entries() == []


# ---------------------------------------------------------------- route inventory (guards B/C/D routers after merge)
# Every /api/v1 route must depend on an owner or worker check. Public exceptions need a reason here.
PUBLIC_V1 = {
    "/api/v1/contracts": "JSON Schemas only, no data",
    "/api/v1/session": "the login endpoint itself (rate limited when delegated to security.open_session)",
    "/api/v1/health/live": "liveness probe, returns only {'ok': true}",
    "/api/v1/studio/auth": "sub-agent D: tells the Studio page whether to show the login (mode only, no data)",
    "/api/v1/studio/logout": "sub-agent D: clears the session cookie (worst case of a forged request: a logout)",
}
# Sub-agent B's pull-worker endpoints (/api/v1/compute/workers/next, /workers/{id}/result) depend on its own
# `require_worker` (per-worker bearer, hash-only), which this check recognises by name.
PUBLIC_V1_PATTERNS = (r"/oauth/.*callback$",)  # Google redirects the browser here: protected by single-use state + PKCE


def _flat(routes):
    for r in routes:
        if hasattr(r, "original_router"):  # FastAPI >= 0.16x keeps included routers lazily
            yield from _flat(r.original_router.routes)
        elif hasattr(r, "routes") and not hasattr(r, "dependant"):
            yield from _flat(r.routes)
        else:
            yield r


def _deps(dependant):
    for d in getattr(dependant, "dependencies", []):
        yield d.call
        yield from _deps(d)


def unprotected_routes(app) -> list[str]:
    import re
    unprotected = []
    for r in _flat(app.routes):
        path = getattr(r, "path", "")
        if not path.startswith("/api/v1") or path in PUBLIC_V1 or any(re.search(p, path) for p in PUBLIC_V1_PATTERNS):
            continue
        names = [getattr(c, "__name__", "") for c in _deps(getattr(r, "dependant", None))]
        if not any("owner" in n or "worker" in n for n in names):
            unprotected.append(f"{sorted(getattr(r, 'methods', []) or [])} {path}")
    return unprotected


def test_every_v1_route_requires_auth(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from nexus import api
    bad = unprotected_routes(api.app)
    assert not bad, f"/api/v1 routes without require_owner/require_worker: {bad}"


def test_route_inventory_detects_unprotected(app):
    from fastapi import APIRouter
    r = APIRouter(prefix="/api/v1/x")

    @r.post("/open")
    def open_():
        return {}

    @r.get("/closed", dependencies=[Depends(security.require_owner)])
    def closed():
        return {}

    @r.get("/oauth/google/callback")
    def cb():
        return {}

    app.include_router(r)
    # the test app's /_session is deliberately public (login); /x/open is the planted unprotected route
    assert sorted(unprotected_routes(app)) == ["['POST'] /api/v1/_session", "['POST'] /api/v1/x/open"]


# ---------------------------------------------------------------- recovery
def _pre_cloud_db(path: Path, version: int) -> None:
    """A database as an older release left it: base schema + migrations up to `version`, with real rows."""
    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA)
    for steps in db.MIGRATIONS[:version]:
        for sql in steps:
            c.execute(sql)
    c.execute(f"PRAGMA user_version={version}")
    c.execute("INSERT INTO jobs(id,kind,status,created) VALUES('job_old','render','done',1.0)")
    c.execute("INSERT INTO docs(kind,id,created,data) VALUES('series','s1',1.0,'{\"id\":\"s1\"}')")
    c.commit()
    c.close()


def recovery_checks(home: Path) -> dict:
    """Run every recovery check in `home` (a scratch NEXUS_HOME). Returns {check: {ok, detail}}."""
    out: dict[str, dict] = {}
    old_home, old_key = config.HOME, os.environ.pop("NEXUS_VAULT_KEY", None)
    try:
        db.reset_for_tests()
        config.set_home(home)
        config.ensure_dirs()

        # 1. vault survives a process restart (a fresh interpreter reads what this one stored)
        vault.put("kaggle.main", "kgat-value-for-recovery-test", {"kind": "kaggle"})
        code = "import sys; from nexus import config, vault; config.set_home(sys.argv[1]); print(vault.get('kaggle.main'))"
        env = {**os.environ, "NEXUS_HOME": str(home)}
        env.pop("NEXUS_VAULT_KEY", None)
        got = subprocess.run([sys.executable, "-c", code, str(home)], capture_output=True, text=True, cwd=ROOT, env=env,
                             timeout=60).stdout.strip()
        out["vault_survives_restart"] = {"ok": got == "kgat-value-for-recovery-test", "detail": "new interpreter decrypted the stored secret"}

        # 2. corrupted key fails closed: no exception, no value, health shows it
        from nexus.cloud.routes_security import vault_status
        kf = home / "secrets" / "vault.key"
        good_key = kf.read_bytes()
        kf.write_bytes(b"not-a-fernet-key")
        try:
            v = vault.get("kaggle.main")
            red = vault.redact("text with kgat-value-for-recovery-test")
            st = vault_status()
            ok = v is None and st["ok"] is False and "kgat-value" in red  # nothing decrypts; redact must not crash
            out["corrupted_key_fails_closed"] = {"ok": ok, "detail": f"get=None, health vault={st}"}
        except Exception as e:  # noqa: BLE001
            out["corrupted_key_fails_closed"] = {"ok": False, "detail": f"raised {type(e).__name__}"}

        # 3. wrong key from the environment fails closed
        from cryptography.fernet import Fernet
        os.environ["NEXUS_VAULT_KEY"] = Fernet.generate_key().decode()
        try:
            out["wrong_env_key_fails_closed"] = {"ok": vault.get("kaggle.main") is None and vault_status()["undecryptable"] == 1,
                                                 "detail": "NEXUS_VAULT_KEY set to another key"}
        finally:
            os.environ.pop("NEXUS_VAULT_KEY", None)

        # 4. missing key: a new key is generated, old secrets stay unreadable (never mis-decrypted); restoring the
        #    backed-up key file recovers them
        kf.unlink()
        lost = vault.get("kaggle.main")
        st = vault_status()
        kf.write_bytes(good_key)
        back = vault.get("kaggle.main")
        out["missing_key_fails_closed_and_backup_restores"] = {
            "ok": lost is None and st["undecryptable"] == 1 and back == "kgat-value-for-recovery-test",
            "detail": "deleted vault.key -> get None + health undecryptable=1; restored key backup -> value back"}

        # 5. migration 2 applies to a pre-cloud database (user_version 1) and to a pre-ledger one (0); rows survive
        for ver in (1, 0):
            db.reset_for_tests()
            if config.DB_PATH.exists():
                config.DB_PATH.unlink()
            for suffix in ("-wal", "-shm"):
                Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
            _pre_cloud_db(config.DB_PATH, ver)
            db.reset_for_tests()
            tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
            need = {"cloud_connections", "remote_jobs", "remote_checkpoints", "cloud_events", "notifications", "artifacts",
                    "transfers", "scheduler_state"}
            kept = db.query("SELECT status FROM jobs WHERE id='job_old'") == [{"status": "done"}] and db.get_doc("series", "s1")
            cols = {r["name"] for r in db.query("PRAGMA table_info(jobs)")}
            ok = db.schema_version() == len(db.MIGRATIONS) and need <= tables and bool(kept) and "stages" in cols
            out[f"migration_from_v{ver}"] = {"ok": ok, "detail": f"schema {ver} -> {db.schema_version()}, cloud tables present, old rows kept"}

        # 6. redaction: a stored secret never reaches the event log
        from nexus.cloud import events
        vault.put("drive_a.refresh", "refresh-secret-value-123456", {"kind": "oauth"})
        ev = events.emit("storage.failed", "upload failed with refresh-secret-value-123456 and ghp_" + "a" * 36)
        row = db.query("SELECT message FROM cloud_events WHERE id=?", (ev.event_id,))[0]["message"]
        note = json.dumps(events.notifications())
        ok = "refresh-secret-value" not in row and "ghp_aaaa" not in row and "refresh-secret-value" not in note
        out["redaction_in_events"] = {"ok": ok, "detail": row}
    finally:
        db.reset_for_tests()
        config.set_home(old_home)
        if old_key is not None:
            os.environ["NEXUS_VAULT_KEY"] = old_key
    return out


def test_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_VAULT_KEY", raising=False)
    res = recovery_checks(tmp_path / "home")
    bad = {k: v for k, v in res.items() if not v["ok"]}
    assert not bad, bad
    assert len(res) == 7


if __name__ == "__main__":  # evidence run
    import platform
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        res = recovery_checks(Path(d) / "home")
    dest = ROOT / "docs" / "results" / "account1" / "security_recovery.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    dest.write_text(json.dumps({"ok": all(v["ok"] for v in res.values()), "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "commit": commit, "platform": platform.system(), "python": platform.python_version(),
                                "checks": res}, indent=1) + "\n", encoding="utf-8")
    print(dest, all(v["ok"] for v in res.values()))
    sys.exit(0 if all(v["ok"] for v in res.values()) else 1)


def test_alerts_fire_once_per_condition_and_label_estimates(home, monkeypatch):
    """Part 8: a storage threshold or quota warning notifies once (not on every check), and an estimated quota is never
    presented as provider-reported."""
    from nexus.cloud import alerts, events
    from nexus.contracts import QuotaStatus
    q = QuotaStatus(provider="kaggle", connection_id="kaggle_primary", resource="gpu_seconds_week", limit=1000, used=900,
                    remaining=100, source="measured")
    import nexus.cloud.compute.dispatcher as d
    monkeypatch.setattr(d, "quota_status", lambda: [q])
    first = alerts.check(now=1_000_000.0)
    assert any("kaggle" in a["title"] and "estimate" in a["body"] for a in first)
    assert alerts.check(now=1_000_100.0) == []                       # same condition: no second notification
    assert any(n["title"].startswith("kaggle") for n in events.notifications())
    assert alerts.check(now=1_000_000.0 + alerts.REPEAT_S + 1)        # repeats after the quiet period while it lasts
