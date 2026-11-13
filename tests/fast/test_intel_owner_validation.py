"""Owner-authorised private YouTube validation, exercised with a MOCK transport only (no real upload happens here;
the report says mock: true). Proves the blocked states, the dry run and the private-only verification logic."""
import json

import pytest

from nexus.intelligence.publishing import creds, owner_validation as ov
from nexus.intelligence.publishing.adapters import youtube as yt
from nexus.intelligence.publishing.adapters.base import FakeTransport, Response

QA = {"output": "final.mp4", "status": "PASSED_AUTOMATED_CHECKS", "stages": [], "human_approval": "PENDING"}
PROV = {"audio": [{"kind": "tts"}]}
TOKEN = ("POST", "https://oauth2.googleapis.com/token", Response(200, {}, b'{"access_token":"at","expires_in":3599}'))


@pytest.fixture()
def clip(home):
    p = home / "final.mp4"
    p.write_bytes(b"\3" * 900)
    return p


def _creds(monkeypatch):
    for k in ("client_id", "client_secret", "refresh_token"):
        monkeypatch.setenv(creds.env_name(creds.secret_name("youtube", "main", k)), f"test-{k}")


def test_without_credentials_it_is_blocked_with_owner_steps(clip):
    r = ov.youtube_private(clip, QA, provenance=PROV)
    assert r["verdict"] == "blocked" and any("console.cloud.google.com" in s for s in r["owner_actions"])
    assert not r["steps"][0]["ok"]


def test_failed_quality_report_blocks_even_with_credentials(clip, monkeypatch):
    _creds(monkeypatch)
    r = ov.youtube_private(clip, {**QA, "status": "BLOCKED"}, provenance=PROV, authorized=True, transport=FakeTransport([]))
    assert r["verdict"] == "blocked" and any("BLOCKED" in b for b in r["owner_actions"])


def test_dry_run_makes_no_request(clip, monkeypatch):
    _creds(monkeypatch)
    t = FakeTransport([])
    r = ov.youtube_private(clip, QA, provenance=PROV, transport=t)
    assert r["verdict"] == "dry_run_ready" and t.calls == [] and ov.FLAG in r["next"]


def _upload_script(privacy):
    sess = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=V"
    return [TOKEN, ("POST", yt.UPLOAD_URL, Response(200, {"Location": sess})),
            ("PUT", sess, Response(201, {}, json.dumps({"id": "vv1", "status": {"uploadStatus": "uploaded", "privacyStatus": privacy}}).encode())),
            ("GET", yt.VIDEOS_URL, Response(200, {}, json.dumps({"items": [{"status": {"uploadStatus": "processed", "privacyStatus": privacy},
                                                                            "processingDetails": {"processingStatus": "succeeded"}}]}).encode()))]


def test_authorized_mock_run_verifies_private_and_is_labelled_mock(clip, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    _creds(monkeypatch)
    t = FakeTransport(_upload_script("private"))
    r = ov.youtube_private(clip, QA, provenance=PROV, authorized=True, transport=t)
    assert r["verdict"] == "verified_private_upload" and r["mock"] is True and r["video_id"] == "vv1"
    body = json.loads(t.calls[1]["body"])
    assert body["status"]["privacyStatus"] == "private" and body["status"]["containsSyntheticMedia"] is True
    assert "test-refresh_token" not in json.dumps(r) and "at" != r.get("video_id")


def test_a_non_private_result_is_flagged_loudly(clip, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    _creds(monkeypatch)
    r = ov.youtube_private(clip, QA, provenance=PROV, authorized=True, transport=FakeTransport(_upload_script("public")))
    assert r["verdict"].startswith("UPLOADED_BUT_NOT_PRIVATE")
