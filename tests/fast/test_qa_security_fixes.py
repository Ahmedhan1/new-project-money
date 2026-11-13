"""Regression tests for Account 3 QA findings QA-D1 (anonymous requests locked the owner out), QA-D6 (logout did not
revoke copied sessions) and QA-D7 (raw-token cookie accepted without expiry). Each fails on the pre-fix code."""
import time

import pytest

TOKEN = "qa-owner-token-0123456789abcdefghij"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setenv("NEXUS_API_TOKEN", TOKEN)
    from fastapi.testclient import TestClient

    from nexus import api
    from nexus.cloud import security
    security.reset_rate_limits()
    yield TestClient(api.app, base_url="https://testserver")
    security.reset_rate_limits()


def test_qa_d1_anonymous_requests_do_not_lock_out_the_owner(client):
    from nexus.cloud import security
    for _ in range(security.MAX_FAILS + 5):
        assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers=H).status_code == 200


def test_qa_d1_expired_genuine_session_does_not_count_but_wrong_tokens_still_lock(client):
    from nexus.cloud import security
    old = security.make_session_value(TOKEN, now=time.time() - security.SESSION_TTL_S - 60)
    for _ in range(security.MAX_FAILS + 3):
        r = client.get("/api/projects", headers={"cookie": f"nexus_session={old}"})
        assert r.status_code == 401 and "expired" in r.json()["detail"]
    assert client.get("/api/projects", headers=H).status_code == 200
    for _ in range(security.MAX_FAILS):
        assert client.get("/api/projects", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong-token"}).status_code == 429  # brute force still limited


def test_qa_d6_logout_revokes_copied_session_cookies(client):
    from nexus.cloud import security
    copied = security.make_session_value(TOKEN)
    ck = {"cookie": f"nexus_session={copied}"}
    assert client.get("/api/projects", headers=ck).status_code == 200
    assert client.post("/api/v1/studio/logout", headers={**ck, "X-NEXUS-CSRF": "1"}).status_code == 200
    r = client.get("/api/projects", headers=ck)
    assert r.status_code == 401
    fresh = security.make_session_value(TOKEN)  # a new sign-in works again
    assert client.get("/api/projects", headers={"cookie": f"nexus_session={fresh}"}).status_code == 200


def test_qa_d7_raw_token_cookie_is_refused(client):
    from nexus.cloud import security
    assert not security.session_valid(TOKEN, TOKEN)
    assert client.get("/api/projects", headers={"cookie": f"nexus_session={TOKEN}"}).status_code == 401
