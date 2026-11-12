"""REAL Google Drive end-to-end check (Demo B). Skipped unless NEXUS_LIVE_DRIVE=1.

Prerequisites (owner only - see docs/ACCOUNT1_PROVIDER_COMPARISON.md "Owner setup: Google OAuth client"):
  1. python -m nexus.cloud.storage.setup_google <client_secret.json>
  2. connect each account in the Studio (Storage -> Connect -> device code) or via
     POST /api/v1/storage/connections/drive_a/device/start + /device/poll
  3. registry/components.yaml declares accounts.google.com, oauth2.googleapis.com, www.googleapis.com
  4. NEXUS_LIVE_DRIVE=1 [NEXUS_LIVE_DRIVE_IDS=drive_a,drive_b] python -m pytest tests/live/test_drive_live.py -q
Uses the owner's real NEXUS_HOME. Uploads a few MB of random bytes into NEXUS/Archives/_live_test/ on each connected
account and deletes them again. Writes docs/results/account1/storage_drive_live.json.
"""
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NEXUS_LIVE_DRIVE") != "1", reason="set NEXUS_LIVE_DRIVE=1 (owner-connected Drive)")

IDS = [x.strip() for x in os.environ.get("NEXUS_LIVE_DRIVE_IDS", "drive_a").split(",") if x.strip()]
OUT = Path(__file__).resolve().parents[2] / "docs" / "results" / "account1" / "storage_drive_live.json"
RESULTS: dict = {}


class Crash(Exception):
    pass


@pytest.mark.parametrize("cid", IDS)
def test_real_drive_roundtrip(cid, tmp_path):
    from nexus import costguard
    from nexus.cloud.storage import artifacts, oauth, transfers
    from nexus.cloud.storage.base import sha256_file
    from nexus.cloud.storage.gdrive import GoogleDriveProvider
    for h in ("oauth2.googleapis.com", "www.googleapis.com"):
        costguard.check_host(f"https://{h}/")  # fails loudly if components.yaml lacks the declaration
    oauth.set_endpoints(None)
    p = GoogleDriveProvider(cid)
    h = p.health()
    assert h.status == "ok", f"{cid}: {h.status} {h.detail}"
    q = h.quotas[0]
    assert q.source == "provider_api"
    rec = {"identity_domain": (h.identity or "@").split("@")[-1], "quota_limit": q.limit, "quota_used": q.used}
    folders = artifacts.create_structure(cid) if cid in artifacts.rules().get("authorized_remote", []) else {
        "NEXUS/Archives": p.mkdirs("NEXUS/Archives")}
    rec["folders"] = len(folders)
    src = tmp_path / "live.bin"
    src.write_bytes(os.urandom(5 * 2 ** 20 + 12345))
    tid = f"xfer_live_{cid}_{int(time.time())}"
    calls = []

    def crash(sent, total):
        calls.append(sent)
        if len(calls) == 3:
            raise Crash()
    t0 = time.time()
    with pytest.raises(Crash):
        p.upload(src, "NEXUS/Archives/_live_test/live.bin", chunk_size=4 * 256 * 1024, transfer_id=tid, on_chunk=crash)
    acked = transfers.get(tid)["sent"]
    oauth.reset_cache()
    f = GoogleDriveProvider(cid).resume(tid)
    rec.update(upload_seconds=round(time.time() - t0, 2), interrupted_at=acked, sha256Checksum_from_drive=bool(f.sha256),
               md5_from_drive=bool(f.md5))
    assert (f.sha256 or "").lower() == sha256_file(src) or f.md5
    back = tmp_path / "back.bin"
    p.download(f.ref, back)
    assert back.read_bytes() == src.read_bytes()
    rec["versions"] = len(p.versions(f.ref))
    mv = p.move(f.ref, "NEXUS/Archives/_live_test/live-moved.bin")
    assert mv.name == "live-moved.bin"
    assert p.delete(mv.ref) and p.stat(mv.ref) is None
    rec["ok"] = True
    RESULTS[cid] = rec
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "connections": RESULTS}, indent=1) + "\n")
