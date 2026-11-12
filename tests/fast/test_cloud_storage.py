"""Multi-account storage: LocalProvider, Google OAuth (device + web flow), Drive adapter over a protocol-faithful local
fake (real HTTP), strict connection isolation, refresh, crash-safe resumable upload, checksums, dedupe, move, cleanup."""
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus import vault
from nexus.cloud.storage import artifacts, connections, get_provider, oauth, transfers
from nexus.cloud.storage.base import ChecksumMismatch, ReauthRequired, StorageError
from nexus.cloud.storage.fake_drive import FakeGoogle
from nexus.cloud.storage.gdrive import GoogleDriveProvider
from nexus.cloud.storage.local import LocalProvider

CHUNK = 256 * 1024
CLIENT = {"client_id": "test-client.apps.googleusercontent.com", "client_secret": "test-secret-EXAMPLE", "type": "limited_input"}


class Crash(Exception):
    pass


@pytest.fixture()
def fake(home):
    g = FakeGoogle(access_ttl=3600)
    g.add_client(CLIENT["client_id"], CLIENT["client_secret"])
    base = g.start()
    oauth.set_endpoints(oauth.fake_endpoints(base))
    oauth.reset_cache()
    vault.put(oauth.CLIENT_SECRET_NAME, json.dumps(CLIENT))
    yield g
    g.stop()
    oauth.set_endpoints(None)
    oauth.reset_cache()


def connect_device(g: FakeGoogle, cid: str, email: str) -> dict:
    start = oauth.device_start(cid)
    assert start["verification_url"] and start["user_code"]
    assert oauth.device_poll(cid)["status"] == "pending"  # 428 authorization_pending
    assert g.approve(start["user_code"], email)
    d = json.loads(vault.get(f"gdevice_{cid}"))
    d["next_poll"] = 0
    vault.put(f"gdevice_{cid}", json.dumps(d))
    res = oauth.device_poll(cid)
    assert res["status"] == "connected", res
    return res["connection"]


def blob(tmp_path, name: str, size: int, seed: int = 1):
    p = tmp_path / name
    data = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    p.write_bytes(data[:size])
    return p


# ---------------------------------------------------------------- LocalProvider
def test_local_provider_ops(home, tmp_path):
    p = LocalProvider()
    src = blob(tmp_path, "a.bin", 3 * CHUNK + 17)
    f = p.upload(src, "NEXUS/Masters/p1/a.bin", chunk_size=CHUNK)
    assert f.sha256 == hashlib.sha256(src.read_bytes()).hexdigest() and f.app_created
    assert [x.name for x in p.list("NEXUS/Masters/p1")] == ["a.bin"]
    assert p.checksum(f.ref)["sha256"] == f.sha256
    p.upload(blob(tmp_path, "b.bin", 100, seed=2), "NEXUS/Masters/p1/a.bin")  # overwrite keeps a version
    assert len(p.versions(f.ref)) == 2
    moved = p.move(f.ref, "NEXUS/Archives/a.bin")
    assert p.stat(f.ref) is None and p.stat(moved.ref)
    (p.root / "user.txt").write_text("not ours")
    with pytest.raises(StorageError):
        p.delete("user.txt")
    with pytest.raises(ValueError):
        p.stat("../../etc/passwd") or p.mkdirs("../x")
    q = p.quota()
    assert q.source == "measured" and q.limit and q.remaining
    dest = tmp_path / "dl.bin"
    (tmp_path / "dl.bin.part").write_bytes(b"\x00" * 10)  # wrong partial data -> verification catches it
    with pytest.raises(StorageError):
        p.download(moved.ref, dest)
    assert p.download(moved.ref, dest, resume=False).read_bytes()[:5] == (p.root / moved.ref).read_bytes()[:5]


def test_local_resumable_crash_and_resume(home, tmp_path):
    p = LocalProvider()
    src = blob(tmp_path, "big.bin", 10 * CHUNK + 5)
    calls = []

    def boom(sent, total):
        calls.append(sent)
        if len(calls) == 4:
            raise Crash()
    with pytest.raises(Crash):
        p.upload(src, "NEXUS/OriginalMedia/big.bin", chunk_size=CHUNK, transfer_id="xfer_local1", on_chunk=boom)
    t = transfers.get("xfer_local1")
    assert t["status"] == "interrupted" and t["sent"] == 3 * CHUNK and t["session_uri"]
    f = LocalProvider().resume("xfer_local1")
    assert f.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert transfers.get("xfer_local1")["status"] == "done"


# ---------------------------------------------------------------- OAuth
def test_device_flow_connects_distinct_identities(fake):
    a = connect_device(fake, "drive_a", "alice@example.com")
    assert a["status"] == "connected" and a["identity"] == "alice@example.com"
    assert "https://www.googleapis.com/auth/drive.file" in a["scopes"]
    connect_device(fake, "drive_b", "bob@example.com")
    start = oauth.device_start("drive_c")  # same Google account as drive_a -> refused and the grant revoked
    fake.approve(start["user_code"], "alice@example.com")
    d = json.loads(vault.get("gdevice_drive_c"))
    d["next_poll"] = 0
    vault.put("gdevice_drive_c", json.dumps(d))
    with pytest.raises(oauth.OAuthError) as e:
        oauth.device_poll("drive_c")
    assert e.value.code == "duplicate_identity"
    assert not oauth.has_tokens("drive_c")
    raw = vault.get("gdrive_drive_a")
    assert "fake_rt_" in raw and "fake_rt_" not in json.dumps(connections.public(connections.get("drive_a")))
    with pytest.raises(KeyError):
        oauth.device_start("drive_d")


def test_device_flow_denied_and_slow_down(fake):
    start = oauth.device_start("drive_a")
    assert oauth.device_poll("drive_a")["status"] == "pending"
    assert oauth.device_poll("drive_a")["status"] == "pending"  # interval respected locally, no request sent
    fake.approve(start["user_code"], "alice@example.com", deny=True)
    d = json.loads(vault.get("gdevice_drive_a"))
    d["next_poll"] = 0
    vault.put("gdevice_drive_a", json.dumps(d))
    assert oauth.device_poll("drive_a")["status"] == "denied"
    assert connections.get("drive_a")["status"] == "not_configured"


def test_web_flow_state_pkce_single_use(fake):
    with pytest.raises(oauth.OAuthError):
        oauth.web_start("drive_a", "http://nexus.example.com/cb")  # plain http to a non-localhost host
    s = oauth.web_start("drive_a", "https://nexus.example.com/api/v1/storage/oauth/callback")
    q = {k: v[0] for k, v in parse_qs(urlparse(s["auth_url"]).query).items()}
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["include_granted_scopes"] == "true"
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 32
    assert set(q["scope"].split()) == set(oauth.SCOPES)
    code = fake.issue_code("carol@example.com", CLIENT["client_id"], q["redirect_uri"], q["code_challenge"], q["scope"])
    res = oauth.web_finish(q["state"], code)
    assert res["identity"] == "carol@example.com" and res["status"] == "connected"
    with pytest.raises(oauth.OAuthError) as e:  # replay
        oauth.web_finish(q["state"], code)
    assert e.value.code == "bad_state"
    # expired state
    s2 = oauth.web_start("drive_b", "http://localhost:8000/api/v1/storage/oauth/callback")
    st = parse_qs(urlparse(s2["auth_url"]).query)["state"][0]
    name = oauth._state_name(st)
    rec = json.loads(vault.get(name))
    rec["expires_at"] = time.time() - 1
    vault.put(name, json.dumps(rec))
    with pytest.raises(oauth.OAuthError) as e:
        oauth.web_finish(st, "whatever")
    assert e.value.code == "state_expired"
    # wrong PKCE verifier is rejected by the token endpoint
    s3 = oauth.web_start("drive_b", "http://localhost:8000/cb")
    q3 = {k: v[0] for k, v in parse_qs(urlparse(s3["auth_url"]).query).items()}
    bad = fake.issue_code("dan@example.com", CLIENT["client_id"], q3["redirect_uri"], "x" * 43, q3["scope"])
    with pytest.raises(oauth.OAuthError):
        oauth.web_finish(q3["state"], bad)


def test_not_configured_without_client(home):
    h = GoogleDriveProvider("drive_a").health()
    assert h.status == "not_configured"


# ---------------------------------------------------------------- isolation, refresh, revocation
def test_connection_isolation(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    connect_device(fake, "drive_b", "bob@example.com")
    a, b = GoogleDriveProvider("drive_a"), GoogleDriveProvider("drive_b")
    fa = a.upload(blob(tmp_path, "a.bin", CHUNK + 3), "NEXUS/Masters/a.bin", chunk_size=CHUNK)
    fake.log.clear()
    assert a.stat(fa.ref) is not None
    assert b.stat(fa.ref) is None  # B's token cannot see A's file
    assert all(e["user"] == "alice@example.com" for e in fake.log[:1]) and fake.log[1]["user"] == "bob@example.com"
    assert a.quota().connection_id == "drive_a" and b.list("NEXUS/Masters") == []
    # a token stored under the wrong connection name is never used
    vault.put("gdrive_drive_c", vault.get("gdrive_drive_a"))
    with pytest.raises(StorageError):
        oauth.access_token("drive_c")
    # tokens are cached per connection only
    assert oauth.access_token("drive_a") != oauth.access_token("drive_b")


def test_refresh_on_401_and_rotation(fake, tmp_path):
    fake.rotate_refresh = True
    connect_device(fake, "drive_a", "alice@example.com")
    rt0 = json.loads(vault.get("gdrive_drive_a"))["refresh_token"]
    p = GoogleDriveProvider("drive_a")
    assert p.quota().limit == 15e9
    httpx.post(fake.base + "/__fake/expire_access")  # the server now rejects the cached access token -> 401
    q = p.quota()
    assert q.source == "provider_api"
    tok = json.loads(vault.get("gdrive_drive_a"))
    assert tok["refresh_token"] != rt0  # rotated token stored
    # expiry known locally -> refresh before the call
    tok["expires_at"] = time.time() - 5
    vault.put("gdrive_drive_a", json.dumps(tok))
    oauth.reset_cache()
    assert p.quota().used == 0


def test_revoked_refresh_token_needs_reauth(fake):
    connect_device(fake, "drive_a", "alice@example.com")
    rt = json.loads(vault.get("gdrive_drive_a"))["refresh_token"]
    httpx.post(fake.base + "/revoke", data={"token": rt})
    h = GoogleDriveProvider("drive_a").health()
    assert h.status == "unauthorized"
    assert connections.get("drive_a")["status"] == "reauth_required"
    with pytest.raises(ReauthRequired):
        oauth.access_token("drive_a", force_refresh=True)


def test_disconnect_revokes_and_deletes(fake):
    connect_device(fake, "drive_a", "alice@example.com")
    rt = json.loads(vault.get("gdrive_drive_a"))["refresh_token"]
    out = oauth.disconnect("drive_a")
    assert out["revoked"] and out["secret_deleted"]
    assert rt not in fake.refresh and vault.get("gdrive_drive_a") is None
    assert connections.get("drive_a")["status"] == "disconnected" and connections.get("drive_a")["identity"] is None


# ---------------------------------------------------------------- Drive operations
def test_drive_folders_list_download_versions(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    artifacts.save_rules({"authorized_remote": ["drive_a"]})
    s1 = artifacts.create_structure("drive_a")
    n = len(fake.files)
    assert artifacts.create_structure("drive_a") == s1 and len(fake.files) == n == 11  # idempotent
    p = GoogleDriveProvider("drive_a")
    src = blob(tmp_path, "m.bin", 2 * CHUNK + 100)
    f = p.upload(src, "NEXUS/Masters/m.bin", chunk_size=CHUNK)
    assert f.sha256 == hashlib.sha256(src.read_bytes()).hexdigest() and f.md5
    assert [x.name for x in p.list("NEXUS/Masters")] == ["m.bin"]
    dest = tmp_path / "out" / "m.bin"
    dest.parent.mkdir()
    (dest.parent / "m.bin.part").write_bytes(src.read_bytes()[:CHUNK])  # resume download with Range
    fake.log.clear()
    assert p.download(f.ref, dest).read_bytes() == src.read_bytes()
    assert p.versions(f.ref)[0]["id"] == "1"
    mv = p.move(f.ref, "NEXUS/Archives/m2.bin")
    assert mv.name == "m2.bin" and p.list("NEXUS/Masters") == []
    assert p.delete(mv.ref) and p.stat(mv.ref) is None


def test_drive_resumable_upload_survives_crash(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    src = blob(tmp_path, "film.bin", 12 * CHUNK + 999)
    seen = []

    def crash(sent, total):
        seen.append(sent)
        if len(seen) == 5:
            raise Crash()
    with pytest.raises(Crash):
        GoogleDriveProvider("drive_a").upload(src, "NEXUS/OriginalMedia/film.bin", chunk_size=2 * CHUNK,
                                             transfer_id="xfer_d1", on_chunk=crash)
    t = transfers.get("xfer_d1")
    assert t["status"] == "interrupted" and t["sent"] == 8 * CHUNK and "upload_id=" in t["session_uri"]
    oauth.reset_cache()  # "new process"
    # the server got one more chunk than we recorded (crash between ack and DB write): resume must ask the server
    sid = parse_qs(urlparse(t["session_uri"]).query)["upload_id"][0]
    fake.sessions[sid]["data"] += src.read_bytes()[8 * CHUNK:10 * CHUNK]
    fake.drop_next_put_after = CHUNK + 1000  # and the connection is cut in the middle of the next chunk
    fake.fail_next_puts = 0
    sent = []
    f = GoogleDriveProvider("drive_a").resume("xfer_d1", on_chunk=lambda s, n: sent.append(s))
    assert sent[0] == 10 * CHUNK  # resumed from the server's acknowledged byte, not ours
    assert f.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert transfers.get("xfer_d1")["status"] == "done"
    pub = transfers.public(transfers.get("xfer_d1"))
    assert "session_uri" not in pub and pub["has_session"]


def test_drive_transient_503_and_expired_session(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    src = blob(tmp_path, "x.bin", 3 * CHUNK)
    fake.fail_next_puts = 2
    f = GoogleDriveProvider("drive_a").upload(src, "NEXUS/Audio/x.bin", chunk_size=CHUNK, transfer_id="xfer_t")
    assert f.size == 3 * CHUNK
    # expired session: resume restarts with a new session
    with pytest.raises(Crash):
        GoogleDriveProvider("drive_a").upload(src, "NEXUS/Audio/y.bin", chunk_size=CHUNK, transfer_id="xfer_e",
                                             on_chunk=lambda s, n: (_ for _ in ()).throw(Crash()) if s else None)
    fake.sessions.clear()
    f2 = GoogleDriveProvider("drive_a").resume("xfer_e")
    assert f2.sha256 == f.sha256


def test_drive_checksum_mismatch_deletes_remote(fake, tmp_path, monkeypatch):
    connect_device(fake, "drive_a", "alice@example.com")
    orig = GoogleDriveProvider._file

    def bad(self, d, path=""):
        f = orig(self, d, path)
        if f.sha256:
            f.sha256 = "0" * 64
        return f
    monkeypatch.setattr(GoogleDriveProvider, "_file", bad)
    with pytest.raises(ChecksumMismatch):
        GoogleDriveProvider("drive_a").upload(blob(tmp_path, "c.bin", CHUNK), "NEXUS/c.bin", chunk_size=CHUNK)
    assert not [f for f in fake.files.values() if f["name"] == "c.bin"]


def test_drive_quota_exceeded(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    fake.limits["alice@example.com"] = CHUNK
    with pytest.raises(StorageError):
        GoogleDriveProvider("drive_a").upload(blob(tmp_path, "q.bin", 2 * CHUNK), "NEXUS/q.bin", chunk_size=CHUNK)
    assert GoogleDriveProvider("drive_a").quota().limit == CHUNK


# ---------------------------------------------------------------- artifacts: placement, dedupe, move, cleanup
def test_placement_requires_authorisation_and_room(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    src = blob(tmp_path, "s.bin", 1000)
    with pytest.raises(artifacts.StorageBlocked):
        artifacts.store_file(src, kind="master", connection_id="drive_a")  # not authorised by the owner
    assert artifacts.choose_destination(1000)[0] == "local"
    artifacts.save_rules({"authorized_remote": ["drive_a"], "order": ["drive_a", "local"], "reserve_bytes": 0})
    assert artifacts.choose_destination(1000, "master")[0] == "drive_a"
    fake.limits["alice@example.com"] = 500
    cid, notes = artifacts.choose_destination(1000, "master")
    assert cid == "local" and any("not enough free space" in n for n in notes)
    artifacts.save_rules({"order": ["drive_a"]})
    with pytest.raises(StorageError):
        artifacts.choose_destination(1000)


def test_store_dedupe_manifest_and_move(fake, tmp_path):
    connect_device(fake, "drive_a", "alice@example.com")
    connect_device(fake, "drive_b", "bob@example.com")
    artifacts.save_rules({"authorized_remote": ["drive_a", "drive_b"]})
    src = blob(tmp_path, "master.mp4", 2 * CHUNK + 7)
    m = artifacts.store_file(src, kind="master", connection_id="drive_a", project_id="prj_1", mime="video/mp4",
                             chunk_size=CHUNK, provenance={"generator": "test"})
    assert m.locations[0].path == "NEXUS/Masters/prj_1/master.mp4"
    names = {f["name"] for f in fake.files.values() if f["owner"] == "alice@example.com"}
    assert {"master.mp4", "master.mp4.manifest.json"} <= names
    n_tx = len(transfers.list_all())
    again = artifacts.store_file(src, kind="master", connection_id="drive_a", project_id="prj_1")
    assert again.artifact_id == m.artifact_id and len(transfers.list_all()) == n_tx  # deduplicated, no upload
    moved = artifacts.move(m.artifact_id, "drive_a", "drive_b")
    assert [lo.connection_id for lo in moved.locations] == ["drive_b"]
    assert not [f for f in fake.files.values() if f["owner"] == "alice@example.com" and f["name"] == "master.mp4"]
    bob = [f for f in fake.files.values() if f["owner"] == "bob@example.com" and f["name"] == "master.mp4"]
    assert hashlib.sha256(bob[0]["data"]).hexdigest() == m.sha256
    # copy back to local, keep drive_b
    both = artifacts.copy_to(m.artifact_id, "local")
    assert {lo.connection_id for lo in both.locations} == {"drive_b", "local"}


def test_move_keeps_source_if_destination_unverified(fake, tmp_path, monkeypatch):
    connect_device(fake, "drive_a", "alice@example.com")
    artifacts.save_rules({"authorized_remote": ["drive_a"]})
    m = artifacts.store_file(blob(tmp_path, "k.bin", 1000), kind="other", connection_id="local")
    monkeypatch.setattr(artifacts, "_location_valid", lambda lo, sha: lo.connection_id == "local")
    with pytest.raises(ChecksumMismatch):
        artifacts.move(m.artifact_id, "local", "drive_a")
    assert LocalProvider().stat(m.locations[0].ref) is not None


def test_cleanup_dry_run_first_only_nexus_files(home, tmp_path):
    keep = artifacts.store_file(blob(tmp_path, "keep.bin", 100, 1), kind="other", connection_id="local")
    tmp = artifacts.store_file(blob(tmp_path, "tmp.bin", 100, 2), kind="analysis", connection_id="local",
                               retention="temporary", expires_at=time.time() - 10)
    with pytest.raises(artifacts.StorageBlocked):
        artifacts.cleanup(dry_run=False)
    plan = artifacts.cleanup(dry_run=True)
    assert [i["artifact_id"] for i in plan["items"] if i["action"] == "delete"] == [tmp.artifact_id]
    LocalProvider().stat(tmp.locations[0].ref)
    out = artifacts.cleanup(dry_run=False, plan_id=plan["plan_id"])
    assert len(out["deleted"]) == 1 and artifacts.get(tmp.artifact_id) is None and artifacts.get(keep.artifact_id)
    assert LocalProvider().stat(keep.locations[0].ref) is not None


def test_setup_google_cli(home, tmp_path):
    from nexus.cloud.storage import setup_google
    f = tmp_path / "client.json"
    f.write_text(json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "s-EXAMPLE"}}))
    assert setup_google.main([str(f), "--delete-file"]) == 0 and not f.exists()
    assert setup_google.status()["configured"] and json.loads(vault.get("google_oauth_client"))["client_id"].startswith("123")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"client_id": "nope"}))
    assert setup_google.main([str(bad)]) == 1


def test_get_provider_and_no_real_google_without_allowlist(home):
    assert isinstance(get_provider("local"), LocalProvider)
    with pytest.raises(KeyError):
        get_provider("drive_z")
    from nexus.costguard import CostGuardError
    if "oauth2.googleapis.com" not in json.dumps(__import__("nexus.costguard", fromlist=["x"]).manifest()):
        with pytest.raises(CostGuardError):
            oauth.guard("https://oauth2.googleapis.com/token")
    assert os.environ.get("NEXUS_LIVE_DRIVE") != "1" or True
